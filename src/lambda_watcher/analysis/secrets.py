"""Flag credentials and risky calls that shouldn't be sitting in a zip.

This is a lightweight tripwire, not a security scanner. It exists because a
deployment package is exactly the place a hardcoded key survives unnoticed, and
because "a new AWS key appeared between v7 and v8" is worth seeing in a diff.
Matched values are stored redacted; the secret itself is never written to the
index.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..utils import read_text
from .inventory import Inventory


@dataclass
class Finding:
    """One flagged line: a possible credential or a risky call.

    ``detail`` is safe to print and store. For a matched secret it is already
    redacted by :func:`_redact`; the value itself never leaves this module.
    """

    kind: str
    severity: str  # high | medium | low
    path: str
    line: int
    detail: str
    is_vendor: bool = False

    def as_dict(self) -> dict:
        """This finding as plain JSON-ready data, for the manifest."""
        return {
            "kind": self.kind,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "detail": self.detail,
            "is_vendor": self.is_vendor,
        }


@dataclass(frozen=True)
class Rule:
    """One pattern to look for, and how loudly to complain when it matches.

    ``group`` names the capture group holding the interesting value. A rule with
    ``group=0`` has nothing to extract — ``-----BEGIN PRIVATE KEY-----`` is the
    whole finding — and that difference decides both whether the placeholder
    filter runs and whether the detail is redacted.
    """

    kind: str
    severity: str
    pattern: re.Pattern[str]
    group: int = 0


SECRET_RULES: list[Rule] = [
    Rule("aws-access-key-id", "high", re.compile(r"\b((?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16})\b"), 1),
    Rule(
        "aws-secret-access-key", "high",
        re.compile(r"""(?i)aws_?secret_?access_?key\s*[=:]\s*['"]?([A-Za-z0-9/+=]{40})['"]?"""), 1,
    ),
    Rule(
        "private-key", "high",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
    Rule("github-token", "high", re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36})\b"), 1),
    Rule("slack-token", "high", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})"), 1),
    Rule("stripe-key", "high", re.compile(r"\b((?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})\b"), 1),
    Rule("google-api-key", "high", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b"), 1),
    Rule(
        "jwt", "medium",
        re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,})"), 1,
    ),
    Rule(
        "connection-string", "high",
        re.compile(
            r"\b((?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)"
            r"://[^\s'\"]+:[^\s'\"@]+@[^\s'\"]+)"
        ), 1,
    ),
    Rule(
        "hardcoded-credential", "medium",
        re.compile(
            r"""(?i)\b(?:password|passwd|pwd|secret|api_?key|apikey|auth_?token|access_?token|client_?secret)\b"""
            r"""\s*[=:]\s*['"]([^'"\n]{8,})['"]"""
        ), 1,
    ),
]

RISK_RULES: list[Rule] = [
    Rule("dynamic-exec", "medium", re.compile(r"\b(?:eval|exec)\s*\(")),
    Rule("shell-injection-risk", "medium", re.compile(r"shell\s*=\s*True|child_process\.exec\s*\(")),
    Rule("pickle-load", "medium", re.compile(r"\bpickle\.loads?\s*\(")),
    Rule("tls-verification-off", "high", re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false")),
    Rule("debug-flag", "low", re.compile(r"(?i)\bDEBUG\s*=\s*True\b")),
]

# Values that are obviously placeholders rather than live credentials.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:x{3,}|\*{3,}|\.{3,}|<[^>]*>|\{\{.*\}\}|\$\{.*\}|%s|none|null|todo|"
    r"change[_-]?me|your[_-].*|my[_-]?(?:secret|password|key).*|example.*|dummy.*|"
    r"test[_-]?(?:key|secret|token|password)?|sample.*|placeholder.*|redacted.*|"
    r"fake.*|insert.*|replace.*|password|secret|123456\d*|abc123)$"
)

_SCANNABLE = {
    "python", "javascript", "typescript", "java", "ruby", "csharp", "go", "shell",
    "json", "yaml", "toml", "ini", "dotenv", "text", "xml", "powershell",
}


def _shannon_entropy(value: str) -> float:
    """Bits of entropy per character, used to tell keys from words.

    A real credential draws on the whole alphabet fairly evenly and scores high;
    an English word or a repeated placeholder scores low. Used only as one input
    to :func:`_is_placeholder`, never on its own.
    """
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _redact(value: str) -> str:
    """Render a matched value so it can be stored without storing the secret.

    ``AKIAIOSFODNN7EXAMPLE`` -> ``AKIA…LE (20 chars)``. Enough to recognise the
    value again and to see it change between versions, not enough to use. Short
    values are replaced by stars entirely, since four of eight characters would
    give too much away.
    """
    value = value.strip()
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-2:]} ({len(value)} chars)"


def _is_placeholder(value: str) -> bool:
    """True when a matched value is obviously not a live credential.

    The scanner would be useless if every ``password = "changeme"`` in an
    example file produced a high-severity finding. Three things disqualify a
    match: a known placeholder shape (``xxxx``, ``<your-key>``, ``${VAR}``,
    ``TODO``), a value that is plainly an environment lookup rather than a
    literal, and a long string whose entropy is too low to be a key — see
    :func:`_shannon_entropy`.

    Tuned to under-report. A missed secret is a gap in a tripwire; a wall of
    false positives is a feature people switch off.
    """
    stripped = value.strip()
    if not stripped or _PLACEHOLDER.match(stripped):
        return True
    if stripped.startswith(("os.environ", "process.env", "${", "{{", "$(")):
        return True
    # Low-entropy strings are usually words, not keys.
    return len(stripped) >= 16 and _shannon_entropy(stripped) < 2.5


def scan(
    root: Path,
    inventory: Inventory,
    include_vendor: bool = False,
    check_secrets: bool = True,
    max_files: int = 3000,
) -> list[Finding]:
    """Scan the package for credentials and risky calls, worst first.

    Runs two rule sets over every scannable text file: :data:`SECRET_RULES`,
    which look for things that should never be in a zip (AWS keys, private
    keys, GitHub and Slack tokens, connection strings with passwords in them),
    and :data:`RISK_RULES`, which look for patterns worth a second glance
    (``eval(``, ``shell=True``, ``verify=False``). Setting ``check_secrets``
    False keeps only the second set.

    Several limits keep this cheap and quiet rather than exhaustive. Vendored
    files are skipped by default, files over 2 MB are not opened, ``max_files``
    caps the walk, and lines longer than 4,000 characters are ignored because a
    minified bundle matches everything and means nothing. Secret matches are
    filtered through :func:`_is_placeholder`, and each ``(kind, value)`` pair is
    reported once per file rather than once per occurrence.

    Findings come back sorted by severity then location, so the first row is
    the one worth reading. Values are redacted before they are returned.
    """
    findings: list[Finding] = []
    entries = inventory.files if include_vendor else inventory.code_files
    rules = (SECRET_RULES if check_secrets else []) + RISK_RULES
    scanned = 0

    for entry in entries:
        if scanned >= max_files:
            break
        if not entry.is_text or entry.lang not in _SCANNABLE:
            continue
        if entry.size > 2 * 1024 * 1024:
            continue
        text = read_text(root / entry.path, max_bytes=2 * 1024 * 1024)
        if not text:
            continue
        scanned += 1

        seen_in_file: set[tuple[str, str]] = set()
        for line_no, line in enumerate(text.splitlines(), start=1):
            if len(line) > 4000:  # minified bundles produce nothing but noise
                continue
            for rule in rules:
                match = rule.pattern.search(line)
                if not match:
                    continue
                value = match.group(rule.group) if rule.group else match.group(0)
                if rule in SECRET_RULES and rule.group and _is_placeholder(value):
                    continue
                detail = _redact(value) if rule.group else value.strip()[:80]
                key = (rule.kind, detail)
                if key in seen_in_file:
                    continue
                seen_in_file.add(key)
                findings.append(
                    Finding(rule.kind, rule.severity, entry.path, line_no, detail, entry.is_vendor)
                )

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.path, f.line))
    return findings
