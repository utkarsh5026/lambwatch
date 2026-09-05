"""Small shared helpers: hashing, text detection, path matching, formatting."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

LOG = logging.getLogger("lambda_watcher")

_CHUNK = 1 << 20  # 1 MiB

# Extension -> language label, used for reporting and syntax hints.
LANG_BY_EXT: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin", ".go": "go", ".rb": "ruby", ".rs": "rust",
    ".cs": "csharp", ".php": "php", ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".cfg": "ini", ".xml": "xml", ".html": "html", ".css": "css", ".sql": "sql",
    ".md": "markdown", ".txt": "text", ".csv": "csv", ".env": "dotenv",
    ".jar": "binary", ".so": "binary", ".dll": "binary", ".dylib": "binary",
    ".pyc": "binary", ".zip": "binary", ".png": "binary", ".jpg": "binary",
    ".gz": "binary", ".whl": "binary", ".class": "binary",
}

BINARY_LANGS = {"binary"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_hash(entries: list[tuple[str, str]]) -> str:
    """Content hash of a whole extracted tree.

    ``entries`` is a list of ``(relative_path, file_sha256)``. Deliberately
    ignores timestamps, file order and zip metadata so that re-downloading an
    unchanged function produces the same hash.
    """
    payload = "\n".join(f"{digest}  {path}" for path, digest in sorted(entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short_hash(digest: str, length: int = 8) -> str:
    return digest[:length]


def is_probably_text(path: Path, sniff_bytes: int = 8192) -> bool:
    """Cheap binary check: NUL bytes or undecodable content means binary."""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(sniff_bytes)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        # Latin-1 fallback keeps logs and odd encodings readable.
        printable = sum(1 for b in chunk if 32 <= b < 127 or b in (9, 10, 13))
        return printable / len(chunk) > 0.85
    return True


def language_for(path: str) -> str:
    ext = PurePosixPath(path).suffix.lower()
    if ext:
        return LANG_BY_EXT.get(ext, ext.lstrip("."))
    name = PurePosixPath(path).name.lower()
    if name in {"dockerfile", "makefile", "gemfile", "rakefile", "procfile"}:
        return name
    return "text"


def read_text(path: Path, max_bytes: int | None = None) -> str | None:
    """Read a file as text, returning None if it is not decodable."""
    try:
        data = path.read_bytes() if max_bytes is None else path.read_bytes()[:max_bytes]
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def count_lines(path: Path) -> int:
    """Number of lines, counting a trailing partial line."""
    total = 0
    last = b""
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                total += chunk.count(b"\n")
                last = chunk
    except OSError:
        return 0
    if last and not last.endswith(b"\n"):
        total += 1
    return total


def slugify(name: str, fallback: str = "unnamed") -> str:
    """Filesystem-safe directory name that still resembles the function name."""
    normalised = unicodedata.normalize("NFKD", name)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_only).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = fallback
    # Windows reserves a handful of device names.
    if slug.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        slug = f"_{slug}"
    return slug[:120]


# The ref a source-archive directory is named after: `myrepo-1.2.3`,
# `myrepo-main`, `myrepo-a1b2c3d`. A bare `-v2` is deliberately absent, for the
# same reason NamingConfig.strip_patterns leaves it alone: it is far more often
# part of a real name than a version tag.
_REF_SUFFIX = re.compile(
    r"[-_](?P<ref>"
    r"v?\d+\.\d+(?:\.\d+)?(?:[-.][0-9A-Za-z.]+)?"   # 1.2.3, v1.2.3, 1.2.3-rc1
    r"|main|master|develop|trunk"
    r"|[0-9a-f]{7,40}"                                # commit sha, short or full
    r")$"
)


def ref_from_dirname(name: str) -> str | None:
    """Pull the version ref out of a source-archive directory name.

    ``myrepo-1.2.3`` -> ``v1.2.3``; ``myrepo-main`` -> ``main``. Returns None
    when the name carries no ref, which is the common case for a deployment
    package. This only ever produces a label, never an identity, so a name that
    happens to end in something hex-shaped costs a cosmetic mislabel and
    nothing more.
    """
    match = _REF_SUFFIX.search(name.strip())
    if not match:
        return None
    ref = match.group("ref")
    # Normalise a bare `1.2.3` to `v1.2.3`; leave `main` and shas as they are.
    return f"v{ref}" if ref[0].isdigit() else ref


def matches_any(path: str, patterns: list[str]) -> bool:
    """True when a posix-style relative path matches any glob pattern."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # ``foo/**`` should also match ``foo/bar`` on platforms where fnmatch
        # does not treat ** specially.
        if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
    return False


def human_size(num_bytes: float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < step:
            return f"{num_bytes:,.0f} {unit}" if unit == "B" else f"{num_bytes:,.1f} {unit}"
        num_bytes /= step
    return f"{num_bytes:,.1f} PB"


def rename_label(old: str, new: str) -> tuple[str, str, str, str]:
    """A rename split into ``(shared prefix, was, is now, shared suffix)``.

    ``site-packages/boto3-1.34.0.dist-info/METADATA`` becoming
    ``site-packages/boto3-1.35.20.dist-info/METADATA`` is one version number
    moving, but written out in full twice it is 90 characters of near-identical
    path and the reader has to diff it by eye. Naming only the part that moved
    is what git does, and for the same reason.

    The split lands on separator boundaries, so the middle is always whole path
    segments or whole ``-``/``.``-delimited pieces of a name rather than a cut
    through the middle of a word. When the two paths share nothing, the middle
    is simply both of them entire.
    """
    separators = "/-_."
    head = 0
    for i, (a, b) in enumerate(zip(old, new, strict=False)):
        if a != b:
            break
        if a in separators:
            head = i + 1
    else:
        head = min(len(old), len(new))

    tail = 0
    for i, (a, b) in enumerate(
        zip(reversed(old[head:]), reversed(new[head:]), strict=False)
    ):
        if a != b:
            break
        if a in separators:
            tail = i + 1

    stop_old, stop_new = len(old) - tail, len(new) - tail
    return old[:head], old[head:stop_old], new[head:stop_new], old[stop_old:]


def signed(num: int) -> str:
    return f"+{num}" if num > 0 else str(num)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_ts(value: str | None) -> str:
    dt = parse_iso(value)
    if dt is None:
        return "-"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def relative_ts(value: str | None) -> str:
    """"20 minutes ago", "yesterday", "3 weeks ago" — a timestamp read at a glance.

    The dashboard answers "did it catch my last deploy?", and a wall-clock stamp
    makes the reader do the subtraction themselves. Past a couple of months the
    exact day starts mattering more than the distance, so it falls back to the
    date.
    """
    dt = parse_iso(value)
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 60:
        return "just now"                       # also covers a little clock skew
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    return dt.astimezone().strftime("%Y-%m-%d")


def rmtree(path: Path) -> None:
    """Delete a tree, tolerating read-only files (common inside zips)."""
    import shutil
    import stat

    def _onerror(func, target, _exc):  # pragma: no cover - platform specific
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> logging.Logger:
    LOG.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    LOG.handlers.clear()
    LOG.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    return LOG
