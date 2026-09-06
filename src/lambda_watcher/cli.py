"""Command line interface."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Config, default_config_path, load_config
from .db import Database
from .diffing import code_dir, diff_from_index
from .diffing.render_html import render_timeline, write_html
from .diffing.render_text import render as render_diff
from .gitmirror import diff as mirror_diff
from .gitmirror import git_available, has_tag
from .gitmirror import passthrough as git_passthrough
from .ingest import Ingestor
from .service import (
    ServiceError,
    ServiceStatus,
    current_status,
    install_service,
    stop_service,
)
from .store import Store
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


def _version_or_fail(db: Database, function_id: int, seq: int):
    """Fetch one version by sequence number, or exit saying it does not exist."""
    row = db.get_version(function_id, seq)
    if row is None:
        _fail(f"version {seq} not found")
    return row


def _build_diff(db: Database, store: Store, cfg: Config, function_row, a_seq: int, b_seq: int,
                include_vendor: bool | None, compute_diffs: bool = True):
    """Resolve two versions and build the diff between them.

    Warns, rather than fails, when a version's extracted code is missing from
    disk: the index still knows what changed at the file level, so the summary
    and dependency layers are worth printing even though the line diffs come out
    empty. Deleting an archive directory by hand is the usual cause.
    """
    a = _version_or_fail(db, function_row["id"], a_seq)
    b = _version_or_fail(db, function_row["id"], b_seq)
    for row, seq in ((a, a_seq), (b, b_seq)):
        path = code_dir(store, row)
        if not path.exists():
            err_console.print(
                f"[yellow]warning:[/yellow] code for v{seq:04d} is missing at {path}; "
                "line diffs for it will be empty"
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
    watched = ", ".join(_home_relative(d) for d in cfg.watch_dirs())

    console.print(f"[bold]lambda-watcher[/bold] {__version__}")
    if state.running:
        console.print(f"\n  [green]●[/green] watching {watched}   [dim]{state.manager}[/dim]")
    elif state.installed:
        console.print(f"\n  [yellow]●[/yellow] installed but not running   [dim]{state.manager}[/dim]")
    else:
        console.print(f"\n  [dim]○[/dim] not watching   [dim]{watched} is not being archived[/dim]")

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

    console.print()
    for command, blurb in _next_steps(state, rows[0]["name"] if rows else None):
        console.print(f"  [bold]{command}[/bold]   [dim]{blurb}[/dim]")


def _next_steps(state: ServiceStatus, newest: str | None) -> list[tuple[str, str]]:
    """The two or three commands most worth typing from where the user is now.

    Not watching is always the first thing to fix — an archive that has stopped
    growing is the failure this tool has to be loud about — but someone with
    history already deserves to be told how to read it in the same breath.
    """
    steps: list[tuple[str, str]] = []
    if not state.running:
        steps.append(
            ("lw start", "start watching again") if state.installed
            else ("lw setup", "watch your downloads folder from now on")
        )
    if newest is None:
        if state.running:
            steps.append(("lw doctor", "check the watch folder is the right one"))
        return steps
    steps.append((f'lw diff "{newest}"', "what changed in the last version"))
    if state.running:
        steps.append((f'lw report "{newest}"', "the whole history, in your browser"))
    return steps


@app.command(rich_help_panel="Everyday")
def status() -> None:
    """Is the watcher running, and what has it archived? (Also plain `lw`.)"""
    _print_status()


# ----------------------------------------------------------- getting started
@app.command(rich_help_panel="Everyday")
def setup(
    yes: bool = typer.Option(False, "--yes", "-y", help="Take the default answer to every prompt."),
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
        from .templates import DEFAULT_CONFIG_YAML
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
        console.print(f"  [green]✓[/green] wrote {_home_relative(config_path)}")
    else:
        console.print(f"  [green]✓[/green] using {_home_relative(config_path)}")

    cfg = _cfg()
    console.print(f"  [green]✓[/green] archive at {_home_relative(cfg.root)}")

    missing = [d for d in cfg.watch_dirs() if not d.exists()]
    for directory in cfg.watch_dirs():
        mark = "[green]✓[/green]" if directory.exists() else "[red]✗[/red]"
        console.print(f"  {mark} watching {_home_relative(directory)}")
    if missing:
        console.print(
            f"\n[yellow]note:[/yellow] {len(missing)} watch folder(s) do not exist. "
            f"Set [bold]watch.dirs[/bold] in {_home_relative(config_path)} and run setup again."
        )

    _offer_backfill(cfg, yes)

    if no_service:
        console.print("\n[dim]Skipping the background watcher. Run [bold]lw watch[/bold] "
                      "when you want it, or [bold]lw start[/bold] to install it later.[/dim]")
    else:
        console.print()
        _start_service(cfg)

    console.print("\n[dim]That is the whole setup. Download a Lambda zip as you normally would; "
                  "run [bold]lw[/bold] to see what it caught.[/dim]")


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
    if not yes:
        if not sys.stdin.isatty():
            console.print("  [dim]run [bold]lw backfill <folder>[/bold] to archive them[/dim]")
            return
        if not typer.confirm("  archive them now as history?", default=False):
            console.print("  [dim]skipped — [bold]lw backfill <folder>[/bold] does it later[/dim]")
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


@app.command(rich_help_panel="Watching")
def start() -> None:
    """Watch in the background, now and after every reboot."""
    cfg = _cfg()
    if not _start_service(cfg):
        raise typer.Exit(1)


@app.command(rich_help_panel="Watching")
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


@app.command(rich_help_panel="Watching")
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
@app.command(rich_help_panel="Everyday")
def doctor() -> None:
    """Check that everything the tool needs is in place."""
    cfg = _cfg()
    rows: list[tuple[str, str, str]] = []

    config_path = _CONFIG_PATH or default_config_path()
    rows.append(("config file", "ok" if config_path.exists() else "using defaults", str(config_path)))
    rows.append(("archive root", "ok" if cfg.root.exists() else "missing", str(cfg.root)))

    for directory in cfg.watch_dirs():
        rows.append((
            "watch dir",
            "ok" if directory.exists() else "MISSING",
            str(directory),
        ))
    rows.append((
        "git mirror",
        "ok" if git_available() else "git not found",
        "enabled" if cfg.git_mirror.enabled else "disabled in config",
    ))

    try:
        db = _open_db(cfg)
        functions = db.list_functions()
        total_versions = sum(int(f["version_count"] or 0) for f in functions)
        rows.append(("index", "ok", f"{len(functions)} function(s), {total_versions} version(s)"))
        db.close()
    except Exception as exc:  # noqa: BLE001
        rows.append(("index", "FAILED", str(exc)))

    try:
        usage = shutil.disk_usage(cfg.root)
        rows.append(("disk free", "ok", human_size(usage.free)))
    except OSError:
        pass

    table = Table(box=None, header_style="bold", padding=(0, 2, 0, 0))
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", style="dim")
    for name, status, detail in rows:
        style = "green" if status == "ok" else ("red" if status.isupper() else "yellow")
        table.add_row(name, f"[{style}]{status}[/{style}]", detail)
    console.print(table)


# ------------------------------------------------------------------ watch
@app.command(rich_help_panel="Watching")
def watch(
    once: bool = typer.Option(False, "--once", help="Process what is already there, then exit."),
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
    ingestor = Ingestor(cfg, db)

    def report(result) -> None:
        """Print one line per ingest as the watcher runs, colour-coded by outcome."""
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
        # The comparison is rendered during ingest, so what is offered here is a
        # file that already exists rather than a command to go and produce it.
        if result.report_path is not None:
            console.print(f"[dim]{'':>18}  report: {_home_relative(result.report_path)}[/dim]")
        elif result.status == "new" and result.changed_from:
            console.print(
                f"[dim]{'':>18}  review: lw diff "
                f'"{result.function_name}" --html --open[/dim]'
            )

    watcher = Watcher(cfg, db, ingestor, on_result=report)
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
        console.print("[dim]done[/dim]")
        return
    watcher.wait_forever()
    watcher.stop()
    console.print("[dim]stopped[/dim]")


@app.command(rich_help_panel="Watching")
def ingest(
    paths: list[Path] = typer.Argument(..., help="Zip file(s) to archive."),
    function: Optional[str] = typer.Option(
        None, "--as", "-a", help="Force the function name instead of guessing it."
    ),
    force: bool = typer.Option(False, "--force", help="Archive even if the content is unchanged."),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Note to attach to this version."),
) -> None:
    """Archive one or more zip files by hand."""
    cfg = _cfg()
    db = _open_db(cfg)
    ingestor = Ingestor(cfg, db)
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
    if failures:
        raise typer.Exit(1)


@app.command(rich_help_panel="Watching")
def backfill(
    directory: Path = typer.Argument(..., help="Folder full of previously downloaded zips."),
    pattern: str = typer.Option("*.zip", "--pattern", "-p", help="Glob to match."),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Descend into subfolders."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be archived."),
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
@app.command("ls", rich_help_panel="Everyday")
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


@app.command(rich_help_panel="Reading the archive")
def versions(
    function: str = typer.Argument(
        ..., help="Function name (a unique substring works).",
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


@app.command(rich_help_panel="Reading the archive")
def show(
    function: str = typer.Argument(..., autocompletion=_complete_function),
    version: Optional[str] = typer.Argument(None, help="Version number, or 'latest' (default)."),
    files: bool = typer.Option(False, "--files", help="List every file in the package."),
    json_out: bool = typer.Option(False, "--json", help="Print the raw manifest."),
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


@app.command(rich_help_panel="Everyday")
def diff(
    function: str = typer.Argument(..., autocompletion=_complete_function),
    from_: Optional[str] = typer.Option(
        None, "--from", "-f", help="Older version (default: the one before --to)."
    ),
    to: Optional[str] = typer.Option(None, "--to", "-t", help="Newer version (default: latest)."),
    html: bool = typer.Option(False, "--html", help="Write an HTML report instead of terminal output."),
    open_report: bool = typer.Option(False, "--open", help="Open the HTML report in your browser."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Where to write the HTML report."),
    vendor: bool = typer.Option(False, "--vendor", help="Include vendored dependency files."),
    whitespace: bool = typer.Option(
        False, "--whitespace", help="Show the hunk for files that changed only in whitespace."
    ),
    no_patch: bool = typer.Option(False, "--no-patch", help="Summary only, no line diffs."),
    json_out: bool = typer.Option(False, "--json", help="Emit the diff as JSON."),
    mirror: bool = typer.Option(
        False, "--mirror", help="Show the git mirror's answer for these two versions instead."
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
    if a_seq > b_seq:
        a_seq, b_seq = b_seq, a_seq

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
        write_html(result, target)
        console.print(f"[green]wrote[/green] {target}")
        if open_report:
            webbrowser.open(target.resolve().as_uri())
        return

    render_diff(console, result, show_diffs=not no_patch)


@app.command(rich_help_panel="Everyday")
def report(
    function: str = typer.Argument(..., autocompletion=_complete_function),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Directory for the report."),
    open_report: bool = typer.Option(False, "--open", help="Open the index in your browser."),
    limit: int = typer.Option(25, "--limit", "-n", help="How many recent versions to include."),
    vendor: bool = typer.Option(False, "--vendor", help="Include vendored files in the diffs."),
) -> None:
    """Build a browsable HTML history: every version plus a diff for each step."""
    cfg = _cfg()
    db = _open_db(cfg)
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
    include_vendor = True if vendor else None

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
            write_html(pair, target_dir / filename)
            entry["diff_href"] = filename
            entry["diff_summary"] = pair.headline()
        entries.append(entry)

    index = target_dir / "index.html"
    index.write_text(render_timeline(row["name"], entries), encoding="utf-8")
    console.print(f"[green]wrote[/green] {index} [dim]({len(entries)} versions)[/dim]")
    if open_report:
        webbrowser.open(index.resolve().as_uri())


# ---------------------------------------------------------------- editing
@app.command(rich_help_panel="Housekeeping")
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
        for version in db.list_versions(int(row["id"])):
            updated = version["dir"].replace(f"functions/{old_slug}/", f"functions/{new_slug}/", 1)
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

    db.rename_function(int(row["id"]), new_name, new_slug)
    if alias:
        db.add_alias(int(row["id"]), alias)
    console.print(f"[green]renamed[/green] {row['name']} → {new_name}")
    if alias:
        console.print(f"[dim]future downloads containing {alias!r} will map here automatically[/dim]")


@app.command(rich_help_panel="Housekeeping")
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

    with db.transaction():
        # Park every version on a temporary sequence to dodge the UNIQUE index.
        for offset, version in enumerate(everything, start=1):
            db.conn.execute(
                "UPDATE versions SET function_id = ?, seq = ? WHERE id = ?",
                (int(dst["id"]), -offset, version["id"]),
            )
        for new_seq, version in enumerate(everything, start=1):
            db.conn.execute("UPDATE versions SET seq = ? WHERE id = ?", (new_seq, version["id"]))
        db.conn.execute("UPDATE aliases SET function_id = ? WHERE function_id = ?",
                        (int(dst["id"]), int(src["id"])))
        db.conn.execute("DELETE FROM functions WHERE id = ?", (int(src["id"]),))

    src_dir = store.function_dir(src["slug"])
    dst_versions = store.versions_dir(dst["slug"])
    dst_versions.mkdir(parents=True, exist_ok=True)
    if src_dir.exists():
        for version_dir in (src_dir / "versions").glob("*"):
            if version_dir.is_dir():
                destination = dst_versions / version_dir.name
                if not destination.exists():
                    shutil.move(str(version_dir), str(destination))
        rmtree(src_dir)
    # The target's mirror no longer matches the renumbered versions, but the
    # source's belongs to a function that no longer exists at all.
    rmtree(store.repo_dir(src["slug"]))

    # Directory names still carry the old sequence numbers; re-point the index.
    for version in db.list_versions(int(dst["id"])):
        stored = store.resolve_version_dir(version["dir"])
        if stored.exists():
            continue
        candidate = dst_versions / Path(version["dir"]).name
        if candidate.exists():
            db.conn.execute(
                "UPDATE versions SET dir = ? WHERE id = ?",
                (store.relative(candidate), version["id"]),
            )

    console.print(
        f"[green]merged[/green] {src['name']} into {dst['name']} "
        f"({len(everything)} versions, renumbered by archive time)"
    )
    console.print("[dim]run `lw reindex` if any diffs look wrong[/dim]")


@app.command(rich_help_panel="Housekeeping")
def label(
    function: str = typer.Argument(..., autocompletion=_complete_function),
    version: str = typer.Argument(..., help="Version number, or 'latest'."),
    text: str = typer.Argument(..., help="Note to attach, e.g. 'prod deploy 2026-03-01'."),
) -> None:
    """Annotate a version so you can recognise it later."""
    cfg = _cfg()
    db = _open_db(cfg)
    row = _resolve_function(db, function)
    seq = _resolve_seq(db, int(row["id"]), version)
    version_row = _version_or_fail(db, int(row["id"]), seq)
    db.set_version_label(int(version_row["id"]), text or None)
    console.print(f"[green]labelled[/green] {row['name']} v{seq:04d}: {text}")


@app.command("rm", rich_help_panel="Housekeeping")
def remove(
    function: str = typer.Argument(..., autocompletion=_complete_function),
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
    console.print(f"[green]deleted[/green] {row['name']}")


# --------------------------------------------------------------- plumbing
@app.command(rich_help_panel="Reading the archive")
def export(
    function: str = typer.Argument(..., autocompletion=_complete_function),
    version: Optional[str] = typer.Argument(None, help="Version number, or 'latest' (default)."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination path."),
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


@app.command("open", rich_help_panel="Reading the archive")
def open_in_editor(
    function: str = typer.Argument(..., help="Function name, slug or id.", autocompletion=_complete_function),
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


@app.command(rich_help_panel="Housekeeping")
def path(
    function: str = typer.Argument(..., autocompletion=_complete_function),
    version: Optional[str] = typer.Argument(None),
    repo: bool = typer.Option(
        False, "--repo", "--git", help="Print the git mirror path instead."
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
    help="Run a git command inside a function's mirror repo, e.g. `lw git my-fn log --oneline`.",
    rich_help_panel="Reading the archive",
)
def git(ctx: typer.Context, function: str = typer.Argument(...)) -> None:
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


@app.command(rich_help_panel="Reading the archive")
def search(
    term: str = typer.Argument(..., help="Filename fragment or package name."),
    kind: str = typer.Option("all", "--kind", "-k", help="all | files | deps"),
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


@app.command("log", rich_help_panel="Housekeeping")
def show_log(limit: int = typer.Option(25, "--limit", "-n")) -> None:
    """Recent activity, including downloads that were skipped and why."""
    cfg = _cfg()
    db = _open_db(cfg)
    rows = db.recent_events(limit)
    if not rows:
        console.print("[dim]no activity recorded yet[/dim]")
        return
    table = Table(box=None, header_style="bold", padding=(0, 2, 0, 0))
    table.add_column("when", style="dim")
    table.add_column("event")
    table.add_column("function")
    table.add_column("detail", style="dim")
    colours = {"new-version": "green", "unchanged": "cyan", "failed": "red",
               "duplicate-download": "dim"}
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


@app.command(rich_help_panel="Housekeeping")
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    """Write a commented config file you can edit."""
    from .templates import DEFAULT_CONFIG_YAML

    path = _CONFIG_PATH or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        _fail(f"{path} already exists (use --force to overwrite)")
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    console.print(f"[green]wrote[/green] {path}")
    cfg = load_config(path)
    cfg.ensure_dirs()
    console.print(f"archive root: {cfg.root}")
    console.print(f"watching:     {', '.join(str(d) for d in cfg.watch_dirs())}")
    console.print("\nNext: [bold]lw start[/bold]")


@app.command(rich_help_panel="Housekeeping")
def reindex(
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Rebuild the index from the manifests on disk (the archive is the source of truth)."""
    cfg = _cfg()
    if not yes and not typer.confirm(f"Rebuild {cfg.db_path} from {cfg.functions_dir}?"):
        raise typer.Abort()

    from .reindex import rebuild

    stats = rebuild(cfg)
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
