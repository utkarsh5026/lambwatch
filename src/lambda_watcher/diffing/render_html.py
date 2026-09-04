"""Self-contained HTML report for a version diff.

No JavaScript frameworks, no CDN, no network: one file you can open, keep,
attach to a change ticket, or send to a colleague.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..utils import format_ts, human_size, read_text, signed
from . import icons
from .compare import FileChange, VersionDiff
from .highlight import highlight, highlight_lines, language_of

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

ICON_CSS = icons.css()

#: A file's lines as ``(source, highlighted)`` pairs — see `_paint`.
_Painted = list[tuple[str, str]] | None

CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --panel: #f7f8fa; --border: #e3e6ea; --text: #1b1f24;
  --muted: #616a76; --accent: #0b62d0;
  --add-bg: #e7f7ec; --add-fg: #04562a; --add-gutter: #cdeed8;
  --del-bg: #fdeaec; --del-fg: #7d1220; --del-gutter: #f7ced4;
  --meta-bg: #eef2f7; --chip: #e8ebef;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  /* Syntax tokens, One Light. Numbers and constants share a colour on purpose:
     both are literal values, and the eye reads them as the same thing. */
  --tk-c: #8b8f97; --tk-k: #a626a4; --tk-s: #50a14f; --tk-n: #986801;
  --tk-t: #986801; --tk-f: #4078f2; --tk-y: #0184bc;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1216; --panel: #161b22; --border: #2a313a; --text: #e6edf3;
    --muted: #9198a1; --accent: #58a6ff;
    --add-bg: #10261a; --add-fg: #6fd08c; --add-gutter: #17351f;
    --del-bg: #2a1418; --del-fg: #ff8b95; --del-gutter: #3d1a20;
    --meta-bg: #1b2129; --chip: #232a33;
    --tk-c: #7f848e; --tk-k: #c678dd; --tk-s: #98c379; --tk-n: #d19a66;
    --tk-t: #d19a66; --tk-f: #61afef; --tk-y: #56b6c2;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
a { color: var(--accent); }
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 80px; }
header.top { border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 20px; }
h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -0.01em; }
h1 .ver { color: var(--accent); font-variant-numeric: tabular-nums; }
.sub { color: var(--muted); font-size: 13px; }
h2 { font-size: 15px; margin: 28px 0 10px; letter-spacing: -0.01em; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 18px 0 4px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.card .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
.card .v { font-size: 20px; font-weight: 600; margin-top: 3px; font-variant-numeric: tabular-nums; }
.add { color: var(--add-fg); } .del { color: var(--del-fg); }
table.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
table.grid th { text-align: left; color: var(--muted); font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: .06em; padding: 6px 10px; border-bottom: 1px solid var(--border); }
table.grid td { padding: 6px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
table.grid tr:last-child td { border-bottom: none; }
.mono { font-family: var(--mono); font-size: 12.5px; }
.chip { display: inline-block; padding: 1px 7px; border-radius: 999px; background: var(--chip);
  font-size: 11px; font-weight: 600; letter-spacing: .02em; }
.chip.added { background: var(--add-gutter); color: var(--add-fg); }
.chip.removed { background: var(--del-gutter); color: var(--del-fg); }
.chip.modified { background: var(--meta-bg); color: var(--accent); }
.chip.renamed, .chip.changed { background: var(--meta-bg); color: var(--muted); }
.chip.high { background: var(--del-gutter); color: var(--del-fg); }
.chip.medium { background: #f4e6c9; color: #6b4a06; }
@media (prefers-color-scheme: dark) { .chip.medium { background: #33290f; color: #e8c46a; } }
.chip.low { background: var(--chip); color: var(--muted); }
.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 18px 0 8px;
  position: sticky; top: 0; background: var(--bg); padding: 10px 0; z-index: 5; border-bottom: 1px solid var(--border); }
.toolbar #shown-count { margin-left: auto; white-space: nowrap; flex: 0 0 auto; }
.toolbar input[type=search] { flex: 1 1 200px; min-width: 140px; padding: 7px 10px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--panel); color: var(--text); font-size: 13px; }
.toolbar label { color: var(--muted); font-size: 13px; display: inline-flex; gap: 6px; align-items: center; cursor: pointer; }
.toolbar button { padding: 7px 12px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--panel); color: var(--text); font-size: 13px; cursor: pointer; }
.toolbar button:hover { border-color: var(--accent); color: var(--accent); }
details.file { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; overflow: hidden; background: var(--panel); }
details.file > summary { cursor: pointer; padding: 9px 12px; display: flex; gap: 10px; align-items: center;
  list-style: none; font-size: 13px; }
details.file > summary::-webkit-details-marker { display: none; }
details.file > summary:hover { background: var(--meta-bg); }
summary .path { display: flex; align-items: center; gap: 7px; flex: 1; min-width: 0; }
summary .path .p { font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere; }
summary .stat { font-variant-numeric: tabular-nums; font-size: 12px; white-space: nowrap; }
.diff { background: var(--bg); border-top: 1px solid var(--border); overflow-x: auto; }
.diff table { border-collapse: collapse; width: 100%; font-family: var(--mono); font-size: 12.5px; }
.diff td { padding: 0 8px; white-space: pre; vertical-align: top; }
.diff td.ln { width: 1%; min-width: 46px; text-align: right; color: var(--muted); user-select: none;
  background: var(--panel); border-right: 1px solid var(--border); font-variant-numeric: tabular-nums; }
.diff td.mark { width: 1%; padding: 0 2px 0 8px; text-align: center; color: var(--muted); user-select: none; }
.diff td.code { padding-left: 6px; }
.diff tr.add td.code, .diff tr.add td.mark { background: var(--add-bg); }
.diff tr.add td.mark { color: var(--add-fg); }
.diff tr.add td.ln { background: var(--add-gutter); }
.diff tr.del td.code, .diff tr.del td.mark { background: var(--del-bg); }
.diff tr.del td.mark { color: var(--del-fg); }
.diff tr.del td.ln { background: var(--del-gutter); }
.diff tr.hunk td { background: var(--meta-bg); color: var(--muted); font-size: 11.5px; padding: 3px 8px; }
.tk-c { color: var(--tk-c); font-style: italic; }
.tk-k { color: var(--tk-k); }
.tk-s { color: var(--tk-s); }
.tk-n { color: var(--tk-n); }
.tk-t { color: var(--tk-t); }
.tk-f { color: var(--tk-f); }
.tk-y { color: var(--tk-y); }
.note { color: var(--muted); font-size: 12.5px; padding: 8px 12px; }
.empty { color: var(--muted); padding: 12px 0; }
footer { margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--border);
  color: var(--muted); font-size: 12px; }
.hidden { display: none !important; }
"""

JS = """
(function () {
  var search = document.getElementById('filter');
  var vendorToggle = document.getElementById('vendor');
  var files = Array.prototype.slice.call(document.querySelectorAll('details.file'));

  function apply() {
    var term = (search.value || '').toLowerCase();
    var showVendor = vendorToggle ? vendorToggle.checked : true;
    var shown = 0;
    files.forEach(function (el) {
      var path = (el.getAttribute('data-path') || '').toLowerCase();
      var isVendor = el.getAttribute('data-vendor') === '1';
      var ok = (!term || path.indexOf(term) !== -1) && (showVendor || !isVendor);
      el.classList.toggle('hidden', !ok);
      if (ok) shown++;
    });
    var counter = document.getElementById('shown-count');
    if (counter) counter.textContent = shown + ' of ' + files.length + ' files shown';
  }

  if (search) search.addEventListener('input', apply);
  if (vendorToggle) vendorToggle.addEventListener('change', apply);

  var expand = document.getElementById('expand-all');
  var collapse = document.getElementById('collapse-all');
  if (expand) expand.addEventListener('click', function () {
    files.forEach(function (f) { if (!f.classList.contains('hidden')) f.open = true; });
  });
  if (collapse) collapse.addEventListener('click', function () {
    files.forEach(function (f) { f.open = false; });
  });
  apply();
})();
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


@dataclass
class _Row:
    css: str
    old_no: str
    new_no: str
    text: str


def _diff_rows(change: FileChange) -> list[_Row]:
    """Turn unified-diff lines into numbered rows."""
    rows: list[_Row] = []
    old_no = new_no = 0
    for line in change.diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            continue
        hunk = _HUNK.match(line)
        if hunk:
            old_no = int(hunk.group(1))
            new_no = int(hunk.group(3))
            rows.append(_Row("hunk", "", "", line))
            continue
        if line.startswith("+"):
            rows.append(_Row("add", "", str(new_no), line[1:]))
            new_no += 1
        elif line.startswith("-"):
            rows.append(_Row("del", str(old_no), "", line[1:]))
            old_no += 1
        elif line.startswith("\\"):
            rows.append(_Row("meta", "", "", line))
        else:
            rows.append(_Row("ctx", str(old_no), str(new_no), line[1:] if line else ""))
            old_no += 1
            new_no += 1
    return rows


def _paint(root: Path | None, path: str | None, lang: str) -> list[tuple[str, str]] | None:
    """One file as ``(source line, highlighted line)`` pairs, or None if unreadable.

    The plain half is kept so the caller can prove a row and the line it is about
    to borrow colour from are the same text.
    """
    if root is None or not path:
        return None
    text = read_text(root / path)
    if text is None:
        return None
    # strict: `highlight_lines` promises one entry per line. If that ever broke,
    # every row below would borrow colour from its neighbour — loud beats subtle.
    return list(zip(text.split("\n"), highlight_lines(text, lang), strict=True))


def _row_code(row: _Row, lang: str, old: _Painted, new: _Painted) -> str:
    """The code cell for one diff row, coloured with the whole file in view.

    A removed line belongs to the older version, an added or context line to the
    newer one — which is why both sides are painted. Where the file cannot be
    read, or the line the diff quoted is not the line sitting at that number any
    more, this falls back to colouring the row on its own: worse colour on that
    row, never colour borrowed from the wrong line.
    """
    if row.css == "meta":  # `\ No newline at end of file` is diff's note, not the file's
        return _esc(row.text)
    painted, number = (old, row.old_no) if row.css == "del" else (new, row.new_no)
    if painted and number:
        index = int(number) - 1
        if 0 <= index < len(painted):
            source, coloured = painted[index]
            if source == row.text:
                return coloured
    return highlight(row.text, lang)


def _render_file(change: FileChange, a_root: Path | None = None, b_root: Path | None = None) -> str:
    lang = language_of(change.path, change.lang)
    title = (
        f"{_esc(change.old_path)} → {_esc(change.path)}"
        if change.kind == "renamed" and change.old_path
        else _esc(change.path)
    )
    stat = ""
    if change.added_lines:
        stat += f'<span class="add">+{change.added_lines}</span> '
    if change.removed_lines:
        stat += f'<span class="del">−{change.removed_lines}</span> '
    if change.size_delta:
        stat += f'<span style="color:var(--muted)">{signed(change.size_delta)} B</span>'

    parts = [
        f'<details class="file" data-path="{_esc(change.path)} {_esc(change.old_path or "")}" '
        f'data-vendor="{1 if change.is_vendor else 0}">',
        "<summary>",
        f'<span class="chip {_esc(change.kind)}">{_esc(change.kind)}</span>',
        f'<span class="path">{icons.file_icon(change.path, lang)}'
        f'<span class="p">{title}</span></span>',
        f'<span class="stat">{stat}</span>',
        "</summary>",
    ]

    if change.diff_lines:
        old = _paint(a_root, change.old_path or change.path, lang) if change.old else None
        new = _paint(b_root, change.path, lang) if change.new else None
        parts.append('<div class="diff"><table>')
        for row in _diff_rows(change):
            if row.css == "hunk":
                parts.append(f'<tr class="hunk"><td colspan="4">{_esc(row.text)}</td></tr>')
                continue
            css = f' class="{row.css}"' if row.css in ("add", "del") else ""
            # The sign lives in its own unselectable cell so that copying a
            # block of the diff yields the code, not code with markers glued on.
            mark = {"add": "+", "del": "\u2212"}.get(row.css, "")
            code = _row_code(row, lang, old, new)
            parts.append(
                f"<tr{css}>"
                f'<td class="ln">{row.old_no}</td>'
                f'<td class="ln">{row.new_no}</td>'
                f'<td class="mark">{mark}</td>'
                f'<td class="code">{code}</td>'
                "</tr>"
            )
        parts.append("</table></div>")
        if change.truncated:
            parts.append('<div class="note">Diff truncated — raise <code>diff.max_diff_lines</code> to see the rest.</div>')
    else:
        reason = change.skipped_reason or (
            "file is empty" if change.kind in {"added", "removed"} else "no textual change"
        )
        old_size = change.old.size if change.old else 0
        new_size = change.new.size if change.new else 0
        parts.append(
            f'<div class="note">No line diff shown ({_esc(reason)}). '
            f"{human_size(old_size)} → {human_size(new_size)}.</div>"
        )

    parts.append("</details>")
    return "\n".join(parts)


def _cards(diff: VersionDiff) -> str:
    counts = diff.counts()
    changed = sum(counts.values())
    size_a = diff.a_meta.get("total_size", 0)
    size_b = diff.b_meta.get("total_size", 0)
    lines = (
        f"+{diff.total_added_lines} / −{diff.total_removed_lines}"
        if diff.diffs_computed
        else "not computed"
    )
    cards = [
        ("Files changed", str(changed), ""),
        ("Lines", lines, ""),
        ("Dependencies", str(len(diff.deps)), ""),
        ("Package size", human_size(size_b), f"{signed(size_b - size_a)} B"),
    ]
    if diff.findings_new:
        cards.append(("New findings", str(len(diff.findings_new)), ""))
    if diff.vendor_files_changed:
        cards.append(
            ("Vendored files", str(diff.vendor_files_changed), "re-run with --vendor to see them")
        )

    html_parts = ['<div class="cards">']
    for key, value, note in cards:
        note_html = f'<div class="sub" style="font-size:11px">{_esc(note)}</div>' if note else ""
        html_parts.append(
            f'<div class="card"><div class="k">{_esc(key)}</div>'
            f'<div class="v">{_esc(value)}</div>{note_html}</div>'
        )
    html_parts.append("</div>")
    return "\n".join(html_parts)


def _dep_table(diff: VersionDiff) -> str:
    if not diff.deps:
        return ""
    rows = []
    for change in diff.deps:
        rows.append(
            "<tr>"
            f'<td><span class="chip {_esc(change.kind)}">{_esc(change.kind)}</span></td>'
            f'<td class="mono">{_esc(change.name)}</td>'
            f'<td class="mono del">{_esc(change.old_version or "—")}</td>'
            f'<td class="mono add">{_esc(change.new_version or "—")}</td>'
            f"<td>{_esc(change.manager)}</td>"
            f'<td>{"declared" if change.is_declared else "installed"}</td>'
            "</tr>"
        )
    return (
        "<h2>Dependencies</h2><table class='grid'>"
        "<thead><tr><th></th><th>package</th><th>from</th><th>to</th><th>manager</th><th>origin</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _context_section(diff: VersionDiff) -> str:
    blocks: list[str] = []

    def listing(label: str, values: list[str], css: str, hint: str = "") -> str:
        if not values:
            return ""
        chips = " ".join(f'<span class="chip {css}">{_esc(v)}</span>' for v in values)
        hint_html = f'<div class="sub" style="margin-top:4px">{_esc(hint)}</div>' if hint else ""
        return f"<tr><td style='width:200px'>{_esc(label)}</td><td>{chips}{hint_html}</td></tr>"

    rows = "".join(
        [
            listing(
                "Environment variables added", diff.env_added, "added",
                "These must exist in the function's environment configuration before you deploy.",
            ),
            listing("Environment variables removed", diff.env_removed, "removed"),
            listing(
                "AWS services added", diff.services_added, "added",
                "The execution role may need new IAM permissions.",
            ),
            listing("AWS services removed", diff.services_removed, "removed"),
        ]
    )
    if rows:
        blocks.append(f"<h2>Configuration impact</h2><table class='grid'><tbody>{rows}</tbody></table>")

    if diff.runtime_change or diff.handler_change:
        entries = []
        if diff.runtime_change:
            entries.append(
                f"<tr><td style='width:200px'>Runtime</td><td class='mono'>"
                f"{_esc(diff.runtime_change[0])} → {_esc(diff.runtime_change[1])}</td></tr>"
            )
        if diff.handler_change:
            before, after = diff.handler_change
            entries.append(
                f"<tr><td style='width:200px'>Handler</td><td class='mono'>"
                f"{_esc(before or '?')} → {_esc(after or '?')}</td></tr>"
            )
        blocks.append(f"<h2>Entry point</h2><table class='grid'><tbody>{''.join(entries)}</tbody></table>")

    return "".join(blocks)


def _findings_section(diff: VersionDiff) -> str:
    if not diff.findings_new and not diff.findings_fixed:
        return ""
    rows = []
    for finding in diff.findings_new:
        rows.append(
            "<tr>"
            f'<td><span class="chip {_esc(finding["severity"])}">{_esc(finding["severity"])}</span></td>'
            f'<td>{_esc(finding["kind"])}</td>'
            f'<td class="mono">{_esc(finding["path"])}:{_esc(finding["line"])}</td>'
            f'<td class="mono">{_esc(finding["detail"])}</td>'
            "</tr>"
        )
    resolved = (
        f'<div class="sub" style="margin-top:8px">{len(diff.findings_fixed)} finding(s) '
        "present in the older version are gone.</div>"
        if diff.findings_fixed
        else ""
    )
    table = (
        "<table class='grid'><thead><tr><th>severity</th><th>kind</th><th>where</th><th>detail</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        if rows
        else ""
    )
    return f"<h2>New findings</h2>{table}{resolved}"


def render_html(diff: VersionDiff, generated_by: str = "lambda-watcher") -> str:
    """Render the full report as a single HTML document."""
    title = f"{diff.function_name} · v{diff.a_seq:04d} → v{diff.b_seq:04d}"
    a_when = format_ts(diff.a_meta.get("ingested_at"))
    b_when = format_ts(diff.b_meta.get("ingested_at"))

    file_blocks = "\n".join(_render_file(c, diff.a_root, diff.b_root) for c in diff.files)
    if not diff.files:
        file_blocks = '<div class="empty">No file-level changes between these versions.</div>'

    vendor_toggle = (
        '<label><input type="checkbox" id="vendor" checked> show vendored files</label>'
        if any(c.is_vendor for c in diff.files)
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{CSS}{ICON_CSS}</style>
</head>
<body>
{icons.sprite()}
<div class="wrap">
  <header class="top">
    <h1>{_esc(diff.function_name)} <span class="ver">v{diff.a_seq:04d} → v{diff.b_seq:04d}</span></h1>
    <div class="sub">
      v{diff.a_seq:04d} archived {_esc(a_when)} · v{diff.b_seq:04d} archived {_esc(b_when)}
      · {_esc(diff.headline())}
    </div>
  </header>

  {_cards(diff)}
  {_dep_table(diff)}
  {_context_section(diff)}
  {_findings_section(diff)}

  <h2>File changes</h2>
  <div class="toolbar">
    <input type="search" id="filter" placeholder="Filter by path…" autocomplete="off">
    {vendor_toggle}
    <button type="button" id="expand-all">Expand all</button>
    <button type="button" id="collapse-all">Collapse all</button>
    <span class="sub" id="shown-count"></span>
  </div>
  {file_blocks}

  <footer>
    Generated by {_esc(generated_by)} on {_esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}.
    Content hashes ignore zip timestamps, so re-downloading unchanged code does not create a new version.
  </footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def write_html(diff: VersionDiff, path: Path, generated_by: str = "lambda-watcher") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(diff, generated_by), encoding="utf-8")
    return path


def render_timeline(
    function_name: str,
    versions: list[dict[str, Any]],
    generated_by: str = "lambda-watcher",
) -> str:
    """Index page: every archived version of one function, newest first.

    ``versions`` entries carry the per-version stats plus ``diff_href`` /
    ``diff_summary`` describing the step from the previous version.
    """
    rows: list[str] = []
    for entry in versions:
        seq = entry["seq"]
        href = entry.get("diff_href")
        step = (
            f'<a href="{_esc(href)}">{_esc(entry.get("diff_summary") or "view diff")}</a>'
            if href
            else '<span class="sub">first version</span>'
        )
        label = f' <span class="chip">{_esc(entry["label"])}</span>' if entry.get("label") else ""
        rows.append(
            "<tr>"
            f'<td class="mono"><strong>v{seq:04d}</strong>{label}</td>'
            f'<td>{_esc(format_ts(entry.get("ingested_at")))}</td>'
            f'<td class="mono">{_esc(entry.get("runtime") or "?")}</td>'
            f'<td class="mono">{_esc(entry.get("handler") or "?")}</td>'
            f'<td style="text-align:right">{entry.get("file_count", 0):,}</td>'
            f'<td style="text-align:right">{_esc(human_size(entry.get("total_size", 0)))}</td>'
            f'<td class="mono sub">{_esc(str(entry.get("source_name") or ""))}</td>'
            f"<td>{step}</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(function_name)} · version history</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>{_esc(function_name)}</h1>
    <div class="sub">{len(versions)} archived version(s) · newest first</div>
  </header>
  <table class="grid">
    <thead><tr>
      <th>version</th><th>archived</th><th>runtime</th><th>handler</th>
      <th style="text-align:right">files</th><th style="text-align:right">size</th>
      <th>downloaded as</th><th>change from previous</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <footer>Generated by {_esc(generated_by)} on {_esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}.</footer>
</div>
</body>
</html>
"""
