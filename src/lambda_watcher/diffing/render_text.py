"""Terminal rendering of a version diff, using rich."""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..utils import human_size, rename_label, signed
from .compare import VersionDiff

_KIND_STYLE = {
    "added": "green",
    "removed": "red",
    "modified": "yellow",
    "renamed": "cyan",
    "mode-changed": "magenta",
}
_SEVERITY_STYLE = {"high": "bold red", "medium": "yellow", "low": "dim"}


def _label(change) -> str:
    """How one file is named in a listing.

    A rename writes the two paths as one, with the part that moved in braces —
    the alternative is 90 characters of identical path twice over, wrapped
    across two rows, for a version number that changed in the middle.
    """
    if change.kind != "renamed" or not change.old_path:
        return change.path
    head, was, now, tail = rename_label(change.old_path, change.path)
    return f"{head}{{{was} \u2192 {now}}}{tail}"


def _stat_line(diff: VersionDiff) -> Text:
    """The one-line tally under the header: counts per kind, then ``+24 / -5 lines``.

    Says ``line counts skipped`` rather than ``+0/-0`` when diffs were not
    computed — zero changes and unknown changes are different answers.
    """
    counts = diff.counts()
    text = Text()
    for kind in ("added", "removed", "modified", "renamed"):
        if counts.get(kind):
            text.append(f"{counts[kind]} {kind}  ", style=_KIND_STYLE[kind])
    if diff.vendor_files_changed:
        text.append(f"{diff.vendor_files_changed} vendored (hidden)  ", style="dim")
    if diff.diffs_computed:
        text.append(f"+{diff.total_added_lines}", style="green")
        text.append(" / ")
        text.append(f"-{diff.total_removed_lines}", style="red")
        text.append(" lines")
    else:
        text.append("line counts skipped (--no-patch)", style="dim")
    return text


def render_summary(console: Console, diff: VersionDiff) -> None:
    """Print the header panel: what changed about the package itself.

    The framing before any file list — function name, the two versions, the
    tally, and any change of runtime, handler or total size. Size carries a
    colour, green when the package got smaller.
    """
    header = Text()
    header.append(diff.function_name, style="bold")
    header.append(f"   v{diff.a_seq:04d} → v{diff.b_seq:04d}", style="bold cyan")
    console.print(Panel(Group(header, _stat_line(diff)), border_style="cyan", expand=False))

    if diff.runtime_change:
        console.print(f"  [bold]runtime[/bold]  {diff.runtime_change[0]} → {diff.runtime_change[1]}")
    if diff.handler_change:
        before, after = diff.handler_change
        console.print(f"  [bold]handler[/bold]  {before or '?'} → {after or '?'}")

    size_a = diff.a_meta.get("total_size", 0)
    size_b = diff.b_meta.get("total_size", 0)
    if size_a or size_b:
        console.print(
            f"  [bold]size[/bold]     {human_size(size_a)} → {human_size(size_b)} "
            f"([{'green' if size_b <= size_a else 'yellow'}]{signed(size_b - size_a)} B[/])"
        )


def render_dependencies(console: Console, diff: VersionDiff) -> None:
    """Print the dependency table, or nothing when no dependency moved.

    Usually the section that explains the file churn: one row reading
    ``boto3 1.34.0 → 1.35.20`` stands in for several thousand changed files
    under ``site-packages``.
    """
    if not diff.deps:
        return
    table = Table(title="Dependencies", title_justify="left", header_style="bold", box=None,
                  padding=(0, 2, 0, 0))
    table.add_column("")
    table.add_column("manager", style="dim")
    table.add_column("package")
    table.add_column("from", style="red")
    table.add_column("to", style="green")
    table.add_column("origin", style="dim")

    marks = {"added": ("+", "green"), "removed": ("−", "red"), "changed": ("~", "yellow")}
    for change in diff.deps:
        mark, style = marks[change.kind]
        table.add_row(
            Text(mark, style=style),
            change.manager,
            change.name,
            change.old_version or "—",
            change.new_version or "—",
            "declared" if change.is_declared else "installed",
        )
    console.print()
    console.print(table)


def render_context(console: Console, diff: VersionDiff) -> None:
    """Print what the new version needs from outside the zip.

    Environment variables and AWS services that appeared or disappeared. These
    are the changes a deployment can silently fail on — the code is fine, but
    the function's configuration no longer matches it — so added env vars get an
    explicit reminder that they have to exist in the console too. Prints nothing
    when neither moved.
    """
    rows: list[tuple[str, str, str]] = []
    if diff.env_added:
        rows.append(("Env vars added", ", ".join(diff.env_added), "green"))
    if diff.env_removed:
        rows.append(("Env vars removed", ", ".join(diff.env_removed), "red"))
    if diff.services_added:
        rows.append(("AWS services added", ", ".join(diff.services_added), "green"))
    if diff.services_removed:
        rows.append(("AWS services removed", ", ".join(diff.services_removed), "red"))
    if not rows:
        return
    console.print()
    for label, value, style in rows:
        console.print(f"  [bold]{label}:[/bold] [{style}]{value}[/{style}]")
    if diff.env_added:
        console.print(
            "  [dim]↑ these need to exist in the function's environment configuration[/dim]"
        )


def render_findings(console: Console, diff: VersionDiff) -> None:
    """Print new security findings, and a count of the resolved ones.

    New findings get a table, capped at 25 rows; resolved ones get a single
    green line, since the detail of something that is gone is rarely worth the
    space. Prints nothing when neither list has anything in it.
    """
    if not diff.findings_new and not diff.findings_fixed:
        return
    console.print()
    if diff.findings_new:
        table = Table(title="New findings", title_justify="left", title_style="bold red",
                      header_style="bold", box=None, padding=(0, 2, 0, 0), show_header=False)
        table.add_column("", justify="right")
        table.add_column("kind")
        table.add_column("where", style="dim")
        table.add_column("detail", style="dim")
        for finding in diff.findings_new[:25]:
            table.add_row(
                Text(finding["severity"], style=_SEVERITY_STYLE.get(finding["severity"], "")),
                finding["kind"],
                f"{finding['path']}:{finding['line']}",
                finding["detail"],
            )
        console.print(table)
    if diff.findings_fixed:
        console.print(f"[green]Resolved findings:[/green] {len(diff.findings_fixed)}")


def render_files(console: Console, diff: VersionDiff, show_diffs: bool = True,
                 max_files: int = 200) -> None:
    """Print the file table and, unless asked not to, each file's line diff.

    The table lists every change with its line counts and size delta, capped at
    ``max_files`` with a note saying how many were left out. ``show_diffs=False``
    stops after the table, which is what ``--no-patch`` wants.

    Files come pre-sorted by :func:`~.compare.compare_versions` — first-party
    before vendored, modified before added — so reading top to bottom means
    reading the most relevant changes first. A file whose diff was skipped
    simply has no patch to print; the table row still records that it changed.
    """
    if not diff.files:
        console.print("\n[dim]No file-level changes.[/dim]")
        return

    console.print()
    table = Table(title="Files", title_justify="left", header_style="bold", box=None,
                  padding=(0, 2, 0, 0))
    table.add_column("")
    table.add_column("path")
    table.add_column("+", justify="right", style="green")
    table.add_column("−", justify="right", style="red")
    table.add_column("size", justify="right", style="dim")

    marks = {"added": "+", "removed": "−", "modified": "~", "renamed": "→", "mode-changed": "m"}
    for change in diff.files[:max_files]:
        table.add_row(
            Text(marks.get(change.kind, "?"), style=_KIND_STYLE.get(change.kind, "")),
            Text(_label(change), style="dim" if change.is_vendor else ""),
            str(change.added_lines or ""),
            str(change.removed_lines or ""),
            signed(change.size_delta) if change.size_delta else "",
        )
    console.print(table)
    if len(diff.files) > max_files:
        console.print(f"[dim]… and {len(diff.files) - max_files} more files[/dim]")

    if not show_diffs:
        return

    for change in diff.files:
        if not change.diff_lines:
            continue
        console.print()
        console.print(Rule(f"[bold]{_label(change)}[/bold]",
                           style=_KIND_STYLE.get(change.kind, "white")))
        body = "\n".join(change.diff_lines)
        console.print(Syntax(body, "diff", theme="ansi_dark", word_wrap=False, background_color="default"))
        if change.truncated:
            console.print("[dim]… diff truncated (raise diff.max_diff_lines to see more)[/dim]")


def render(console: Console, diff: VersionDiff, show_diffs: bool = True) -> None:
    """Render a whole diff to the console, in the order the layers matter.

    The package, then its dependencies, then its environment, then findings,
    then files. Ends with an explicit "identical" line when nothing changed at
    all, because a command that prints nothing looks like a command that
    failed.
    """
    render_summary(console, diff)
    render_dependencies(console, diff)
    render_context(console, diff)
    render_findings(console, diff)
    render_files(console, diff, show_diffs=show_diffs)
    if diff.is_empty:
        console.print("\n[green]These two versions are identical.[/green]")
