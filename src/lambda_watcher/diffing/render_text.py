"""Terminal rendering of a version diff, using rich."""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..utils import human_size, signed
from .compare import VersionDiff

_KIND_STYLE = {
    "added": "green",
    "removed": "red",
    "modified": "yellow",
    "renamed": "cyan",
    "mode-changed": "magenta",
}
_SEVERITY_STYLE = {"high": "bold red", "medium": "yellow", "low": "dim"}


def _stat_line(diff: VersionDiff) -> Text:
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
    if not diff.findings_new and not diff.findings_fixed:
        return
    console.print()
    if diff.findings_new:
        console.print("[bold red]New findings[/bold red]")
        for finding in diff.findings_new[:25]:
            style = _SEVERITY_STYLE.get(finding["severity"], "")
            console.print(
                f"  [{style}]{finding['severity']:>6}[/] {finding['kind']}  "
                f"{finding['path']}:{finding['line']}  [dim]{finding['detail']}[/dim]"
            )
    if diff.findings_fixed:
        console.print(f"[green]Resolved findings:[/green] {len(diff.findings_fixed)}")


def render_files(console: Console, diff: VersionDiff, show_diffs: bool = True,
                 max_files: int = 200) -> None:
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
        label = change.path if change.kind != "renamed" else f"{change.old_path} → {change.path}"
        table.add_row(
            Text(marks.get(change.kind, "?"), style=_KIND_STYLE.get(change.kind, "")),
            Text(label, style="dim" if change.is_vendor else ""),
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
        title = change.path if change.kind != "renamed" else f"{change.old_path} → {change.path}"
        console.print(Rule(f"[bold]{title}[/bold]", style=_KIND_STYLE.get(change.kind, "white")))
        body = "\n".join(change.diff_lines)
        console.print(Syntax(body, "diff", theme="ansi_dark", word_wrap=False, background_color="default"))
        if change.truncated:
            console.print("[dim]… diff truncated (raise diff.max_diff_lines to see more)[/dim]")


def render(console: Console, diff: VersionDiff, show_diffs: bool = True) -> None:
    render_summary(console, diff)
    render_dependencies(console, diff)
    render_context(console, diff)
    render_findings(console, diff)
    render_files(console, diff, show_diffs=show_diffs)
    if diff.is_empty:
        console.print("\n[green]These two versions are identical.[/green]")
