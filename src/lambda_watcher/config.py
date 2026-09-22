"""Configuration loading for lambda-watcher.

Config lives in a single YAML file (default ``~/.lambda-watcher/config.yaml``).
Every field has a working default, so the tool runs with no config at all.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml

DEFAULT_HOME = Path(os.environ.get("LAMBDA_WATCHER_HOME", "~/.lambda-watcher")).expanduser()
CONFIG_ENV_VAR = "LAMBDA_WATCHER_CONFIG"


#: Windows profile folders that belong to the system rather than to a person.
#: Anything under ``/mnt/<drive>/Users`` that is not one of these is somebody's
#: account, and so a place a browser might really be writing downloads to.
_WSL_SYSTEM_PROFILES = frozenset({
    "public", "default", "default user", "all users", "wsiaccount",
})

#: Where a Windows drive is mounted inside a WSL guest.
_WSL_MOUNT_ROOT = Path("/mnt")

#: The kernel release string, which is how a WSL guest gives itself away.
_WSL_OSRELEASE = Path("/proc/sys/kernel/osrelease")


def on_wsl() -> bool:
    """Whether this Linux is really a WSL guest sitting underneath Windows.

    Two signals, because either can be missing on its own: ``WSL_DISTRO_NAME`` is
    unset for a process a Windows service started rather than the user's shell, and
    the kernel release — ``5.15.167.4-microsoft-standard-WSL2`` — is the one marker
    every WSL kernel carries however it was launched.
    """
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = _WSL_OSRELEASE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "microsoft" in release.lower()


def windows_downloads_on_wsl() -> list[Path]:
    """The Windows-side Downloads folder this WSL guest can see, when there is one.

    On WSL the browser is a Windows program, so the folder that fills up is
    ``/mnt/c/Users/Someone/Downloads`` while the Linux ``~/Downloads`` frequently
    does not exist at all. Getting this wrong is not a cosmetic miss — it is the
    difference between a watcher that archives and one that calls itself healthy
    for weeks having seen nothing.

    Deliberately cheap: a glob and a few stats, no subprocess, because this runs
    on every ``Config()`` construction and so on every command the user types.
    That cheapness is also its limit — it cannot tell two real people apart, so a
    machine with more than one populated profile gets nothing back rather than a
    coin toss between them. :func:`windows_home_on_wsl` is the slower call that
    asks Windows itself, and ``lw setup`` is where it is worth paying for.
    """
    found: list[Path] = []
    try:
        user_roots = sorted(_WSL_MOUNT_ROOT.glob("*/Users"))
    except OSError:
        return found
    for users in user_roots:
        try:
            profiles = sorted(users.iterdir())
        except OSError:
            continue
        for profile in profiles:
            if profile.name.lower() in _WSL_SYSTEM_PROFILES:
                continue
            downloads = profile / "Downloads"
            try:
                if downloads.is_dir():
                    found.append(downloads)
            except OSError:
                continue
    return found if len(found) == 1 else []


def windows_home_on_wsl() -> Path | None:
    """Ask Windows itself which profile is the one logged in, e.g. ``/mnt/c/Users/Sam``.

    ``windows_downloads_on_wsl`` gives up when a machine has several real accounts
    on it, and this is how that is settled: ``%USERPROFILE%`` is the answer Windows
    would give its own programs. It costs a subprocess and roughly a tenth of a
    second, so it belongs in ``lw setup``, ``lw init`` and ``lw doctor`` — commands
    that write or diagnose a config — never on the path every command pays for.

    Returns ``None`` rather than raising for every way this can fail: no interop,
    no ``wslpath``, a slow or wedged ``cmd.exe``. A guess that cannot be made is
    not an error; it just leaves the caller with the cheap answer.
    """
    if not on_wsl():
        return None
    # cmd.exe cannot sit in a Linux directory: from one it prints a UNC warning and
    # relocates itself, so the profile path arrives buried in chatter. Start it
    # somewhere it can stand, and read back only the line shaped like a Windows path.
    cwd = _WSL_MOUNT_ROOT if _WSL_MOUNT_ROOT.is_dir() else Path("/")
    try:
        completed = subprocess.run(
            ["cmd.exe", "/C", "echo %USERPROFILE%"],
            capture_output=True, text=True, timeout=10, cwd=str(cwd),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    windows_path = ""
    for line in completed.stdout.splitlines():
        candidate = line.strip().strip('"')
        if re.match(r"^[A-Za-z]:\\", candidate):
            windows_path = candidate
            break
    if not windows_path:
        return None
    try:
        translated = subprocess.run(
            ["wslpath", "-u", windows_path],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    home = translated.stdout.strip()
    return Path(home) if home else None


def needs_polling(directory: Path) -> bool:
    """Whether this folder only ever reports changes if we go and look.

    A WSL guest receives no inotify events at all for the Windows drives under
    ``/mnt``: the kernel that owns those files is not the one running inotify. A
    watcher there starts cleanly, says it is watching, and never fires once.

    Deciding it per directory at start-up, rather than leaving it to
    ``watch.force_polling``, is what carries the fix to someone whose config was
    written before this existed — they get a watcher that works without being
    asked to edit anything. Drop this and those archives silently stop growing.
    """
    if not on_wsl():
        return False
    try:
        resolved = directory.resolve()
    except OSError:
        resolved = directory
    return resolved == _WSL_MOUNT_ROOT or _WSL_MOUNT_ROOT in resolved.parents


def default_download_dirs() -> list[str]:
    """Best guess at the user's downloads folder for the current platform."""
    candidates: list[Path] = []
    if sys.platform.startswith("linux"):
        # Respect XDG user dirs when present (handles localised folder names).
        xdg = Path("~/.config/user-dirs.dirs").expanduser()
        if xdg.exists():
            try:
                text = xdg.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'^XDG_DOWNLOAD_DIR="(.+)"', text, re.MULTILINE)
                if m:
                    raw = m.group(1).replace("$HOME", str(Path.home()))
                    candidates.append(Path(raw))
            except OSError:
                pass
        if on_wsl():
            candidates.extend(windows_downloads_on_wsl())
    candidates.append(Path("~/Downloads").expanduser())
    seen: list[str] = []
    for c in candidates:
        s = str(c)
        if s not in seen:
            seen.append(s)
    return seen


@dataclass
class WatchConfig:
    """How the filesystem watcher behaves."""

    dirs: list[str] = field(default_factory=default_download_dirs)
    #: Only files with these suffixes are considered.
    extensions: list[str] = field(default_factory=lambda: [".zip"])
    #: Suffixes browsers use for in-flight downloads; never ingested directly.
    partial_suffixes: list[str] = field(
        default_factory=lambda: [".crdownload", ".part", ".download", ".tmp", ".partial", ".opdownload"]
    )
    #: A file must keep the same size/mtime for this long before it is ingested.
    stable_seconds: float = 2.0
    #: Give up waiting for a file to settle after this long.
    max_wait_seconds: float = 900.0
    #: Watch sub-directories of the watched folders too.
    recursive: bool = False
    #: Use polling instead of native OS events (needed on network/WSL mounts).
    force_polling: bool = False
    polling_interval: float = 2.0
    #: How recently a file must have been written to still count as an arrival.
    #: Windows raises "modified" events for antivirus scans, search indexing and
    #: cloud-sync attribute changes as well as for real writes, so those events
    #: are ignored for anything older than this - and a file this old is never
    #: deleted from the watched folder by ``store.on_ingest: move``.
    #: 0 disables the check.
    arrival_max_age_seconds: float = 300.0
    #: Ingest matching files already present when the watcher starts.
    scan_on_start: bool = True
    #: Ignore files older than this at startup scan (0 disables the cutoff).
    scan_on_start_max_age_hours: float = 24.0


@dataclass
class StoreConfig:
    """Where archived versions live and what gets kept."""

    root: str = str(DEFAULT_HOME)
    #: ``copy`` keeps the download in place, ``move`` clears it out of Downloads,
    #: ``leave`` archives nothing but the extracted tree.
    on_ingest: str = "copy"
    #: Keep the original .zip alongside the extracted tree.
    keep_zip: bool = True
    #: Lift a lone wrapping directory's contents to the root of the version.
    #: Source archives (GitHub, npm, `git archive`) name that directory after
    #: the ref, so leaving it in place makes every download look like a total
    #: rewrite. Turn off to archive trees exactly as the zip laid them out.
    strip_wrapper_dir: bool = True
    #: Refuse archives whose uncompressed size exceeds this (zip-bomb guard).
    max_uncompressed_mb: int = 2048
    max_files: int = 200_000
    #: Delete versions beyond this count per function (0 = keep everything).
    max_versions_per_function: int = 0


@dataclass
class NamingConfig:
    """Turning a downloaded filename into a Lambda function name."""

    #: Explicit rules, applied first. ``pattern`` is a regex matched against the
    #: filename; ``name`` may reference groups, e.g. ``\\1``.
    rules: list[dict[str, str]] = field(default_factory=list)
    #: Regexes stripped from the stem, in order, before falling back to it.
    #: Order matters: the most specific suffixes are removed first. A trailing
    #: "-2"/"-v2" is deliberately NOT stripped, because it is usually part of a
    #: real function name; `lambda-watcher rename --merge` fixes the rare miss.
    strip_patterns: list[str] = field(
        default_factory=lambda: [
            r"\s*\(\d+\)$",                                    # Chrome/Firefox "name (1).zip"
            r"[-_][0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",  # uuid
            r"[-_]\d{4}-\d{2}-\d{2}([-_T]\d{2}[-_:]?\d{2}([-_:]?\d{2})?)?$",  # 2026-01-15 stamps
            r"[-_]\d{8}([-_T]\d{4,6})?$",                       # 20260115-1030 stamps
            r"[-_][0-9a-fA-F]{16,64}$",                          # hex blobs / code sha
            r"[-_]\d{10,13}$",                                   # epoch stamps
            r"[-_](copy|final|backup|bak|old|new|latest)$",
            # A source archive is named after the ref it was cut from, so one
            # repo downloaded at three refs is still one project. These sit
            # last: anything date- or epoch-shaped is claimed above first.
            r"[-_](main|master|develop|trunk|HEAD)$",
            r"[-_]v?\d+\.\d+(\.\d+)?([-.][A-Za-z0-9.]+)?$",   # 1.2.3, v1.2.3, 2.0.0-rc1
            # Short commit sha. The lookahead demands at least one digit, so
            # English words that happen to be all-hex ("defaced", "effaced")
            # keep their place on the end of a name.
            r"[-_](?=[0-9a-f]*\d)[0-9a-f]{7,12}$",
        ]
    )
    #: Case-insensitive matching when resolving an existing function.
    case_insensitive: bool = True
    #: If the stem yields nothing useful, look inside the zip for a single top
    #: level directory and use that as the name.
    infer_from_zip: bool = True


@dataclass
class AnalysisConfig:
    """What the analysers look at."""

    scan_secrets: bool = True
    scan_env_vars: bool = True
    scan_aws_services: bool = True
    #: Files larger than this are hashed but not read for text analysis.
    max_scan_file_kb: int = 2048
    #: Path prefixes/globs treated as vendored dependencies rather than your code.
    vendor_globs: list[str] = field(
        default_factory=lambda: [
            "node_modules/**",
            "**/node_modules/**",
            "**/site-packages/**",
            "**/dist-info/**",
            "**/*.dist-info/**",
            "**/*.egg-info/**",
            "vendor/**",
            "**/__pycache__/**",
            ".venv/**",
            "venv/**",
        ]
    )


@dataclass
class DiffConfig:
    """Defaults for the diff commands."""

    #: Skip vendored files in diffs unless asked for.
    ignore_vendor: bool = True
    #: Unified-diff context lines.
    context_lines: int = 3
    #: Files bigger than this are reported as changed without a line diff.
    max_diff_file_kb: int = 512
    #: Diffs longer than this many lines are truncated in reports.
    max_diff_lines: int = 2000
    #: Paths never diffed (still tracked for add/remove). The lock files are
    #: here because the dependency layer already tells their story as version
    #: numbers, and their own diff is thousands of lines of resolved hashes.
    ignore_globs: list[str] = field(
        default_factory=lambda: [
            "**/*.pyc", "**/*.so", "**/*.map",
            "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
            "**/poetry.lock", "**/Pipfile.lock", "**/composer.lock",
            "**/Cargo.lock", "**/go.sum",
        ]
    )
    #: Report a file whose only change is whitespace as ``whitespace only``
    #: instead of printing the hunk. A reindent renders as every touched line
    #: removed and re-added, which is a rewrite of the file to say nothing
    #: happened to it; ``lw diff --whitespace`` prints it anyway for the one
    #: time you need to check.
    collapse_whitespace_only: bool = True
    #: Above this average line length a file is diffed word by word instead of
    #: line by line. A minified bundle is one 8,000-character line, so a line
    #: diff quotes the whole file to show a changed digit. Mean, not maximum: a
    #: single long data literal in an ordinary module still diffs by line. Set
    #: it very high to always use the line diff.
    long_line_mean_chars: int = 400
    #: How many file pairs rename detection may score before it stops and says
    #: so. Comparing files that moved *and* changed is quadratic in the number
    #: of candidates, so a restructure of a few thousand files needs a ceiling
    #: somewhere; raise this to trade seconds for a complete rename map.
    max_rename_pairs: int = 120_000
    #: How many files have to move between the same two directories before the
    #: diff reports the move once instead of once per file. One directory
    #: rename otherwise arrives as N rows that differ only in the filename at
    #: the end. Set it very high to always list every file separately.
    min_moved_files: int = 3


@dataclass
class GitMirrorConfig:
    """Optional per-function git repository, one commit per version."""

    enabled: bool = True
    author_name: str = "lambda-watcher"
    author_email: str = "lambda-watcher@localhost"
    #: Include vendored files in the mirror (off keeps repos small and readable).
    include_vendor: bool = True
    tag_prefix: str = "v"


@dataclass
class NotifyConfig:
    """Desktop notifications when a new version is archived."""

    enabled: bool = True
    #: Only notify when the new version differs from the previous one.
    only_on_change: bool = True
    #: Say what changed, not just that something did. A notification reading
    #: "2 modified, +24/-5 lines, 1 env var" is worth glancing at; one reading
    #: "34 files, 8.9 KB" is bookkeeping.
    summarise_changes: bool = True


@dataclass
class ReportConfig:
    """HTML written without anyone asking for it.

    Once the watcher runs in the background, nobody is looking at a terminal at
    the moment a version lands. Rendering the comparison right then means the
    notification can point at a page that already exists, and the answer to
    "what changed?" is a bookmark rather than a command.
    """

    #: Render the diff against the previous version as each one is archived.
    auto_diff: bool = True
    #: Include vendored dependency files in those automatic diffs.
    include_vendor: bool = False


@dataclass
class Config:
    """The whole configuration: one nested dataclass per area, all defaulted.

    Constructing ``Config()`` with no arguments produces a working setup, which
    is what lets every command run before anybody has written a config file. The
    YAML file is an overlay on top of this, not a requirement — see
    :func:`load_config`.
    """

    watch: WatchConfig = field(default_factory=WatchConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)
    git_mirror: GitMirrorConfig = field(default_factory=GitMirrorConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    #: Command `open` launches on a folder. Empty means "find one on PATH".
    editor: str = ""
    log_level: str = "INFO"

    # -- derived paths ---------------------------------------------------
    @property
    def root(self) -> Path:
        """The archive root, with ``~`` expanded. Everything else hangs off this."""
        return Path(self.store.root).expanduser()

    @property
    def db_path(self) -> Path:
        """``<root>/index.db`` — the rebuildable SQLite index."""
        return self.root / "index.db"

    @property
    def functions_dir(self) -> Path:
        """``<root>/functions/`` — one directory per archived function."""
        return self.root / "functions"

    @property
    def log_dir(self) -> Path:
        """``<root>/logs/`` — where the background service writes."""
        return self.root / "logs"

    @property
    def reports_dir(self) -> Path:
        """``<root>/reports/`` — generated HTML, one directory per function."""
        return self.root / "reports"

    @property
    def repos_dir(self) -> Path:
        """``<root>/repos/`` — the per-function git mirrors."""
        return self.root / "repos"

    @property
    def quarantine_dir(self) -> Path:
        """``<root>/quarantine/`` — archives that could not be extracted, plus a reason file."""
        return self.root / "quarantine"

    @property
    def heartbeat_path(self) -> Path:
        """``<root>/state/watcher.json`` — what the running watcher says about itself.

        Kept out of ``index.db`` deliberately: it is the one piece of state that has
        to be readable when the database is locked, mid-rebuild or absent, because
        the question it answers — is the watcher alive — is most often asked when
        something else has already gone wrong.
        """
        return self.root / "state" / "watcher.json"

    def ensure_dirs(self) -> None:
        """Create the archive directories that must exist, ignoring ones that already do.

        Called on every :class:`~lambda_watcher.store.Store` construction, so no
        command depends on a setup step having been run first. ``repos/`` and
        ``quarantine/`` are not in the list: each is created by the code that
        first writes to it, so neither appears until it holds something.
        """
        for path in (self.root, self.functions_dir, self.log_dir, self.reports_dir):
            path.mkdir(parents=True, exist_ok=True)

    def watch_dirs(self) -> list[Path]:
        """The configured watch directories as paths, with ``~`` expanded."""
        return [Path(d).expanduser() for d in self.watch.dirs]


def _from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Build a (possibly nested) dataclass from a plain dict, ignoring unknowns.

    The field types come from :func:`typing.get_type_hints`, not from
    ``Field.type``. This module has ``from __future__ import annotations``, which
    makes that attribute the *source text* of the annotation — the string
    ``"WatchConfig"``, never the class — so ``is_dataclass(f.type)`` is False for
    every field and a nested section would be handed to the constructor as a raw
    dict. No config section nests today, which is precisely what makes it worth
    fixing here rather than the first time one does: the symptom is a config
    field that silently holds a dict, not an error.
    """
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key not in known:
            continue
        declared = hints.get(key)
        if isinstance(value, dict) and isinstance(declared, type) and is_dataclass(declared):
            kwargs[key] = _from_dict(declared, value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def default_config_path() -> Path:
    """Where the config file lives: ``$LAMBDA_WATCHER_CONFIG`` or ``<home>/config.yaml``.

    The environment variable is what lets a test or a one-off command point at a
    scratch config without disturbing the real one.
    """
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_HOME / "config.yaml"


#: Each top-level section of the config file and the dataclass it fills. One map
#: rather than a list repeated per call site, so :func:`load_config` and
#: :func:`unknown_keys` cannot disagree about what a valid section is.
CONFIG_SECTIONS: dict[str, type] = {
    "watch": WatchConfig,
    "store": StoreConfig,
    "naming": NamingConfig,
    "analysis": AnalysisConfig,
    "diff": DiffConfig,
    "git_mirror": GitMirrorConfig,
    "notify": NotifyConfig,
    "report": ReportConfig,
}

#: Settings that sit at the top level rather than inside a section.
CONFIG_SCALARS = frozenset({"editor", "log_level"})


def unknown_keys(path: Path | str | None = None) -> list[str]:
    """Settings in the config file that nothing reads, e.g. ``watch.dir`` for ``dirs``.

    :func:`_from_dict` ignores what it does not recognise, which is what lets a
    config written by an older release still load. The cost is silence in the one
    case that matters most: write ``dir:`` for ``dirs:`` and the tool watches its
    default folder while you believe it is watching yours, with no error, ever.

    So the keys stay ignored and become *reportable* instead — ``lw doctor`` names
    them. Returns dotted names, sorted. A file that cannot be read or is not a
    mapping returns nothing; saying what is wrong with it is :func:`load_config`'s
    job and it does it more loudly.
    """
    cfg_path = Path(path).expanduser() if path else default_config_path()
    try:
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(loaded, dict):
        return []

    unknown: list[str] = []
    for key, value in loaded.items():
        if key in CONFIG_SCALARS:
            continue
        section = CONFIG_SECTIONS.get(key)
        if section is None:
            unknown.append(str(key))
            continue
        if not isinstance(value, dict):
            continue
        known = {f.name for f in fields(section)}
        unknown.extend(f"{key}.{name}" for name in value if name not in known)
    return sorted(unknown)


def load_config(path: Path | str | None = None) -> Config:
    """Load config from YAML, falling back to defaults for anything absent."""
    cfg_path = Path(path).expanduser() if path else default_config_path()
    data: dict[str, Any] = {}
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{cfg_path} must contain a YAML mapping")
        data = loaded

    cfg = Config(
        **{name: _from_dict(cls, data.get(name, {})) for name, cls in CONFIG_SECTIONS.items()},
        editor=data.get("editor", ""),
        log_level=data.get("log_level", "INFO"),
    )
    # Environment overrides make it easy to run one-off commands elsewhere.
    if os.environ.get("LAMBDA_WATCHER_HOME"):
        cfg.store.root = str(Path(os.environ["LAMBDA_WATCHER_HOME"]).expanduser())
    if os.environ.get("LAMBDA_WATCHER_LOG_LEVEL"):
        cfg.log_level = os.environ["LAMBDA_WATCHER_LOG_LEVEL"]
    if os.environ.get("LAMBDA_WATCHER_EDITOR"):
        cfg.editor = os.environ["LAMBDA_WATCHER_EDITOR"]
    return cfg
