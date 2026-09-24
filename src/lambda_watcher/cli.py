"""Command line interface."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import sys
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, Optional

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from . import heartbeat
from .ai.explanation import Explanation, clear_pending, load_record, save_done, save_failed, save_pending
from .ai.prompt import SYSTEM_PROMPT, build_prompt
from .ai.providers import AIError, list_models, normalize_local_url, parse_azure_endpoint, ping
from .ai.report import explain_command, headline_for, panel_for, shell_word
from .ai.run import Explainer, ExplainJob, explain_diff, rewrite_pages
from .ai.settings import PROVIDERS, AISettings, ModelEntry, SettingsError, provider_key
from .helptext import FUNCTION_HELP, LATEST_VERSION_HELP, VERSION_HELP, for_command, for_group
from .config import (
    Config, default_config_path, default_download_dirs, load_config, on_wsl, unknown_keys,
    windows_home_on_wsl,
)
from .db import Database
from .diffing import code_dir, diff_from_index, write_archive_index
from .diffing.render_html import render_timeline, write_html
from .diffing.render_text import render as render_diff
from .diffing.render_text import render_explanation
from .gitmirror import diff as mirror_diff
from .gitmirror import git_available, has_tag
from .gitmirror import passthrough as git_passthrough
from .ingest import IngestResult, Ingestor
from .service import (
    ServiceError,
    ServiceStatus,
    current_status,
    install_service,
    stop_service,
)
from .store import Store, posix_stored_dir
from .utils import format_ts, human_size, relative_ts, rmtree, setup_logging, slugify

#: Index connections opened by the running command; see `_close_open_dbs`.
_OPEN_DBS: list[Database] = []


def _close_open_dbs(*_args: object, **_kwargs: object) -> None:
    """Close every index connection opened during this command.

    Registered as Typer's result callback. Normally the process exits straight
    after a command and nothing needs closing, but Windows keeps an open SQLite
    handle from being deleted, so ``reindex`` — which replaces ``index.db`` —
    fails whenever another command ran first in the same process. Closing here
    makes that deterministic rather than dependent on the garbage collector.
    """
    while _OPEN_DBS:
        _OPEN_DBS.pop().close()


app = typer.Typer(
    add_completion=True,
    # Bare `lambda-watcher` answers "is it on, and what has it seen?" rather
    # than printing a wall of twenty commands. Someone who has just installed
    # the tool learns more from their own status than from the command list,
    # and `--help` is still one flag away.
    no_args_is_help=False,
    help="Watch your Downloads folder for Lambda deployment zips, archive every "
    "version, and diff any two of them.",
    epilog="Every command explains itself with examples: lw diff --help, lw setup --help, …",
    # Pinned rather than left to Typer's default, which has changed between
    # releases: the help in helptext.py is written as Rich markup, and its
    # Examples panel needs the Rich renderer printing as it goes.
    rich_markup_mode="rich",
    result_callback=_close_open_dbs,
)
console = Console()
err_console = Console(stderr=True)

_CONFIG_PATH: Path | None = None


# ---------------------------------------------------------------- helpers
def _cfg() -> Config:
    """Load the config, or explain why it could not be loaded.

    Bare `lw` reads the config now, so a stray tab in the YAML would otherwise
    greet the reader with a traceback on the command they type most. The file is
    hand-edited and the fix is always in it, so name it.
    """
    try:
        cfg = load_config(_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        path = _CONFIG_PATH or default_config_path()
        _fail(f"could not read {path}:\n  {exc}\n  Fix it, or delete it to fall back to defaults.")
    cfg.ensure_dirs()
    setup_logging(cfg.log_level, cfg.log_dir / "watcher.log")
    return cfg


def _open_db(cfg: Config) -> Database:
    """Open the index and register it to be closed when the command finishes.

    Use this rather than constructing :class:`Database` directly; see
    :func:`_close_open_dbs`.
    """
    db = Database(cfg.db_path)
    _OPEN_DBS.append(db)
    return db


def _fail(message: str, code: int = 1) -> NoReturn:
    """Print an error and exit with ``code``.

    Every message passed here should end with something the reader can type. A
    message that only reports a state leaves them where they started.
    """
    err_console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code)


def _complete_function(incomplete: str) -> list[str]:
    """Tab-completion for a function argument.

    Runs inside the user's shell on every TAB, so it opens its own short-lived
    connection, touches nothing, and swallows everything: a completion that
    raises prints a traceback into the middle of the command line being typed.
    """
    try:
        cfg = load_config(_CONFIG_PATH)
        if not cfg.db_path.exists():
            return []
        with Database(cfg.db_path) as db:
            return [
                row["name"] for row in db.list_functions()
                if row["name"].lower().startswith(incomplete.lower())
            ]
    except Exception:                                 # noqa: BLE001 - never break a prompt
        return []


def _resolve_function(db: Database, ident: str):
    """Look up a function by name, slug or prefix, or exit naming the ones that exist.

    The failure is the useful part: a mistyped name lists the known functions,
    and an empty archive says so plainly instead of pretending the name was
    wrong.
    """
    row = db.get_function(ident)
    if row is None:
        names = [r["name"] for r in db.list_functions()]
        hint = f" Known functions: {', '.join(names)}" if names else " Nothing has been archived yet."
        _fail(f"no function matching {ident!r}.{hint}")
    return row


def _resolve_seq(db: Database, function_id: int, spec: str | int | None, default_offset: int = 0) -> int:
    """Turn 'latest', '-2', '7' into a concrete version number."""
    versions = db.list_versions(function_id)  # newest first
    if not versions:
        _fail("this function has no archived versions")
    seqs = [int(v["seq"]) for v in versions]

    if spec is None:
        index = min(default_offset, len(seqs) - 1)
        return seqs[index]

    text = str(spec).strip().lower()
    if text in {"latest", "last", "head"}:
        return seqs[0]
    if text in {"first", "oldest"}:
        return seqs[-1]
    if text.startswith("v"):
        text = text[1:]
    try:
        value = int(text)
    except ValueError:
        _fail(f"cannot understand version {spec!r}; use a number, 'latest', 'first' or -1")
    if value < 0:  # -1 = latest, -2 = the one before it
        index = -value - 1
        if index >= len(seqs):
            _fail(f"only {len(seqs)} version(s) archived")
        return seqs[index]
    if value not in seqs:
        _fail(f"version {value} not found (have: {', '.join(str(s) for s in seqs)})")
    return value


def _resolve_pair(db: Database, function_id: int, from_: str | None, to: str | None) -> tuple[int, int]:
    """The two versions ``--from`` and ``--to`` name, oldest first: by default the last two.

    Shared by ``diff`` and ``explain``, so the two commands can never disagree
    about which comparison ``lw diff orders --to 7`` and ``lw explain orders
    --to 7`` mean. Given the wrong way round, the pair is swapped rather than
    refused — which version is older is not in doubt.
    """
    b_seq = _resolve_seq(db, function_id, to, default_offset=0)
    if from_ is None:
        available = [int(v["seq"]) for v in db.list_versions(function_id) if int(v["seq"]) < b_seq]
        if not available:
            _fail(f"v{b_seq:04d} is the oldest archived version; nothing to compare it against")
        a_seq = max(available)
    else:
        a_seq = _resolve_seq(db, function_id, from_, default_offset=1)

    if a_seq == b_seq:
        _fail("--from and --to are the same version")
    return (b_seq, a_seq) if a_seq > b_seq else (a_seq, b_seq)


def _version_or_fail(db: Database, function_id: int, seq: int):
    """Fetch one version by sequence number, or exit saying it does not exist."""
    row = db.get_version(function_id, seq)
    if row is None:
        _fail(f"version {seq} not found")
    return row


def _stored_dirname(stored_dir: str) -> str:
    """The last segment of a version's stored path: ``0007-a1b2c3d4``.

    Goes through :func:`~lambda_watcher.store.posix_stored_dir` first, so it
    reads the same answer out of a value an older release wrote on Windows —
    where ``Path(...).name`` on Linux returns the whole backslash-joined string
    instead of the directory name, and the caller then builds a path to nothing.
    """
    # back-compat: `posix_stored_dir` is the whole point of this helper existing
    # rather than callers reaching for `Path(...).name` themselves.
    return PurePosixPath(posix_stored_dir(stored_dir)).name


def _build_diff(db: Database, store: Store, cfg: Config, function_row, a_seq: int, b_seq: int,
                include_vendor: bool | None, compute_diffs: bool = True):
    """Resolve two versions and build the diff between them.

    Warns, rather than fails, when a version's extracted code is missing from
    disk: the index still knows what changed at the file level, so the summary
    and dependency layers are worth printing even though the line diffs come out
    empty. Deleting an archive directory by hand is one cause; an index pointing
    somewhere the directory no longer is, after the archive was moved or copied
    from another machine, is the other — which is why the warning names
    ``lw reindex``, the command that rebuilds the index from what is on disk.
    """
    a = _version_or_fail(db, function_row["id"], a_seq)
    b = _version_or_fail(db, function_row["id"], b_seq)
    for row, seq in ((a, a_seq), (b, b_seq)):
        path = code_dir(store, row)
        if not path.exists():
            err_console.print(
                f"[yellow]warning:[/yellow] code for v{seq:04d} is missing at {path}; "
                "line diffs for it will be empty. If the archive was moved or copied "
                "here, `lw reindex` re-points the index at what is on disk."
            )
    return diff_from_index(db, store, cfg.diff, function_row["name"], a, b,
                           include_vendor=include_vendor, compute_diffs=compute_diffs)


def _open_path(path: Path) -> None:
    """Open a file or folder in whatever the desktop uses for it.

    ``open`` on macOS, ``startfile`` on Windows, ``xdg-open`` elsewhere. A
    failure is a warning, not an error — the path has already been printed, and
    the reader can open it themselves.
    """
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        err_console.print(f"[yellow]could not open {path}: {exc}[/yellow]")


def _desktop_in_front() -> bool:
    """Whether a browser opened now would appear in front of whoever typed the command.

    This is what lets ``lw report`` open its page unasked — the page is the
    whole point of asking, and a path to paste into a browser is one more step
    between the reader and it — without that ever being the wrong call. Output
    that is not a terminal means a script, a pipe or
    a test runner, and nobody is looking at a screen. An SSH session means the
    desktop, if there is one, belongs to a different machine. A Linux box with
    no ``DISPLAY``, no ``WAYLAND_DISPLAY`` and no ``$BROWSER`` would get
    :mod:`webbrowser`'s fallback of ``lynx`` taking over the terminal, which is
    worse than just printing the path. WSL counts as a desktop without either
    variable, because :func:`_open_in_browser` hands the page to Windows.
    ``--open`` skips this check and ``--no-open`` never gets here.
    """
    if not sys.stdout.isatty():
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform != "linux" or on_wsl():
        return True
    return any(os.environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"))


def _open_in_browser(page: Path) -> bool:
    r"""Show an HTML file in the reader's browser, and say whether anything took it.

    On WSL the browser is a Windows program, and ``file:///home/me/r.html``
    sends it looking for ``C:\home\me\r.html``. So there the page goes to Windows
    under the name Windows uses for it — ``\\wsl.localhost\Ubuntu\home\me\r.html``
    — through ``explorer.exe``, which opens it in the default browser.
    Everywhere else :mod:`webbrowser` already knows how. ``explorer.exe`` exits 1
    even when it worked, so on WSL a launch that did not raise counts as success.
    """
    if on_wsl() and shutil.which("wslpath") and shutil.which("explorer.exe"):
        try:
            windows_path = subprocess.run(
                ["wslpath", "-w", str(page.resolve())],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            subprocess.run(["explorer.exe", windows_path], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.CalledProcessError):
            pass  # fall through to whatever webbrowser can find
    return webbrowser.open(page.resolve().as_uri())


#: Editors that take a folder as their argument, in the order they are tried.
#: Everything here is VS Code or a fork of it except the last two, so `--reuse`
#: (VS Code's `-r`) applies to all but those.
_EDITORS = ("code", "cursor", "windsurf", "code-insiders", "codium", "vscodium", "zed", "subl")
_REUSE_SUPPORTED = {"code", "cursor", "windsurf", "code-insiders", "codium", "vscodium"}


def _resolve_editor(cfg: Config, override: str | None) -> list[str]:
    """The command to launch on a folder, as argv.

    An explicit choice — the flag, then ``editor`` in the config (which
    ``LAMBDA_WATCHER_EDITOR`` overrides) — is used as given and is an error when
    it is not installed, because silently opening a different editor than the
    one you asked for is worse than the error. With no choice made, the first
    of ``_EDITORS`` on PATH wins.
    """
    chosen = (override or cfg.editor).strip()
    if chosen:
        # A command that resolves as it stands is taken as it stands: on Windows
        # the natural thing to write is a full path, and shlex would turn
        # `C:\Program Files\...\code.cmd` into four broken tokens. Splitting is
        # only for a command that carries arguments.
        if shutil.which(chosen):
            return [chosen]
        argv = shlex.split(chosen)
        if not argv:
            _fail("the editor command is empty")
        if not shutil.which(argv[0]):
            _fail(f"{argv[0]!r} is not on PATH")
        return argv
    for candidate in _EDITORS:
        found = shutil.which(candidate)
        if found:
            return [found]
    _fail(
        "no editor found on PATH (looked for " + ", ".join(_EDITORS) + "). "
        "Pass --editor CMD, set `editor:` in the config, or use `lw path` "
        "and open the folder yourself."
    )
    return []  # unreachable; _fail exits


def _launch_editor(argv: list[str], target: Path, reuse: bool) -> None:
    """Run the editor on ``target``, waiting for it to exit.

    ``--reuse`` becomes ``-r`` for the VS Code family and is refused out loud for
    the editors that have no equivalent, rather than being dropped silently or
    passed through as an argument they would misread.
    """
    name = Path(argv[0]).stem
    if reuse:
        if name in _REUSE_SUPPORTED:
            argv = [*argv, "-r"]
        else:
            err_console.print(f"[yellow]--reuse means nothing to {name}; ignoring it[/yellow]")
    try:
        proc = subprocess.run([*argv, str(target)], check=False)
    except OSError as exc:
        _fail(f"could not launch {name}: {exc}")
        return
    if proc.returncode != 0:
        _fail(f"{name} exited with status {proc.returncode}", proc.returncode)


def _version_callback(value: bool) -> None:
    """Print the version and exit, for ``--version``.

    Eager, so it answers before any other option is processed.
    """
    if value:
        console.print(f"lambda-watcher {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.yaml (default: ~/.lambda-watcher/config.yaml)."
    ),
    version: bool = typer.Option(
        False, "--version", help="Print the version and exit.",
        callback=_version_callback, is_eager=True,
    ),
) -> None:
    """Root callback: remember ``--config`` and, with no subcommand, show the status.

    Bare ``lw`` printing a dashboard rather than ``--help`` is deliberate.
    Someone who has just installed the tool learns more from *is it running and
    what has it caught* than from a list of twenty commands, and ``--help`` is
    still one flag away.
    """
    global _CONFIG_PATH
    _CONFIG_PATH = config
    if ctx.invoked_subcommand is None:
        _print_status()


# --------------------------------------------------------------- dashboard
def _home_relative(path: Path) -> str:
    """``~/Downloads`` rather than ``/Users/someone/Downloads``, where it applies.

    The separator is the platform's, not a hardcoded slash: pasting ``~/`` onto
    a Windows relative path produced ``~/.lambda-watcher\\config.yaml``, which
    is a path from neither operating system.
    """
    try:
        return f"~{os.sep}{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _typeable(path: Path) -> str:
    """A path the way it can be pasted into a shell: ``~/Downloads``, or quoted if it must be.

    ``~/...`` is kept where it is safe, because it is shorter and it is what the
    reader recognises. A path with a space in it — every Windows profile named
    after a person, ``/mnt/c/Users/Sam Jones/Downloads`` — is written out in full
    inside double quotes instead: a quoted ``~`` is not expanded by any shell, and
    double quotes are the one form bash, zsh and PowerShell all accept.
    """
    shown = _home_relative(path)
    if re.fullmatch(r"[\w@%+=:,./~\\-]+", shown):
        return shown
    return f'"{path}"'


def _print_status() -> None:
    """What bare `lw` prints: is it on, what has it seen, what to do next.

    This is the first thing most people will ever see from the tool, so it
    answers the two questions a newcomer actually has — *is it running* and
    *did it catch anything* — and then names the one or two commands worth
    typing next. Nothing here fails: an archive that does not exist yet is a
    normal state to be in, not an error.
    """
    cfg = _cfg()
    state = current_status(cfg, _CONFIG_PATH)
    beat = heartbeat.read(cfg.heartbeat_path)
    watched = ", ".join(_home_relative(d) for d in cfg.watch_dirs())
    absent = [d for d in cfg.watch_dirs() if not d.exists()]
    # The green-dot line names only what can actually be watched; the folders
    # that are missing get their own red line below rather than a cheerful mention.
    present = ", ".join(_home_relative(d) for d in cfg.watch_dirs() if d.exists()) or "nothing"

    # A watcher started by hand with `lw watch` is invisible to the service
    # manager, so without the heartbeat it read as "not watching" from a second
    # terminal while it was busy archiving in the first.
    by_hand = (not state.running and beat is not None
               and beat.is_running() and not beat.is_stale())

    console.print(f"[bold]lambda-watcher[/bold] {__version__}")
    if state.running:
        console.print(f"\n  [green]●[/green] watching {present}   [dim]{state.manager}[/dim]")
    elif by_hand:
        console.print(f"\n  [green]●[/green] watching {present}   "
                      f"[dim]in a terminal · stops when it closes[/dim]")
    elif state.installed:
        console.print(f"\n  [yellow]●[/yellow] installed but not running   [dim]{state.manager}[/dim]")
    else:
        console.print(f"\n  [dim]○[/dim] not watching   [dim]{watched} is not being archived[/dim]")

    # A green dot over a folder that is not there is the failure this tool has to
    # be loudest about: everything downstream looks healthy while nothing arrives.
    for directory in absent:
        console.print(f"    [red]![/red] [red]{_home_relative(directory)} does not exist[/red]")
    if state.running and beat is not None and beat.is_stale():
        console.print("    [yellow]![/yellow] [yellow]no sign of life from the watcher "
                      "since " + relative_ts(beat.last_beat_at) + "[/yellow]")
    elif (state.running or by_hand) and beat is not None and beat.observer:
        console.print(f"    [dim]listening by {beat.observer} · {beat.seen} file(s) seen "
                      f"since {relative_ts(beat.started_at)}[/dim]")
    if not state.running and not by_hand and state.detail:
        console.print(f"    [dim]{state.detail}[/dim]")
    # Only a log that exists is worth naming: every manager reports where it
    # *would* write one, including for a service that was never installed.
    if not state.running and not by_hand and state.log_path is not None and state.log_path.exists():
        console.print(f"    [dim]log: {_home_relative(state.log_path)} · lw logs --service[/dim]")

    db = _open_db(cfg)
    functions, versions_count, total_bytes = db.archive_totals()
    if functions:
        console.print(
            f"    [dim]{functions} function{'s' if functions != 1 else ''} · "
            f"{versions_count} version{'s' if versions_count != 1 else ''} · "
            f"{human_size(total_bytes)} in {_home_relative(cfg.root)}[/dim]"
        )
    else:
        console.print(f"    [dim]nothing archived yet · {_home_relative(cfg.root)}[/dim]")
    console.print(_ai_status_line(cfg))

    rows = db.list_functions()[:5]
    if rows:
        table = Table(box=None, show_header=False, padding=(0, 2, 0, 2))
        table.add_column("function")
        table.add_column("latest", justify="right")
        table.add_column("when", style="dim")
        table.add_column("runtime", style="dim")
        for row in rows:
            latest = db.latest_version(int(row["id"]))
            table.add_row(
                row["name"],
                f"v{int(row['latest_seq']):04d}" if row["latest_seq"] else "-",
                relative_ts(row["last_seen"]),
                (latest["runtime"] if latest else "") or "",
            )
        console.print()
        console.print(table)

    if rows:
        front_page = cfg.reports_dir / "index.html"
        newest_report = cfg.reports_dir / slugify(rows[0]["name"]) / "latest.html"
        if front_page.exists():
            console.print(f"\n  [dim]reports: {_home_relative(front_page)} · lw report[/dim]")
        # back-compat: an archive written before reports/ had a front page has
        # only each function's latest.html until its next ingest. Naming the
        # newest of those keeps the dashboard pointing somewhere readable in the
        # meantime; drop it and the line vanishes for an archive nobody has added
        # to since the upgrade, which for an unattended install can be months.
        # It is safe to remove only once no such archive can still be opened.
        elif newest_report.exists():
            console.print(f"\n  [dim]latest report: {_home_relative(newest_report)}[/dim]")

    console.print()
    steps = _next_steps(state, rows[0]["name"] if rows else None, absent)
    width = max((len(command) for command, _ in steps), default=0)
    for command, blurb in steps:
        console.print(f"  [bold]{command:<{width}}[/bold]   [dim]{blurb}[/dim]")


def _ai_status_line(cfg: Config) -> str:
    """The dashboard's one line about AI: which model explains changes, and whether on its own.

    Present in every state, because bare ``lw`` is where people find out what
    the tool can do: "not set up · lw ai add" is how someone learns the
    feature exists, and "off · lw ai on" is how someone who switched it off
    remembers they did. A default model without a key is the one state worth
    colour, since it means explanations are silently not being written.
    """
    settings = AISettings.load(cfg.root)
    if not settings.enabled:
        return "    [dim]✦ AI explanations off · lw ai on[/dim]"
    entry = settings.default_entry()
    if entry is None:
        return "    [dim]✦ AI explanations not set up · lw ai add[/dim]"
    if entry.key_problem():
        return f"    [yellow]! AI model {escape(entry.name)} has no key · lw ai[/yellow]"
    how = "for every new version" if settings.auto_explain else "when you run lw explain"
    return f"    [dim]✦ AI explanations by {escape(entry.name)}, {how}[/dim]"


def _next_steps(
    state: ServiceStatus, newest: str | None, absent: list[Path] | None = None
) -> list[tuple[str, str]]:
    """The two or three commands most worth typing from where the user is now.

    Not watching is always the first thing to fix — an archive that has stopped
    growing is the failure this tool has to be loud about — but someone with
    history already deserves to be told how to read it in the same breath. The
    browser hint is bare ``lw report`` rather than the newest function's history:
    that page links every function, so it does not have to guess which one the
    reader came for.

    A watch folder that does not exist outranks even that: starting a watcher over
    a folder that is not there produces a tidy green dot and no archive, so the
    reader is sent to ``lw doctor``, which says which folder and what to do.
    """
    steps: list[tuple[str, str]] = []
    if absent:
        steps.append(("lw doctor", "a watch folder does not exist — nothing can arrive"))
    if not state.running:
        steps.append(
            ("lw start", "start watching again") if state.installed
            else ("lw setup", "watch your downloads folder from now on")
        )
    if newest is None:
        if state.running:
            steps.append(("lw doctor", "check the watch folder is the right one"))
        steps.append(("lw demo", "see what it does, on a sample Lambda"))
        return steps
    steps.append((f'lw diff "{newest}"', "what changed in the last version"))
    if state.running:
        steps.append(("lw report", "every function on one page, in your browser"))
    return steps


@app.command(rich_help_panel="Everyday", **for_command("status"))
def status() -> None:
    """Is the watcher running, and what has it archived? (Also plain `lw`.)"""
    _print_status()


def _best_watch_dirs() -> list[str]:
    """Where downloads really land, asked properly rather than cheaply.

    :func:`config.default_download_dirs` is the guess every command pays for, so it
    may not spend a subprocess and on WSL gives up rather than choose between two
    real Windows accounts. A command that is about to *write* a config file can
    afford to ask Windows outright, once, and bake the answer in — which is the
    difference between a config pointing at ``~/Downloads``, a folder that usually
    does not exist there, and one pointing at the folder the browser fills.

    Falls back to the cheap answer whenever the accurate one is unavailable or
    already right; a guess that cannot be improved is not an error.
    """
    guessed = default_download_dirs()
    if any(Path(d).expanduser().is_dir() for d in guessed):
        return guessed
    home = windows_home_on_wsl()
    if home is not None:
        downloads = home / "Downloads"
        if downloads.is_dir():
            return [str(downloads)]
    return guessed


# ----------------------------------------------------------- getting started
@app.command(rich_help_panel="Everyday", **for_command("setup"))
def setup(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Answer yes to every question, including importing zips already there."
    ),
    no_service: bool = typer.Option(
        False, "--no-service", help="Set up the archive but do not run in the background."
    ),
) -> None:
    """Set everything up: config, background watcher, and any history already on disk."""
    config_path = _CONFIG_PATH or default_config_path()
    console.print(f"[bold]lambda-watcher[/bold] {__version__} — setting up\n")

    # The config is written before anything reads one, so that every step below
    # — which folders to watch, above all — runs against the file the user will
    # be editing rather than against defaults that happen to match it today.
    if not config_path.exists():
        from .templates import render_config
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(render_config(_best_watch_dirs()), encoding="utf-8")
        console.print(f"  [green]✓[/green] wrote {_home_relative(config_path)}")
    else:
        console.print(f"  [green]✓[/green] using {_home_relative(config_path)}")

    cfg = _cfg()
    console.print(f"  [green]✓[/green] archive at {_home_relative(cfg.root)}")

    missing = [d for d in cfg.watch_dirs() if not d.exists()]
    for directory in cfg.watch_dirs():
        mark = "[green]✓[/green]" if directory.exists() else "[red]✗[/red]"
        console.print(f"  {mark} watching {_home_relative(directory)}")
    if missing and _offer_better_watch_dir(cfg, config_path, yes):
        cfg = _cfg()
        missing = [d for d in cfg.watch_dirs() if not d.exists()]
    if missing:
        console.print(
            f"\n[yellow]note:[/yellow] {len(missing)} watch folder(s) do not exist, "
            f"so nothing will ever be archived from them."
        )
        console.print(
            f"       Set [bold]watch.dirs[/bold] in {_home_relative(config_path)}, "
            f"then run [bold]lw setup[/bold] again."
        )

    _offer_backfill(cfg, yes)

    if no_service:
        console.print("\n[dim]Skipping the background watcher. Run [bold]lw watch[/bold] "
                      "when you want it, or [bold]lw start[/bold] to install it later.[/dim]")
    else:
        console.print()
        _start_service(cfg)

    console.print("\n[dim]That is the whole setup. Download a Lambda zip as you normally would; "
                  "run [bold]lw[/bold] to see what it caught.[/dim]\n")
    # Setup must never end on an empty archive with nothing to look at: that is
    # the "install it and wait for your next deploy" gap `lw demo` exists to close.
    if not _open_db(cfg).archive_totals()[0]:
        console.print("  [bold]lw demo[/bold]                   "
                      "[dim]see what it does now, on a sample Lambda[/dim]")
    console.print("  [bold]lw --install-completion[/bold]   "
                  "[dim]tab-complete function names in your shell[/dim]")


def _offer_better_watch_dir(cfg: Config, config_path: Path, yes: bool) -> bool:
    """Offer to point an existing config at the folder downloads really land in.

    A new config already gets the right folder written into it. This is for the
    config written before that was possible — by an older release on WSL, say,
    naming a ``~/Downloads`` that has never existed there — whose owner would
    otherwise be told to go and edit YAML to fix a guess this tool got wrong.

    Keeps every configured folder that exists and swaps out the ones that do not.
    Asks first, since it edits a file the user may have written by hand; ``--yes``
    takes the offer, and without a terminal to ask in it is left alone. Returns
    whether the file was changed.
    """
    configured = {str(Path(d).expanduser()) for d in cfg.watch.dirs}
    better = next(
        (d for d in _best_watch_dirs()
         if Path(d).expanduser().is_dir() and str(Path(d).expanduser()) not in configured),
        None,
    )
    if better is None:
        return False
    console.print(f"\n  your downloads look like they land in [bold]{better}[/bold].")
    if yes:
        accepted = True
    elif sys.stdin.isatty():
        accepted = typer.confirm("  watch that folder instead?", default=True)
    else:
        return False
    if not accepted:
        return False

    kept = [d for d in cfg.watch.dirs if Path(d).expanduser().exists()]
    if not _rewrite_watch_dirs(config_path, [*kept, better]):
        console.print(f"  [yellow]could not edit {_home_relative(config_path)} safely[/yellow] — "
                      f"it is not laid out the way `lw init` writes it.")
        return False
    console.print(f"  [green]✓[/green] now watching {_home_relative(Path(better))}")
    return True


def _rewrite_watch_dirs(config_path: Path, dirs: list[str]) -> bool:
    """Replace ``watch.dirs`` in the config file in place, leaving everything else alone.

    Loading the YAML and dumping it back would be simpler and would destroy every
    comment in a file that is half documentation. So this edits the one setting
    as text, in either of the two shapes it is ever written in: the block list
    ``lw init`` produces, and the one-line ``dirs: ["~/Downloads"]`` the README
    shows. Anything else — anchors, a second ``dirs``, a hand-rolled layout — is
    declined rather than guessed at.

    The edit is only kept if the result reads back as exactly ``dirs``, so a
    rewrite can never leave behind a config that loads differently from what was
    meant, or does not load at all. Paths are written JSON-quoted, which is valid
    double-quoted YAML and is what keeps a Windows backslash from becoming an
    escape — see ``templates._yaml_str``.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    section = re.search(r"^watch:[ \t]*\n((?:[ \t]+.*\n|[ \t]*\n)*)", text, re.M)
    if section is None:
        return False
    body = section.group(1)
    as_block = re.compile(r"^  dirs:[ \t]*\n(?:    - .*\n)+", re.M)
    as_flow = re.compile(r"^  dirs:[ \t]*\[.*\][ \t]*$", re.M)
    # A function rather than a replacement string, so re.sub never reads the
    # backslashes in a Windows path as group references.
    block = "  dirs:\n" + "".join(f"    - {json.dumps(d)}\n" for d in dirs)
    flow = "  dirs: [" + ", ".join(json.dumps(d) for d in dirs) + "]"
    if len(as_block.findall(body)) == 1:
        body = as_block.sub(lambda _m: block, body, count=1)
    elif len(as_flow.findall(body)) == 1:
        body = as_flow.sub(lambda _m: flow, body, count=1)
    else:
        return False
    rewritten = text[:section.start(1)] + body + text[section.end(1):]
    try:
        loaded = yaml.safe_load(rewritten)
    except yaml.YAMLError:
        return False
    if not isinstance(loaded, dict) or (loaded.get("watch") or {}).get("dirs") != dirs:
        return False
    config_path.write_text(rewritten, encoding="utf-8")
    return True


def _offer_backfill(cfg: Config, yes: bool) -> None:
    """Archive zips already sitting in the watch folders, if the user wants them.

    The watcher's own start-up scan only reaches back ``scan_on_start_max_age_hours``,
    so anything older is invisible unless it is imported deliberately. Importing
    it is not the obvious default — a Downloads folder is full of zips that have
    nothing to do with Lambda — so this asks, and only assumes yes when told to.
    """
    candidates: list[Path] = []
    ingestor = Ingestor(cfg, _open_db(cfg))
    for directory in cfg.watch_dirs():
        if not directory.exists():
            continue
        for extension in cfg.watch.extensions:
            candidates += [
                p for p in sorted(directory.glob(f"*{extension}")) if ingestor.is_candidate(p)
            ]
    if not candidates:
        return

    console.print(f"\n  found {len(candidates)} zip(s) already in your download folder(s)")
    # Named outright rather than as `<folder>`: a hint is only worth printing if
    # it can be pasted back as it stands.
    folders = sorted({p.parent for p in candidates})
    later = "; ".join(f"lw backfill {_typeable(f)}" for f in folders)
    if not yes:
        if not sys.stdin.isatty():
            console.print(f"  [dim]run [bold]{later}[/bold] to archive them[/dim]")
            return
        if not typer.confirm("  archive them now as history?", default=False):
            console.print(f"  [dim]skipped — [bold]{later}[/bold] does it later[/dim]")
            return

    candidates.sort(key=lambda p: p.stat().st_mtime)     # oldest first, so seq matches history
    stats: dict[str, int] = {}
    for path in candidates:
        result = ingestor.ingest(path, just_downloaded=False)
        stats[result.status] = stats.get(result.status, 0) + 1
    console.print("  " + "  ".join(f"[bold]{k}[/bold]: {v}" for k, v in sorted(stats.items())))


# ------------------------------------------------------------------ service
def _start_service(cfg: Config) -> bool:
    """Install and start the background watcher, explaining whatever happens.

    Returns whether it got one. Not being able to install a service is a
    disappointment, not a catastrophe — everything else `setup` did still
    stands, and `lw watch` still works — so this reports and lets the caller
    decide whether that is fatal.
    """
    try:
        state = install_service(cfg, _CONFIG_PATH)
    except ServiceError as exc:
        err_console.print(
            f"  [yellow]![/yellow] could not install a background watcher: {exc}\n"
            "    [dim]Run [bold]lw watch[/bold] in a terminal instead, or see docs/autostart.md "
            "for the manual recipe.[/dim]"
        )
        return False
    if state.running:
        console.print(f"  [green]●[/green] watching in the background   [dim]{state.manager}[/dim]")
    else:
        console.print(
            f"  [yellow]●[/yellow] registered with {state.manager} but not running yet"
            f"{' — ' + state.detail if state.detail else ''}"
        )
    if state.manager == "pidfile":
        console.print(
            "  [dim]no systemd user session here, so this will not come back after a reboot; "
            "run [bold]lw start[/bold] again, or see docs/autostart.md[/dim]"
        )
    elif state.manager == "startup-folder":
        console.print(
            "  [dim]registering a scheduled task was refused, so this starts from your "
            "Startup folder instead — it will come back at logon, but nothing will "
            "restart it if it crashes[/dim]"
        )
    return True


@app.command(rich_help_panel="Everyday", **for_command("demo"))
def demo(
    open_report: Optional[bool] = typer.Option(
        None, "--open/--no-open",
        help="Open the HTML report afterwards. Asks when run in a terminal, and does not otherwise.",
    ),
    clean: bool = typer.Option(False, "--clean", help="Remove the demo archive, and do nothing else."),
) -> None:
    """See the whole thing work on a sample Lambda, without touching your archive.

    Before this, a new install had nothing to show until the next real deploy:
    ``lw setup``, decline the backfill, see "nothing archived yet", and wait. This
    runs three downloads of a sample function through the real pipeline — the
    same code ``lw watch`` runs — so every layer of the diff is visible in the
    first minute.

    It all happens in ``<archive>/demo/``, a separate archive with its own index,
    so nothing it does appears in ``lw ls`` or counts towards your own history.
    Desktop notifications and the git mirror are switched off for it: a pop-up
    about a function you have never heard of is alarming, and the mirror is the
    slowest step for the least to see. Each run starts from nothing, so it always
    shows the same thing.
    """
    from . import demo as sample

    cfg = _cfg()
    demo_root = cfg.root / "demo"
    if clean:
        if demo_root.exists():
            rmtree(demo_root)
            console.print(f"[green]removed[/green] {_home_relative(demo_root)}")
        else:
            console.print(f"[dim]nothing to remove — {_home_relative(demo_root)} does not exist[/dim]")
        return

    if demo_root.exists():
        rmtree(demo_root)
    # A fresh Config rather than a copy of the user's: the demo should look the
    # same on every machine, not inherit someone's diff settings or ignore globs.
    demo_cfg = Config()
    demo_cfg.store.root = str(demo_root)
    demo_cfg.watch.dirs = [str(demo_root / "Downloads")]
    demo_cfg.notify.enabled = False
    demo_cfg.git_mirror.enabled = False
    demo_cfg.ensure_dirs()
    # The demo's own log, so none of its lines land in the real watcher's.
    setup_logging(cfg.log_level, demo_cfg.log_dir / "watcher.log")

    console.print(f"[bold]lambda-watcher[/bold] {__version__} — a demo, on a sample Lambda\n")
    console.print(f"[dim]Three downloads of {sample.DEMO_FUNCTION} land in a scratch Downloads "
                  "folder: a release, the next release, and that release downloaded again.[/dim]\n")

    db = _open_db(demo_cfg)
    ingestor = Ingestor(demo_cfg, db)
    ingestor.ai_in_reports = False
    for path in sample.stage_downloads(demo_root / "Downloads"):
        _print_ingest_result(ingestor.ingest(path, just_downloaded=False))

    row = db.get_function_by_name(sample.DEMO_FUNCTION)
    if row is None:
        _fail("the demo archived nothing, which should not be possible. "
              f"`lw logs` and {_home_relative(demo_cfg.log_dir / 'watcher.log')} say why.")
    result = _build_diff(db, Store(demo_cfg), demo_cfg, row, 1, 2, include_vendor=None)
    console.print("\n[dim]What changed between the first two, as `lw diff` shows it:[/dim]\n")
    render_diff(console, result, show_diffs=False)

    page = demo_cfg.reports_dir / slugify(sample.DEMO_FUNCTION) / "latest.html"
    console.print(f"\n  [bold]the same comparison as a page[/bold]  {_home_relative(page)}")
    if open_report is None:
        open_report = (sys.stdin.isatty() and sys.stdout.isatty()
                       and typer.confirm("  open it in your browser?", default=True))
    if open_report and page.exists() and not _open_in_browser(page):
        err_console.print("[yellow]could not find a browser to show it in; "
                          "open the file above yourself.[/yellow]")

    console.print("\n[dim]That was a sample function. Your own archive was not touched; "
                  "`lw demo --clean` removes this one.[/dim]\n")
    if current_status(cfg, _CONFIG_PATH).running:
        console.print("  [bold]your watcher is already running[/bold]   "
                      "[dim]download a Lambda zip as usual, then run lw[/dim]")
    else:
        console.print("  [bold]lw setup[/bold]   [dim]do this for real — watch your downloads folder "
                      "from now on[/dim]")


@app.command(rich_help_panel="Watching", **for_command("start"))
def start() -> None:
    """Watch in the background, now and after every reboot."""
    cfg = _cfg()
    if not _start_service(cfg):
        raise typer.Exit(1)


@app.command(rich_help_panel="Watching", **for_command("stop"))
def stop(
    remove: bool = typer.Option(
        False, "--remove", help="Also unregister it, so it does not come back at login."
    ),
) -> None:
    """Stop the background watcher."""
    cfg = _cfg()
    try:
        stop_service(cfg, _CONFIG_PATH, remove=remove)
    except ServiceError as exc:
        _fail(str(exc))
    console.print(
        "[dim]unregistered[/dim]" if remove else
        "[dim]stopped (it will start again at login; --remove prevents that)[/dim]"
    )


@app.command(rich_help_panel="Watching", **for_command("restart"))
def restart() -> None:
    """Stop and start the background watcher — use it after editing the config."""
    cfg = _cfg()
    try:
        stop_service(cfg, _CONFIG_PATH)
    except ServiceError as exc:
        _fail(str(exc))
    if not _start_service(cfg):
        raise typer.Exit(1)


# ----------------------------------------------------------------- checkup
@app.command(rich_help_panel="Everyday", **for_command("doctor"))
def doctor() -> None:
    """Check that everything the tool needs is in place, and exit nonzero if it is not.

    Every row that is not ``ok`` carries a remedy, because a checkup that only
    names a problem has done half the job. Paths are shown as ``~/...``, as ``lw``
    shows them: an absolute path sets the width of the detail column, and with the
    remedy column after it, a long home directory — macOS keeps temp homes under
    ``/private/var/folders/...`` — pushed every remedy sideways. The exit code is what makes this usable
    from a cron or a CI step: it used to print ``MISSING`` in red and still exit 0,
    so nothing automated could ever notice.
    """
    cfg = _cfg()
    rows: list[tuple[str, str, str, str]] = []

    config_path = _CONFIG_PATH or default_config_path()
    rows.append((
        "config file", "ok" if config_path.exists() else "using defaults", _home_relative(config_path),
        "" if config_path.exists() else "lw init writes one you can edit",
    ))

    for key in unknown_keys(config_path):
        rows.append((
            "config key", "UNKNOWN", key,
            f"nothing reads {key} — check the spelling, or delete the line",
        ))

    rows.append((
        "archive root", "ok" if cfg.root.exists() else "missing", _home_relative(cfg.root),
        "" if cfg.root.exists() else "lw setup creates it",
    ))

    for directory in cfg.watch_dirs():
        if not directory.exists():
            better = [d for d in _best_watch_dirs() if Path(d).expanduser().is_dir()]
            hint = f"downloads look like they land in {better[0]}" if better else \
                "set watch.dirs in the config, then lw restart"
            rows.append(("watch dir", "MISSING", _home_relative(directory), hint))
        elif not os.access(directory, os.R_OK):
            rows.append((
                "watch dir", "UNREADABLE", _home_relative(directory),
                "grant read access, or point watch.dirs somewhere else",
            ))
        else:
            rows.append(("watch dir", "ok", _home_relative(directory), ""))

    state = current_status(cfg, _CONFIG_PATH)
    if state.running or state.installed:
        rows.append((
            "watcher", "ok" if state.running else "stopped", f"{state.summary} · {state.manager}",
            "" if state.running else "lw start",
        ))
    else:
        rows.append(("watcher", "not installed", "nothing is archiving in the background", "lw setup"))

    beat = heartbeat.read(cfg.heartbeat_path)
    if beat is None:
        rows.append((
            "heartbeat", "none" if not state.running else "MISSING",
            "the watcher has not reported in",
            "" if not state.running else "lw restart, then lw logs to see why",
        ))
    elif beat.is_stale() and state.running:
        rows.append((
            "heartbeat", "STALE", f"last beat {relative_ts(beat.last_beat_at)}",
            "lw restart, then lw logs",
        ))
    elif not beat.is_running() or beat.is_stale():
        # The watcher that wrote this has gone — cleanly or otherwise. Worth
        # saying when, but not a fault: nothing claims to be running.
        how = "stopped" if beat.stopped_at else "last heard from"
        rows.append((
            "heartbeat", "stopped", f"{how} {relative_ts(beat.stopped_at or beat.last_beat_at)}",
            "lw start" if not beat.stopped_at else "",
        ))
    else:
        rows.append((
            "heartbeat", "ok",
            f"{beat.observer or 'unknown'} · {beat.seen} file(s) seen "
            f"· last beat {relative_ts(beat.last_beat_at)}",
            "",
        ))

    rows.append((
        "git mirror", "ok" if git_available() else "git not found",
        "enabled" if cfg.git_mirror.enabled else "disabled in config",
        "" if git_available() else "install git, or set git_mirror.enabled: false",
    ))

    rows.append(_ai_doctor_row(cfg))

    try:
        db = _open_db(cfg)
        functions = db.list_functions()
        total_versions = sum(int(f["version_count"] or 0) for f in functions)
        rows.append(("index", "ok", f"{len(functions)} function(s), {total_versions} version(s)", ""))
        db.close()
    except Exception as exc:  # noqa: BLE001
        rows.append(("index", "FAILED", str(exc), "lw reindex rebuilds it from the manifests"))

    quarantined = []
    if cfg.quarantine_dir.exists():
        quarantined = [q for q in cfg.quarantine_dir.glob("*") if q.suffix != ".txt"]
    if quarantined:
        rows.append((
            "quarantine", "held", f"{len(quarantined)} archive(s) refused",
            f"read why in {_home_relative(cfg.quarantine_dir)}",
        ))

    try:
        usage = shutil.disk_usage(cfg.root)
        rows.append(("disk free", "ok", human_size(usage.free), ""))
    except OSError:
        pass

    table = Table(box=None, header_style="bold", padding=(0, 2, 0, 0))
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", style="dim")
    table.add_column("what to do", style="dim")
    unhealthy = 0
    for name, status, detail, remedy in rows:
        if status == "ok":
            style = "green"
        elif status.isupper():
            style = "red"
            unhealthy += 1
        else:
            style = "yellow"
        table.add_row(name, f"[{style}]{status}[/{style}]", detail, remedy)
    console.print(table)

    if unhealthy:
        console.print(f"\n[red]{unhealthy} problem(s) found.[/red] "
                      "Each row above ends in the command that fixes it.")
        raise typer.Exit(1)


def _ai_doctor_row(cfg: Config) -> tuple[str, str, str, str]:
    """``lw doctor``'s row about AI. Only a broken setup counts as a problem.

    Not having AI set up is a choice, not a fault, so it is reported in yellow
    and never fails the checkup. A settings file that cannot be read, or a
    default model with no key, *is* a fault — explanations would silently
    stop — so those are upper-case and make ``doctor`` exit 1. The check never
    makes a request: whether a key works is ``lw ai test``'s question, asked
    only when someone asks it.
    """
    settings = AISettings.load(cfg.root)
    if settings.problem:
        return ("ai", "UNREADABLE", _home_relative(settings.path or cfg.root / "ai.json"),
                "lw ai add writes a fresh one and keeps the old file")
    if not settings.enabled:
        return ("ai", "off", "switched off with lw ai off", "lw ai on")
    entry = settings.default_entry()
    if entry is None:
        return ("ai", "not set up", "optional — explains each change in plain English", "lw ai add")
    if entry.key_problem():
        return ("ai", "NO KEY", f"{entry.name} has no API key here",
                f"lw ai add {entry.provider} --name {entry.name}")
    how = "every new version" if settings.auto_explain else "on request"
    return ("ai", "ok", f"{entry.label}, {how}", "lw ai test checks it answers")


# ------------------------------------------------------------------ watch
def _print_ingest_result(result: IngestResult) -> None:
    """One line per archived download, colour-coded by what became of it.

    What ``lw watch`` prints as zips arrive and what ``lw demo`` prints for its
    sample downloads, so the demo shows exactly the output the real thing will.
    A new version that changed something gets a second line saying what, and a
    third naming the report already rendered for it: by the time this prints the
    comparison exists on disk, so the reader is handed a file rather than a
    command to go and produce one.
    """
    colours = {
        "new": "green", "unchanged": "cyan", "duplicate-download": "dim", "failed": "red",
    }
    colour = colours.get(result.status, "white")
    label = f"{result.function_name or '?'}"
    if result.seq:
        label += f" v{result.seq:04d}"
    console.print(
        f"[{colour}]{result.status:>18}[/{colour}]  {label}  "
        f"[dim]{result.source.name} — {result.change_summary or result.message}[/dim]"
    )
    if result.change_impact:
        console.print(f"[dim]{'':>18}  {result.change_impact}[/dim]")
    if result.report_path is not None:
        console.print(f"[dim]{'':>18}  report: {_home_relative(result.report_path)}[/dim]")
    elif result.status == "new" and result.changed_from:
        console.print(
            f"[dim]{'':>18}  review: lw diff "
            f'"{result.function_name}" --html --open[/dim]'
        )


def _print_explained(job: ExplainJob, explanation: Explanation | None, error: AIError | None) -> None:
    """One line for an explanation the background worker finished, lined up under the ingest lines.

    Called on the explainer's thread as each answer lands, so a terminal
    running ``lw watch`` shows the headline a few seconds after the version
    it explains — or, when it failed, why and the command that tries again.
    """
    diff = job.diff
    label = f"{diff.function_name} v{diff.b_seq:04d}"
    if explanation is not None:
        risk = f"  ({explanation.risk} risk)" if explanation.risk else ""
        console.print(f"[magenta]{'explained':>18}[/magenta]  {escape(label)}  "
                      f"[dim]{escape(explanation.headline or explanation.summary[:120])}{risk}[/dim]")
    elif error is not None:
        command = explain_command(diff.function_name, diff.a_seq, diff.b_seq)
        console.print(f"[yellow]{'not explained':>18}[/yellow]  {escape(label)}  "
                      f"[dim]{escape(str(error))} — {escape(command)} tries again[/dim]")


def _finish_explaining(explainer: Explainer) -> None:
    """Wait for the explanations a one-shot command queued, with a spinner saying so.

    ``lw ingest`` and ``lw watch --once`` exit when their work is done, and a
    background thread dies with its process — so they wait here rather than
    leave a report promising an answer that will never arrive. Ctrl-C stops
    waiting without losing anything that matters: the unfinished pairs are put
    back to "not explained yet", and ``lw explain`` writes them later.
    """
    if not explainer.outstanding:
        explainer.stop()
        return
    try:
        with err_console.status(f"writing {explainer.outstanding} AI explanation"
                                f"{'s' if explainer.outstanding != 1 else ''}… (Ctrl-C to skip)"):
            explainer.drain()
    except KeyboardInterrupt:
        err_console.print("[dim]skipped — `lw explain <function>` writes them later[/dim]")
    finally:
        explainer.stop()


@app.command(rich_help_panel="Watching", **for_command("watch"))
def watch(
    once: bool = typer.Option(False, "--once", help="Archive what is already there, then stop."),
    dir: Optional[list[Path]] = typer.Option(
        None, "--dir", "-d", help="Watch this directory instead of the configured ones."
    ),
) -> None:
    """Watch the downloads folder and archive every Lambda zip that lands in it."""
    from .watcher import Watcher

    cfg = _cfg()
    if dir:
        cfg.watch.dirs = [str(Path(d).expanduser()) for d in dir]

    db = _open_db(cfg)
    store = Store(cfg)
    explainer = Explainer(cfg, db, store, on_done=_print_explained)
    ingestor = Ingestor(cfg, db, store, explainer=explainer)
    watcher = Watcher(cfg, db, ingestor, on_result=_print_ingest_result)
    watcher.stop_on_termination()
    try:
        watcher.start()
    except FileNotFoundError as exc:
        _fail(str(exc))

    console.print(
        f"[bold]lambda-watcher[/bold] {__version__} — archiving into {cfg.root}\n"
        f"[dim]watching {', '.join(str(d) for d in cfg.watch_dirs())}. Press Ctrl-C to stop.[/dim]"
    )
    if once:
        watcher.drain(timeout=cfg.watch.max_wait_seconds + 60)
        watcher.stop()
        _finish_explaining(explainer)
        console.print("[dim]done[/dim]")
        return
    watcher.wait_forever()
    watcher.stop()
    explainer.stop()
    console.print("[dim]stopped[/dim]")


@app.command(rich_help_panel="Watching", **for_command("ingest"))
def ingest(
    paths: list[Path] = typer.Argument(..., help="Zip file(s) to archive."),
    function: Optional[str] = typer.Option(
        None, "--as", "-a", help="File it under this function name instead of guessing one."
    ),
    force: bool = typer.Option(False, "--force", help="Archive even if the content is unchanged."),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Note to attach to this version."),
) -> None:
    """Archive one or more zip files by hand."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    explainer = Explainer(cfg, db, store, on_done=_print_explained)
    ingestor = Ingestor(cfg, db, store, explainer=explainer)
    failures = 0
    for path in paths:
        result = ingestor.ingest(Path(path).expanduser(), function, force, label)
        colour = {"new": "green", "unchanged": "cyan", "duplicate-download": "dim"}.get(
            result.status, "red"
        )
        suffix = f" v{result.seq:04d}" if result.seq else ""
        console.print(
            f"[{colour}]{result.status}[/{colour}] {result.function_name or '?'}{suffix} "
            f"[dim]({result.message})[/dim]"
        )
        if result.status == "failed":
            failures += 1
    _finish_explaining(explainer)
    if failures:
        raise typer.Exit(1)


@app.command(rich_help_panel="Watching", **for_command("backfill"))
def backfill(
    directory: Path = typer.Argument(..., help="Folder full of previously downloaded zips."),
    pattern: str = typer.Option(
        "*.zip", "--pattern", "-p", help="Which files to import, as a pattern like 'order-*.zip'."
    ),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Look in the folders inside it too."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be imported, and import nothing."),
) -> None:
    """Import a folder of old backups, oldest first, so version order matches history."""
    cfg = _cfg()
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        _fail(f"{directory} is not a directory")

    files = sorted(
        (directory.rglob(pattern) if recursive else directory.glob(pattern)),
        key=lambda p: p.stat().st_mtime,
    )
    files = [f for f in files if f.is_file()]
    if not files:
        console.print("[yellow]nothing to import[/yellow]")
        return

    if dry_run:
        from .identify import identify

        table = Table(box=None, header_style="bold", padding=(0, 2, 0, 0))
        table.add_column("file")
        table.add_column("modified", style="dim")
        table.add_column("would become")
        table.add_column("via", style="dim")
        for path in files:
            ident = identify(path, cfg.naming, None)
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            table.add_row(
                path.name,
                format_ts(modified.isoformat()),
                ident.name,
                f"{ident.strategy}/{ident.confidence}",
            )
        console.print(table)
        console.print(f"[dim]{len(files)} file(s). Re-run without --dry-run to import.[/dim]")
        return

    db = _open_db(cfg)
    ingestor = Ingestor(cfg, db)
    stats: dict[str, int] = {}
    for path in files:
        # Someone else's backup folder: archive from it, never delete out of it.
        result = ingestor.ingest(path, just_downloaded=False)
        stats[result.status] = stats.get(result.status, 0) + 1
        suffix = f" v{result.seq:04d}" if result.seq else ""
        console.print(f"  {result.status:>18}  {result.function_name or '?'}{suffix}  [dim]{path.name}[/dim]")
    console.print("\n" + "  ".join(f"[bold]{k}[/bold]: {v}" for k, v in sorted(stats.items())))


# ------------------------------------------------------------- inspection
@app.command("ls", rich_help_panel="Everyday", **for_command("ls"))
def list_functions() -> None:
    """List every Lambda function that has been archived."""
    cfg = _cfg()
    db = _open_db(cfg)
    rows = db.list_functions()
    if not rows:
        console.print(
            "[dim]Nothing archived yet. "
            "Run [bold]lw start[/bold] and download a zip.[/dim]"
        )
        return
    table = Table(box=None, header_style="bold", padding=(0, 2, 0, 0))
    table.add_column("function")
    table.add_column("versions", justify="right")
    table.add_column("latest", justify="right")
    table.add_column("last seen", style="dim")
    table.add_column("runtime", style="dim")
    for row in rows:
        latest = db.latest_version(int(row["id"]))
        table.add_row(
            row["name"],
            str(row["version_count"]),
            f"v{int(row['latest_seq']):04d}" if row["latest_seq"] else "-",
            format_ts(row["last_seen"]),
            (latest["runtime"] if latest else "") or "",
        )
    console.print(table)


@app.command(rich_help_panel="Reading the archive", **for_command("versions"))
def versions(
    function: str = typer.Argument(
        ..., help=FUNCTION_HELP,
        autocompletion=_complete_function,
    ),
    limit: int = typer.Option(30, "--limit", "-n", help="How many to show."),
) -> None:
    """List the archived versions of one function."""
    cfg = _cfg()
    db = _open_db(cfg)
    row = _resolve_function(db, function)
    rows = db.list_versions(int(row["id"]), limit)
    if not rows:
        console.print("[dim]no versions archived[/dim]")
        return

    table = Table(box=None, header_style="bold", padding=(0, 2, 0, 0),
                  title=row["name"], title_justify="left")
    table.add_column("version")
    table.add_column("archived", style="dim")
    table.add_column("files", justify="right")
    table.add_column("size", justify="right")
    table.add_column("handler")
    table.add_column("downloaded as", style="dim")
    table.add_column("label", style="cyan")
    for version in rows:
        table.add_row(
            f"v{int(version['seq']):04d}",
            format_ts(version["ingested_at"]),
            f"{version['file_count']:,}",
            human_size(version["total_size"]),
            version["handler"] or "-",
            version["source_name"] or "-",
            version["label"] or "",
        )
    console.print(table)
    console.print(
        f"\n[dim]Compare the last two: [bold]lw diff \"{row['name']}\"[/bold][/dim]"
    )


@app.command(rich_help_panel="Reading the archive", **for_command("show"))
def show(
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
    version: Optional[str] = typer.Argument(None, help=LATEST_VERSION_HELP),
    files: bool = typer.Option(False, "--files", help="List every file in the package."),
    json_out: bool = typer.Option(
        False, "--json", help="Print everything recorded about the version, as JSON."
    ),
) -> None:
    """Show what one archived version contains."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    seq = _resolve_seq(db, int(row["id"]), version)
    version_row = _version_or_fail(db, int(row["id"]), seq)
    version_dir = store.resolve_version_dir(version_row["dir"])

    if json_out:
        manifest = store.read_manifest(version_dir)
        console.print_json(json.dumps(manifest or dict(version_row)))
        return

    console.print(f"[bold]{row['name']}[/bold] [cyan]v{seq:04d}[/cyan]")
    console.print(f"  archived   {format_ts(version_row['ingested_at'])}")
    console.print(f"  source     {version_row['source_name']}")
    console.print(
        f"  runtime    {version_row['runtime']} "
        f"[dim]({version_row['runtime_confidence']} confidence)[/dim]"
    )
    console.print(f"  handler    {version_row['handler'] or '-'}")
    console.print(
        f"  contents   {version_row['file_count']:,} files, {human_size(version_row['total_size'])} "
        f"[dim]({version_row['code_file_count']:,} first-party, {version_row['code_lines']:,} lines)[/dim]"
    )
    console.print(f"  tree hash  [dim]{version_row['tree_hash'][:16]}[/dim]")
    console.print(f"  location   [dim]{version_dir}[/dim]")

    deps = db.deps_for(int(version_row["id"]))
    if deps:
        installed = [d for d in deps if not d["is_declared"]]
        declared = [d for d in deps if d["is_declared"]]
        console.print(
            f"\n[bold]Dependencies[/bold]  [dim]{len(declared)} declared, {len(installed)} installed[/dim]"
        )
        for dep in (installed or declared)[:25]:
            console.print(f"  {dep['name']} [dim]{dep['version'] or ''}[/dim]")
        if len(installed or declared) > 25:
            console.print(f"  [dim]… {len(installed or declared) - 25} more[/dim]")

    env = db.env_for(int(version_row["id"]))
    if env:
        console.print("\n[bold]Environment variables read[/bold]")
        console.print("  " + ", ".join(sorted({e["name"] for e in env})))

    services = db.services_for(int(version_row["id"]))
    if services:
        console.print("\n[bold]AWS services used[/bold]")
        console.print("  " + ", ".join(sorted({s["service"] for s in services})))

    findings = db.findings_for(int(version_row["id"]))
    if findings:
        console.print("\n[bold]Findings[/bold]")
        for finding in findings[:20]:
            colour = {"high": "red", "medium": "yellow"}.get(finding["severity"], "dim")
            console.print(
                f"  [{colour}]{finding['severity']:>6}[/{colour}] {finding['kind']} "
                f"[dim]{finding['path']}:{finding['line']} {finding['detail']}[/dim]"
            )

    if files:
        console.print("\n[bold]Files[/bold]")
        for entry in db.files_for(int(version_row["id"])):
            marker = "[dim]v[/dim]" if entry["is_vendor"] else " "
            console.print(f"  {marker} {entry['path']} [dim]{human_size(entry['size'])}[/dim]")


# ------------------------------------------------------------------- diff
def _show_mirror_diff(cfg: Config, store: Store, row, a_seq: int, b_seq: int) -> None:
    """Print the git mirror's own patch for two versions, from the diff command.

    Not a second diff engine so much as a reconciliation. ``lw diff`` hides
    vendored files and the mirror keeps them, so the same two versions read as
    ``1 modified`` here and ``3 files changed`` there; getting both from one
    command, under version specs that already resolve ``latest`` and ``-2``,
    turns that from a contradiction into a policy difference the reader can see.
    See :func:`_vendor_policy_note`, which says the same thing from the other
    side, and :func:`lambda_watcher.gitmirror.diff`, which runs it.

    Every failure names what to type next, because the mirror is optional and
    each way it can be absent has a different answer: git missing, the mirror
    switched off, or versions archived before it was switched on.
    """
    if not git_available():
        _fail("git is not on PATH, so there is no mirror to read. "
              f"`lw diff {row['slug']}` compares these versions without it.")
    repo = store.repo_dir(row["slug"])
    tag_a = f"{cfg.git_mirror.tag_prefix}{a_seq:04d}"
    tag_b = f"{cfg.git_mirror.tag_prefix}{b_seq:04d}"
    missing = [tag for tag in (tag_a, tag_b) if not has_tag(repo, tag)]
    if missing:
        _fail(f"the git mirror has no {' or '.join(missing)}. Set git_mirror.enabled in your "
              f"config (`lw init` writes one) and re-ingest, or drop --mirror to compare with "
              f"`lw diff {row['slug']}`.")
    patch = mirror_diff(repo, tag_a, tag_b)
    if not patch:
        console.print(f"[green]The mirror reports no difference between {tag_a} and {tag_b}.[/green]")
        return
    # Straight to stdout rather than through rich: a patch has to survive being
    # piped into `git apply`, and console.print would wrap long lines and read
    # square brackets in the diff body as markup.
    sys.stdout.write(patch + "\n")


@app.command(rich_help_panel="Everyday", **for_command("diff"))
def diff(
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
    from_: Optional[str] = typer.Option(
        None, "--from", "-f", help="Older version (default: the one before --to)."
    ),
    to: Optional[str] = typer.Option(None, "--to", "-t", help="Newer version (default: latest)."),
    html: bool = typer.Option(
        False, "--html", help="Write the comparison as a web page instead of printing it."
    ),
    open_report: bool = typer.Option(False, "--open", help="Open the HTML report in your browser."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Where to write the HTML report."),
    vendor: bool = typer.Option(
        False, "--vendor", help="Also compare vendored packages (node_modules, site-packages) file by file."
    ),
    whitespace: bool = typer.Option(
        False, "--whitespace", help="Show the changed lines of files that differ only in spacing."
    ),
    no_patch: bool = typer.Option(False, "--no-patch", help="Only the summary, without the changed lines."),
    json_out: bool = typer.Option(False, "--json", help="Print the comparison as JSON, for scripts."),
    mirror: bool = typer.Option(
        False, "--mirror", help="Print git's own patch for these two versions instead."
    ),
) -> None:
    """Compare two versions of a function. Defaults to the last two."""
    cfg = _cfg()
    # A retab renders as every touched line removed and re-added, so the diff
    # collapses it to a label by default. This is the way back for the one time
    # the reader wants to check that a reindent is all it was; the override goes
    # on the config rather than through `compare_versions` because that is where
    # everything else reads the setting from.
    cfg.diff.collapse_whitespace_only = not whitespace
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    function_id = int(row["id"])

    a_seq, b_seq = _resolve_pair(db, function_id, from_, to)

    if mirror:
        if json_out or html or open_report or output:
            _fail("--mirror prints git's own patch, so it cannot be combined with "
                  "--json, --html or --output. Drop --mirror for those.")
        _show_mirror_diff(cfg, store, row, a_seq, b_seq)
        return

    include_vendor = True if vendor else None
    result = _build_diff(db, store, cfg, row, a_seq, b_seq, include_vendor,
                         compute_diffs=not no_patch)

    if json_out:
        console.print_json(json.dumps(result.as_dict()))
        return

    if html or open_report or output:
        target = Path(output).expanduser() if output else (
            cfg.reports_dir / f"{slugify(row['name'])}-v{a_seq:04d}-v{b_seq:04d}.html"
        )
        # --output can put the page anywhere, so the top bar links the archive
        # index only when the page sits beside it in reports/.
        archive_index = cfg.reports_dir / "index.html"
        write_html(result, target,
                   archive_href="index.html" if output is None and archive_index.exists() else None,
                   ai=panel_for(store, result, AISettings.load(cfg.root)))
        console.print(f"[green]wrote[/green] {target}")
        if open_report and not _open_in_browser(target):
            err_console.print("[yellow]could not find a browser to show it in; "
                              "open the file above yourself.[/yellow]")
        return

    render_diff(console, result, show_diffs=not no_patch)
    _mention_explanation(store, cfg, result)


def _mention_explanation(store: Store, cfg: Config, result) -> None:
    """One line after a terminal diff pointing at its AI explanation, or at the command that writes one.

    Only when AI is switched on and could be used: someone who never set it up
    is not nagged from the terminal on every diff — the report's card and bare
    ``lw`` are where it is offered — and someone who switched it off hears
    nothing at all.
    """
    settings = AISettings.load(cfg.root)
    if not settings.enabled:
        return
    command = explain_command(result.function_name, result.a_seq, result.b_seq)
    record = load_record(store, result.a_meta, result.b_meta)
    if record is not None and record.explanation is not None and record.explanation.headline:
        console.print(f"\n[magenta]✦[/magenta] {escape(record.explanation.headline)}  "
                      f"[dim]— {escape(command)} shows the whole explanation[/dim]")
    elif settings.resolve() is not None:
        console.print(f"\n[magenta]✦[/magenta] [dim]{escape(command)} explains this change "
                      "in plain English[/dim]")


def _refresh_archive_index(cfg: Config, db: Database) -> None:
    """Rewrite ``reports/index.html`` after a command changed what it shows, quietly.

    ``rm``, ``rename`` and ``merge`` change which functions it lists, ``label``
    changes a chip on it, ``reindex`` may change anything, and ``lw report FN``
    writes the history page it links to. Only a page that already exists is
    rewritten: a housekeeping command is no reason to start writing reports for
    someone who switched them off. A failure is a warning, because the command's
    own work is already done. See
    :func:`~lambda_watcher.diffing.build.write_archive_index`.
    """
    if not (cfg.reports_dir / "index.html").exists():
        return
    try:
        write_archive_index(db, cfg.reports_dir, store=Store(cfg))
    except OSError as exc:
        err_console.print(f"[yellow]could not update {cfg.reports_dir / 'index.html'}: {exc}. "
                          "`lw report` rewrites it.[/yellow]")


def _offer_page(page: Path, open_report: bool | None) -> None:
    """Open a report just written, if asked to or if someone is at a desktop to see it.

    ``--open`` insists, ``--no-open`` never opens, and no flag at all asks
    :func:`_desktop_in_front`. A browser that does not answer is a warning that
    names the way on, since the path is already on screen.
    """
    if open_report is False or (open_report is None and not _desktop_in_front()):
        return
    if not _open_in_browser(page):
        err_console.print("[yellow]could not find a browser to show it in; "
                          "open the file above yourself.[/yellow]")


def _report_every_function(
    cfg: Config, db: Database, output: Path | None, open_report: bool | None
) -> None:
    """What bare ``lw report`` does: write the front page of ``reports/`` and show it.

    The page every function's reports hang off, so reading them never starts
    with remembering a function's name. An empty archive is not an error — it is
    where everybody starts — so it says what to type and exits 0 rather than
    opening a page with nothing on it.
    """
    if not db.archive_totals()[1]:
        console.print("[dim]nothing archived yet, so there is no report to write. "
                      "`lw setup` watches your downloads folder from now on.[/dim]")
        return
    page_dir = Path(output).expanduser() if output else cfg.reports_dir
    try:
        index, count = write_archive_index(db, cfg.reports_dir, page_dir, store=Store(cfg))
    except OSError as exc:
        _fail(f"could not write the report index into {page_dir}: {exc}. "
              "Pass --output with a folder you can write to.")
    console.print(f"[green]wrote[/green] {index} "
                  f"[dim]({count} function{'s' if count != 1 else ''})[/dim]")
    _offer_page(index, open_report)


@app.command(rich_help_panel="Everyday", **for_command("report"))
def report(
    function: Optional[str] = typer.Argument(
        None, autocompletion=_complete_function,
        help="Whose history to build. Leave it out for one page linking every function.",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Folder to write the pages into (default: the archive's reports folder)."
    ),
    open_report: Optional[bool] = typer.Option(
        None, "--open/--no-open",
        help="Open the page in your browser. Default: open it when run from a desktop terminal.",
    ),
    limit: int = typer.Option(
        25, "--limit", "-n", help="How many recent versions to include (with a function)."
    ),
    vendor: bool = typer.Option(
        False, "--vendor", help="Include vendored files in the diffs (with a function)."
    ),
) -> None:
    """Build a browsable HTML history: every version plus a diff for each step.

    Without a function, writes the front page of the reports folder instead:
    every archived function, its latest change and its secrets, linking the
    pages already written.
    """
    cfg = _cfg()
    db = _open_db(cfg)
    if function is None:
        _report_every_function(cfg, db, output, open_report)
        return
    store = Store(cfg)
    row = _resolve_function(db, function)
    function_id = int(row["id"])
    all_versions = db.list_versions(function_id)
    if not all_versions:
        _fail("nothing archived for this function yet")

    selected = all_versions[:limit]
    target_dir = Path(output).expanduser() if output else cfg.reports_dir / slugify(row["name"])
    target_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    seqs = [int(v["seq"]) for v in selected]
    # Under reports/<slug>/ the archive index is one level up, and the
    # _refresh_archive_index below writes it; --output leaves no such guarantee.
    archive_href = "../index.html" if output is None else None
    include_vendor = True if vendor else None
    settings = AISettings.load(cfg.root)
    unexplained = 0

    for version in selected:
        seq = int(version["seq"])
        entry = {
            "seq": seq,
            "ingested_at": version["ingested_at"],
            "runtime": version["runtime"],
            "handler": version["handler"],
            "file_count": version["file_count"],
            "total_size": version["total_size"],
            "source_name": version["source_name"],
            "label": version["label"],
        }
        previous = [s for s in seqs if s < seq]
        if previous:
            a_seq = max(previous)
            pair = _build_diff(db, store, cfg, row, a_seq, seq, include_vendor)
            filename = f"v{a_seq:04d}-v{seq:04d}.html"
            write_html(pair, target_dir / filename, archive_href=archive_href,
                       history_href="index.html", ai=panel_for(store, pair, settings))
            entry["diff_href"] = filename
            entry["diff_summary"] = pair.headline()
            explained = headline_for(store, pair.a_meta, pair.b_meta)
            if explained:
                entry["ai_headline"], entry["ai_risk"] = explained
            else:
                unexplained += 1
        entries.append(entry)

    index = target_dir / "index.html"
    index.write_text(render_timeline(row["name"], entries, archive_href=archive_href), encoding="utf-8")
    console.print(f"[green]wrote[/green] {index} [dim]({len(entries)} versions)[/dim]")
    if unexplained and settings.enabled and settings.resolve() is not None:
        # The history reads as a list of what each release did once every step
        # has a headline; say how to get there rather than leave gaps unexplained.
        console.print(f"[dim]{unexplained} step{'s' if unexplained != 1 else ''} not explained yet — "
                      f"lw explain {escape(shell_word(row['name']))} --all writes "
                      f"{'them' if unexplained != 1 else 'it'}[/dim]")
    if output is None:
        _refresh_archive_index(cfg, db)
    _offer_page(index, open_report)


# ------------------------------------------------------------ explaining
def _can_ask() -> bool:
    """Whether there is a person at a terminal to answer a question.

    One function rather than ``sys.stdin.isatty()`` at every question the AI
    commands ask, so the walk-through can be tested: a test runner's input is
    never a terminal, and without this the only path it could reach is the one
    for scripts.
    """
    return sys.stdin.isatty()


def _complete_model(incomplete: str) -> list[str]:
    """Tab-completion for a saved model's name. Like :func:`_complete_function`, it never raises."""
    try:
        settings = AISettings.load(load_config(_CONFIG_PATH).root)
        return [m.name for m in settings.models if m.name.lower().startswith(incomplete.lower())]
    except Exception:                                 # noqa: BLE001 - never break a prompt
        return []


def _ai_settings(cfg: Config) -> AISettings:
    """The AI settings, warning once if ``ai.json`` exists but could not be read.

    A damaged file is never fatal — see :meth:`AISettings.load` — but it is
    worth a line, because it means models the user set up are being ignored.
    """
    settings = AISettings.load(cfg.root)
    if settings.problem:
        err_console.print(f"[yellow]warning:[/yellow] {escape(settings.problem)}. It is being ignored; "
                          "`lw ai add` starts a fresh one and keeps the old file as ai.json.broken.")
    return settings


def _usable_model(
    cfg: Config, settings: AISettings, requested: str | None, *, interactive: bool
) -> ModelEntry:
    """The model to explain with, offering to set one up when there is none.

    From a terminal, having no model is a question rather than an error:
    "set one up now?" runs the same walk-through as ``lw ai add`` and carries
    straight on with the explanation that was asked for. From a script it is
    an error naming ``lw ai add``. A key found only in the environment is used,
    with a note that saving it lets the background watcher use it too.
    """
    if not settings.enabled:
        _fail("AI explanations are switched off. `lw ai on` turns them back on.")
    entry = settings.resolve(requested)
    if entry is None:
        if not (interactive and _can_ask()):
            _fail("no AI model is set up yet. `lw ai add` sets one up in a minute — Anthropic, "
                  "OpenAI, Azure OpenAI or a model running on your own machine.")
        console.print("No AI model is set up yet. It takes a minute: pick a service, paste a key.\n")
        if not typer.confirm("  set one up now?", default=True):
            raise typer.Exit(1)
        entry = _add_model(cfg, settings)
        console.print()
    problem = entry.key_problem()
    if problem:
        _fail(f"{entry.label} cannot be used: {problem}.")
    if entry.name not in {m.name for m in settings.models} and requested is None:
        env = entry.key_env or "the environment"
        err_console.print(f"[dim]using {entry.label} with the key in ${env}. `lw ai add "
                          f"{entry.provider}` saves it, so the background watcher can use it too.[/dim]")
    return entry


def _explain_pair(cfg: Config, db: Database, store: Store, function_id: int, pair,
                  settings: AISettings, entry: ModelEntry) -> Explanation | AIError:
    """Explain one comparison from a terminal: pending, the request, then the saved result and pages.

    The pair is marked pending first and its page rewritten, so a report
    already open in a browser shows the explanation arriving. Retries print as
    they happen — "rate limited — retrying in 8s (2 of 5)" — so a wait never
    looks like a hang. Every outcome is saved and drawn: an answer replaces
    the card, and a failure leaves the page saying what went wrong and how to
    try again. Ctrl-C takes the pending mark back off before it exits.
    """
    def quietly_rewrite() -> None:
        """Redraw the pair's pages, never letting a page failure hide the answer."""
        try:
            rewrite_pages(cfg, db, store, function_id, pair, settings)
        except OSError as exc:
            err_console.print(f"[yellow]could not update the report: {exc}[/yellow]")

    save_pending(store, pair.a_meta, pair.b_meta, entry.label)
    quietly_rewrite()
    attempts = settings.max_retries + 1

    def on_retry(attempt: int, wait: float, error: AIError) -> None:
        """Say out loud that a request is being retried, and when."""
        err_console.print(f"  [yellow]{escape(error.title)}[/yellow] [dim]— retrying in {wait:.0f}s "
                          f"(attempt {attempt + 1} of {attempts})[/dim]")

    try:
        with err_console.status(f"asking {entry.label} what changed between "
                                f"v{pair.a_seq:04d} and v{pair.b_seq:04d}…"):
            explanation = explain_diff(pair, entry, settings, on_retry=on_retry)
    except AIError as error:
        save_failed(store, pair.a_meta, pair.b_meta, entry.label, error.as_dict())
        quietly_rewrite()
        return error
    except KeyboardInterrupt:
        clear_pending(store, pair.a_meta, pair.b_meta)
        quietly_rewrite()
        raise
    save_done(store, pair.a_meta, pair.b_meta, explanation)
    quietly_rewrite()
    return explanation


def _report_failure(error: AIError, command: str) -> None:
    """Print a failed explanation: what went wrong, what to do, and the command that retries."""
    err_console.print(f"[red]error:[/red] {escape(str(error))}")
    if error.hint:
        err_console.print(f"  [dim]what to do:[/dim] {escape(error.hint)}")
    err_console.print(f"  [dim]the report is unchanged; this tries again:[/dim] {escape(command)}")


def _provenance_line(explanation: Explanation) -> str:
    """The dim line under a printed explanation: which model, how long, from how much of the change."""
    parts = [f"by {explanation.model or explanation.model_name}"]
    if explanation.seconds:
        parts.append(f"{explanation.seconds:.1f}s")
    if explanation.attempts > 1:
        parts.append(f"{explanation.attempts} attempts")
    if not explanation.send_code:
        parts.append("structure only, no code sent")
    elif explanation.files_total:
        parts.append(f"from {explanation.files_sent} of {explanation.files_total} changed files")
    if explanation.redactions:
        parts.append(f"{explanation.redactions} value{'s' if explanation.redactions != 1 else ''} redacted")
    return " · ".join(parts)


def _print_dry_run(pair, settings: AISettings, entry: ModelEntry | None) -> None:
    """Print exactly what ``lw explain`` would send for this pair, and send nothing.

    Straight to stdout rather than through Rich: the prompt quotes code, and
    Rich would read its square brackets as markup and wrap its long lines.
    """
    budget = settings.prompt_budget(entry) if entry else PROVIDERS["anthropic"].prompt_chars
    built = build_prompt(pair, send_code=settings.send_code, budget=budget)
    target = entry.label if entry else "your default model (none is set up yet; `lw ai add`)"
    notes = [f"{len(built.text):,} characters",
             f"{built.files_sent} of {built.files_total} changed files with their lines"]
    if built.withheld:
        notes.append(f"{len(built.withheld)} withheld")
    notes.append(f"{built.redactions} value{'s' if built.redactions != 1 else ''} redacted")
    sys.stdout.write(
        f"# What `lw explain` would send to {target}.\n"
        f"# {' · '.join(notes)}. Nothing was sent.\n\n"
        f"----- instructions -----\n{SYSTEM_PROMPT}\n----- the change -----\n{built.text}\n"
    )


@app.command(rich_help_panel="Everyday", **for_command("explain"))
def explain(
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
    from_: Optional[str] = typer.Option(
        None, "--from", "-f", help="Older version (default: the one before --to)."
    ),
    to: Optional[str] = typer.Option(None, "--to", "-t", help="Newer version (default: latest)."),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", autocompletion=_complete_model,
        help="A saved model's name, or any model id on your default model's service.",
    ),
    refresh: bool = typer.Option(
        False, "--refresh", "-r", help="Ask again, even if this change was explained before."
    ),
    every: bool = typer.Option(
        False, "--all", help="Explain every step of the history that has no explanation yet."
    ),
    limit: int = typer.Option(25, "--limit", "-n", help="With --all: how many recent steps to cover."),
    open_report: bool = typer.Option(
        False, "--open", help="Open the report with the explanation in your browser."
    ),
    json_out: bool = typer.Option(False, "--json", help="Print the explanation as JSON, for scripts."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print exactly what would be sent, and send nothing."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="With --all: do not ask before making several requests."
    ),
) -> None:
    """Explain a change in plain English, print it, and add it to the report.

    The answer is saved beside the version (see :mod:`lambda_watcher.ai.explanation`),
    so asking again is instant and free; ``--refresh`` or a different
    ``--model`` asks again. Everything that can go wrong on the way is sorted
    into a message and a next step by :mod:`lambda_watcher.ai.providers`.
    """
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    function_id = int(row["id"])
    settings = _ai_settings(cfg)
    include_vendor = True if cfg.report.include_vendor else None

    if every:
        _explain_history(cfg, db, store, row, settings, model, refresh=refresh, limit=limit,
                         yes=yes, json_out=json_out, dry_run=dry_run)
        return

    a_seq, b_seq = _resolve_pair(db, function_id, from_, to)
    pair = _build_diff(db, store, cfg, row, a_seq, b_seq, include_vendor)
    if dry_run:
        _print_dry_run(pair, settings, settings.resolve(model))
        return

    if not settings.enabled:
        _fail("AI explanations are switched off. `lw ai on` turns them back on.")
    record = load_record(store, pair.a_meta, pair.b_meta)
    saved = record.explanation if record is not None else None
    # A saved answer is reused unless asked not to — by --refresh, or by
    # naming a model other than the one that wrote it.
    chosen = settings.resolve(model) if model is not None else None
    other_model = chosen is not None and saved is not None and chosen.name != saved.model_name
    if saved is not None and not refresh and not other_model:
        explanation: Explanation | AIError = saved
        rewrite_pages(cfg, db, store, function_id, pair, settings)
        fresh = False
    else:
        entry = _usable_model(cfg, settings, model, interactive=not json_out)
        explanation = _explain_pair(cfg, db, store, function_id, pair, settings, entry)
        fresh = True

    command = explain_command(row["name"], a_seq, b_seq)
    if isinstance(explanation, AIError):
        _report_failure(explanation, command)
        raise typer.Exit(1)

    page = cfg.reports_dir / slugify(row["name"]) / f"v{a_seq:04d}-v{b_seq:04d}.html"
    if json_out:
        console.print_json(json.dumps({"function": row["name"], "from": a_seq, "to": b_seq,
                                       "report": str(page), **explanation.as_dict()}))
        return
    render_explanation(console, explanation, function_name=row["name"], a_seq=a_seq, b_seq=b_seq)
    console.print()
    when = "" if fresh else f"saved {relative_ts(explanation.created_at)} · "
    console.print(f"[dim]{escape(when + _provenance_line(explanation))}. AI can be wrong — "
                  f"lw diff {escape(shell_word(row['name']))} --from {a_seq} --to {b_seq} "
                  "is the record.[/dim]")
    console.print(f"[dim]report: {_home_relative(page)}"
                  + ("" if open_report else f" · {escape(command)} --open") + "[/dim]")
    if not fresh:
        console.print(f"[dim]{escape(command)} --refresh asks again[/dim]")
    if open_report and not _open_in_browser(page):
        err_console.print("[yellow]could not find a browser to show it in; "
                          "open the file above yourself.[/yellow]")


def _explain_history(cfg: Config, db: Database, store: Store, row, settings: AISettings,
                     model: str | None, *, refresh: bool, limit: int, yes: bool, json_out: bool,
                     dry_run: bool) -> None:
    """``lw explain FN --all``: explain every step of a function's history that has no answer yet.

    Oldest first, so the history fills in the order it happened. Asks before
    making more than one request, because each one costs money on a paid
    service; ``--yes`` skips the question. A failure that the next request
    would repeat — a rejected key, an empty account — stops the run, while a
    rate limit on one step only skips that step, and running the same command
    again picks up exactly the steps still missing.
    """
    function_id = int(row["id"])
    versions = db.list_versions(function_id, limit + 1)          # newest first
    pairs = list(zip(versions[1:], versions[:-1], strict=True))   # (older, newer)
    todo = []
    for older, newer in reversed(pairs):
        record = load_record(store, dict(older), dict(newer))
        if refresh or record is None or record.explanation is None:
            todo.append((int(older["seq"]), int(newer["seq"])))
    if not pairs:
        _fail(f"{row['name']} has only one version, so there is no change to explain yet.")
    if not todo:
        console.print(f"[green]every step of {escape(row['name'])}'s last {len(pairs)} is already "
                      f"explained.[/green] [dim]lw report {escape(shell_word(row['name']))} shows "
                      "them together; --refresh asks again.[/dim]")
        return
    if dry_run:
        for a_seq, b_seq in todo:
            console.print(f"  would explain v{a_seq:04d} → v{b_seq:04d}")
        console.print(f"[dim]{len(todo)} request(s). Nothing was sent.[/dim]")
        return
    entry = _usable_model(cfg, settings, model, interactive=not json_out)
    if len(todo) > 1 and not yes:
        if not _can_ask():
            _fail(f"this would make {len(todo)} requests to {entry.label}. Add --yes to go ahead.")
        if not typer.confirm(f"  explain {len(todo)} steps with {entry.label}? "
                             f"That is {len(todo)} requests.", default=True):
            raise typer.Exit(1)

    include_vendor = True if cfg.report.include_vendor else None
    results: list[dict] = []
    failed = 0
    for a_seq, b_seq in todo:
        pair = _build_diff(db, store, cfg, row, a_seq, b_seq, include_vendor)
        outcome = _explain_pair(cfg, db, store, function_id, pair, settings, entry)
        label = f"v{a_seq:04d} → v{b_seq:04d}"
        if isinstance(outcome, AIError):
            failed += 1
            results.append({"from": a_seq, "to": b_seq, "error": outcome.as_dict()})
            if not json_out:
                console.print(f"  [yellow]✗[/yellow] {label}  [dim]{escape(str(outcome))}[/dim]")
            if outcome.kind in {"auth", "quota", "setup", "tls", "not-found"}:
                if not json_out:
                    _report_failure(outcome, f"lw explain {shell_word(row['name'])} --all")
                break
            continue
        results.append({"from": a_seq, "to": b_seq, **outcome.as_dict()})
        if not json_out:
            risk = f"  [dim]({outcome.risk} risk)[/dim]" if outcome.risk else ""
            console.print(f"  [green]✓[/green] {label}  {escape(outcome.headline)}{risk}")
    if json_out:
        console.print_json(json.dumps({"function": row["name"], "steps": results}))
    else:
        done = len(results) - failed
        console.print(f"\n[dim]explained {done} of {len(todo)} step{'s' if len(todo) != 1 else ''}"
                      + (f"; run the same command again for the {failed} that failed" if failed else "")
                      + f". lw report {escape(shell_word(row['name']))} shows the whole history.[/dim]")
    _refresh_archive_index(cfg, db)
    if failed:
        raise typer.Exit(1)


# ----------------------------------------------------------------- lw ai
ai_app = typer.Typer(rich_markup_mode="rich")
app.add_typer(ai_app, name="ai", rich_help_panel="Everyday", **for_group("ai"))


@ai_app.callback(invoke_without_command=True)
def _ai_main(ctx: typer.Context) -> None:
    """Bare ``lw ai``: what is set up, and what to type next — the AI counterpart of bare ``lw``."""
    if ctx.invoked_subcommand is None:
        _print_ai_status(_cfg())


def _print_ai_status(cfg: Config) -> None:
    """Show the models, the default, the switches and the next commands worth typing.

    Written for the two people who run it: someone who has never set AI up,
    who needs to learn what it does and that nothing is sent until they say so,
    and someone who has, who needs to see at a glance which model is in use,
    where its key comes from, and whether it will run on its own.
    """
    settings = AISettings.load(cfg.root)
    newest = None
    try:
        functions = _open_db(cfg).list_functions()
        newest = functions[0]["name"] if functions else None
    except Exception:                                 # noqa: BLE001 - a status must not fail
        pass
    target = shell_word(newest) if newest else "<function>"

    if settings.problem:
        console.print(f"[yellow]![/yellow] {escape(settings.problem)}\n"
                      "  [dim]`lw ai add` starts a fresh one and keeps the old file as "
                      "ai.json.broken[/dim]\n")

    if not settings.models:
        headline = "[yellow]off[/yellow]" if not settings.enabled else "[dim]not set up[/dim]"
        console.print(f"[bold]AI explanations[/bold]  {headline}\n")
        console.print(
            "  A model can read each change and explain it in plain English: what the\n"
            "  function now does differently, what could break, and what to do before\n"
            "  deploying — in the terminal and in every report. It works with Anthropic,\n"
            "  OpenAI, Azure OpenAI or a model on your own machine, and nothing is sent\n"
            "  anywhere until you set one up."
        )
        detected = settings.detected_entries()
        if detected:
            env = detected[0].key_env
            console.print(f"\n  [green]found ${env}[/green] in your environment — `lw explain {target}` "
                          f"can use it now, and `lw ai add {detected[0].provider}` saves it for the watcher")
        steps = [("lw ai add", "set one up — it takes a minute")]
        if not settings.enabled:
            steps.append(("lw ai on", "switch AI back on"))
    else:
        default = settings.default_entry()
        if not settings.enabled:
            state = "[yellow]off[/yellow] [dim]· nothing is sent anywhere until `lw ai on`[/dim]"
        elif settings.auto_explain:
            state = "[green]on[/green] [dim]· each new version is explained as it is archived[/dim]"
        else:
            state = "[green]on[/green] [dim]· only when you run lw explain[/dim]"
        console.print(f"[bold]AI explanations[/bold]  {state}\n")
        table = Table(box=None, header_style="bold", padding=(0, 2, 0, 2))
        table.add_column("")
        table.add_column("name")
        table.add_column("service")
        table.add_column("model")
        table.add_column("key")
        for entry in settings.models:
            is_default = default is not None and entry.name == default.name
            problem = entry.key_problem()
            key = f"[red]{escape(entry.key_source())}[/red]" if problem else escape(entry.key_source())
            where = entry.info.label
            if entry.provider in {"azure", "local"} and entry.endpoint:
                where += f" [dim]{escape(entry.endpoint)}[/dim]"
            table.add_row("[green]●[/green]" if is_default else "", escape(entry.name), where,
                          escape(entry.model) if entry.model != entry.name else "[dim]same[/dim]", key)
        console.print(table)
        console.print()
        sends = ("the changed lines of your own code, with credentials redacted"
                 if settings.send_code else "only the shape of each change — no code")
        console.print(f"  [dim]sends      {sends}[/dim]")
        console.print(f"  [dim]retries    up to {settings.max_retries} times on rate limits, "
                      "outages, timeouts and dropped connections[/dim]")
        if settings.path is not None:
            console.print(f"  [dim]saved in   {_home_relative(settings.path)} · readable only by you[/dim]")
        if default is not None and default.key_problem():
            console.print(f"\n  [red]![/red] {escape(default.name)}: {escape(default.key_problem() or '')}")
        steps = [(f"lw explain {target}", "explain the newest change")] if settings.enabled else [
            ("lw ai on", "switch AI back on")]
        steps.append(("lw ai add", "add another model"))
        if len(settings.models) > 1:
            steps.append(("lw ai use <name>", "switch the default"))
        steps.append(("lw ai settings", "choose when it runs and what is sent"))
    console.print()
    width = max(len(command) for command, _ in steps)
    for command, blurb in steps:
        console.print(f"  [bold]{escape(command):<{width}}[/bold]   [dim]{blurb}[/dim]")


def _choose(options: list[tuple[str, str]], prompt: str = "  choose", *, free_text: bool = False) -> str:
    """Offer a numbered list and return the value picked, by number or by typing it.

    ``options`` are ``(value, note)`` pairs and the first is the default, so
    Enter takes the recommendation. With ``free_text`` anything typed that is
    not a number is taken as the answer itself — how a model the list does not
    show is still chosen.
    """
    width = max(len(value) for value, _ in options)
    for index, (value, note) in enumerate(options, start=1):
        console.print(f"  [bold]{index:>2}[/bold]  {escape(value):<{width}}   [dim]{escape(note)}[/dim]")
    while True:
        answer = typer.prompt(prompt, default="1").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        matches = [value for value, _ in options if value.lower() == answer.lower()]
        if matches:
            return matches[0]
        if free_text and answer:
            return answer
        err_console.print(f"  [yellow]pick a number from 1 to {len(options)}[/yellow]")


def _model_choices(provider: str, listed: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The models to offer, recommended one first, from what the service listed or the built-in suggestions.

    A service's own list is preferred because it is never stale and only holds
    models the key can use; the suggestions in :data:`PROVIDERS` are the
    fallback when it cannot be asked. At most ten, since a menu of forty
    models helps nobody choose.
    """
    info = PROVIDERS[provider]
    suggested = dict(info.suggestions)
    if not listed:
        return list(info.suggestions)
    ids = [model_id for model_id, _ in listed]
    ordered = [m for m, _ in info.suggestions if m in ids] + [m for m in ids if m not in suggested]
    choices = []
    for model_id in ordered[:10]:
        note = suggested.get(model_id) or dict(listed).get(model_id, "")
        choices.append((model_id, note))
    return choices


def _add_model(
    cfg: Config,
    settings: AISettings,
    *,
    provider: str | None = None,
    model: str | None = None,
    key: str | None = None,
    key_env: str | None = None,
    endpoint: str | None = None,
    api_version: str | None = None,
    name: str | None = None,
    make_default: bool = False,
    auto: bool | None = None,
    test: bool = True,
) -> ModelEntry:
    """Set up one model — asking for whatever was not given — check it answers, and save it.

    The walk-through ``lw ai add`` and the "set one up now?" offer in ``lw
    explain`` share. At a terminal every missing piece is asked for, with the
    likely answer as the default: a key already in the environment or already
    saved for the same service, the models the key can actually use, the
    Ollama address. Without one, every missing piece that has no safe default
    is an error naming the option that supplies it.

    Nothing is saved until the model has answered one tiny request, unless
    ``test`` is off: a model saved with a mistyped key would otherwise fail
    for the first time in the background, where nobody is watching.
    """
    interactive = _can_ask()

    # -- which service ----------------------------------------------------
    if provider is None:
        if not interactive:
            _fail("say which service: `lw ai add anthropic`, `openai`, `azure` or `local`.")
        console.print("[bold]Which AI service should explain your changes?[/bold]")
        provider = _choose([(key_, f"{info.label} — {info.blurb}") for key_, info in PROVIDERS.items()])
    chosen = provider_key(provider)
    if chosen is None:
        _fail(f"{provider!r} is not a service this knows. Choose anthropic, openai, azure or local.")
    info = PROVIDERS[chosen]
    console.print(f"\n[bold]{info.label}[/bold]")

    # -- where it is ------------------------------------------------------
    endpoint_value = ""
    if chosen == "azure":
        raw = endpoint
        if not raw:
            if not interactive:
                _fail("Azure OpenAI needs --endpoint https://<resource>.openai.azure.com "
                      "and --model <deployment name>.")
            console.print("  [dim]paste the endpoint, or a whole Target URI, from your resource's "
                          "Keys and Endpoint page[/dim]")
            raw = typer.prompt("  endpoint")
        base, deployment, version = parse_azure_endpoint(raw)
        if not base or "." not in base:
            _fail(f"{raw!r} does not look like an Azure endpoint, which reads like "
                  "https://<resource>.openai.azure.com.")
        endpoint_value = base
        model = model or deployment or None
        api_version = api_version or version or None
    elif chosen == "local":
        raw = endpoint
        if not raw and interactive:
            console.print("  [dim]Ollama serves on http://localhost:11434/v1 and LM Studio on "
                          "http://localhost:1234/v1[/dim]")
            raw = typer.prompt("  server URL", default="http://localhost:11434/v1")
        endpoint_value = normalize_local_url(raw or "")
    elif endpoint:
        endpoint_value = endpoint.strip().rstrip("/")

    # -- the key ----------------------------------------------------------
    api_key = (key or "").strip().strip("'\"")
    env_name = (key_env or "").strip().lstrip("$")
    if env_name and not os.environ.get(env_name):
        err_console.print(f"  [yellow]${env_name} is not set in this shell[/yellow] [dim]— saved anyway; "
                          "it has to be set wherever lw runs, the background watcher included[/dim]")
    if not api_key and not env_name:
        same = next((m for m in settings.models if m.provider == chosen and m.api_key
                     and m.endpoint == endpoint_value), None)
        in_env = next((e for e in info.key_envs if os.environ.get(e)), None)
        if interactive:
            if same is not None and typer.confirm(f"  use the key already saved for {same.name}?",
                                                  default=True):
                api_key = same.api_key
            elif in_env and typer.confirm(f"  use the key in ${in_env}?", default=True):
                api_key = os.environ[in_env]
            elif info.needs_key:
                if info.key_url:
                    console.print(f"  [dim]get a key at {escape(info.key_url)}[/dim]")
                while not api_key:
                    api_key = typer.prompt("  API key (typing is hidden)", hide_input=True).strip()
            else:
                api_key = typer.prompt("  API key, if your server needs one (Enter for none)",
                                       hide_input=True, default="", show_default=False).strip()
        elif same is not None:
            api_key = same.api_key
        elif in_env:
            api_key = os.environ[in_env]
            console.print(f"  [dim]using the key in ${in_env}[/dim]")
        elif info.needs_key:
            _fail(f"{info.label} needs an API key: `lw ai add {chosen} --key <key>`, or "
                  f"`--key-env VARIABLE` to read it from the environment.")

    # -- which model ------------------------------------------------------
    probe = ModelEntry(name="probe", provider=chosen, model=model or info.default_model,
                       api_key=api_key, key_env=env_name, endpoint=endpoint_value,
                       api_version=api_version or "")
    if not model:
        if chosen == "azure":
            if not interactive:
                _fail("Azure OpenAI needs the deployment name: --model <deployment>.")
            console.print("  [dim]the deployment name is under Azure AI Foundry → Deployments[/dim]")
            model = typer.prompt("  deployment name").strip()
        else:
            listed: list[tuple[str, str]] = []
            try:
                with err_console.status("asking which models are available…"):
                    listed = list_models(probe, timeout=15)
            except AIError as error:
                if error.kind in {"auth", "network", "tls"} and chosen != "local":
                    err_console.print(f"  [yellow]{escape(str(error))}[/yellow]")
            choices = _model_choices(chosen, listed)
            if not choices:
                if not interactive:
                    _fail("say which model: --model <name>. `ollama list` shows what Ollama has.")
                model = typer.prompt("  model name (`ollama list` shows what you have)").strip()
            elif interactive:
                console.print("  [bold]Which model?[/bold] [dim](or type any model id)[/dim]")
                model = _choose(choices, "  model", free_text=True)
            else:
                model = choices[0][0]
    model = model.strip()
    entry = ModelEntry(name=(name or "").strip() or settings.unique_name(model, chosen),
                       provider=chosen, model=model, api_key=api_key, key_env=env_name,
                       endpoint=endpoint_value, api_version=(api_version or "").strip())

    # -- does it answer ---------------------------------------------------
    if test:
        try:
            with err_console.status(f"checking {entry.label} answers…"):
                reply = ping(entry, timeout=300.0 if chosen == "local" else 60.0)
        except AIError as error:
            if error.kind in {"rate-limit", "overloaded"}:
                # Being told to slow down proves the key and the model were
                # accepted — a service only rate-limits a request it would
                # otherwise have served — so this is a busy service, not a
                # wrong setup, and refusing to save would be refusing a
                # working model.
                console.print(f"  [green]✓[/green] {escape(entry.label)} accepted the key "
                              f"[dim](it is busy right now: {escape(error.title)})[/dim]")
            else:
                err_console.print(f"  [red]✗[/red] {escape(str(error))}")
                if error.hint:
                    err_console.print(f"    [dim]what to do:[/dim] {escape(error.hint)}")
                if not (interactive and typer.confirm("  save it anyway?", default=False)):
                    _fail("nothing was saved. Fix the above and run `lw ai add` again, or add "
                          "--no-test to save it without checking.")
        else:
            console.print(f"  [green]✓[/green] {escape(entry.label)} answered in {reply.seconds:.1f}s")

    # -- when to use it ---------------------------------------------------
    first = not settings.models
    if auto is None and interactive and first:
        where = "your own machine" if chosen == "local" else info.label
        console.print(f"\n  [dim]Explaining automatically sends each new version's changed code to "
                      f"{escape(where)}, with credentials redacted.[/dim]")
        auto = typer.confirm("  explain each new version automatically as it is archived?", default=True)
    if auto is not None:
        settings.auto_explain = auto
    was_off = not settings.enabled
    settings.enabled = True
    replaced = settings.add(entry, make_default=make_default)
    try:
        path = settings.save()
    except SettingsError as exc:
        _fail(f"{exc}. Check the archive folder is writable (`lw doctor`).")

    is_default = settings.default == entry.name
    verb = "updated" if replaced else "saved"
    console.print(f"\n  [green]✓[/green] {verb} [bold]{escape(entry.name)}[/bold] "
                  f"({escape(info.label)}){' — your default model' if is_default else ''}")
    if api_key:
        console.print(f"    [dim]key saved in {_home_relative(path)}, readable only by you[/dim]")
    elif env_name:
        console.print(f"    [dim]key read from ${env_name} whenever it is needed[/dim]")
    if was_off:
        console.print("    [dim]AI was switched off; adding a model switched it back on[/dim]")
    if settings.auto_explain:
        console.print("    [dim]new versions are explained as they are archived · "
                      "lw ai settings --no-auto stops that[/dim]")
    else:
        console.print("    [dim]explanations are written when you run lw explain · "
                      "lw ai settings --auto writes them for every new version[/dim]")
    return entry


@ai_app.command("add", **for_command("ai add"))
def ai_add(
    provider: Optional[str] = typer.Argument(
        None, help="anthropic, openai, azure or local. Leave it out to be asked."
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m",
        help="The model id; for Azure, the deployment name. Leave it out to pick from a list.",
    ),
    key: Optional[str] = typer.Option(
        None, "--key", help="The API key. Leave it out to be asked without it showing on screen."
    ),
    key_env: Optional[str] = typer.Option(
        None, "--key-env", help="Read the key from this environment variable instead of saving it."
    ),
    endpoint: Optional[str] = typer.Option(
        None, "--endpoint",
        help="Azure: the resource URL or a Target URI. Local: the server URL. "
             "Others: a gateway, if you use one.",
    ),
    api_version: Optional[str] = typer.Option(
        None, "--api-version", help="Azure only: an API version to pin."
    ),
    name: Optional[str] = typer.Option(None, "--name", help="What to call it (default: the model id)."),
    default: bool = typer.Option(False, "--default", help="Make it the default even if another model is."),
    auto: Optional[bool] = typer.Option(
        None, "--auto/--no-auto", help="Explain new versions automatically as they are archived."
    ),
    test: bool = typer.Option(True, "--test/--no-test", help="Check it answers before saving it."),
) -> None:
    """Add a model through :func:`_add_model`, then name the commands worth typing next."""
    cfg = _cfg()
    settings = _ai_settings(cfg)
    entry = _add_model(cfg, settings, provider=provider, model=model, key=key, key_env=key_env,
                       endpoint=endpoint, api_version=api_version, name=name, make_default=default,
                       auto=auto, test=test)
    newest = None
    try:
        functions = _open_db(cfg).list_functions()
        newest = functions[0]["name"] if functions else None
    except Exception:                                 # noqa: BLE001 - only picking an example
        pass
    console.print()
    target = shell_word(newest) if newest else "<function>"
    console.print(f"  [bold]lw explain {escape(target)}[/bold]   [dim]explain the newest change now[/dim]")
    console.print(f"  [bold]{'lw ai':<{len('lw explain ' + target)}}[/bold]   "
                  f"[dim]see and change your AI setup[/dim]")
    if entry.provider == "local":
        console.print("\n[dim]A local model sees a smaller slice of each change; "
                      "`lw ai settings --max-prompt-kb 32` gives it more if it has the context for it.[/dim]")


def _find_model(settings: AISettings, name: str) -> ModelEntry:
    """A saved model by name or unique part of one, or exit listing the saved names."""
    entry = settings.find(name)
    if entry is None:
        saved = ", ".join(m.name for m in settings.models)
        _fail(f"no saved model matches {name!r}. " + (f"Saved: {saved}." if saved else
              "None is saved yet; `lw ai add` sets one up."))
    return entry


@ai_app.command("remove", **for_command("ai remove"))
def ai_remove(
    name: str = typer.Argument(
        ..., help="The model's name, as `lw ai` lists it.", autocompletion=_complete_model
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Forget a saved model and its key, handing the default on if it held it."""
    cfg = _cfg()
    settings = _ai_settings(cfg)
    entry = _find_model(settings, name)
    if not yes:
        if not _can_ask():
            _fail(f"add --yes to remove {entry.name} without being asked.")
        if not typer.confirm(f"remove {entry.label} and any key saved with it?", default=False):
            raise typer.Abort()
    was_default = settings.default_entry() is not None and settings.default_entry().name == entry.name  # type: ignore[union-attr]
    now_default = settings.remove(entry)
    try:
        settings.save()
    except SettingsError as exc:
        _fail(str(exc))
    console.print(f"[green]removed[/green] {escape(entry.name)}")
    if now_default is None:
        console.print("[dim]no models are left, so nothing will be explained until `lw ai add`[/dim]")
    elif was_default:
        console.print(f"[dim]{escape(now_default.name)} is the default now · "
                      "lw ai use <name> picks another[/dim]")


@ai_app.command("use", **for_command("ai use"))
def ai_use(
    name: str = typer.Argument(
        ..., help="The model's name, as `lw ai` lists it.", autocompletion=_complete_model
    ),
) -> None:
    """Make a saved model the one ``lw explain`` and the watcher use."""
    cfg = _cfg()
    settings = _ai_settings(cfg)
    entry = _find_model(settings, name)
    settings.default = entry.name
    try:
        settings.save()
    except SettingsError as exc:
        _fail(str(exc))
    console.print(f"[green]{escape(entry.name)}[/green] ({escape(entry.info.label)}) is the default now "
                  "[dim]· lw explain and the watcher use it from the next request[/dim]")
    if entry.key_problem():
        err_console.print(f"[yellow]but:[/yellow] {escape(entry.key_problem() or '')}")


@ai_app.command("test", **for_command("ai test"))
def ai_test(
    name: Optional[str] = typer.Argument(
        None, help="The model to test (default: the default model).", autocompletion=_complete_model
    ),
    every: bool = typer.Option(False, "--all", help="Test every saved model."),
) -> None:
    """Send each chosen model the smallest request there is, and say how it went. Exits 1 on any failure."""
    cfg = _cfg()
    settings = _ai_settings(cfg)
    if every:
        entries = list(settings.models)
    elif name:
        entries = [_find_model(settings, name)]
    else:
        found = settings.resolve()
        entries = [found] if found else []
    if not entries:
        _fail("no AI model is set up yet. `lw ai add` sets one up.")
    failed = 0
    for entry in entries:
        try:
            with err_console.status(f"asking {entry.label}…"):
                reply = ping(entry, timeout=300.0 if entry.provider == "local" else 60.0,
                             retries=settings.max_retries)
        except AIError as error:
            failed += 1
            console.print(f"[red]✗[/red] {escape(entry.name)}  {escape(str(error))}")
            if error.hint:
                console.print(f"  [dim]what to do: {escape(error.hint)}[/dim]")
            continue
        console.print(f"[green]✓[/green] {escape(entry.name)}  [dim]{escape(entry.label)} answered "
                      f"in {reply.seconds:.1f}s[/dim]")
    if failed:
        raise typer.Exit(1)


def _switch_ai(enabled: bool) -> None:
    """Flip the master switch and say what it means now."""
    cfg = _cfg()
    settings = _ai_settings(cfg)
    settings.enabled = enabled
    try:
        settings.save()
    except SettingsError as exc:
        _fail(str(exc))
    if enabled:
        console.print("[green]AI explanations are on.[/green]"
                      + ("" if settings.models else " [dim]No model is set up yet — `lw ai add`.[/dim]"))
    else:
        console.print("[yellow]AI explanations are off.[/yellow] [dim]Nothing is sent anywhere and reports "
                      "stop mentioning AI. Your models and keys are kept; `lw ai on` turns it back on.[/dim]")


@ai_app.command("on", **for_command("ai on"))
def ai_on() -> None:
    """Switch AI explanations back on."""
    _switch_ai(True)


@ai_app.command("off", **for_command("ai off"))
def ai_off() -> None:
    """Switch AI explanations off, keeping every model and key."""
    _switch_ai(False)


@ai_app.command("settings", **for_command("ai settings"))
def ai_settings(
    auto: Optional[bool] = typer.Option(
        None, "--auto/--no-auto", help="Explain each new version as it is archived, or only when asked."
    ),
    send_code: Optional[bool] = typer.Option(
        None, "--send-code/--no-send-code",
        help="Send the changed lines of your code, or only the shape of each change.",
    ),
    retries: Optional[int] = typer.Option(
        None, "--retries", min=0, max=20,
        help="How many times to retry a request that failed for a passing reason.",
    ),
    timeout: Optional[int] = typer.Option(
        None, "--timeout", min=0, help="Seconds to wait for a silent service. 0 means the service's default."
    ),
    max_prompt_kb: Optional[int] = typer.Option(
        None, "--max-prompt-kb", min=0, help="Most of a change to send, in KB. 0 means the service's default."
    ),
) -> None:
    """Show the switches, and change the ones given. Every row says how to flip it."""
    cfg = _cfg()
    settings = _ai_settings(cfg)
    changes = {"auto_explain": auto, "send_code": send_code, "max_retries": retries,
               "timeout_seconds": timeout, "max_prompt_kb": max_prompt_kb}
    changed = {k: v for k, v in changes.items() if v is not None}
    if changed:
        for attribute, value in changed.items():
            setattr(settings, attribute, value)
        try:
            settings.save()
        except SettingsError as exc:
            _fail(str(exc))
        console.print("[green]saved.[/green] [dim]The running watcher uses it from the next version; "
                      "no restart needed.[/dim]\n")

    default_timeout = "the service's default (240s, or 600s for a local model)"
    default_size = "the service's default (about 150 KB, 16 KB for a local model)"
    rows = [
        ("AI explanations", "on" if settings.enabled else "off",
         "master switch" if settings.enabled else "nothing is sent anywhere",
         "lw ai off" if settings.enabled else "lw ai on"),
        ("automatic", "on" if settings.auto_explain else "off",
         "each new version is explained as it is archived" if settings.auto_explain
         else "only when you run lw explain",
         "lw ai settings --no-auto" if settings.auto_explain else "lw ai settings --auto"),
        ("code sent", "yes" if settings.send_code else "no",
         "changed lines of your own code, credentials redacted" if settings.send_code
         else "only files, dependencies, env vars and services",
         "lw ai settings --no-send-code" if settings.send_code else "lw ai settings --send-code"),
        ("retries", str(settings.max_retries), "rate limits, outages, timeouts, dropped connections",
         "lw ai settings --retries 6"),
        ("timeout", f"{settings.timeout_seconds}s" if settings.timeout_seconds else "default",
         default_timeout if not settings.timeout_seconds else "per request, while nothing arrives",
         "lw ai settings --timeout 600"),
        ("prompt size", f"{settings.max_prompt_kb} KB" if settings.max_prompt_kb else "default",
         default_size if not settings.max_prompt_kb else "the most of one change that is sent",
         "lw ai settings --max-prompt-kb 60"),
    ]
    table = Table(box=None, header_style="bold", padding=(0, 2, 0, 0))
    table.add_column("setting")
    table.add_column("now")
    table.add_column("what it means", style="dim")
    table.add_column("change it", style="dim")
    for setting, value, meaning, command in rows:
        style = "green" if value in {"on", "yes"} else ("yellow" if value in {"off", "no"} else "")
        table.add_row(setting, f"[{style}]{value}[/{style}]" if style else value, meaning, command)
    console.print(table)


# ---------------------------------------------------------------- editing
@app.command(rich_help_panel="Housekeeping", **for_command("rename"))
def rename(
    current: str = typer.Argument(
        ..., help="The function as it is recorded now.", autocompletion=_complete_function
    ),
    new_name: str = typer.Argument(..., help="What it should be called."),
    alias: Optional[str] = typer.Option(
        None, "--alias", help="Also remember this filename fragment as belonging to the function."
    ),
) -> None:
    """Fix a misidentified function name (and optionally remember the mapping)."""
    cfg = _cfg()
    db = _open_db(cfg)
    row = _resolve_function(db, current)
    existing = db.get_function_by_name(new_name)
    if existing and int(existing["id"]) != int(row["id"]):
        _fail(
            f"{new_name!r} already exists. Use `lw merge {current!r} {new_name!r}` "
            "to combine them."
        )

    store = Store(cfg)
    old_slug = row["slug"]
    new_slug = slugify(new_name)
    old_dir = store.function_dir(old_slug)
    new_dir = store.function_dir(new_slug)
    if old_slug != new_slug and old_dir.exists():
        if new_dir.exists():
            _fail(f"{new_dir} already exists on disk; move it aside first")
        old_dir.rename(new_dir)
        # back-compat: re-point the index by rebuilding each path from where the
        # directory now is, rather than by patching the stored string. A
        # `functions/<old slug>/` replacement silently matched nothing in an
        # archive an older release wrote on Windows, where the separator is a
        # backslash — and a version left pointing at the pre-rename path reads
        # as missing from then on, which is a diff with no lines in it.
        new_versions = store.versions_dir(new_slug)
        for version in db.list_versions(int(row["id"])):
            updated = store.relative(new_versions / _stored_dirname(version["dir"]))
            db.conn.execute("UPDATE versions SET dir = ? WHERE id = ?", (updated, version["id"]))
    if old_slug != new_slug:
        # The mirror lives outside the function directory, so it does not move
        # with it — and its folder name is what an editor puts in the sidebar.
        old_repo = store.repo_dir(old_slug)
        new_repo = cfg.repos_dir / new_slug
        if old_repo.exists() and not new_repo.exists():
            try:
                old_repo.rename(new_repo)
            except OSError as exc:
                err_console.print(f"[yellow]could not move {old_repo} to {new_repo}: {exc}[/yellow]")

    # Disk first, then the index: the manifests are what `lw reindex` rebuilds
    # from, so a rename that reaches only index.db is one a rebuild undoes.
    identity = {"name": new_name, "slug": new_slug}
    for version in db.list_versions(int(row["id"])):
        store.patch_manifest(store.resolve_version_dir(version["dir"]), function=identity)
    if alias:
        store.write_aliases(new_slug, [*db.aliases_for(int(row["id"])), (alias, False)])

    db.rename_function(int(row["id"]), new_name, new_slug)
    if alias:
        db.add_alias(int(row["id"]), alias)
    _refresh_archive_index(cfg, db)
    console.print(f"[green]renamed[/green] {row['name']} → {new_name}")
    if alias:
        console.print(f"[dim]future downloads containing {alias!r} will map here automatically[/dim]")


@app.command(rich_help_panel="Housekeeping", **for_command("merge"))
def merge(
    source: str = typer.Argument(
        ..., help="Function whose versions should move.", autocompletion=_complete_function
    ),
    target: str = typer.Argument(
        ..., help="Function they should move into.", autocompletion=_complete_function
    ),
) -> None:
    """Combine two entries that are really the same Lambda, renumbering by time."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    src = _resolve_function(db, source)
    dst = _resolve_function(db, target)
    if int(src["id"]) == int(dst["id"]):
        _fail("source and target are the same function")

    moving = db.list_versions(int(src["id"]))
    if not moving:
        db.delete_function(int(src["id"]))
        console.print("[green]merged[/green] (the source had no versions)")
        return

    everything = list(db.list_versions(int(dst["id"]))) + list(moving)
    everything.sort(key=lambda v: (v["ingested_at"], v["seq"]))
    dst_versions = store.versions_dir(dst["slug"])
    dst_versions.mkdir(parents=True, exist_ok=True)
    identity = {"name": dst["name"], "slug": dst["slug"]}

    # Disk first, in three steps, and the index last to agree with it. The
    # manifests are what `lw reindex` rebuilds from, so they are told their new
    # function and number before anything moves: a merge interrupted part-way
    # leaves an archive a rebuild reads correctly, whatever the directories are
    # called at that moment.
    placed: list[tuple[int, Any, Path | None]] = []
    for new_seq, version in enumerate(everything, start=1):
        current = store.resolve_version_dir(version["dir"])
        if current.exists():
            store.patch_manifest(current, function=identity, seq=new_seq)
            placed.append((new_seq, version, current))
        else:
            placed.append((new_seq, version, None))

    # Then every directory is renamed to its new number, through a temporary name
    # because the new numbers overlap the old ones. Moving them by their old names
    # is what this used to do, and it deleted data: a version whose directory name
    # was already taken in the target — same number, same content, `0001-7fc98e0e`
    # — was left behind, and the source directory was then removed with it inside.
    staged: list[tuple[int, Any, Path | None]] = []
    for new_seq, version, current in placed:
        if current is None or not current.exists():
            staged.append((new_seq, version, None))
            continue
        temporary = dst_versions / f".merging-{new_seq:04d}-{current.name}"
        shutil.move(str(current), str(temporary))
        staged.append((new_seq, version, temporary))
    final_dirs: dict[int, Path] = {}
    for new_seq, version, temporary in staged:
        if temporary is None:
            continue
        final = dst_versions / store.version_dirname(new_seq, version["tree_hash"])
        if final.exists():
            # Only a directory the index never knew about can be sitting here.
            # Leave ours on its temporary name rather than guess which to keep.
            err_console.print(f"[yellow]warning:[/yellow] {final} is in the way; "
                              f"left v{new_seq:04d} at {temporary.name}")
            final = temporary
        else:
            temporary.rename(final)
        final_dirs[int(version["id"])] = final

    with db.transaction():
        # Park every version on a temporary sequence to dodge the UNIQUE index.
        for offset, version in enumerate(everything, start=1):
            db.conn.execute(
                "UPDATE versions SET function_id = ?, seq = ? WHERE id = ?",
                (int(dst["id"]), -offset, version["id"]),
            )
        for new_seq, version in enumerate(everything, start=1):
            moved_to = final_dirs.get(int(version["id"]))
            if moved_to is None:
                db.conn.execute("UPDATE versions SET seq = ? WHERE id = ?", (new_seq, version["id"]))
            else:
                db.conn.execute(
                    "UPDATE versions SET seq = ?, dir = ? WHERE id = ?",
                    (new_seq, store.relative(moved_to), version["id"]),
                )
        db.conn.execute("UPDATE OR IGNORE aliases SET function_id = ? WHERE function_id = ?",
                        (int(dst["id"]), int(src["id"])))
        db.conn.execute("DELETE FROM functions WHERE id = ?", (int(src["id"]),))
    store.write_aliases(dst["slug"], db.aliases_for(int(dst["id"])))

    # Everything worth keeping has moved out of the source's directory by now.
    rmtree(store.function_dir(src["slug"]))
    # The target's mirror no longer matches the renumbered versions, but the
    # source's belongs to a function that no longer exists at all.
    rmtree(store.repo_dir(src["slug"]))

    _refresh_archive_index(cfg, db)
    console.print(
        f"[green]merged[/green] {src['name']} into {dst['name']} "
        f"({len(everything)} versions, renumbered by archive time)"
    )


@app.command(rich_help_panel="Housekeeping", **for_command("label"))
def label(
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
    version: str = typer.Argument(..., help=VERSION_HELP),
    text: str = typer.Argument(..., help="Note to attach, e.g. 'prod deploy 2026-03-01'."),
) -> None:
    """Annotate a version so you can recognise it later."""
    cfg = _cfg()
    db = _open_db(cfg)
    row = _resolve_function(db, function)
    seq = _resolve_seq(db, int(row["id"]), version)
    version_row = _version_or_fail(db, int(row["id"]), seq)
    # The manifest too, or the next `lw reindex` quietly drops the label.
    store = Store(cfg)
    store.patch_manifest(store.resolve_version_dir(version_row["dir"]), label=text or None)
    db.set_version_label(int(version_row["id"]), text or None)
    _refresh_archive_index(cfg, db)
    console.print(f"[green]labelled[/green] {row['name']} v{seq:04d}: {text}")


@app.command("rm", rich_help_panel="Housekeeping", **for_command("rm"))
def remove(
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Delete a function and everything archived for it."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    count = len(db.list_versions(int(row["id"])))
    if not yes:
        confirm = typer.confirm(
            f"Delete {row['name']} and all {count} archived version(s)? This cannot be undone"
        )
        if not confirm:
            raise typer.Abort()
    rmtree(store.function_dir(row["slug"]))
    # The mirror is a second full copy of the code; leaving it behind would make
    # "deleted" a lie. It needs the read-only-tolerant rmtree more than anything
    # else in the store does: git's object files are read-only by design, and on
    # Windows shutil.rmtree walks straight past them and reports success.
    rmtree(store.repo_dir(row["slug"]))
    db.delete_function(int(row["id"]))
    _refresh_archive_index(cfg, db)
    console.print(f"[green]deleted[/green] {row['name']}")


# --------------------------------------------------------------- plumbing
@app.command(rich_help_panel="Reading the archive", **for_command("export"))
def export(
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
    version: Optional[str] = typer.Argument(None, help=LATEST_VERSION_HELP),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Where to write it (default: the current folder)."
    ),
    as_zip: bool = typer.Option(True, "--zip/--tree", help="Write a zip, or copy the folder."),
) -> None:
    """Get a version back out — a deployable zip or a plain folder."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    seq = _resolve_seq(db, int(row["id"]), version)
    version_row = _version_or_fail(db, int(row["id"]), seq)
    code_dir = store.resolve_version_dir(version_row["dir"]) / "code"
    if not code_dir.exists():
        _fail(f"the extracted code for v{seq:04d} is missing at {code_dir}")

    if as_zip:
        default_name = f"{slugify(row['name'])}-v{seq:04d}.zip"
        target = Path(output).expanduser() if output else Path.cwd() / default_name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(code_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(code_dir).as_posix())
        console.print(f"[green]wrote[/green] {target} [dim]({human_size(target.stat().st_size)})[/dim]")
    else:
        target = Path(output).expanduser() if output else Path.cwd() / f"{slugify(row['name'])}-v{seq:04d}"
        if target.exists():
            _fail(f"{target} already exists")
        shutil.copytree(code_dir, target)
        console.print(f"[green]copied[/green] {target}")


@app.command("open", rich_help_panel="Reading the archive", **for_command("open"))
def open_in_editor(
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
    version: Optional[str] = typer.Argument(
        None, help="Open this version's files alone instead of the whole repo."
    ),
    editor: Optional[str] = typer.Option(
        None, "--editor", "-e",
        help="Editor command to launch (default: `editor` in the config, else VS Code and friends on PATH).",
    ),
    reuse: bool = typer.Option(
        False, "--reuse", "-r", help="Reuse the editor's current window instead of opening a new one."
    ),
    print_only: bool = typer.Option(
        False, "--print", help="Print the folder that would be opened and launch nothing."
    ),
) -> None:
    """Open a function's archived code in your editor.

    With no version, this opens the git mirror: a real working tree holding the
    latest version, with every earlier one a commit tagged `v0001`, `v0002`, …
    so the editor's own history, blame and diff views cover the whole archive.
    Name a version and you get that version's files on their own instead.
    """
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)

    note = ""
    if version is not None:
        seq = _resolve_seq(db, int(row["id"]), version)
        target = store.resolve_version_dir(_version_or_fail(db, int(row["id"]), seq)["dir"]) / "code"
        if not target.exists():
            _fail(f"the extracted code for v{seq:04d} is missing at {target}")
        subtitle = f"v{seq:04d} only — no history, just the files"
    else:
        target = store.repo_dir(row["slug"])
        if (target / ".git").is_dir():
            seqs = [int(v["seq"]) for v in db.list_versions(int(row["id"]))]
            subtitle = (
                f"{len(seqs)} version(s), tagged v{min(seqs):04d}…v{max(seqs):04d}; "
                f"the working tree is v{max(seqs):04d}"
                if seqs else "no versions archived yet"
            )
        else:
            # No mirror to open, but the request was to look at the code, and
            # the newest version is the closest thing to what was asked for.
            seq = _resolve_seq(db, int(row["id"]), "latest")
            target = store.resolve_version_dir(_version_or_fail(db, int(row["id"]), seq)["dir"]) / "code"
            subtitle = f"v{seq:04d} only — no history, just the files"
            note = (
                f"no git mirror for {row['name']} yet. Set `git_mirror.enabled: true` in "
                f"{_CONFIG_PATH or default_config_path()} and re-ingest to get one."
            )

    if note:
        err_console.print(f"[yellow]{note}[/yellow]")
    if print_only:
        print(target)
        return

    argv = _resolve_editor(cfg, editor)
    _launch_editor(argv, target, reuse)
    console.print(f"[green]opened[/green] {row['name']} in {Path(argv[0]).stem} [dim]({subtitle})[/dim]")
    console.print(f"[dim]{target}[/dim]")


@app.command(rich_help_panel="Housekeeping", **for_command("path"))
def path(
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
    version: Optional[str] = typer.Argument(
        None, help="Which version: 7, v7, latest or first. Leave it out for the function's own folder."
    ),
    repo: bool = typer.Option(
        False, "--repo", "--git", help="Print the function's git repository folder instead."
    ),
    open_it: bool = typer.Option(False, "--open", help="Open it in the file manager."),
) -> None:
    """Print where something lives on disk (handy for `cd $(...)`)."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    if repo:
        target = store.repo_dir(row["slug"])
    elif version is None:
        target = store.function_dir(row["slug"])
    else:
        seq = _resolve_seq(db, int(row["id"]), version)
        version_row = _version_or_fail(db, int(row["id"]), seq)
        target = store.resolve_version_dir(version_row["dir"]) / "code"
    print(target)
    if open_it:
        _open_path(target)


#: git subcommands whose output is a patch or a changed-file list, and so is a
#: direct answer to the question `lw diff` also answers.
_PATCH_SUBCOMMANDS = {"diff", "show", "diff-tree", "whatchanged"}
#: Flags that make any subcommand render one — `log --stat` included.
_PATCH_FLAGS = {
    "-p", "--patch", "--stat", "--numstat", "--shortstat", "--name-only", "--name-status",
}


def _vendor_policy_note(cfg: Config, slug: str, args: list[str]) -> str | None:
    """The line that reconciles ``lw git my-fn diff`` with ``lw diff my-fn``, or None.

    The two commands answer the same question under opposite vendor policies —
    ``diff.ignore_vendor`` hides vendored files, ``git_mirror.include_vendor``
    keeps them — so the same two versions come back as ``1 modified`` from one
    and ``3 files changed`` from the other. Both defaults are right on their own
    and neither answer is wrong, which is exactly why the discrepancy is
    expensive to meet cold: there is nothing to find by reading harder. So
    whichever command the reader typed says the other exists.

    None when there is nothing to reconcile — the two settings agree, or the
    subcommand renders no patch, as ``lw git my-fn log --oneline`` does not.
    """
    if not (cfg.diff.ignore_vendor and cfg.git_mirror.include_vendor):
        return None
    subcommand = next((a for a in args if not a.startswith("-")), None)
    if subcommand not in _PATCH_SUBCOMMANDS and not _PATCH_FLAGS.intersection(args):
        return None
    return (
        f"note: the mirror keeps vendored files. `lw diff {slug}` hides them and "
        f"reports the dependency bumps instead."
    )


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    rich_help_panel="Reading the archive",
    **for_command("git"),
)
def git(
    ctx: typer.Context,
    function: str = typer.Argument(..., help=FUNCTION_HELP, autocompletion=_complete_function),
) -> None:
    """Run git against the per-function mirror repository."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    repo = store.repo_dir(row["slug"])
    if not (repo / ".git").exists():
        _fail(
            f"no git mirror for {row['name']} at {repo}. "
            "Enable git_mirror in the config and re-ingest, or use `lw diff`."
        )
    args = list(ctx.args) or ["log", "--oneline", "--decorate", "-20"]
    note = _vendor_policy_note(cfg, row["slug"], args)
    if note:
        # stderr, so `lw git my-fn diff > patch.diff` still writes a clean patch.
        err_console.print(note, style="dim")
    raise typer.Exit(git_passthrough(repo, args))


@app.command(rich_help_panel="Reading the archive", **for_command("search"))
def search(
    term: str = typer.Argument(..., help="Filename fragment or package name."),
    kind: str = typer.Option(
        "all", "--kind", "-k", help="What to search: all, files, or deps (package names)."
    ),
) -> None:
    """Search across everything archived."""
    cfg = _cfg()
    db = _open_db(cfg)
    if kind in {"all", "files"}:
        rows = db.search_files(term)
        if rows:
            table = Table(title="Files", title_justify="left", box=None, header_style="bold",
                          padding=(0, 2, 0, 0))
            table.add_column("function")
            table.add_column("version")
            table.add_column("path")
            table.add_column("size", justify="right", style="dim")
            for row in rows[:60]:
                table.add_row(row["function_name"], f"v{int(row['seq']):04d}", row["path"],
                              human_size(row["size"]))
            console.print(table)
    if kind in {"all", "deps"}:
        rows = db.search_deps(term)
        if rows:
            table = Table(title="Dependencies", title_justify="left", box=None, header_style="bold",
                          padding=(0, 2, 0, 0))
            table.add_column("function")
            table.add_column("version")
            table.add_column("package")
            table.add_column("version", style="dim")
            for row in rows[:60]:
                table.add_row(row["function_name"], f"v{int(row['seq']):04d}", row["name"],
                              row["version"] or "-")
            console.print(table)


@app.command("logs", rich_help_panel="Housekeeping", **for_command("logs"))
def show_logs(
    lines: int = typer.Option(40, "--lines", "-n", help="How many lines from the end to show."),
    service: bool = typer.Option(
        False, "--service", help="The service manager's own output, rather than the watcher's."
    ),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing as new lines arrive."),
) -> None:
    """Show the watcher's log file — what it saw, and what it made of it.

    Distinct from `lw log`, which reads the archive's own record of what was
    ingested. This is the file the watcher writes as it runs, and it is the only
    place that answers "did it even notice my download?". Both existed before;
    neither was reachable without knowing the path by heart.
    """
    cfg = _cfg()
    path = cfg.log_dir / ("service.log" if service else "watcher.log")
    if not path.exists():
        other = "watcher" if service else "service"
        _fail(f"no log at {path} yet. Start the watcher with `lw start`, "
              f"or try `lw logs --{other}`." if not service else
              f"no log at {path} yet. The service writes it once `lw start` has run.")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _fail(f"could not read {path}: {exc}")

    tail = text.splitlines()[-lines:] if lines > 0 else text.splitlines()
    if not tail:
        console.print(f"[dim]{_home_relative(path)} is empty — the watcher has written nothing "
                      "yet. `lw doctor` says whether it is running.[/dim]")
        return
    console.print(f"[dim]{_home_relative(path)}[/dim]\n")
    for line in tail:
        console.print(line, highlight=False, markup=False, soft_wrap=True)

    if not follow:
        return
    # Reopened rather than held, so a rotation mid-follow is picked up instead of
    # leaving us reading a file that nothing writes to any more.
    console.print("\n[dim]following; Ctrl-C to stop.[/dim]")
    position = path.stat().st_size
    try:
        while True:
            time.sleep(0.5)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < position:      # rotated out from under us
                position = 0
            if size == position:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                fresh = handle.read()
                position = handle.tell()
            for line in fresh.splitlines():
                console.print(line, highlight=False, markup=False, soft_wrap=True)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped following.[/dim]")


@app.command("log", rich_help_panel="Housekeeping", **for_command("log"))
def show_log(
    limit: int = typer.Option(25, "--limit", "-n", help="How many events to show."),
) -> None:
    """Recent activity, including downloads that were skipped and why.

    The archive's own record of what happened to it. For what the watcher was
    thinking at the time — including downloads it never considered candidates —
    see `lw logs`, which reads the log file it writes as it runs.
    """
    cfg = _cfg()
    db = _open_db(cfg)
    rows = db.recent_events(limit)
    if not rows:
        console.print("[dim]no activity recorded yet. `lw doctor` says whether the watcher "
                      "is running; `lw logs` shows what it has been doing.[/dim]")
        return
    table = Table(box=None, header_style="bold", padding=(0, 2, 0, 0))
    table.add_column("when", style="dim")
    table.add_column("event")
    table.add_column("function")
    table.add_column("detail", style="dim")
    colours = {"new-version": "green", "unchanged": "cyan", "failed": "red",
               "duplicate-download": "dim", "watcher-started": "blue",
               "watcher-stopped": "yellow"}
    for row in rows:
        detail = row["detail"] or ""
        if row["source_path"]:
            detail = f"{Path(row['source_path']).name}  {detail}"
        colour = colours.get(row["kind"], "white")
        table.add_row(
            format_ts(row["ts"]),
            f"[{colour}]{row['kind']}[/{colour}]",
            row["function_name"] or "-",
            detail[:110],
        )
    console.print(table)


@app.command(rich_help_panel="Housekeeping", **for_command("init"))
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    """Write a commented config file you can edit."""
    from .templates import render_config

    path = _CONFIG_PATH or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        _fail(f"{path} already exists (use --force to overwrite)")
    path.write_text(render_config(_best_watch_dirs()), encoding="utf-8")
    console.print(f"[green]wrote[/green] {path}")
    cfg = load_config(path)
    cfg.ensure_dirs()
    console.print(f"archive root: {cfg.root}")
    console.print(f"watching:     {', '.join(str(d) for d in cfg.watch_dirs())}")
    console.print("\nNext: [bold]lw start[/bold]")


@app.command(rich_help_panel="Housekeeping", **for_command("reindex"))
def reindex(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Rebuild the index from the manifests on disk (the archive is the source of truth)."""
    cfg = _cfg()
    if not yes and not typer.confirm(f"Rebuild {cfg.db_path} from {cfg.functions_dir}?"):
        raise typer.Abort()

    from .reindex import rebuild

    stats = rebuild(cfg)
    _refresh_archive_index(cfg, _open_db(cfg))
    console.print(
        f"[green]reindexed[/green] {stats['functions']} function(s), "
        f"{stats['versions']} version(s)"
        + (f", [yellow]{stats['skipped']} skipped[/yellow]" if stats["skipped"] else "")
    )


def main() -> None:
    """Console-script entry point: run the app, exiting 130 on Ctrl-C.

    Catching :class:`KeyboardInterrupt` here is what keeps a deliberate Ctrl-C
    out of ``lw watch`` from printing a traceback.
    """
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        err_console.print("\n[dim]interrupted[/dim]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
