"""A comparison of two versions, written out as the question a model is asked.

The input is the :class:`~lambda_watcher.diffing.compare.VersionDiff` the diff
engine already built, laid out in the same order the report reads — what the
package is now, what it depends on, what it needs from its environment, what
the scanner noticed, and only then the changed lines — because that is also
the order in which a model with a limited budget should meet it. When the
change is bigger than the budget, the changed lines are what gets cut, first
from the least important files, and the prompt says what was left out so the
answer can say so too rather than describing a change it only saw half of.

Four things never leave the machine, whatever the budget:

* vendored files — the dependency section already says ``boto3 1.34.0 →
  1.35.20``, and their line diffs would be most of the prompt and none of the
  news
* the contents of files shaped like credentials (``.env``, ``*.pem``,
  ``id_rsa``) — named, never quoted
* anything matching the secret scanner's patterns, or long enough and random
  enough to be a key, in any line that *is* sent
* with ``send_code`` off, any line of code at all — the model then sees the
  structure alone

The redaction is a mitigation, not a guarantee: :mod:`..analysis.secrets` is
tuned to under-report, and no pattern list recognises every credential. Anyone
for whom that is not good enough has two real answers, both one command away —
``lw ai settings --no-send-code``, or a model running on their own machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..analysis.secrets import SECRET_RULES, _shannon_entropy
from ..utils import format_ts, human_size

if TYPE_CHECKING:                                  # the diffing package imports this
    from ..diffing.compare import FileChange, VersionDiff   # layer's siblings at runtime

#: Bumped whenever the prompt changes enough that an explanation written by an
#: older one is worth regenerating. Stored with every explanation, so a report
#: can tell an answer to the current question from an answer to an older one.
PROMPT_VERSION = 1

SYSTEM_PROMPT = """\
You explain changes to AWS Lambda deployment packages to the engineer who is about \
to deploy them, or who is trying to understand what a colleague shipped.

You are given a comparison of two versions of one function, produced by a diff tool: \
the runtime and handler, dependency changes, the environment variables and AWS \
services the code uses, security-scanner findings, and unified diffs of the \
first-party source files. Third-party packages are summarised as dependency \
version changes rather than shown. Credential-like values have been replaced \
with «redacted …» markers before you see them.

Write for a busy reader:
- Lead with behaviour: what the function now does differently when it runs, not \
which lines moved.
- Be concrete: name the functions, endpoints, tables, queues, environment \
variables and status codes involved, exactly as they appear in the diff.
- Say what could break or needs doing before deploying: new environment \
variables or IAM permissions, changed request or response shapes, removed error \
handling, new external calls, timeouts, retries, data migrations, credentials \
committed to the package.
- Only state what the input supports. When something is not visible — a file \
omitted for length, a file withheld — say so rather than guessing.
- Refer to files by the exact paths given in the input.
- No filler, no praise, and no restating counts the reader can already see.

Reply with one JSON object and nothing else, in exactly this shape:
{
  "headline": "one sentence of at most 100 characters: the most important thing this version changes",
  "summary": "two to four sentences of plain English: what changed and why it matters",
  "risk": "low | medium | high: how careful the reader should be deploying this",
  "risk_reason": "one short sentence explaining that risk level",
  "changes": [
    {"kind": "feature | fix | behaviour | refactor | dependency | config | security | removal | other",
     "title": "at most 80 characters",
     "detail": "one or two sentences: what behaviour changed and what that means",
     "files": ["paths exactly as given in the input"]}
  ],
  "risks": [
    {"level": "high | medium | low",
     "title": "at most 80 characters",
     "detail": "what could go wrong, and how to check for it",
     "files": ["paths exactly as given in the input"]}
  ],
  "checklist": ["a concrete step to take before or after deploying, e.g. \
\\"Add QUEUE_URL to the function's environment variables\\""],
  "files": {"path/exactly/as/given.py": "one line: what changed in this file and why it matters"}
}
Order changes by importance. Use empty lists when there is nothing to say, and \
never invent risks to fill space. At most 8 changes, 6 risks and 8 checklist items.
"""

#: Files whose contents are never sent, only their names: the places a
#: credential lives on purpose rather than by accident.
_WITHHELD = re.compile(
    r"(?:^|/)(?:\.env(?:\.[^/]*)?|\.npmrc|\.pypirc|\.netrc|\.git-credentials|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|[^/]*credentials[^/]*|"
    r"secrets?\.(?:json|ya?ml|toml|ini|txt|env)|[^/]*\.(?:pem|key|p12|pfx|jks|keystore|kdbx))$",
    re.I,
)

#: A run of characters long enough and varied enough to be a key nobody wrote
#: a pattern for. Tested for entropy before it is replaced — see :func:`redact`.
_TOKENISH = re.compile(r"[A-Za-z0-9+/_=-]{32,}")

#: Languages whose changes are code, as opposed to configuration or prose; the
#: former are sent first when the budget is tight.
_CODE_LANGS = frozenset({
    "python", "javascript", "typescript", "go", "java", "kotlin", "ruby", "csharp", "rust",
    "php", "scala", "shell", "powershell", "sql",
})

#: Kinds of change in the order their lines are worth sending: an edited file
#: says the most, a deleted one the least (its lines are all gone).
_KIND_ORDER = {"modified": 0, "renamed": 1, "added": 2, "mode-changed": 3, "removed": 4}

#: Most lines quoted from a removed file. That it is gone is the news; the
#: first screenful says what it was.
_REMOVED_LINES = 40

#: Most dependency changes listed. A lock-file refresh can move hundreds, and
#: the first eighty tell the story.
_MAX_DEPS = 80


@dataclass
class BuiltPrompt:
    """The prompt, and an account of what went into it.

    The account travels with the explanation into the report — "written from
    14 of 16 changed files" — because an answer that silently saw part of a
    change reads exactly like one that saw all of it. ``paths`` is every path
    the model may cite, used to link its answer back to the diffs.
    """

    text: str
    paths: set[str] = field(default_factory=set)
    files_total: int = 0
    files_sent: int = 0
    omitted: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    redactions: int = 0
    send_code: bool = True
    budget: int = 0


def redact(line: str) -> tuple[str, int]:
    """A line with anything credential-shaped replaced, and how many replacements were made.

    ``password = "hunter2hunter2"`` → ``password = "«redacted hardcoded-credential»"``.
    Uses the secret scanner's own patterns, but without its placeholder
    filter: that filter exists to keep findings quiet, and here quiet is the
    wrong way to fail — replacing a harmless ``changeme`` costs a word, while
    missing a real key sends it off the machine. Long random-looking runs are
    replaced too (Shannon entropy of at least 4 bits a character, with digits
    and letters both present), which catches keys no pattern names and also
    the odd hash, which a reader of the explanation will never miss.
    """
    count = 0
    for rule in SECRET_RULES:
        def swap(match: re.Match[str], rule=rule) -> str:
            """The match with its secret part — the rule's group, or all of it — replaced."""
            nonlocal count
            count += 1
            whole, start = match.group(0), match.start(0)
            lo, hi = (match.start(rule.group) - start, match.end(rule.group) - start) if rule.group \
                else (0, len(whole))
            return f"{whole[:lo]}«redacted {rule.kind}»{whole[hi:]}"
        line = rule.pattern.sub(swap, line)

    def swap_token(match: re.Match[str]) -> str:
        """A long run replaced when it looks random rather than like a word."""
        nonlocal count
        token = match.group(0)
        if (_shannon_entropy(token) >= 4.0 and re.search(r"\d", token)
                and re.search(r"[A-Za-z]", token)):
            count += 1
            return "«redacted high-entropy string»"
        return token

    return _TOKENISH.sub(swap_token, line), count


def _handler_module(diff: VersionDiff) -> str | None:
    """The handler's module as a path without extension: ``app.lambda_handler`` → ``app``.

    ``src.handlers.orders.main`` becomes ``src/handlers/orders``, so the file
    that holds the entry point can be sent first whatever language it is in.
    """
    handler = diff.b_meta.get("handler") or diff.a_meta.get("handler")
    if not handler or "." not in str(handler):
        return None
    return str(handler).rsplit(".", 1)[0].replace(".", "/")


def _is_handler_file(path: str, module: str | None) -> bool:
    """Whether ``path`` is the handler's module in any language: ``app.py``, ``app.mjs``."""
    if not module:
        return False
    stem = path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path
    return stem == module or stem.endswith("/" + module)


def _file_priority(change: FileChange, module: str | None) -> tuple:
    """The order files are given lines in: handler, code, configuration; big edits before small."""
    return (
        0 if _is_handler_file(change.path, module) else 1,
        0 if change.lang in _CODE_LANGS else 1,
        _KIND_ORDER.get(change.kind, 5),
        -(change.added_lines + change.removed_lines),
        change.path,
    )


def _counts(change: FileChange) -> str:
    """``+12 −3`` for a file, or its note when lines were not counted (``whitespace only``)."""
    if change.added_lines or change.removed_lines:
        return f"+{change.added_lines} −{change.removed_lines}"
    return change.line_count_note or "no line changes"


def _file_title(change: FileChange, module: str | None) -> str:
    """The heading one file's section starts with: ``modified: app.py (+12 −3) — the handler``."""
    title = f"{change.kind}: {change.path} ({_counts(change)})"
    if change.old_path and change.old_path != change.path:
        title += f", was {change.old_path}"
    if _is_handler_file(change.path, module):
        title += " — contains the Lambda handler"
    return title


def _file_body(change: FileChange, limit: int) -> tuple[str, int]:
    """One file's changed lines as a fenced diff, cut at ``limit`` characters; plus redactions made.

    Returns an empty body for a file with nothing quotable — binary,
    whitespace-only, too large to diff — and the title's note says why. The
    ``---``/``+++`` header lines are dropped: the section title already names
    both paths, and they cost tokens to say it again.
    """
    redactions = 0
    lines: list[str] = []
    if change.word_edits:
        for edit in change.word_edits[:12]:
            text = f"at character {edit.at}: …{edit.lead}[{edit.before} → {edit.after}]{edit.trail}…"
            text, found = redact(text)
            redactions += found
            lines.append(text)
        body = "Minified file, diffed by word:\n" + "\n".join(lines)
        return body[:limit], redactions
    if not change.diff_lines:
        return "", 0
    source = [ln for ln in change.diff_lines if not ln.startswith(("---", "+++"))]
    dropped = 0
    if change.kind == "removed" and len(source) > _REMOVED_LINES:
        dropped = len(source) - _REMOVED_LINES
        source = source[:_REMOVED_LINES]
    used = 0
    for index, line in enumerate(source):
        line, found = redact(line)
        redactions += found
        if used + len(line) + 1 > limit:
            dropped += len(source) - index
            break
        lines.append(line)
        used += len(line) + 1
    body = "```diff\n" + "\n".join(lines) + "\n```"
    if dropped:
        body += f"\n({dropped} more line{'s' if dropped != 1 else ''} of this file not shown)"
    if change.truncated:
        body += "\n(the diff tool itself truncated this file's diff)"
    return body, redactions


def _overview(diff: VersionDiff) -> list[str]:
    """The opening facts: which function, which two versions, and what the package is now."""
    a, b = diff.a_meta, diff.b_meta
    # Counted here rather than taken from `diff.counts()`, which includes any
    # vendored files the report was built to show: the model is told about
    # first-party files only, and the numbers have to agree with that.
    counts: dict[str, int] = {}
    for change in diff.files:
        if not change.is_vendor:
            counts[change.kind] = counts.get(change.kind, 0) + 1
    lines = [
        f"# Lambda function {diff.function_name!r}: version {diff.a_seq} → version {diff.b_seq}",
        "",
        f"- Older: v{diff.a_seq:04d}, archived {format_ts(a.get('ingested_at'))}"
        + (f", downloaded as {a['source_name']}" if a.get("source_name") else "")
        + (f", labelled {a['label']!r}" if a.get("label") else ""),
        f"- Newer: v{diff.b_seq:04d}, archived {format_ts(b.get('ingested_at'))}"
        + (f", downloaded as {b['source_name']}" if b.get("source_name") else "")
        + (f", labelled {b['label']!r}" if b.get("label") else ""),
    ]
    if diff.runtime_change:
        lines.append(f"- Runtime CHANGED: {diff.runtime_change[0]} → {diff.runtime_change[1]}")
    elif b.get("runtime"):
        lines.append(f"- Runtime: {b['runtime']} (unchanged)")
    if diff.handler_change:
        lines.append(f"- Handler CHANGED: {diff.handler_change[0]} → {diff.handler_change[1]}")
    elif b.get("handler"):
        lines.append(f"- Handler: {b['handler']} (unchanged)")
    lines.append(
        f"- Package: {a.get('file_count', '?')} files, {human_size(a.get('total_size') or 0)} → "
        f"{b.get('file_count', '?')} files, {human_size(b.get('total_size') or 0)}"
    )
    described = ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "none"
    added = sum(c.added_lines for c in diff.files if not c.is_vendor)
    removed = sum(c.removed_lines for c in diff.files if not c.is_vendor)
    lines.append(f"- First-party files changed: {described}"
                 + (f"; +{added} −{removed} lines" if diff.diffs_computed else ""))
    vendored = diff.vendor_files_changed + sum(1 for c in diff.files if c.is_vendor)
    if vendored:
        lines.append(f"- Vendored third-party files changed: {vendored} "
                     "(not shown; the dependency changes below account for them)")
    return lines


def _structure(diff: VersionDiff) -> list[str]:
    """Dependencies, environment, services, findings and moves: the change's shape, no code."""
    lines: list[str] = []
    if diff.deps:
        lines += ["", "## Dependency changes"]
        for dep in diff.deps[:_MAX_DEPS]:
            source = "declared" if dep.is_declared else "installed"
            if dep.kind == "changed":
                move = f"{dep.old_version} → {dep.new_version}"
            elif dep.kind == "added":
                move = f"added at {dep.new_version or 'unspecified version'}"
            else:
                move = f"removed (was {dep.old_version or 'unspecified version'})"
            lines.append(f"- {dep.name} ({dep.manager}, {source}): {move}")
        if len(diff.deps) > _MAX_DEPS:
            lines.append(f"- … and {len(diff.deps) - _MAX_DEPS} more")
    if diff.env_added or diff.env_removed:
        lines += ["", "## Environment variables the code reads"]
        if diff.env_added:
            lines.append("- Newly read: " + ", ".join(diff.env_added))
        if diff.env_removed:
            lines.append("- No longer read: " + ", ".join(diff.env_removed))
    if diff.services_added or diff.services_removed:
        lines += ["", "## AWS services the code calls"]
        if diff.services_added:
            lines.append("- Newly used: " + ", ".join(diff.services_added))
        if diff.services_removed:
            lines.append("- No longer used: " + ", ".join(diff.services_removed))
    if diff.findings_new or diff.findings_fixed:
        lines += ["", "## Security scanner"]
        for finding in diff.findings_new[:30]:
            lines.append(f"- NEW {finding.get('severity')} {finding.get('kind')} at "
                         f"{finding.get('path')}:{finding.get('line')} ({finding.get('detail')})")
        if diff.findings_fixed:
            lines.append(f"- {len(diff.findings_fixed)} finding(s) from the older version are gone")
    moves = [m for m in diff.moves if not m.is_vendor]
    if moves:
        lines += ["", "## Directory moves"]
        for move in moves:
            old_dir, new_dir = move.display_dirs
            lines.append(f"- {old_dir}/ → {new_dir}/ ({move.moved} files"
                         + (f", {move.edited} of them also edited" if move.edited else "") + ")")
    if diff.renames_unexamined:
        lines.append(f"\nNote: {diff.renames_unexamined} added files were not checked for renames, "
                     "so some adds and removes may be the same file moved.")
    return lines


def build_prompt(diff: VersionDiff, *, send_code: bool = True, budget: int = 150_000) -> BuiltPrompt:
    """Write the question for one comparison, in at most roughly ``budget`` characters.

    The fixed sections (:func:`_overview`, :func:`_structure`) always go in;
    they are small. What remains of the budget goes to first-party files in
    the order :func:`_file_priority` gives — the handler first, then code, then
    configuration, bigger edits before smaller — each capped at a third of the
    budget, so one rewritten file cannot crowd out every other. Files that do
    not fit are listed by name and size, never silently dropped.
    """
    built = BuiltPrompt(text="", send_code=send_code, budget=budget)
    module = _handler_module(diff)
    first_party = [c for c in diff.files if not c.is_vendor]
    built.files_total = len(first_party)
    for change in first_party:
        built.paths.add(change.path)
        if change.old_path:
            built.paths.add(change.old_path)

    head = _overview(diff) + _structure(diff)
    sections: list[str] = []
    used = sum(len(line) + 1 for line in head) + 400
    per_file = max(3000, budget // 3)

    ordered = sorted(first_party, key=lambda c: _file_priority(c, module))
    listed_only: list[FileChange] = []
    for change in ordered:
        title = f"### {_file_title(change, module)}"
        if _WITHHELD.search(change.path):
            built.withheld.append(change.path)
            sections.append(f"{title}\n(contents withheld: this kind of file usually holds credentials)")
            used += len(sections[-1]) + 2
            continue
        if not send_code:
            listed_only.append(change)
            continue
        room = min(per_file, budget - used - len(title) - 40)
        if room < 400:
            built.omitted.append(change.path)
            listed_only.append(change)
            continue
        body, found = _file_body(change, room)
        built.redactions += found
        section = f"{title}\n{body}" if body else f"{title}\n(no line diff: {_counts(change)})"
        sections.append(section)
        used += len(section) + 2
        built.files_sent += 1

    lines = list(head)
    if sections:
        lines += ["", "## File changes (first-party code)", "", "\n\n".join(sections)]
    if listed_only:
        why = ("Source lines were not included on request; these files changed:"
               if not send_code else
               "Not shown, to stay within the size limit (name and size of change only):")
        lines += ["", f"## {why}"]
        lines += [f"- {c.kind}: {c.path} ({_counts(c)})" for c in listed_only[:300]]
        if len(listed_only) > 300:
            lines.append(f"- … and {len(listed_only) - 300} more")
    if not first_party:
        lines += ["", "No first-party files changed; only the package structure above differs."]
    lines += ["", "Explain this change as instructed. Reply with the JSON object only."]
    built.text = "\n".join(lines)
    return built
