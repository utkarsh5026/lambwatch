"""Talking to the AI services: one request in, text out, retried when retrying can help.

Plain HTTPS through the standard library rather than each vendor's SDK. The
SDKs would be two more dependencies — and with them httpx, pydantic and a
compiled extension — for every install, including all the ones that never
switch AI on, and "installs cleanly into any Python" is worth more to this
tool than anything the SDKs add for four JSON requests. What they would have
supplied is here instead: retries with backoff that honour ``Retry-After``,
errors sorted into kinds a person can act on, and certificates that work on a
stock macOS Python.

Three wire formats cover four services:

* Anthropic's Messages API
* OpenAI's Chat Completions, which a local server (Ollama, LM Studio, vLLM)
  speaks too, at a URL of its own
* Azure OpenAI, which is Chat Completions addressed to a *deployment*, with its
  key in a different header

Every failure is an :class:`AIError` whose ``hint`` is the next thing to type,
because "HTTP 401" is where a person gets stuck and "the key was rejected —
``lw ai add anthropic`` saves a new one" is where they get unstuck.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import random
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import __version__
from .settings import PROVIDERS, ModelEntry

#: The most a single answer may run to. Generous on purpose: a reasoning model
#: spends part of this allowance thinking before it writes a word, and only
#: tokens actually used are billed.
DEFAULT_MAX_TOKENS = 8000

#: The Messages API version every Claude model accepts.
ANTHROPIC_VERSION = "2023-06-01"

#: The Azure API version used when a deployment has to be addressed the older
#: way (see :class:`AzureClient`). The newest generally-available one when this
#: was written; a Target URI pasted from the Azure portal carries its own.
DEFAULT_AZURE_API_VERSION = "2024-10-21"

#: Statuses where the same request, sent again shortly, can succeed. 529 is
#: Anthropic's "overloaded"; 409 and 425 are conflicts some gateways return
#: while a model is still loading.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})

#: Backoff when the service does not say how long to wait: 2 s, 4 s, 8 s, 16 s,
#: then 30 s at most, each jittered by a quarter either way so that several
#: watchers rate-limited together do not all come back in the same second.
BACKOFF_BASE = 2.0
BACKOFF_CAP = 30.0

#: The longest a ``Retry-After`` is waited out. A service asking for five
#: minutes means an hourly or daily limit, and blocking a terminal — or the
#: watcher's explanation queue — for that long helps nobody; the error says
#: when to try again instead.
MAX_RETRY_AFTER = 90.0

#: What each kind of failure is called on a "retrying" status line.
KIND_TITLES = {
    "rate-limit": "rate limited",
    "overloaded": "the service is overloaded",
    "server": "the service had an error",
    "timeout": "the request timed out",
    "network": "could not connect",
}


class AIError(Exception):
    """A request that failed, sorted into a kind a person can do something about.

    ``kind`` is one of: ``setup`` (nothing usable is configured), ``auth``,
    ``quota`` (the account is out of credit — retrying cannot help, unlike a
    rate limit), ``rate-limit``, ``overloaded``, ``server``, ``timeout``,
    ``network``, ``tls``, ``not-found`` (no such model or deployment),
    ``too-large`` (the prompt does not fit), ``bad-request``, ``filtered`` (a
    content filter withheld the answer) and ``bad-response`` (an answer
    arrived but could not be used).

    ``message`` says what happened in a sentence; ``hint`` says what to type
    next and may be empty only when there is honestly nothing to suggest.
    ``attempts`` is filled in by :func:`with_retries` once it gives up.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        hint: str = "",
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        detail: str = "",
    ) -> None:
        """Record what failed; see the class docstring for what each field means."""
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.detail = detail
        self.attempts = 1

    def __str__(self) -> str:
        """The message, plus how many attempts it took to be sure, when there were several."""
        if self.attempts > 1:
            return f"{self.message} (gave up after {self.attempts} attempts)"
        return self.message

    @property
    def title(self) -> str:
        """A few words for a status line: ``rate limited``, ``could not connect``."""
        return KIND_TITLES.get(self.kind, self.kind.replace("-", " "))

    def as_dict(self) -> dict[str, Any]:
        """The failure as JSON, for the saved record a report reads it back from."""
        return {"kind": self.kind, "message": str(self), "hint": self.hint, "status": self.status}


@dataclass
class Reply:
    """One answer from a model, and what it cost to get.

    ``stop_reason`` is the service's own word for why the answer ended —
    ``end_turn`` or ``stop`` normally, ``max_tokens`` or ``length`` when it was
    cut off — which :attr:`truncated` folds into one question.
    """

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None
    seconds: float = 0.0
    attempts: int = 1

    @property
    def truncated(self) -> bool:
        """True when the answer stopped because it ran out of room, not because it was done."""
        return (self.stop_reason or "") in {"max_tokens", "length", "model_length_context_exceeded"}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@dataclass
class _Response:
    """A finished HTTP exchange: status, lower-cased headers, and the body (JSON when it was)."""

    status: int
    headers: dict[str, str]
    body: Any


def _ssl_context(use_certifi: bool = False) -> ssl.SSLContext:
    """The TLS context to verify a service's certificate with.

    The system's own trust store first, which also honours ``SSL_CERT_FILE``
    — how corporate proxies with their own certificate authority are usually
    set up. ``certifi``'s bundle is the fallback, and only when it happens to be
    installed: the python.org installer on macOS ships with no certificates at
    all until "Install Certificates.command" is run, which is the most common
    reason a first request fails there.
    """
    if use_certifi:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def _certifi_available() -> bool:
    """Whether the ``certifi`` package can be imported, for the TLS fallback."""
    try:
        import certifi  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def _decode(raw: bytes) -> Any:
    """A response body as JSON when it is JSON, otherwise as text."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _host(url: str) -> str:
    """The host a URL points at, for messages: ``api.anthropic.com``."""
    return urllib.parse.urlsplit(url).netloc or url


def _http(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: float,
    *,
    local: bool = False,
) -> _Response:
    """Send one request and return the response, whatever its status.

    HTTP errors come back as responses, for the caller to classify by service;
    only failures to get a response at all — no connection, no answer in time,
    a certificate that does not verify — raise, already sorted into an
    :class:`AIError`. ``local`` changes only the advice: a refused connection to
    ``localhost`` means the model server is not running, not that the internet
    is down.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": f"lambda-watcher/{__version__}",
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if data is not None else {}),
        **headers,
    })
    tried_certifi = False
    while True:
        # The proxy handler build_opener adds by default reads HTTPS_PROXY and
        # NO_PROXY, so a machine that needs a proxy for everything else gets
        # one here too.
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context(tried_certifi)))
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read()
                return _Response(response.status, {k.lower(): v for k, v in response.headers.items()},
                                 _decode(raw))
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp is not None else b""
            return _Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, _decode(raw))
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                if not tried_certifi and _certifi_available():
                    tried_certifi = True
                    continue
                raise AIError(
                    "tls", f"could not verify the certificate of {_host(url)}",
                    hint="on macOS, run \"Install Certificates.command\" from your Python folder; "
                         "behind a company proxy, point SSL_CERT_FILE at its certificate bundle",
                    detail=str(reason),
                ) from exc
            if isinstance(reason, TimeoutError):
                raise _timeout(url, timeout, local) from exc
            raise _unreachable(url, local, str(reason)) from exc
        except TimeoutError as exc:
            raise _timeout(url, timeout, local) from exc
        except (ConnectionError, http.client.HTTPException, OSError) as exc:
            raise _unreachable(url, local, str(exc)) from exc


def _timeout(url: str, timeout: float, local: bool) -> AIError:
    """The error for a service that went quiet for longer than ``timeout`` seconds."""
    hint = ("a local model can be slow on a laptop; `lw ai settings --timeout 900` waits longer"
            if local else "`lw ai settings --timeout 600` waits longer")
    return AIError("timeout", f"{_host(url)} did not answer within {timeout:.0f}s",
                   hint=hint, retryable=True)


def _unreachable(url: str, local: bool, detail: str) -> AIError:
    """The error for a service no connection could be made to."""
    if local:
        return AIError(
            "network", f"nothing is answering at {url}",
            hint="start your model server (for Ollama: `ollama serve`), or fix the URL with "
                 "`lw ai add local --endpoint <url>`",
            retryable=True, detail=detail,
        )
    return AIError(
        "network", f"could not connect to {_host(url)}",
        hint="check your internet connection; behind a proxy, set HTTPS_PROXY",
        retryable=True, detail=detail,
    )


# --------------------------------------------------------------------------- #
# Reading errors
# --------------------------------------------------------------------------- #
def _error_parts(body: Any) -> tuple[str, str, str]:
    """``(message, type, code)`` from an error body, in whichever shape the service used.

    Anthropic sends ``{"error": {"type", "message"}}``, OpenAI and Azure
    ``{"error": {"message", "type", "code"}}``, and a local server or a proxy in
    between may send plain text or HTML; all of them end up here.
    """
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return (str(error.get("message") or ""), str(error.get("type") or ""),
                    str(error.get("code") or ""))
        if isinstance(error, str):
            return error, "", ""
        if body.get("message"):
            return str(body["message"]), str(body.get("type") or ""), str(body.get("code") or "")
    text = str(body or "").strip()
    return (text[:300] + ("…" if len(text) > 300 else "")), "", ""


def _retry_after(headers: dict[str, str]) -> float | None:
    """How long the service asked us to wait, in seconds, if it said.

    ``retry-after-ms`` (OpenAI) is preferred for its precision; ``retry-after``
    may be a number of seconds or an HTTP date, and both forms occur.
    """
    millis = headers.get("retry-after-ms")
    if millis:
        try:
            return max(0.0, float(millis) / 1000)
        except ValueError:
            pass
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


_TOO_LARGE_MARKERS = (
    "context_length_exceeded", "prompt is too long", "maximum context length", "too many tokens",
    "reduce the length", "context window", "request too large", "input is too long",
    "string_above_max_length", "tokens_limit_reached",
)
_QUOTA_MARKERS = ("insufficient_quota", "exceeded your current quota", "credit balance", "billing")
_NOT_FOUND_MARKERS = ("model_not_found", "does not exist", "not_found_error", "deploymentnotfound",
                      "no such model", "model not found", "unknown model")


def classify(response: _Response, entry: ModelEntry, url: str) -> AIError:
    """Turn a failed response into an :class:`AIError` that says what to do about it.

    The status decides most of it; the body's own words settle the cases a
    status cannot. A 429 is a rate limit to wait out *unless* it says the
    account is out of credit, which no amount of waiting fixes — the most
    important distinction here, since retrying a quota error just fails five
    times more slowly. Too-large is recognised whatever status it arrives
    with, because every service reports it differently and the caller can fix
    it by sending less.
    """
    message, kind_word, code = _error_parts(response.body)
    haystack = f"{message} {kind_word} {code}".lower()
    status = response.status
    label = entry.info.label
    what = f"deployment {entry.model!r}" if entry.provider == "azure" else f"model {entry.model!r}"
    readd = f"`lw ai add {entry.provider} --name {entry.name}`"
    shown = message or f"HTTP {status}"

    if any(marker in haystack for marker in _TOO_LARGE_MARKERS) or status == 413:
        return AIError("too-large", f"the change is too large for {entry.model or label} in one request",
                       hint="`lw ai settings --max-prompt-kb 60` sends less of it", status=status,
                       detail=message)
    if "content_filter" in haystack or "responsibleaipolicyviolation" in haystack:
        # Azure screens the *prompt* too, and refuses the request outright when
        # something in the code trips its filter.
        return AIError("filtered", f"{label}'s content filter refused the request",
                       hint="`lw ai settings --no-send-code` sends only the structure, not the code",
                       status=status, detail=message)
    if any(marker in haystack for marker in _QUOTA_MARKERS) and status in {400, 402, 403, 429}:
        return AIError("quota", f"your {label} account has run out of credit or quota",
                       hint=f"add credit in the {label} console, or switch model with `lw ai use <name>`",
                       status=status, detail=message)
    if status == 401:
        return AIError("auth", f"{label} rejected the API key", hint=f"{readd} saves a new one",
                       status=status, detail=message)
    if status == 403:
        return AIError("auth", f"the API key is not allowed to use {what}: {shown}",
                       hint=f"check the key's permissions in the {label} console, or {readd}",
                       status=status, detail=message)
    if status == 404 or any(marker in haystack for marker in _NOT_FOUND_MARKERS):
        if entry.provider == "azure":
            hint = ("check the deployment name under Azure AI Foundry → Deployments, then "
                    f"{readd} --model <deployment>")
        elif entry.provider == "local":
            hint = (f"`ollama pull {entry.model}` if it is an Ollama model, or check the server URL "
                    f"with {readd}")
        else:
            hint = f"`lw ai add {entry.provider}` lists the models your key can use"
        return AIError("not-found", f"{label} has no {what} at {_host(url)}", hint=hint,
                       status=status, detail=message)
    if status == 429:
        return AIError("rate-limit", f"{label} is rate limiting requests", status=status,
                       hint="wait a minute and try again; a smaller or cheaper model often has "
                            "higher limits (`lw ai use <name>`)",
                       retryable=True, retry_after=_retry_after(response.headers), detail=message)
    if status in {503, 529}:
        return AIError("overloaded", f"{label} is overloaded right now", status=status,
                       hint="try again in a few minutes", retryable=True,
                       retry_after=_retry_after(response.headers), detail=message)
    if status in RETRYABLE_STATUSES or status >= 500:
        return AIError("server", f"{label} returned an error ({status}): {shown}", status=status,
                       hint="try again in a few minutes", retryable=status in RETRYABLE_STATUSES
                       or status >= 500, retry_after=_retry_after(response.headers), detail=message)
    return AIError("bad-request", f"{label} refused the request ({status}): {shown}", status=status,
                   hint="if this persists, try another model with `lw ai use <name>`", detail=message)


# --------------------------------------------------------------------------- #
# Retrying
# --------------------------------------------------------------------------- #
def backoff(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Seconds to wait before retry number ``attempt`` (0-based): about 2, 4, 8, 16, 30."""
    base = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
    return base * (0.75 + rng() / 2)


def with_retries(
    send: Callable[[], Reply],
    *,
    retries: int,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, float, AIError], None] | None = None,
    rng: Callable[[], float] = random.random,
) -> Reply:
    """Call ``send`` until it succeeds, a failure is not worth retrying, or ``retries`` run out.

    Only failures marked ``retryable`` are retried — rate limits, overloads,
    server errors, timeouts, dropped connections. A rejected key or an empty
    account fails at once, since the same request will be refused the same
    way. The service's ``Retry-After`` is honoured when it gives one, up to
    :data:`MAX_RETRY_AFTER`; beyond that the error is raised with the wait
    named, rather than holding the caller for minutes.

    ``on_retry(attempt, seconds, error)`` is called before each wait, so a
    terminal can say "rate limited — retrying in 8s (2 of 5)" instead of
    appearing to hang. ``sleep`` and ``rng`` exist for tests.
    """
    attempt = 0
    while True:
        try:
            reply = send()
            reply.attempts = attempt + 1
            return reply
        except AIError as error:
            error.attempts = attempt + 1
            if not error.retryable or attempt >= retries:
                raise
            wait = error.retry_after if error.retry_after is not None else backoff(attempt, rng)
            if wait > MAX_RETRY_AFTER:
                error.message += f" and asked for a {wait:.0f}s wait"
                raise
            if on_retry is not None:
                on_retry(attempt + 1, wait, error)
            sleep(wait)
            attempt += 1


# --------------------------------------------------------------------------- #
# The services
# --------------------------------------------------------------------------- #
def normalize_local_url(text: str) -> str:
    """A local server's base URL in the form requests are built on: ``http://localhost:11434/v1``.

    People paste what they have: a bare ``localhost:11434``, the server root,
    or the full ``…/v1/chat/completions`` from an example. Each of those is cut
    back to, or extended to, the ``/v1`` base every OpenAI-compatible server
    serves.
    """
    url = text.strip() or "http://localhost:11434/v1"
    if "://" not in url:
        url = "http://" + url
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip("/")
    for tail in ("/chat/completions", "/completions", "/models"):
        if path.endswith(tail):
            path = path[: -len(tail)]
    if not path:
        path = "/v1"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def parse_azure_endpoint(text: str) -> tuple[str, str, str]:
    """``(base URL, deployment, api version)`` from whatever the Azure portal offered.

    The portal shows both a bare endpoint, ``https://res.openai.azure.com/``,
    and a full "Target URI" such as
    ``https://res.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2025-01-01-preview``.
    Pasting either works: the deployment and version come out of the second,
    and are empty for the first.
    """
    url = text.strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc, "", "", "")).rstrip("/")
    segments = [s for s in parts.path.split("/") if s]
    deployment = ""
    if "deployments" in segments:
        index = segments.index("deployments")
        if index + 1 < len(segments):
            deployment = urllib.parse.unquote(segments[index + 1])
    version = urllib.parse.parse_qs(parts.query).get("api-version", [""])[0]
    return base, deployment, version


class Client:
    """One configured model, able to answer a prompt and list its siblings.

    Subclasses supply the wire format. A client makes exactly one attempt per
    call; retrying is :func:`with_retries`'s job, so the policy lives in one
    place for every service.
    """

    def __init__(self, entry: ModelEntry, timeout: float) -> None:
        """Bind a client to a model entry and a per-read timeout in seconds."""
        self.entry = entry
        self.timeout = timeout

    @property
    def local(self) -> bool:
        """Whether this talks to a server on the user's own machine or network."""
        return self.entry.provider == "local"

    def _require_key(self) -> str:
        """The API key, or a ``setup`` error naming how to add one."""
        key = self.entry.resolved_key()
        if not key and self.entry.info.needs_key:
            raise AIError("setup", f"no API key for {self.entry.label}",
                          hint=self.entry.key_problem() or f"`lw ai add {self.entry.provider}`")
        return key

    def _send(self, method: str, url: str, headers: dict[str, str],
              payload: dict[str, Any] | None) -> _Response:
        """One HTTP exchange, raising the classified error for anything but a 2xx."""
        response = _http(method, url, headers, payload, self.timeout, local=self.local)
        if not 200 <= response.status < 300:
            raise classify(response, self.entry, url)
        return response

    def complete(self, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> Reply:
        """Ask the model once. Implemented by each service."""
        raise NotImplementedError

    def list_models(self) -> list[tuple[str, str]]:
        """``(model id, note)`` pairs this key can use, newest first; empty when unknown."""
        return []


class AnthropicClient(Client):
    """Claude, through the Messages API."""

    @property
    def base(self) -> str:
        """``https://api.anthropic.com``, or the override with any trailing ``/v1`` removed."""
        base = (self.entry.endpoint or "https://api.anthropic.com").rstrip("/")
        return base[:-3] if base.endswith("/v1") else base

    def _headers(self) -> dict[str, str]:
        """The key and API version every Anthropic request carries."""
        return {"x-api-key": self._require_key(), "anthropic-version": ANTHROPIC_VERSION}

    def complete(self, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> Reply:
        """Send one message and join the text blocks of the answer."""
        started = time.monotonic()
        response = self._send("POST", f"{self.base}/v1/messages", self._headers(), {
            "model": self.entry.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        })
        body = response.body if isinstance(response.body, dict) else {}
        blocks = body.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        usage = body.get("usage") or {}
        if body.get("stop_reason") == "refusal":
            raise AIError("filtered", f"{self.entry.label} declined to answer",
                          hint="try again, or `lw ai settings --no-send-code` to send only the structure")
        return Reply(text=text, model=str(body.get("model") or self.entry.model),
                     input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                     stop_reason=body.get("stop_reason"), seconds=time.monotonic() - started)

    def list_models(self) -> list[tuple[str, str]]:
        """Every model the key can use, as the API lists them — newest first."""
        response = self._send("GET", f"{self.base}/v1/models?limit=100", self._headers(), None)
        data = response.body.get("data", []) if isinstance(response.body, dict) else []
        return [(str(m["id"]), str(m.get("display_name") or "")) for m in data
                if isinstance(m, dict) and m.get("id")]


#: Model ids OpenAI lists that cannot answer a chat request — audio, images,
#: embeddings and the like — so they are left out of the menu ``lw ai add``
#: draws. A heuristic, so a missed one only costs a failed test call.
_NOT_CHAT = ("audio", "realtime", "tts", "transcribe", "whisper", "image", "dall-e", "embedding",
             "moderation", "search", "davinci", "babbage", "instruct", "computer-use", "sora")


#: The limit a server names when it refuses an output length as too large:
#: "supports at most 4096 completion tokens", "maximum value is 16384".
_TOKEN_CEILING = re.compile(r"(?:at most|maximum(?: value)? (?:is|of)|<=|up to)\s*(\d{3,6})", re.I)


class OpenAIClient(Client):
    """OpenAI's Chat Completions, and any local server that speaks the same protocol."""

    #: The name of the output-length parameter to try first. OpenAI's reasoning
    #: models refuse the older ``max_tokens``; many local servers only know it.
    token_param = "max_completion_tokens"

    @property
    def base(self) -> str:
        """The ``…/v1`` URL requests are built on."""
        if self.local:
            return normalize_local_url(self.entry.endpoint)
        return (self.entry.endpoint or "https://api.openai.com/v1").rstrip("/")

    def _headers(self) -> dict[str, str]:
        """A bearer token, when there is a key; a local server usually wants none."""
        key = self._require_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _chat_url(self) -> str:
        """Where a chat request is posted."""
        return f"{self.base}/chat/completions"

    def _body(self, system: str, prompt: str, max_tokens: int, token_param: str) -> dict[str, Any]:
        """The request body. No ``temperature``: reasoning models reject anything but the default."""
        return {
            "model": self.entry.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            token_param: max_tokens,
        }

    def complete(self, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> Reply:
        """Send one chat request, switching the length parameter once if the server objects to it.

        Which of ``max_tokens`` and ``max_completion_tokens`` a server accepts
        depends on the model, the API version and whose server it is, and the
        only reliable way to find out is to be told. The switch is part of this
        one attempt, not a retry: the first request was refused before any
        work was done.
        """
        started = time.monotonic()
        token_param = "max_tokens" if self.local else self.token_param
        try:
            response = self._send("POST", self._chat_url(), self._headers(),
                                  self._body(system, prompt, max_tokens, token_param))
        except AIError as error:
            if error.kind != "bad-request" or token_param not in error.detail:
                raise
            # "max_tokens is too large: 8000. This model supports at most 4096"
            # is the parameter understood and the number refused, so the fix
            # is the number; anything else about it is the name refused.
            ceiling = _TOKEN_CEILING.search(error.detail)
            if ceiling and 0 < int(ceiling.group(1)) < max_tokens:
                max_tokens = int(ceiling.group(1))
            else:
                token_param = "max_tokens" if token_param != "max_tokens" else "max_completion_tokens"
            response = self._send("POST", self._chat_url(), self._headers(),
                                  self._body(system, prompt, max_tokens, token_param))
        return self._reply(response, started)

    def _reply(self, response: _Response, started: float) -> Reply:
        """Read the first choice out of a chat response."""
        body = response.body if isinstance(response.body, dict) else {}
        choices = body.get("choices") or [{}]
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):   # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        finish = choice.get("finish_reason")
        if finish == "content_filter":
            raise AIError("filtered", f"{self.entry.info.label}'s content filter withheld the answer",
                          hint="try again, or `lw ai settings --no-send-code` to send only the structure")
        usage = body.get("usage") or {}
        return Reply(text=str(content or ""), model=str(body.get("model") or self.entry.model),
                     input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                     stop_reason=finish, seconds=time.monotonic() - started)

    def list_models(self) -> list[tuple[str, str]]:
        """Chat models the key can use, newest first. For a local server, everything it serves."""
        response = self._send("GET", f"{self.base}/models", self._headers(), None)
        data = response.body.get("data", []) if isinstance(response.body, dict) else []
        models = [m for m in data if isinstance(m, dict) and m.get("id")]
        if not self.local:
            models = [m for m in models
                      if str(m["id"]).startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt"))
                      and not any(word in str(m["id"]) for word in _NOT_CHAT)]
        models.sort(key=lambda m: int(m.get("created") or 0), reverse=True)
        return [(str(m["id"]), "") for m in models]


class AzureClient(OpenAIClient):
    """Azure OpenAI: Chat Completions addressed to a deployment, keyed by ``api-key``.

    Two ways of addressing a deployment exist. The ``/openai/v1/`` API takes
    the deployment as the ``model`` and needs no version, so it is tried first
    whenever no ``api_version`` is set. Resources or gateways that do not serve
    it answer 404, and then the classic
    ``/openai/deployments/<name>/chat/completions?api-version=…`` form is tried
    once before the deployment is reported missing. Setting ``api_version`` —
    or pasting a Target URI that carries one — skips straight to the classic
    form.
    """

    @property
    def base(self) -> str:
        """The resource's scheme and host, whatever was pasted."""
        return parse_azure_endpoint(self.entry.endpoint)[0]

    def _headers(self) -> dict[str, str]:
        """Azure's own key header."""
        return {"api-key": self._require_key()}

    def _classic_url(self, version: str) -> str:
        """The deployments URL, with its API version."""
        deployment = urllib.parse.quote(self.entry.model, safe="")
        return f"{self.base}/openai/deployments/{deployment}/chat/completions?api-version={version}"

    def _chat_url(self) -> str:
        """The v1 URL, or the classic one when an API version is set."""
        if self.entry.api_version:
            return self._classic_url(self.entry.api_version)
        return f"{self.base}/openai/v1/chat/completions"

    def _body(self, system: str, prompt: str, max_tokens: int, token_param: str) -> dict[str, Any]:
        """The classic form names the deployment in the URL and wants no ``model`` field."""
        body = super()._body(system, prompt, max_tokens, token_param)
        if self.entry.api_version:
            body.pop("model")
        return body

    def complete(self, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> Reply:
        """Ask the deployment once, falling back to the classic URL if v1 is not served."""
        if not self.base or "://" not in self.base or not urllib.parse.urlsplit(self.base).netloc:
            raise AIError("setup", "no Azure OpenAI endpoint is set for this model",
                          hint=f"`lw ai add azure --name {self.entry.name} --endpoint "
                               "https://<resource>.openai.azure.com`")
        try:
            return super().complete(system, prompt, max_tokens)
        except AIError as error:
            if error.kind != "not-found" or self.entry.api_version:
                raise
        classic = ModelEntry(**{**self.entry.as_dict(), "api_version": DEFAULT_AZURE_API_VERSION})
        return AzureClient(classic, self.timeout).complete(system, prompt, max_tokens)

    def list_models(self) -> list[tuple[str, str]]:
        """Nothing: a deployment list needs Azure management credentials, not an API key."""
        return []


def client_for(entry: ModelEntry, timeout: float) -> Client:
    """The client that speaks this entry's service."""
    if entry.provider == "anthropic":
        return AnthropicClient(entry, timeout)
    if entry.provider == "azure":
        return AzureClient(entry, timeout)
    return OpenAIClient(entry, timeout)


def complete(
    entry: ModelEntry,
    system: str,
    prompt: str,
    *,
    timeout: float,
    retries: int,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    on_retry: Callable[[int, float, AIError], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Reply:
    """Ask a model one question, retrying the failures worth retrying. See :func:`with_retries`."""
    if entry.provider not in PROVIDERS:
        raise AIError("setup", f"unknown AI service {entry.provider!r}", hint="`lw ai add` sets one up")
    client = client_for(entry, timeout)
    return with_retries(lambda: client.complete(system, prompt, max_tokens),
                        retries=retries, sleep=sleep, on_retry=on_retry)


def ping(entry: ModelEntry, *, timeout: float = 60.0, retries: int = 1,
         sleep: Callable[[float], None] = time.sleep) -> Reply:
    """Prove a model answers, with the smallest request that can.

    Success is any answer at all, empty included: a reasoning model given a
    small allowance can spend all of it thinking and return no text, and that
    still proves the key, the model and the endpoint are right — which is all
    ``lw ai add`` and ``lw ai test`` need to know.
    """
    return complete(entry, "You are a connectivity check.", "Reply with the single word OK.",
                    timeout=timeout, retries=retries, max_tokens=400, sleep=sleep)


def list_models(entry: ModelEntry, *, timeout: float = 20.0) -> list[tuple[str, str]]:
    """The models this entry's key can use, or an empty list when the service cannot say."""
    return client_for(entry, timeout).list_models()
