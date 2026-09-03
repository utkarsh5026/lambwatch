"""Command line interface."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import Config, default_config_path, load_config
from .db import Database
from .diffing import compare_versions
from .diffing.render_html import render_timeline, write_html
from .diffing.render_text import render as render_diff
from .gitmirror import git_available
from .ingest import Ingestor
from .store import Store
from .utils import format_ts, human_size, setup_logging, slugify

# Commands open the index freely and the process normally exits straight
# after, so nothing closed it. Windows disagrees: an open SQLite handle makes
# the file undeletable, so `reindex` (which replaces index.db) fails whenever
# another command ran first in the same process. Closing on command completion
# keeps that deterministic instead of waiting for the garbage collector.
_OPEN_DBS: list[Database] = []


def _close_open_dbs(*_args: object, **_kwargs: object) -> None:
    while _OPEN_DBS:
        _OPEN_DBS.pop().close()


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Watch your Downloads folder for Lambda deployment zips, archive every "
    "version, and diff any two of them.",
    result_callback=_close_open_dbs,
)
console = Console()
err_console = Console(stderr=True)

_CONFIG_PATH: Path | None = None


# ---------------------------------------------------------------- helpers
def _cfg() -> Config:
    cfg = load_config(_CONFIG_PATH)
    cfg.ensure_dirs()
    setup_logging(cfg.log_level, cfg.log_dir / "watcher.log")
    return cfg


def _open_db(cfg: Config) -> Database:
    db = Database(cfg.db_path)
    _OPEN_DBS.append(db)
    return db


def _fail(message: str, code: int = 1) -> None:
    err_console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code)


def _resolve_function(db: Database, ident: str):
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
    row = db.get_version(function_id, seq)
    if row is None:
        _fail(f"version {seq} not found")
    return row


def _build_diff(db: Database, store: Store, cfg: Config, function_row, a_seq: int, b_seq: int,
                include_vendor: bool | None, compute_diffs: bool = True):
    a = _version_or_fail(db, function_row["id"], a_seq)
    b = _version_or_fail(db, function_row["id"], b_seq)
    a_dir = store.resolve_version_dir(a["dir"]) / "code"
    b_dir = store.resolve_version_dir(b["dir"]) / "code"
    for path, seq in ((a_dir, a_seq), (b_dir, b_seq)):
        if not path.exists():
            err_console.print(
                f"[yellow]warning:[/yellow] code for v{seq:04d} is missing at {path}; "
                "line diffs for it will be empty"
            )
    return compare_versions(
        function_row["name"], a_seq, b_seq,
        db.files_for(a["id"]), db.files_for(b["id"]), a_dir, b_dir, cfg.diff,
        a_deps=db.deps_for(a["id"]), b_deps=db.deps_for(b["id"]),
        a_env=db.env_for(a["id"]), b_env=db.env_for(b["id"]),
        a_services=db.services_for(a["id"]), b_services=db.services_for(b["id"]),
        a_findings=db.findings_for(a["id"]), b_findings=db.findings_for(b["id"]),
        a_meta=dict(a), b_meta=dict(b),
        include_vendor=include_vendor,
        compute_diffs=compute_diffs,
    )


def _open_path(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        err_console.print(f"[yellow]could not open {path}: {exc}[/yellow]")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"lambda-watcher {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to config.yaml (default: ~/.lambda-watcher/config.yaml)."
    ),
    version: bool = typer.Option(
        False, "--version", help="Print the version and exit.",
        callback=_version_callback, is_eager=True,
    ),
) -> None:
    global _CONFIG_PATH
    _CONFIG_PATH = config


# ------------------------------------------------------------------ setup
@app.command()
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
    console.print("\nNext: [bold]lambda-watcher watch[/bold]")


@app.command()
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
@app.command()
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
        colours = {
            "new": "green", "unchanged": "cyan", "duplicate-download": "dim", "failed": "red",
        }
        colour = colours.get(result.status, "white")
        label = f"{result.function_name or '?'}"
        if result.seq:
            label += f" v{result.seq:04d}"
        console.print(
            f"[{colour}]{result.status:>18}[/{colour}]  {label}  "
            f"[dim]{result.source.name} — {result.message}[/dim]"
        )
        if result.status == "new" and result.changed_from:
            console.print(
                f"[dim]{'':>18}  review: lambda-watcher diff "
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


@app.command()
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


@app.command()
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
        result = ingestor.ingest(path)
        stats[result.status] = stats.get(result.status, 0) + 1
        suffix = f" v{result.seq:04d}" if result.seq else ""
        console.print(f"  {result.status:>18}  {result.function_name or '?'}{suffix}  [dim]{path.name}[/dim]")
    console.print("\n" + "  ".join(f"[bold]{k}[/bold]: {v}" for k, v in sorted(stats.items())))


# ------------------------------------------------------------- inspection
@app.command("ls")
def list_functions() -> None:
    """List every Lambda function that has been archived."""
    cfg = _cfg()
    db = _open_db(cfg)
    rows = db.list_functions()
    if not rows:
        console.print(
            "[dim]Nothing archived yet. "
            "Run [bold]lambda-watcher watch[/bold] and download a zip.[/dim]"
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


@app.command()
def versions(
    function: str = typer.Argument(..., help="Function name (a unique substring works)."),
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
        f"\n[dim]Compare the last two: [bold]lambda-watcher diff \"{row['name']}\"[/bold][/dim]"
    )


@app.command()
def show(
    function: str = typer.Argument(...),
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
@app.command()
def diff(
    function: str = typer.Argument(...),
    from_: Optional[str] = typer.Option(
        None, "--from", "-f", help="Older version (default: the one before --to)."
    ),
    to: Optional[str] = typer.Option(None, "--to", "-t", help="Newer version (default: latest)."),
    html: bool = typer.Option(False, "--html", help="Write an HTML report instead of terminal output."),
    open_report: bool = typer.Option(False, "--open", help="Open the HTML report in your browser."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Where to write the HTML report."),
    vendor: bool = typer.Option(False, "--vendor", help="Include vendored dependency files."),
    no_patch: bool = typer.Option(False, "--no-patch", help="Summary only, no line diffs."),
    json_out: bool = typer.Option(False, "--json", help="Emit the diff as JSON."),
) -> None:
    """Compare two versions of a function. Defaults to the last two."""
    cfg = _cfg()
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


@app.command()
def report(
    function: str = typer.Argument(...),
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
@app.command()
def rename(
    current: str = typer.Argument(..., help="The function as it is recorded now."),
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
            f"{new_name!r} already exists. Use `lambda-watcher merge {current!r} {new_name!r}` "
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

    db.rename_function(int(row["id"]), new_name, new_slug)
    if alias:
        db.add_alias(int(row["id"]), alias)
    console.print(f"[green]renamed[/green] {row['name']} → {new_name}")
    if alias:
        console.print(f"[dim]future downloads containing {alias!r} will map here automatically[/dim]")


@app.command()
def merge(
    source: str = typer.Argument(..., help="Function whose versions should move."),
    target: str = typer.Argument(..., help="Function they should move into."),
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
        shutil.rmtree(src_dir, ignore_errors=True)

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
    console.print("[dim]run `lambda-watcher reindex` if any diffs look wrong[/dim]")


@app.command()
def label(
    function: str = typer.Argument(...),
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


@app.command("rm")
def remove(
    function: str = typer.Argument(...),
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
    shutil.rmtree(store.function_dir(row["slug"]), ignore_errors=True)
    db.delete_function(int(row["id"]))
    console.print(f"[green]deleted[/green] {row['name']}")


# --------------------------------------------------------------- plumbing
@app.command()
def export(
    function: str = typer.Argument(...),
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


@app.command()
def path(
    function: str = typer.Argument(...),
    version: Optional[str] = typer.Argument(None),
    git: bool = typer.Option(False, "--git", help="Print the git mirror path instead."),
    open_it: bool = typer.Option(False, "--open", help="Open it in the file manager."),
) -> None:
    """Print where something lives on disk (handy for `cd $(...)`)."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    if git:
        target = store.git_dir(row["slug"])
    elif version is None:
        target = store.function_dir(row["slug"])
    else:
        seq = _resolve_seq(db, int(row["id"]), version)
        version_row = _version_or_fail(db, int(row["id"]), seq)
        target = store.resolve_version_dir(version_row["dir"]) / "code"
    print(target)
    if open_it:
        _open_path(target)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    help="Run a git command inside a function's mirror repo, e.g. `lw git my-fn log --oneline`.",
)
def git(ctx: typer.Context, function: str = typer.Argument(...)) -> None:
    """Run git against the per-function mirror repository."""
    cfg = _cfg()
    db = _open_db(cfg)
    store = Store(cfg)
    row = _resolve_function(db, function)
    repo = store.git_dir(row["slug"])
    if not (repo / ".git").exists():
        _fail(
            f"no git mirror for {row['name']} at {repo}. "
            "Enable git_mirror in the config and re-ingest, or use `lambda-watcher diff`."
        )
    args = list(ctx.args) or ["log", "--oneline", "--decorate", "-20"]
    proc = subprocess.run(["git", "-C", str(repo), *args])
    raise typer.Exit(proc.returncode)


@app.command()
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


@app.command("log")
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


@app.command()
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
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        err_console.print("\n[dim]interrupted[/dim]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
