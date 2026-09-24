"""AI explanations: settings, the HTTP layer, the prompt, the saved record, the report and the CLI.

Every request goes to a scripted HTTP server on localhost, never to a real
service, so these run offline and deterministically — and exercise the same
urllib code a real request does, rather than a mock of it. Each response in a
script is served once, in order, so a test reads as the conversation it is
checking: two 429s, then an answer.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lambda_watcher.ai import providers
from lambda_watcher.ai.explanation import (
    Explanation,
    Record,
    clear_pending,
    load_record,
    parse_answer,
    save_done,
    save_failed,
    save_pending,
)
from lambda_watcher.ai.prompt import build_prompt, redact
from lambda_watcher.ai.providers import (
    AIError,
    complete,
    normalize_local_url,
    parse_azure_endpoint,
    with_retries,
)
from lambda_watcher.ai.report import explain_command, panel_for, shell_word
from lambda_watcher.ai.run import Explainer, ExplainJob, explain_diff, rewrite_pages
from lambda_watcher.ai.settings import AISettings, ModelEntry, mask_key
from lambda_watcher.cli import app
from lambda_watcher.config import Config
from lambda_watcher.db import Database
from lambda_watcher.diffing import diff_from_index
from lambda_watcher.diffing.render_html import render_html
from lambda_watcher.ingest import Ingestor
from lambda_watcher.store import Store
from tests.conftest import PY_V1, PY_V2, fake_secret

runner = CliRunner()

#: Every variable a detected model could come from, cleared for every test so
#: a key exported on the machine running the suite cannot change an answer.
AI_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT")

ANSWER: dict[str, Any] = {
    "headline": "Saved orders are now also published to SQS",
    "summary": "The handler sends each order to QUEUE_URL after saving it and returns 201.",
    "risk": "Medium",
    "risk_reason": "A new environment variable and IAM permission are needed.",
    "changes": [
        {"kind": "feature", "title": "Orders go to SQS", "detail": "send_message after put_item.",
         "files": ["b/lambda_function.py", "made_up.py"]},
        {"kind": "made-up-kind", "title": "Status is 201", "files": ["lambda_function.py"]},
        {"title": ""},
    ],
    "risks": [{"level": "HIGH", "title": "Role needs sqs:SendMessage", "detail": "or every call fails"}],
    "checklist": ["Add QUEUE_URL to the environment", ""],
    "files": {"lambda_function.py": "publishes to SQS", "nowhere.py": "invented"},
}


@pytest.fixture(autouse=True)
def _no_ai_environment(monkeypatch):
    """No key from the machine running the suite, and no proxy between us and the fake server."""
    for name in AI_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


class FakeService:
    """A localhost HTTP server that answers each request with the next scripted response."""

    def __init__(self) -> None:
        """Start serving on a free port; responses are queued with :meth:`respond`."""
        self.script: list[tuple[int, Any, dict[str, str]]] = []
        self.requests: list[dict[str, Any]] = []
        service = self

        class Handler(BaseHTTPRequestHandler):
            """Records each request and replays the next scripted response."""

            def log_message(self, *args: Any) -> None:
                """Stay quiet."""

            def _reply(self) -> None:
                """Serve the next response, or a 599 when the script has run out."""
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                service.requests.append({
                    "method": self.command, "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw) if raw else None,
                })
                status, body, headers = service.script.pop(0) if service.script else (599, {}, {})
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _reply

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        """``http://127.0.0.1:<port>``."""
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def respond(self, status: int, body: Any, headers: dict[str, str] | None = None) -> FakeService:
        """Queue one response."""
        self.script.append((status, body, headers or {}))
        return self

    def claude(self, text: str | None = None, stop: str = "end_turn") -> FakeService:
        """Queue a successful Messages API answer — by default, :data:`ANSWER` in a fence."""
        text = text if text is not None else "Here it is:\n```json\n" + json.dumps(ANSWER) + "\n```"
        return self.respond(200, {"model": "claude-sonnet-5", "stop_reason": stop,
                                  "content": [{"type": "text", "text": text}],
                                  "usage": {"input_tokens": 100, "output_tokens": 50}})

    def close(self) -> None:
        """Stop serving."""
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def service():
    """A fresh scripted server for one test."""
    fake = FakeService()
    yield fake
    fake.close()


def claude_at(service: FakeService, **overrides: Any) -> ModelEntry:
    """An Anthropic model entry pointed at the fake server."""
    values = {"name": "claude-sonnet-5", "provider": "anthropic", "model": "claude-sonnet-5",
              "api_key": "sk-ant-test-0000000000", "endpoint": service.url}
    return ModelEntry(**{**values, **overrides})


def _no_sleep(waits: list[float] | None = None):
    """A sleep that records how long it was asked to wait, and does not."""
    return (lambda seconds: waits.append(seconds)) if waits is not None else (lambda _s: None)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_no_settings_file_is_a_normal_state_not_an_error(tmp_path: Path):
    settings = AISettings.load(tmp_path)
    assert settings.models == [] and settings.problem is None
    assert settings.resolve() is None and settings.auto_entry() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_keys_are_saved_readable_only_by_their_owner(tmp_path: Path):
    settings = AISettings.load(tmp_path)
    settings.add(ModelEntry(name="c", provider="anthropic", model="c", api_key="sk-secret"))
    path = settings.save()
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert AISettings.load(tmp_path).models[0].api_key == "sk-secret"


def test_a_damaged_settings_file_is_kept_aside_rather_than_overwritten(tmp_path: Path):
    (tmp_path / "ai.json").write_text("{not json", encoding="utf-8")
    settings = AISettings.load(tmp_path)
    assert settings.problem and settings.models == []
    settings.add(ModelEntry(name="c", provider="anthropic", model="c", api_key="k"))
    settings.save()
    assert (tmp_path / "ai.json.broken").read_text(encoding="utf-8") == "{not json"
    assert AISettings.load(tmp_path).problem is None


def test_one_bad_value_costs_that_value_not_every_model(tmp_path: Path):
    (tmp_path / "ai.json").write_text(json.dumps({
        "max_retries": "four", "enabled": "yes", "future_setting": 1,
        "models": [{"name": "c", "provider": "Claude", "model": "m", "api_key": "k", "later": 1},
                   {"name": "", "provider": "anthropic"}, {"name": "x", "provider": "nonsense"}],
    }), encoding="utf-8")
    settings = AISettings.load(tmp_path)
    assert settings.max_retries == 4 and settings.enabled is True
    assert [(m.name, m.provider) for m in settings.models] == [("c", "anthropic")]


def test_models_are_found_by_any_unique_part_of_their_name(tmp_path: Path):
    settings = AISettings.load(tmp_path)
    settings.add(ModelEntry(name="claude-sonnet-5", provider="anthropic", model="claude-sonnet-5"))
    settings.add(ModelEntry(name="quick", provider="anthropic", model="claude-haiku-4-5-20251001"))
    assert settings.find("sonnet").name == "claude-sonnet-5"
    assert settings.find("QUICK").name == "quick"
    assert settings.find("haiku").name == "quick"          # by the model id as well
    assert settings.find("claude") is None                  # ambiguous: two start that way


def test_a_model_id_on_the_command_line_borrows_the_saved_key(tmp_path: Path):
    settings = AISettings.load(tmp_path)
    settings.add(ModelEntry(name="claude-sonnet-5", provider="anthropic", model="claude-sonnet-5",
                            api_key="sk-saved"))
    borrowed = settings.resolve("claude-opus-5-5")
    assert (borrowed.provider, borrowed.model, borrowed.api_key) == \
        ("anthropic", "claude-opus-5-5", "sk-saved")


def test_the_first_model_is_the_default_and_removing_it_hands_the_role_on(tmp_path: Path):
    settings = AISettings.load(tmp_path)
    first = ModelEntry(name="a", provider="anthropic", model="a")
    settings.add(first)
    settings.add(ModelEntry(name="b", provider="openai", model="b"))
    assert settings.default == "a"
    assert settings.remove(first).name == "b" and settings.default == "b"


def test_adding_a_model_again_replaces_it_in_place(tmp_path: Path):
    settings = AISettings.load(tmp_path)
    settings.add(ModelEntry(name="a", provider="anthropic", model="a", api_key="old"))
    settings.add(ModelEntry(name="b", provider="anthropic", model="b"))
    assert settings.add(ModelEntry(name="a", provider="anthropic", model="a", api_key="new")) is True
    assert [(m.name, m.api_key) for m in settings.models] == [("a", "new"), ("b", "")]


def test_a_name_clash_across_services_gets_the_service_in_front(tmp_path: Path):
    settings = AISettings.load(tmp_path)
    settings.add(ModelEntry(name="llama3", provider="local", model="llama3"))
    assert settings.unique_name("llama3", "local") == "llama3"
    assert settings.unique_name("llama3", "openai") == "openai-llama3"


def test_a_key_in_the_environment_works_from_a_terminal_but_never_in_the_background(
    tmp_path: Path, monkeypatch
):
    """A stray variable must never be what starts sending code off the machine unasked."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    settings = AISettings.load(tmp_path)
    assert settings.resolve().key_env == "ANTHROPIC_API_KEY"
    assert settings.resolve(allow_detected=False) is None
    assert settings.auto_entry() is None


def test_nothing_runs_in_the_background_when_it_should_not(tmp_path: Path):
    settings = AISettings.load(tmp_path)
    settings.add(ModelEntry(name="a", provider="anthropic", model="a", api_key="k"))
    assert settings.auto_entry() is not None
    settings.auto_explain = False
    assert settings.auto_entry() is None
    settings.auto_explain, settings.enabled = True, False
    assert settings.auto_entry() is None
    settings.enabled = True
    settings.models[0].api_key = ""
    assert settings.auto_entry() is None                  # no key anywhere


def test_keys_are_printed_masked():
    assert mask_key("sk-ant-api03-abcdefghijklmnopqrstuvwxyz9f2c").endswith("…9f2c")
    assert "abcdefghijklmnop" not in mask_key("sk-ant-api03-abcdefghijklmnopqrstuvwxyz9f2c")
    assert mask_key("short") == "•••••"


# --------------------------------------------------------------------------- #
# The HTTP layer
# --------------------------------------------------------------------------- #
def test_anthropic_requests_carry_the_key_version_model_and_prompt(service: FakeService):
    service.claude("hello")
    reply = complete(claude_at(service), "sys", "the prompt", timeout=5, retries=0)
    request = service.requests[0]
    assert request["path"] == "/v1/messages"
    assert request["headers"]["x-api-key"] == "sk-ant-test-0000000000"
    assert request["headers"]["anthropic-version"] == providers.ANTHROPIC_VERSION
    assert request["body"]["system"] == "sys"
    assert request["body"]["messages"] == [{"role": "user", "content": "the prompt"}]
    assert (reply.text, reply.input_tokens, reply.output_tokens) == ("hello", 100, 50)


def test_a_rate_limit_is_waited_out_for_as_long_as_the_service_asks(service: FakeService):
    limited = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    service.respond(429, limited, {"retry-after": "7"})
    service.respond(529, {"error": {"message": "busy"}}).claude("ok")
    waits: list[float] = []
    told: list[tuple[int, str]] = []
    reply = complete(claude_at(service), "s", "p", timeout=5, retries=4, sleep=_no_sleep(waits),
                     on_retry=lambda attempt, wait, error: told.append((attempt, error.kind)))
    assert reply.text == "ok" and reply.attempts == 3
    assert waits[0] == 7.0                                  # the service's own Retry-After
    assert 3.0 <= waits[1] <= 5.0                           # then our own backoff
    assert told == [(1, "rate-limit"), (2, "overloaded")]


def test_retries_give_up_and_say_how_many_attempts_it_took(service: FakeService):
    for _ in range(3):
        service.respond(503, {"error": {"message": "unavailable"}})
    with pytest.raises(AIError) as caught:
        complete(claude_at(service), "s", "p", timeout=5, retries=2, sleep=_no_sleep())
    assert caught.value.kind == "overloaded"
    assert "gave up after 3 attempts" in str(caught.value)
    assert len(service.requests) == 3


def test_an_empty_account_is_not_retried_like_a_rate_limit(service: FakeService):
    """429 means two different things, and retrying the second only fails five times more slowly."""
    service.respond(429, {"error": {"message": "You exceeded your current quota",
                                    "type": "insufficient_quota", "code": "insufficient_quota"}})
    entry = ModelEntry(name="g", provider="openai", model="gpt", api_key="k", endpoint=service.url + "/v1")
    with pytest.raises(AIError) as caught:
        complete(entry, "s", "p", timeout=5, retries=4, sleep=_no_sleep())
    assert caught.value.kind == "quota" and not caught.value.retryable
    assert len(service.requests) == 1


def test_a_rejected_key_fails_at_once_and_names_the_command_that_replaces_it(service: FakeService):
    service.respond(401, {"type": "error", "error": {"type": "authentication_error",
                                                     "message": "invalid x-api-key"}})
    with pytest.raises(AIError) as caught:
        complete(claude_at(service), "s", "p", timeout=5, retries=4, sleep=_no_sleep())
    assert caught.value.kind == "auth" and len(service.requests) == 1
    assert "lw ai add anthropic" in caught.value.hint


def test_a_prompt_too_large_is_recognised_whatever_status_it_comes_with(service: FakeService):
    service.respond(400, {"error": {"type": "invalid_request_error",
                                    "message": "prompt is too long: 250000 tokens > 200000 maximum"}})
    with pytest.raises(AIError) as caught:
        complete(claude_at(service), "s", "p", timeout=5, retries=4, sleep=_no_sleep())
    assert caught.value.kind == "too-large"


def test_a_server_that_is_not_running_is_a_connection_error_with_advice(monkeypatch):
    entry = ModelEntry(name="l", provider="local", model="llama3", endpoint="http://127.0.0.1:9/v1")
    with pytest.raises(AIError) as caught:
        complete(entry, "s", "p", timeout=15, retries=1, sleep=_no_sleep())
    assert caught.value.kind == "network" and caught.value.attempts == 2
    assert "ollama serve" in caught.value.hint


def test_a_wait_of_minutes_is_reported_rather_than_sat_through(service: FakeService):
    service.respond(429, {"error": {"message": "daily limit"}}, {"retry-after": "3600"})
    waits: list[float] = []
    with pytest.raises(AIError) as caught:
        complete(claude_at(service), "s", "p", timeout=5, retries=4, sleep=_no_sleep(waits))
    assert waits == [] and "3600s wait" in str(caught.value)


def test_openai_swaps_the_length_parameter_when_the_model_refuses_it(service: FakeService):
    refused = "Unrecognized request argument supplied: max_completion_tokens"
    service.respond(400, {"error": {"message": refused, "type": "invalid_request_error"}})
    service.respond(200, {"model": "gpt",
                          "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    entry = ModelEntry(name="g", provider="openai", model="gpt", api_key="k", endpoint=service.url + "/v1")
    reply = complete(entry, "s", "p", timeout=5, retries=0)
    assert reply.text == "hi"
    assert "max_completion_tokens" in service.requests[0]["body"]
    assert "max_tokens" in service.requests[1]["body"]
    assert service.requests[1]["headers"]["authorization"] == "Bearer k"


def test_azure_tries_the_v1_api_then_the_deployment_url(service: FakeService):
    service.respond(404, {"error": {"code": "404", "message": "Resource not found"}})
    service.respond(200, {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})
    entry = ModelEntry(name="az", provider="azure", model="gpt-4o", api_key="azkey", endpoint=service.url)
    assert complete(entry, "s", "p", timeout=5, retries=0).text == "hi"
    first, second = service.requests
    assert first["path"] == "/openai/v1/chat/completions" and first["body"]["model"] == "gpt-4o"
    assert second["path"].startswith("/openai/deployments/gpt-4o/chat/completions?api-version=")
    assert "model" not in second["body"]
    assert second["headers"]["api-key"] == "azkey"


def test_whatever_the_azure_portal_offered_can_be_pasted():
    uri = ("https://res.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
           "?api-version=2025-01-01-preview")
    assert parse_azure_endpoint(uri) == ("https://res.openai.azure.com", "gpt-4o", "2025-01-01-preview")
    assert parse_azure_endpoint("res.openai.azure.com/") == ("https://res.openai.azure.com", "", "")


@pytest.mark.parametrize("pasted", [
    "localhost:11434", "http://localhost:11434", "http://localhost:11434/v1/",
    "http://localhost:11434/v1/chat/completions",
])
def test_a_local_server_url_is_accepted_in_any_of_its_usual_forms(pasted: str):
    assert normalize_local_url(pasted) == "http://localhost:11434/v1"


def test_backoff_grows_and_stays_bounded():
    waits = [providers.backoff(n, rng=lambda: 0.5) for n in range(8)]
    assert waits[:4] == [2.0, 4.0, 8.0, 16.0] and max(waits) == providers.BACKOFF_CAP


def test_with_retries_leaves_a_non_retryable_failure_alone():
    calls = []

    def send():
        calls.append(1)
        raise AIError("auth", "no")

    with pytest.raises(AIError):
        with_retries(send, retries=5, sleep=_no_sleep())
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #
def _pair(cfg: Config, db: Database, ingestor: Ingestor, make_zip, v1: dict, v2: dict, name="fn"):
    """Archive two versions and return the diff between them."""
    ingestor.ingest(make_zip(f"{name}.zip", v1))
    ingestor.ingest(make_zip(f"{name}.zip", v2, built=(2026, 2, 1, 0, 0, 0)))
    function = db.get_function(name)
    a, b = db.get_version(function["id"], 1), db.get_version(function["id"], 2)
    return diff_from_index(db, ingestor.store, cfg.diff, name, a, b)


def test_the_prompt_leaves_out_vendored_files_and_credentials(cfg, db, ingestor, make_zip):
    secret = fake_secret("stripe")
    diff = _pair(cfg, db, ingestor, make_zip,
                 {"lambda_function.py": PY_V1, "site-packages/boto3/__init__.py": "V = '1.34.0'\n"},
                 {"lambda_function.py": PY_V2, "site-packages/boto3/__init__.py": "V = '1.35.20'\n",
                  "config.py": f'STRIPE = "{secret}"\n', ".env": "DB_PASSWORD=hunter2hunter2\n"})
    built = build_prompt(diff)
    assert secret not in built.text and "hunter2" not in built.text
    assert "site-packages" not in built.text
    assert "«redacted stripe-key»" in built.text and built.redactions >= 1
    assert built.withheld == [".env"]
    assert "QUEUE_URL" in built.text                      # the change itself is there


def test_the_handler_file_is_sent_first(cfg, db, ingestor, make_zip):
    diff = _pair(cfg, db, ingestor, make_zip,
                 {"lambda_function.py": PY_V1, "zzz_util.py": "A = 1\n" * 200},
                 {"lambda_function.py": PY_V2, "zzz_util.py": "A = 2\n" * 200})
    text = build_prompt(diff).text
    assert text.index("lambda_function.py (") < text.index("zzz_util.py (")
    assert "contains the Lambda handler" in text


def test_a_tight_budget_lists_what_it_left_out(cfg, db, ingestor, make_zip):
    many = {f"mod_{i}.py": f"X = {i}\n" * 300 for i in range(6)}
    changed = {f"mod_{i}.py": f"X = {i + 1}\n" * 300 for i in range(6)}
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1, **many},
                 {"lambda_function.py": PY_V2, **changed})
    built = build_prompt(diff, budget=6000)
    assert built.omitted and built.files_sent < built.files_total
    assert "Not shown, to stay within the size limit" in built.text
    assert all(path in built.text for path in built.omitted)


def test_without_send_code_no_line_of_code_is_sent(cfg, db, ingestor, make_zip):
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    built = build_prompt(diff, send_code=False)
    assert "```diff" not in built.text and "sqs.send_message" not in built.text
    assert "lambda_function.py" in built.text and "QUEUE_URL" in built.text


def test_redaction_catches_keys_no_pattern_names():
    line, count = redact('token = "a8Fq2LzP9xW4mB7nK3vR6tY1uJ5hC0dE"')
    assert "a8Fq2LzP9xW4mB7nK3vR6tY1uJ5hC0dE" not in line and count == 1
    ordinary, none = redact("return build_response_for_customer_order_items(event)")
    assert none == 0 and "build_response_for_customer_order_items" in ordinary


# --------------------------------------------------------------------------- #
# Reading the answer
# --------------------------------------------------------------------------- #
def test_an_answer_is_read_through_fences_prefixes_and_invented_paths():
    known = {"lambda_function.py", "helpers/db.py"}
    ex = parse_answer("Sure! Here you go:\n```json\n" + json.dumps(ANSWER) + "\n```", known)
    assert ex.structured and ex.headline.startswith("Saved orders")
    assert ex.risk == "medium"
    assert [p.files for p in ex.changes] == [["lambda_function.py"], ["lambda_function.py"]]
    assert ex.changes[1].kind == "other"                    # an unknown kind is kept, drawn plainly
    assert ex.risks[0].kind == "high"
    assert ex.checklist == ["Add QUEUE_URL to the environment"]
    assert ex.file_notes == {"lambda_function.py": "publishes to SQS"}


def test_a_filename_alone_is_resolved_only_when_it_is_unambiguous():
    known = {"src/a/util.py", "src/b/util.py", "helpers/db.py"}
    ex = parse_answer(json.dumps({"headline": "h", "changes": [
        {"title": "t", "files": ["db.py", "util.py", "`helpers/db.py:12`"]}]}), known)
    assert ex.changes[0].files == ["helpers/db.py"]


def test_prose_instead_of_json_is_kept_as_the_summary():
    ex = parse_answer("The handler now publishes to SQS. It also returns 201.", set())
    assert not ex.structured and ex.headline == "The handler now publishes to SQS."
    assert "returns 201" in ex.summary


# --------------------------------------------------------------------------- #
# The saved record
# --------------------------------------------------------------------------- #
def test_the_record_moves_through_pending_failed_and_done(cfg, db, ingestor, make_zip):
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    store, a, b = ingestor.store, diff.a_meta, diff.b_meta
    assert load_record(store, a, b) is None
    save_done(store, a, b, Explanation(headline="first answer"))
    save_pending(store, a, b, "m")
    pending = load_record(store, a, b)
    assert pending.status == "pending" and pending.explanation.headline == "first answer"
    save_failed(store, a, b, "m", {"kind": "rate-limit", "message": "slow", "hint": "wait"})
    failed = load_record(store, a, b)
    assert failed.status == "failed" and failed.explanation.headline == "first answer"
    save_pending(store, a, b, "m")
    clear_pending(store, a, b)
    assert load_record(store, a, b).status == "done"


def test_a_pending_record_whose_writer_has_gone_is_shown_as_interrupted(cfg, db, ingestor, make_zip):
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    record = Record(status="pending", a_seq=1, b_seq=2, a_tree="a", b_tree="b",
                    started_at="2020-01-01T00:00:00+00:00", pid=os.getpid())
    assert record.pending_is_stale()
    save_pending(ingestor.store, diff.a_meta, diff.b_meta, "m")
    fresh = load_record(ingestor.store, diff.a_meta, diff.b_meta)
    assert not fresh.pending_is_stale()


def test_an_archive_from_before_explanations_reads_as_not_yet_explained(cfg, db, ingestor, make_zip):
    """Nothing to migrate: no ``explanations/`` folder is the same as no explanation."""
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    settings = AISettings.load(cfg.root)
    assert panel_for(ingestor.store, diff, settings).state == "unconfigured"
    settings.add(ModelEntry(name="a", provider="anthropic", model="a", api_key="k"))
    assert panel_for(ingestor.store, diff, settings).state == "missing"
    settings.enabled = False
    assert panel_for(ingestor.store, diff, settings) is None


# --------------------------------------------------------------------------- #
# Explaining, and the pages
# --------------------------------------------------------------------------- #
def test_a_prompt_too_large_is_sent_again_smaller(cfg, db, ingestor, make_zip, monkeypatch):
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    sizes: list[int] = []

    def fake_complete(entry, system, prompt, **kwargs):
        sizes.append(len(prompt))
        if len(sizes) == 1:
            raise AIError("too-large", "too big")
        return providers.Reply(text=json.dumps(ANSWER), model="m")

    monkeypatch.setattr("lambda_watcher.ai.run.complete", fake_complete)
    settings = AISettings(max_prompt_kb=64)
    explanation = explain_diff(diff, ModelEntry(name="a", provider="anthropic", model="a"), settings)
    assert len(sizes) == 2 and explanation.headline


def test_an_answer_cut_off_mid_json_is_a_failure_not_a_summary(cfg, db, ingestor, make_zip, service):
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    service.claude('{"headline": "Orders go to SQS", "summ', stop="max_tokens")
    with pytest.raises(AIError) as caught:
        explain_diff(diff, claude_at(service), AISettings(max_retries=0))
    assert caught.value.kind == "bad-response"


def test_the_report_links_every_file_it_mentions_to_that_files_diff(cfg, db, ingestor, make_zip):
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    save_done(ingestor.store, diff.a_meta, diff.b_meta,
              parse_answer(json.dumps(ANSWER), {c.path for c in diff.files}))
    page = render_html(diff, ai=panel_for(ingestor.store, diff, AISettings.load(cfg.root)))
    assert 'class="ai-headline">Saved orders are now also published to SQS' in page
    assert 'data-open="lambda_function.py"' in page
    assert 'data-key="lambda_function.py"' in page
    assert 'class="ai-note">publishes to SQS' in page
    assert 'class="ai-check" data-key="lw-ai:fn:' in page
    assert "lw explain fn --from 1 --to 2 --refresh" in page
    assert "http://" not in page and "https://" not in page


def test_a_report_says_what_to_type_when_there_is_no_explanation(cfg, db, ingestor, make_zip):
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    offer = render_html(diff, ai=panel_for(ingestor.store, diff, AISettings.load(cfg.root)))
    assert 'data-copy="lw ai add"' in offer and 'data-copy="lw explain fn --from 1 --to 2"' in offer
    assert 'id="ai-summary"' not in render_html(diff, ai=None)


def test_a_page_rewrite_never_puts_an_older_pair_on_latest(cfg, db, ingestor, make_zip):
    diff = _pair(cfg, db, ingestor, make_zip, {"lambda_function.py": PY_V1},
                 {"lambda_function.py": PY_V2})
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V2 + "\n# three\n"},
                             built=(2026, 3, 1, 0, 0, 0)))
    latest = cfg.reports_dir / "fn" / "latest.html"
    before = latest.read_text(encoding="utf-8")
    assert "v0002 → v0003" in before
    rewrite_pages(cfg, db, ingestor.store, int(db.get_function("fn")["id"]), diff, AISettings.load(cfg.root))
    assert latest.read_text(encoding="utf-8") == before
    assert (cfg.reports_dir / "fn" / "v0001-v0002.html").exists()


def _saved_model(cfg: Config, service: FakeService, **settings: Any) -> AISettings:
    """Save a model pointed at the fake server, with any settings given."""
    ai = AISettings.load(cfg.root)
    ai.add(claude_at(service))
    for key, value in settings.items():
        setattr(ai, key, value)
    ai.save()
    return ai


def test_the_watcher_explains_a_new_version_in_the_background(cfg, db, make_zip, service):
    _saved_model(cfg, service, max_retries=0)
    service.claude()
    store = Store(cfg)
    done: list[Any] = []
    explainer = Explainer(cfg, db, store, on_done=lambda job, ex, err: done.append((ex, err)))
    ingestor = Ingestor(cfg, db, store, explainer=explainer)
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V2}, built=(2026, 2, 1, 0, 0, 0)))
    assert explainer.drain(timeout=20)
    explainer.stop()
    assert done and done[0][0] is not None and done[0][1] is None
    latest = (cfg.reports_dir / "fn" / "latest.html").read_text(encoding="utf-8")
    assert 'class="ai-headline">Saved orders are now also published to SQS' in latest


def test_a_background_failure_lands_on_the_page_with_the_command_that_retries(cfg, db, make_zip, service):
    _saved_model(cfg, service, max_retries=0)
    service.respond(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad"}})
    store = Store(cfg)
    explainer = Explainer(cfg, db, store)
    ingestor = Ingestor(cfg, db, store, explainer=explainer)
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V2}, built=(2026, 2, 1, 0, 0, 0)))
    assert explainer.drain(timeout=20)
    explainer.stop()
    latest = (cfg.reports_dir / "fn" / "latest.html").read_text(encoding="utf-8")
    assert "The AI summary could not be written" in latest
    assert "Anthropic rejected the API key" in latest
    assert 'data-copy="lw explain fn --from 1 --to 2"' in latest


def test_switching_ai_off_before_a_queued_job_runs_cancels_it(cfg, db, make_zip, service):
    ai = _saved_model(cfg, service)
    store = Store(cfg)
    ingestor = Ingestor(cfg, db, store)
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V2}, built=(2026, 2, 1, 0, 0, 0)))
    function = db.get_function("fn")
    diff = diff_from_index(db, store, cfg.diff, "fn", db.get_version(function["id"], 1),
                           db.get_version(function["id"], 2))
    save_pending(store, diff.a_meta, diff.b_meta, "m")
    ai.enabled = False
    ai.save()
    explainer = Explainer(cfg, db, store)
    explainer.submit(ExplainJob(int(function["id"]), diff))
    assert explainer.drain(timeout=20)
    explainer.stop()
    assert load_record(store, diff.a_meta, diff.b_meta) is None
    assert service.requests == []


def test_a_backfill_never_explains_anything(cfg, db, make_zip, service):
    """No explainer, no requests — two years of history must not become two hundred paid calls."""
    _saved_model(cfg, service)
    ingestor = Ingestor(cfg, db, Store(cfg))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V2}, built=(2026, 2, 1, 0, 0, 0)))
    assert service.requests == []
    latest = (cfg.reports_dir / "fn" / "latest.html").read_text(encoding="utf-8")
    assert "Get this change explained in plain English" in latest


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #
@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    store = tmp_path / "store"
    monkeypatch.setenv("LAMBDA_WATCHER_HOME", str(store))
    monkeypatch.setenv("COLUMNS", "200")
    return store


@pytest.fixture
def archived(home: Path, tmp_path: Path, make_zip) -> Path:
    for path in (make_zip("order-processor.zip", {"lambda_function.py": PY_V1}),
                 make_zip("order-processor-2.zip", {"lambda_function.py": PY_V2},
                          built=(2026, 2, 1, 0, 0, 0))):
        result = runner.invoke(app, ["ingest", str(path), "--as", "order-processor"])
        assert result.exit_code == 0, result.output
    return home


def _lw(*args: str, code: int = 0):
    result = runner.invoke(app, list(args))
    assert result.exit_code == code, f"`lw {' '.join(args)}` exited {result.exit_code}:\n{result.output}"
    return result


def _add(service: FakeService, *extra: str):
    return _lw("ai", "add", "anthropic", "--key", "sk-ant-test-0000000000", "--endpoint", service.url,
               "--model", "claude-sonnet-5", "--no-test", *extra)


def test_lw_ai_explains_what_it_would_do_before_anything_is_set_up(home: Path):
    result = _lw("ai")
    assert "not set up" in result.output and "lw ai add" in result.output
    assert "nothing is sent" in result.output


def test_adding_listing_switching_and_removing_models(home: Path, service: FakeService):
    _add(service)
    _add(service, "--name", "quick")
    listing = _lw("ai").output
    assert "claude-sonnet-5" in listing and "quick" in listing and "…0000 (saved)" in listing
    assert "sk-ant-test-0000000000" not in listing
    _lw("ai", "use", "quick")
    assert AISettings.load(home).default == "quick"
    _lw("ai", "remove", "quick", "--yes")
    assert AISettings.load(home).default == "claude-sonnet-5"
    _lw("ai", "remove", "nothing-like-it", "--yes", code=1)


def test_adding_a_model_checks_it_answers_and_saves_nothing_when_it_does_not(home, service):
    service.respond(401, {"type": "error", "error": {"type": "authentication_error", "message": "no"}})
    result = _lw("ai", "add", "anthropic", "--key", "bad", "--endpoint", service.url,
                 "--model", "claude-sonnet-5", code=1)
    assert "rejected the API key" in result.output and "nothing was saved" in result.output
    assert AISettings.load(home).models == []


def test_a_busy_service_still_proves_a_key_works(home, service):
    for _ in range(2):
        service.respond(429, {"error": {"message": "slow down"}}, {"retry-after": "0"})
    result = _lw("ai", "add", "anthropic", "--key", "sk-ant-good", "--endpoint", service.url,
                 "--model", "claude-sonnet-5")
    assert "accepted the key" in result.output
    assert AISettings.load(home).models[0].api_key == "sk-ant-good"


def test_scripts_are_told_which_option_is_missing(home: Path):
    assert "lw ai add anthropic" in _lw("ai", "add", code=1).output
    assert "--key" in _lw("ai", "add", "anthropic", "--no-test", code=1).output
    assert "--endpoint" in _lw("ai", "add", "azure", code=1).output


def test_explain_prints_saves_and_reuses_the_answer(archived: Path, service: FakeService):
    _add(service)
    service.claude()
    first = _lw("explain", "order-processor")
    assert "Saved orders are now also published to SQS" in first.output
    assert "Add QUEUE_URL to the environment" in first.output
    page = archived / "reports" / "order-processor" / "v0001-v0002.html"
    assert 'class="ai-headline">Saved orders' in page.read_text(encoding="utf-8")
    requests = len(service.requests)
    again = _lw("explain", "order-processor")
    assert len(service.requests) == requests                 # saved, so nothing sent
    assert "--refresh asks again" in again.output
    service.claude()
    _lw("explain", "order-processor", "--refresh")
    assert len(service.requests) == requests + 1


def test_an_explanation_survives_rename_and_reindex(archived: Path, service: FakeService):
    _add(service)
    service.claude()
    _lw("explain", "order-processor")
    _lw("rename", "order-processor", "orders-api")
    _lw("reindex", "--yes")
    sent = len(service.requests)
    assert "Saved orders" in _lw("explain", "orders-api").output
    assert len(service.requests) == sent


def test_a_failed_explanation_says_why_and_how_to_try_again(archived: Path, service: FakeService):
    _add(service)
    service.respond(401, {"type": "error", "error": {"type": "authentication_error", "message": "no"}})
    result = _lw("explain", "order-processor", code=1)
    assert "rejected the API key" in result.output
    assert "lw explain order-processor --from 1 --to 2" in result.output
    page = (archived / "reports" / "order-processor" / "v0001-v0002.html").read_text(encoding="utf-8")
    assert "The AI summary could not be written" in page


def test_dry_run_prints_the_prompt_and_sends_nothing(archived: Path, service: FakeService):
    _add(service)
    result = _lw("explain", "order-processor", "--dry-run")
    assert "Nothing was sent" in result.output and "QUEUE_URL" in result.output
    assert service.requests == []


def test_explain_with_no_model_names_the_command_that_sets_one_up(archived: Path):
    result = _lw("explain", "order-processor", code=1)
    assert "lw ai add" in result.output


def test_explain_all_fills_in_the_history_oldest_first(archived: Path, service, make_zip):
    _add(service)
    result = runner.invoke(app, ["ingest", str(make_zip("order-processor-3.zip",
                                 {"lambda_function.py": PY_V2 + "\n# three\n"}, built=(2026, 3, 1, 0, 0, 0))),
                                 "--as", "order-processor"])
    assert result.exit_code == 0, result.output
    service.claude().claude()
    output = _lw("explain", "order-processor", "--all", "--yes").output
    assert output.index("v0001 → v0002") < output.index("v0002 → v0003")
    assert "explained 2 of 2 steps" in output
    assert "already explained" in _lw("explain", "order-processor", "--all").output


def test_off_means_off_everywhere(archived: Path, service: FakeService):
    _add(service)
    _lw("ai", "off")
    assert "lw ai on" in _lw("explain", "order-processor", code=1).output
    assert "✦ AI explanations off · lw ai on" in _lw().output
    page = archived / "reports" / "order-processor-v0001-v0002.html"
    _lw("diff", "order-processor", "--html")
    assert 'id="ai-summary"' not in page.read_text(encoding="utf-8")
    _lw("ai", "on")
    _lw("diff", "order-processor", "--html")
    assert 'id="ai-summary"' in page.read_text(encoding="utf-8")


def test_settings_show_every_switch_and_change_the_ones_given(home: Path):
    table = _lw("ai", "settings").output
    for word in ("automatic", "code sent", "retries", "timeout", "prompt size"):
        assert word in table
    _lw("ai", "settings", "--no-auto", "--no-send-code", "--retries", "7")
    saved = AISettings.load(home)
    assert (saved.auto_explain, saved.send_code, saved.max_retries) == (False, False, 7)


def test_the_dashboard_and_doctor_mention_ai_without_failing_over_it(home: Path, service):
    assert "✦ AI explanations not set up · lw ai add" in _lw().output
    from lambda_watcher.cli import _ai_doctor_row
    cfg = Config()
    cfg.store.root = str(home)
    assert _ai_doctor_row(cfg)[1] == "not set up"            # yellow, never a failure
    _add(service)
    assert "✦ AI explanations by claude-sonnet-5, for every new version" in _lw().output
    assert _ai_doctor_row(cfg)[1] == "ok"
    ai = AISettings.load(home)
    ai.models[0].api_key = ""
    ai.save()
    assert _ai_doctor_row(cfg)[1] == "NO KEY"


def test_a_command_to_paste_is_quoted_only_when_it_has_to_be():
    assert shell_word("order-processor") == "order-processor"
    assert shell_word("Order Processor") == '"Order Processor"'
    assert explain_command("Order Processor", 3, 4) == 'lw explain "Order Processor" --from 3 --to 4'


def test_the_walk_through_asks_for_each_piece_and_offers_the_models_the_key_can_use(
    home: Path, service: FakeService, monkeypatch
):
    """What a person at a terminal sees: a menu, a hidden key, a list of real models, a check."""
    monkeypatch.setattr("lambda_watcher.cli._can_ask", lambda: True)
    service.respond(200, {"data": [{"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
                                   {"id": "claude-haiku-4-5-20251001", "display_name": "Haiku"}]})
    service.claude("OK")
    # service 1 (Anthropic), the key, model 2 from the list, then yes to automatic.
    result = runner.invoke(app, ["ai", "add", "--endpoint", service.url],
                           input="1\nsk-ant-typed-key-000\n2\ny\n")
    assert result.exit_code == 0, result.output
    assert "Which AI service" in result.output and "Which model?" in result.output
    assert "sk-ant-typed-key-000" not in result.output       # typed without echo
    saved = AISettings.load(home)
    assert [(m.provider, m.model, m.api_key) for m in saved.models] == [
        ("anthropic", "claude-haiku-4-5-20251001", "sk-ant-typed-key-000")]
    assert saved.auto_explain is True
    assert [r["path"] for r in service.requests] == ["/v1/models?limit=100", "/v1/messages"]


def test_a_second_model_on_the_same_service_offers_the_saved_key(home, service, monkeypatch):
    _add(service)
    monkeypatch.setattr("lambda_watcher.cli._can_ask", lambda: True)
    result = runner.invoke(app, ["ai", "add", "anthropic", "--endpoint", service.url,
                                 "--model", "claude-haiku-4-5-20251001", "--no-test"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "use the key already saved for claude-sonnet-5?" in result.output
    keys = {m.api_key for m in AISettings.load(home).models}
    assert keys == {"sk-ant-test-0000000000"}
