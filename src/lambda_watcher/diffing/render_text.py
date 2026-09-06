"""Terminal rendering of a version diff, using rich."""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from ..utils import human_size, rename_label, signed, slugify
from .compare import MoveGroup, VersionDiff
from .intraline import EDIT_CONTEXT

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


def _move_label(group: MoveGroup) -> str:
    """How one collapsed directory move is named in a listing.

    ``{handlers → lambda/handlers}/ · 20 files``, in the same braces a single
    rename uses, so the two read as the same kind of fact at different scales.
    Kept tight because it competes with a vendored path for the width of one
    table column, and the path is the half that cannot be shortened.

    Two things are said only when they are true, because both are the sort of
    qualifier that means nothing if it always appears. ``20 of 60 files`` marks
    a move that left part of the directory behind — a reader told only the two
    names would fairly conclude the old one is gone. ``3 edited`` marks the
    files rewritten on the way, which are the ones worth opening; that count
    comes from the content hashes, so it survives ``--no-patch``.
    """
    head, was, now, tail = rename_label(*group.display_dirs)
    count = (
        f"{group.moved} files" if group.is_whole_dir
        else f"{group.moved} of {group.total_in_old_dir} files"
    )
    edited = f", {group.edited} edited" if group.edited else ""
    return f"{head}{{{was} → {now}}}{tail}/ · {count}{edited}"


def _stat_line(diff: VersionDiff) -> Text:
    """The one-line tally under the header: counts per kind, then ``+24 / -5 lines``.

    Says ``line counts skipped`` rather than ``+0/-0`` when diffs were not
    computed — zero changes and unknown changes are different answers. A third
    answer gets the same treatment: files counted by word or labelled whitespace
    only contribute no lines, so a diff made of those alone reads ``+0 / -0
    lines (2 not counted by line)`` rather than claiming nothing moved.
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
        if diff.lines_uncounted:
            text.append(f" ({diff.lines_uncounted} not counted by line)", style="dim")
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
    rows: list[Text] = [header, _stat_line(diff)]
    if diff.vendor_files_changed:
        # The tally above says these files are hidden but not how to see them,
        # and a reader who wants them reaches for the git mirror instead — which
        # filters nothing and so answers a different question about the same two
        # versions. Naming the flag keeps that reconciliation inside one command.
        rows.append(Text(
            f"to see the {diff.vendor_files_changed} vendored "
            f"file{'s' if diff.vendor_files_changed != 1 else ''}: "
            f"lw diff {slugify(diff.function_name)} --vendor",
            style="dim",
        ))
    if diff.renames_unexamined:
        # A partial rename map reads exactly like a complete one, so say so:
        # some of the adds and removes below may be halves of the same file.
        rows.append(Text(
            f"rename check stopped early — {diff.renames_unexamined} added "
            f"file{'s' if diff.renames_unexamined != 1 else ''} not compared; raise "
            f"diff.max_rename_pairs in your config (lw init writes one)",
            style="yellow",
        ))
    console.print(Panel(Group(*rows), border_style="cyan", expand=False))

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


def _lead(edit) -> str:
    """The text before one word-level edit, with ``…`` only if something was cut.

    An edit at the very start of the file has nothing elided in front of it, and
    an ellipsis there claims otherwise — in a minified bundle, where every row
    looks like every other row, that is the difference between "this is the
    beginning" and "look further left".
    """
    return ("…" if edit.at > len(edit.lead) else "") + edit.lead


def _trail(edit) -> str:
    """The text after one word-level edit, with ``…`` only if something was cut.

    Cannot tell a full context window from one that happened to end at the file's
    last character, so a run of exactly :data:`~.intraline.EDIT_CONTEXT` closing
    characters gets an ellipsis it does not strictly need. The other way round —
    silently dropping the rest of an 8 KB bundle — is the mistake worth avoiding.
    """
    return edit.trail + ("…" if len(edit.trail) == EDIT_CONTEXT else "")


def _print_word_edits(console: Console, change) -> None:
    """Print the changed runs of a file with no usable lines, one row each.

    ``@ 19   …e){var t=1 → 2;a=1;a=1…`` — the text either side is dimmed and the
    changed run is not, because the point of the row is that the eye lands on
    the change rather than searching a line for it. ``@`` is a character offset,
    not a line number: the file has one line, which is the problem.

    Built as a :class:`~rich.text.Text` piece by piece rather than printed as
    markup, because the pieces are arbitrary file content and a minified bundle
    is full of square brackets rich would read as style tags.
    """
    record = change.new or change.old
    lines = record.lines if record else 0
    console.print(
        f"[dim]{lines} line{'s' if lines != 1 else ''} of "
        f"{human_size(record.size if record else 0)}; showing the changed runs[/dim]"
    )
    for edit in change.word_edits:
        row = Text(f"@ {edit.at:<8}", style="dim")
        row.append(_lead(edit), style="dim")
        if edit.before:
            # Struck through only when nothing replaced it: with an arrow after
            # it the removal is already stated, and saying it twice is louder
            # than the change itself.
            row.append(edit.before, style="red" if edit.after else "red strike")
        if edit.before and edit.after:
            row.append(" → ", style="dim")
        if edit.after:
            row.append(edit.after, style="green")
        row.append(_trail(edit), style="dim")
        console.print(row)


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

    The table is built from :meth:`~.compare.VersionDiff.file_rows`, not from
    ``files``, so a directory move takes one row rather than one per file. The
    patch loop below still walks ``files``: a file that moved *and* was
    rewritten is folded into the move's row but keeps its own diff, because the
    row reports the move and the diff reports the edit.
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
    rows = diff.file_rows()
    for row in rows[:max_files]:
        # Branching on the row itself rather than aliasing it to a `move`
        # variable: `kind` and `line_count_note` are a FileChange's, and only
        # this shape tells a reader - and a type checker - which half owns them.
        if isinstance(row, MoveGroup):
            mark = Text("→", style=_KIND_STYLE["renamed"])
            label = Text(_move_label(row), style="dim" if row.is_vendor else "")
        else:
            mark = Text(marks.get(row.kind, "?"), style=_KIND_STYLE.get(row.kind, ""))
            label = Text(_label(row), style="dim" if row.is_vendor else "")
            if note := row.line_count_note:
                # The + and − columns are blank for these, and a blank cell reads as
                # "nothing changed" rather than "counted in a different unit". The
                # note is what tells the two apart, so it rides on the path.
                label.append(f"  · {note}", style="dim italic")
        table.add_row(
            mark,
            label,
            str(row.added_lines or ""),
            str(row.removed_lines or ""),
            signed(row.size_delta) if row.size_delta else "",
        )
    console.print(table)
    if len(rows) > max_files:
        console.print(f"[dim]… and {len(rows) - max_files} more files[/dim]")

    if not show_diffs:
        return

    for change in diff.files:
        if not (change.diff_lines or change.word_edits):
            continue
        console.print()
        console.print(Rule(f"[bold]{_label(change)}[/bold]",
                           style=_KIND_STYLE.get(change.kind, "white")))
        if change.word_edits:
            _print_word_edits(console, change)
            continue
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
