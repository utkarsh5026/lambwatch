"""What a report page says about AI, decided in one place for every page that says it.

Four writers render comparison pages — the ingest, ``lw diff --html``, ``lw
report`` and ``lw explain`` — and all of them have to agree on the same
question: for *this* pair of versions, is there an explanation to show, one
being written, one that failed, or none yet? And if none, what exactly should
the reader type to get one? :func:`panel_for` is that answer, as an
:class:`AIPanel` the renderer draws without having to know where it came from.

A page is static HTML on disk, so it can never make the request itself. What
it can do is hand the reader the exact command, with a button to copy it, and
— while a request is running — reload itself until the answer lands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .explanation import Explanation, load_record

if TYPE_CHECKING:
    from ..diffing.compare import VersionDiff
    from ..store import Store
    from .settings import AISettings


@dataclass
class AIPanel:
    """What one comparison page shows in its AI card.

    ``state`` is one of:

    ``done``
        ``explanation`` is the answer.
    ``pending``
        One is being written now (``model``, since ``started_at``); the page
        reloads itself until it lands. ``explanation`` may hold the previous
        answer while a refresh runs.
    ``failed``
        The last attempt failed with ``message``; ``hint`` says why it might
        have, and ``command`` tries again. A previous answer may still show.
    ``missing``
        AI is set up but this pair has not been explained: ``command`` does it.
    ``unconfigured``
        No model is set up; the card says how to set one up, then ``command``.

    ``storage_key`` names this pair for the page's own storage, so the deploy
    checklist remembers which boxes were ticked across reloads — and a refresh
    of the page for a *different* pair does not inherit them.
    """

    state: str
    command: str
    explanation: Explanation | None = None
    model: str = ""
    started_at: str = ""
    message: str = ""
    hint: str = ""
    storage_key: str = ""


def shell_word(text: str) -> str:
    """``text`` as it can be pasted into a shell: bare when that is safe, double-quoted otherwise.

    ``order-processor`` stays as it is; ``Order Processor`` becomes
    ``"Order Processor"``, the one quoting bash, zsh and PowerShell all read
    the same way.
    """
    if re.fullmatch(r"[\w@%+=:,./-]+", text):
        return text
    return '"' + text.replace('"', '\\"') + '"'


def explain_command(function_name: str, a_seq: int, b_seq: int, *, refresh: bool = False) -> str:
    """The command that explains exactly this pair: ``lw explain orders --from 6 --to 7``.

    Always names both versions, even for the latest pair where plain ``lw
    explain orders`` would do: a page is read long after it is written, by
    which time "latest" means some other pair.
    """
    command = f"lw explain {shell_word(function_name)} --from {a_seq} --to {b_seq}"
    return command + " --refresh" if refresh else command


def storage_key(function_name: str, a_meta: dict[str, Any], b_meta: dict[str, Any]) -> str:
    """The key a page stores its checklist ticks under, unique to this pair of trees."""
    a_tree = str(a_meta.get("tree_hash") or "")[:12]
    b_tree = str(b_meta.get("tree_hash") or "")[:12]
    return f"lw-ai:{function_name}:{a_tree}-{b_tree}"


def panel_for(store: Store, diff: VersionDiff, settings: AISettings) -> AIPanel | None:
    """The AI card for one comparison page, or ``None`` when the page should not mention AI.

    ``None`` means AI is switched off (``lw ai off``): someone who said no
    should not find a card nudging them back on every page. Otherwise a saved
    record decides it, with one correction — a ``pending`` record whose writer
    has gone (see :meth:`~.explanation.Record.pending_is_stale`) is shown as a
    failure to retry, not as a wait that will never end.
    """
    if not settings.enabled:
        return None
    command = explain_command(diff.function_name, diff.a_seq, diff.b_seq)
    key = storage_key(diff.function_name, diff.a_meta, diff.b_meta)
    record = load_record(store, diff.a_meta, diff.b_meta)
    if record is not None:
        if record.status == "pending" and record.pending_is_stale():
            return AIPanel(state="failed", command=command, explanation=record.explanation,
                           model=record.model, storage_key=key,
                           message="the last attempt was interrupted before it finished")
        if record.status == "failed":
            error = record.error or {}
            return AIPanel(state="failed", command=command, explanation=record.explanation,
                           model=record.model, storage_key=key,
                           message=str(error.get("message") or "the last attempt failed"),
                           hint=str(error.get("hint") or ""))
        if record.status == "pending":
            return AIPanel(state="pending", command=command, explanation=record.explanation,
                           model=record.model, started_at=record.started_at, storage_key=key)
        if record.explanation is not None:
            return AIPanel(state="done", command=command, explanation=record.explanation,
                           model=record.model, storage_key=key)
    configured = settings.resolve() is not None
    return AIPanel(state="missing" if configured else "unconfigured", command=command, storage_key=key)


def headline_for(store: Store, a_meta: dict[str, Any], b_meta: dict[str, Any]) -> tuple[str, str] | None:
    """``(headline, risk)`` for a pair that has been explained, for the history and archive pages.

    Those pages list many pairs and have room for one line each, so this is the
    one line. ``None`` when there is no finished explanation to quote.
    """
    record = load_record(store, a_meta, b_meta)
    if record is None or record.explanation is None or not record.explanation.headline:
        return None
    return record.explanation.headline, record.explanation.risk
