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

from ..utils import format_ts, human_size, read_text, rename_label, signed, slugify
from . import icons, intraline
from .intraline import EDIT_CONTEXT
from .compare import FileChange, MoveGroup, VersionDiff
from .highlight import highlight, highlight_lines, language_of

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

ICON_CSS = icons.css()

#: A file's lines as ``(source, highlighted)`` pairs — see `_paint`.
_Painted = list[tuple[str, str]] | None

CSS = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --panel: #fafbfc; --sunken: #f1f4f7; --border: #e2e6eb;
  --rule: #eceff3; --text: #12161b; --muted: #5b6672; --faint: #8a939f;
  --accent: #0a58ca; --accent-wash: #eaf1fd; --accent-edge: #cfe0fa;
  --add-bg: #e9f7ee; --add-word: #b4ecc6; --add-gutter: #d5efdd; --add-fg: #0a6634;
  --del-bg: #fdedef; --del-word: #f8c4cb; --del-gutter: #f7d5da; --del-fg: #96162a;
  --warn-bg: #f7ecd2; --warn-fg: #6b4a06;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  /* Syntax tokens, One Light. Numbers and constants share a colour on purpose:
     both are literal values, and the eye reads them as the same thing. */
  --tk-c: #8b8f97; --tk-k: #a626a4; --tk-s: #50a14f; --tk-n: #986801;
  --tk-t: #986801; --tk-f: #4078f2; --tk-y: #0184bc;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --panel: #12171e; --sunken: #1a212a; --border: #262d36;
    --rule: #1e242c; --text: #e3e9ef; --muted: #96a0ac; --faint: #6e7885;
    --accent: #6cb0ff; --accent-wash: #16253c; --accent-edge: #294869;
    --add-bg: #0e2417; --add-word: #1f5c34; --add-gutter: #14311f; --add-fg: #6ddb92;
    --del-bg: #2a1319; --del-word: #6d2029; --del-gutter: #3b181f; --del-fg: #ff949e;
    --warn-bg: #3a2d10; --warn-fg: #e6c169;
    --tk-c: #7f848e; --tk-k: #c678dd; --tk-s: #98c379; --tk-n: #d19a66;
    --tk-t: #d19a66; --tk-f: #61afef; --tk-y: #56b6c2;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); }
.wrap { max-width: 1140px; margin: 0 auto; padding: 32px 24px 96px; }

/* ---- header ---------------------------------------------------------- */
header.top { padding-bottom: 18px; margin-bottom: 24px; border-bottom: 1px solid var(--border); }
h1 { font-size: 22px; font-weight: 650; margin: 0; letter-spacing: -0.015em;
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
h1 .ver { font-family: var(--mono); font-size: 14px; font-weight: 600; letter-spacing: 0;
  color: var(--accent); font-variant-numeric: tabular-nums;
  background: var(--accent-wash); border: 1px solid var(--accent-edge);
  border-radius: 5px; padding: 2px 8px; white-space: nowrap; }
h1 .ver .arrow { color: var(--faint); padding: 0 5px; font-weight: 400; }
.sub { color: var(--muted); font-size: 13px; }
header.top .sub { margin-top: 9px; }

/* A heading that carries a hairline to the end of the measure: the sections
   read as sections without a box drawn round each one. */
h2 { font-size: 12px; font-weight: 650; text-transform: uppercase; letter-spacing: .07em;
  color: var(--muted); margin: 32px 0 12px; display: flex; align-items: center; gap: 12px; }
h2::after { content: ""; flex: 1; height: 1px; background: var(--rule); }

/* ---- summary rail ---------------------------------------------------- */
/* One panel divided by hairlines rather than six floating cards. These numbers
   are meant to be read across, and separate boxes put a gutter between every
   pair of them. */
.stats { display: flex; flex-wrap: wrap; border: 1px solid var(--border); border-radius: 10px;
  background: var(--panel); overflow: hidden; }
.stat { flex: 1 1 150px; padding: 11px 16px; border-left: 1px solid var(--border); min-width: 0; }
.stat:first-child { border-left: none; }
.stat .v { font-size: 19px; font-weight: 620; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums; display: flex; align-items: baseline; gap: 7px; }
.stat .v .delta { font-size: 12px; font-weight: 500; color: var(--muted);
  letter-spacing: 0; white-space: nowrap; }
.stat .k { color: var(--muted); font-size: 12px; margin-top: 1px; }
.stat .k .hint { color: var(--faint); }
.stat[title] { cursor: help; }
.add { color: var(--add-fg); } .del { color: var(--del-fg); }

/* ---- tables ---------------------------------------------------------- */
.scroll { overflow-x: auto; }
table.grid { border-collapse: collapse; font-size: 13px; width: 100%; }
table.grid th { text-align: left; color: var(--faint); font-weight: 600; font-size: 11px;
  text-transform: uppercase; letter-spacing: .06em; padding: 0 20px 6px 0;
  border-bottom: 1px solid var(--border); white-space: nowrap; }
table.grid td { padding: 7px 20px 7px 0; border-bottom: 1px solid var(--rule);
  vertical-align: top; white-space: nowrap; }
table.grid th:last-child, table.grid td:last-child { padding-right: 0; width: 100%;
  white-space: normal; }
table.grid tr:last-child td { border-bottom: none; }
table.grid tbody tr:hover td { background: var(--panel); }
table.grid td.label { width: 210px; color: var(--muted); }
.mono { font-family: var(--mono); font-size: 12.5px; }
.num { font-variant-numeric: tabular-nums; text-align: right; }

/* ---- labels ---------------------------------------------------------- */
/* Two different things wear a label here, so they are drawn differently: a
   `chip` classifies a row, a `tok` *is* a name out of the code. */
.chip { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10.5px;
  font-weight: 650; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap;
  background: var(--sunken); color: var(--muted); }
.chip.added   { background: var(--add-gutter); color: var(--add-fg); }
.chip.removed { background: var(--del-gutter); color: var(--del-fg); }
.chip.modified { background: var(--accent-wash); color: var(--accent); }
.chip.high   { background: var(--del-gutter); color: var(--del-fg); }
.chip.medium { background: var(--warn-bg); color: var(--warn-fg); }
.tok { display: inline-block; font-family: var(--mono); font-size: 12px; padding: 1px 7px;
  border-radius: 4px; background: var(--sunken); border: 1px solid var(--border); }
.tok.added   { background: var(--add-bg); border-color: var(--add-gutter); color: var(--add-fg); }
.tok.removed { background: var(--del-bg); border-color: var(--del-gutter); color: var(--del-fg); }

/* ---- toolbar --------------------------------------------------------- */
.toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px;
  position: sticky; top: 0; background: var(--bg); padding: 10px 0; z-index: 5;
  box-shadow: 0 1px 0 var(--border); }
.toolbar #shown-count { margin-left: auto; white-space: nowrap; flex: 0 0 auto; color: var(--faint); }
.toolbar input[type=search] { flex: 1 1 220px; min-width: 140px; padding: 6px 10px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--panel); color: var(--text); font-size: 13px; }
.toolbar input[type=search]:focus { outline: none; border-color: var(--accent); }
.toolbar label { color: var(--muted); font-size: 13px; display: inline-flex; gap: 6px;
  align-items: center; cursor: pointer; user-select: none; }
.toolbar button { padding: 6px 11px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--panel); color: var(--muted); font-size: 13px; cursor: pointer; }
.toolbar button:hover { border-color: var(--faint); color: var(--text); }

/* ---- one file -------------------------------------------------------- */
details.file { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px;
  background: var(--panel); }
details.file[open] { background: var(--bg); }
details.file > summary { cursor: pointer; padding: 8px 12px; display: flex; gap: 10px;
  align-items: center; list-style: none; font-size: 13px; border-radius: 7px; }
details.file > summary::-webkit-details-marker { display: none; }
details.file > summary:hover { background: var(--panel); }
/* The header stays put while a long file scrolls past, so the path is still
   readable eighty lines into its own diff. */
details.file[open] > summary { position: sticky; top: var(--toolbar-h, 54px); z-index: 3;
  background: var(--panel);
  border-bottom: 1px solid var(--border); border-radius: 7px 7px 0 0; }
summary .path { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }
summary .path .p { font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere; }
/* The tint hugs the changed part exactly — padding here would open a gap in
   the middle of a path and read as though the name contained a space. */
summary .path .ren { background: var(--sunken); border-radius: 3px; }
summary .path .was { color: var(--faint); }
summary .stat-line { font-variant-numeric: tabular-nums; font-size: 12px; white-space: nowrap;
  display: flex; gap: 8px; align-items: baseline; color: var(--faint); }

/* ---- the diff itself ------------------------------------------------- */
.diff { overflow-x: auto; border-top: 1px solid var(--border); }
details.file[open] > .diff { border-top: none; }
.diff table { border-collapse: collapse; width: 100%; font-family: var(--mono);
  font-size: 12.5px; line-height: 1.5; }
.diff td { padding: 0 8px; white-space: pre; vertical-align: top; }
.diff td.ln { width: 1%; min-width: 40px; padding: 0 8px; text-align: right; color: var(--faint);
  user-select: none; background: var(--panel); font-variant-numeric: tabular-nums; }
.diff td.ln + td.ln { border-right: 1px solid var(--border); }
/* The sign lives in its own unselectable cell so that copying a block of the
   diff yields the code, not code with markers glued on. It doubles as the
   spine marking how far an added or removed run reaches. */
.diff td.mark { width: 1%; padding: 0 4px 0 6px; text-align: center; color: var(--faint);
  user-select: none; border-left: 2px solid transparent; }
.diff td.code { padding-left: 4px; width: 100%; }
.diff tr.add td.code, .diff tr.add td.mark { background: var(--add-bg); }
.diff tr.add td.mark { color: var(--add-fg); border-left-color: var(--add-fg); }
.diff tr.add td.ln { background: var(--add-gutter); color: var(--add-fg); }
.diff tr.del td.code, .diff tr.del td.mark { background: var(--del-bg); }
.diff tr.del td.mark { color: var(--del-fg); border-left-color: var(--del-fg); }
.diff tr.del td.ln { background: var(--del-gutter); color: var(--del-fg); }
.diff tr.hunk td { background: var(--sunken); color: var(--faint); font-size: 11.5px;
  padding: 4px 10px; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.diff tr.hunk:first-child td { border-top: none; }
/* Which words of the line actually changed. This sits on top of the row wash,
   so it has to be a step stronger than it in both themes. */
.wd { border-radius: 3px; }
tr.add .wd { background: var(--add-word); }
tr.del .wd { background: var(--del-word); }
/* A file with no usable lines: one row per changed run, the unchanged text
   either side dimmed so the eye lands on the part that moved. Shares the add
   and delete colours with the table above so the two read as one legend. */
.stat-line .skipped { color: var(--faint); font-style: italic; }
.wordedit { overflow-x: auto; border-top: 1px solid var(--border); }
.wordedit table { border-collapse: collapse; width: 100%; font-family: var(--mono);
  font-size: 12px; line-height: 1.7; }
.wordedit td { padding: 1px 8px; white-space: pre; vertical-align: top; }
.wordedit td.at { width: 1%; text-align: right; color: var(--faint);
  background: var(--sunken); border-right: 1px solid var(--border); }
.wordedit td.run { width: 100%; color: var(--faint); }
.wordedit .was { background: var(--del-word); color: var(--del-fg); border-radius: 3px; }
.wordedit .was.gone { text-decoration: line-through; }
.wordedit .now { background: var(--add-word); color: var(--add-fg); border-radius: 3px; }
.tk-c { color: var(--tk-c); font-style: italic; }
.tk-k { color: var(--tk-k); }
.tk-s { color: var(--tk-s); }
.tk-n { color: var(--tk-n); }
.tk-t { color: var(--tk-t); }
.tk-f { color: var(--tk-f); }
.tk-y { color: var(--tk-y); }
.note { color: var(--muted); font-size: 12.5px; padding: 10px 12px; border-top: 1px solid var(--border); }
.note code { font-family: var(--mono); font-size: 12px; background: var(--sunken);
  padding: 1px 5px; border-radius: 4px; }
.empty { color: var(--muted); padding: 16px 0; }
footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--faint); font-size: 12px; line-height: 1.7; }
.moved-list { margin: 0; padding: 10px 16px 12px 34px; list-style: disc;
  color: var(--muted); font-size: 12px; line-height: 1.9; }
.moved-list .hint { color: var(--faint); }
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
      if (ok) shown += parseInt(el.getAttribute('data-files') || '1', 10);
    });
    var counter = document.getElementById('shown-count');
    if (counter) counter.textContent = shown + ' of ' + files.length + ' files shown';
  }

  if (search) search.addEventListener('input', apply);
  if (vendorToggle) vendorToggle.addEventListener('change', apply);

  var toolbar = document.querySelector('.toolbar');
  function measure() {
    if (!toolbar) return;
    document.documentElement.style.setProperty('--toolbar-h', toolbar.offsetHeight + 'px');
  }
  window.addEventListener('resize', measure);
  measure();

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
    """HTML-escape any value, quotes included, rendering None as an empty string.

    Everything interpolated into the page goes through here. The report quotes
    file contents and secret details that came out of a downloaded zip, so
    nothing reaches the template unescaped.
    """
    return html.escape("" if value is None else str(value), quote=True)


@dataclass
class _Row:
    """One rendered line of a diff: its CSS class, both line numbers, and its text.

    Line numbers are strings rather than ints because a row often has only one —
    an added line has no old number — and an empty string is what the cell
    should show.
    """

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


def _intraline(rows: list[_Row]) -> dict[int, list[tuple[int, int]]]:
    """Character ranges worth marking, keyed by the row they belong to.

    Only a removed run with an added run immediately after it is considered:
    that is what a rewrite looks like in a unified diff. An added block with
    nothing before it is a line arriving, not a line changing, and has no
    counterpart to compare it against.
    """
    spans: dict[int, list[tuple[int, int]]] = {}
    at = 0
    while at < len(rows):
        if rows[at].css != "del":
            at += 1
            continue
        start = at
        while at < len(rows) and rows[at].css == "del":
            at += 1
        middle = at
        while at < len(rows) and rows[at].css == "add":
            at += 1
        removed = [r.text for r in rows[start:middle]]
        added = [r.text for r in rows[middle:at]]
        for old_i, new_i in intraline.pair_rows(removed, added):
            before, after = intraline.word_diff(removed[old_i], added[new_i])
            if before or after:
                spans[start + old_i] = before
                spans[middle + new_i] = after
    return spans


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
    """Render one file's change as a titled block with its diff table.

    A rename is titled as one file with only the moved part written twice
    (``boto3-{1.34.0 → 1.35.20}.dist-info/METADATA``), rather than as two
    near-identical 90-character paths the reader has to compare by eye.

    The version directories are passed through so the syntax highlighter can
    read the whole file: colouring a hunk correctly means knowing what was
    happening above it, since a line inside a block comment only looks like a
    comment if you can see where it opened.
    """
    lang = language_of(change.path, change.lang)
    # A rename is one file, not two. Written out in full twice, the two paths
    # are near-identical and the reader has to diff 90 characters by eye to
    # find the part that moved; so only that part is written twice.
    title = _esc(change.path)
    if change.kind == "renamed" and change.old_path:
        head, was, now, tail = rename_label(change.old_path, change.path)
        title = (
            f'{_esc(head)}<span class="ren"><span class="was">{_esc(was)}</span>'
            f' → {_esc(now)}</span>{_esc(tail)}'
        )
    stat = ""
    if change.added_lines:
        stat += f'<span class="add">+{change.added_lines}</span>'
    if change.removed_lines:
        stat += f'<span class="del">−{change.removed_lines}</span>'
    if change.size_delta:
        stat += f"<span>{signed(change.size_delta)} B</span>"
    if note := change.line_count_note:
        # The block is collapsed until someone opens it, so a summary row with
        # no ``+`` and no ``−`` is all most readers ever see of this file. The
        # note is the difference between "measured differently" and "unchanged".
        stat += f'<span class="skipped">{_esc(note)}</span>'

    parts = [
        f'<details class="file" data-path="{_esc(change.path)} {_esc(change.old_path or "")}" '
        f'data-vendor="{1 if change.is_vendor else 0}">',
        "<summary>",
        f'<span class="chip {_esc(change.kind)}">{_esc(change.kind)}</span>',
        f'<span class="path">{icons.file_icon(change.path, lang)}'
        f'<span class="p">{title}</span></span>',
        f'<span class="stat-line">{stat}</span>',
        "</summary>",
    ]

    if change.diff_lines:
        old = _paint(a_root, change.old_path or change.path, lang) if change.old else None
        new = _paint(b_root, change.path, lang) if change.new else None
        rows = _diff_rows(change)
        marks = _intraline(rows)
        parts.append('<div class="diff"><table>')
        for index, row in enumerate(rows):
            if row.css == "hunk":
                parts.append(f'<tr class="hunk"><td colspan="4">{_esc(row.text)}</td></tr>')
                continue
            css = f' class="{row.css}"' if row.css in ("add", "del") else ""
            mark = {"add": "+", "del": "\u2212"}.get(row.css, "")
            code = intraline.mark(_row_code(row, lang, old, new), marks.get(index, []))
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
    elif change.word_edits:
        parts.append(_render_word_edits(change))
    else:
        reason = change.skipped_reason or (
            "file is empty" if change.kind in {"added", "removed"} else "no textual change"
        )
        old_size = change.old.size if change.old else 0
        new_size = change.new.size if change.new else 0
        note = (
            f'No line diff shown ({_esc(reason)}). '
            f"{human_size(old_size)} → {human_size(new_size)}."
        )
        if change.whitespace_only:
            # Naming the two ways out matters more here than for the other
            # reasons: this is the only one the reader might disagree with, and
            # a reindent hiding a real edit is exactly what they would want to
            # check.
            note += (' Indentation, line endings or blank lines only — '
                     '<code>lw diff --whitespace</code> shows it anyway.')
        parts.append(f'<div class="note">{note}</div>')

    parts.append("</details>")
    return "\n".join(parts)


def _render_word_edits(change: FileChange) -> str:
    """Render the changed runs of a file whose lines are too long to diff by line.

    A minified bundle is one 8,000-character line, so its unified diff is the
    whole file quoted twice to show a changed digit. This is the fifty
    characters that carry it instead: an offset, the text either side in grey,
    and the change itself in the same red and green the table above uses.

    No syntax highlighting, deliberately. The runs are fragments cut mid-token
    out of generated code, and colouring them by a lexer that never saw the
    statement they came from would be inventing structure to look thorough. See
    :func:`~.intraline.long_line_edits` for how the runs are found, and
    :func:`~.render_text._print_word_edits` for the same block in the terminal.
    """
    record = change.new or change.old
    lines = record.lines if record else 0
    rows = [
        f'<div class="note">{lines} line{"s" if lines != 1 else ""} of '
        f'{human_size(record.size if record else 0)} — no usable lines, so this is '
        f'diffed by word.</div>',
        '<div class="wordedit"><table>',
    ]
    for edit in change.word_edits:
        lead = ("…" if edit.at > len(edit.lead) else "") + edit.lead
        trail = edit.trail + ("…" if len(edit.trail) == EDIT_CONTEXT else "")
        run = _esc(lead)
        if edit.before:
            gone = "" if edit.after else " gone"
            run += f'<span class="was{gone}">{_esc(edit.before)}</span>'
        if edit.before and edit.after:
            run += " → "
        if edit.after:
            run += f'<span class="now">{_esc(edit.after)}</span>'
        run += _esc(trail)
        rows.append(f'<tr><td class="at">{edit.at}</td><td class="run">{run}</td></tr>')
    rows.append("</table></div>")
    return "\n".join(rows)


def _render_move(group: MoveGroup) -> str:
    """Render one collapsed directory move as a single block, members inside.

    The summary carries the whole decision — both directory names, how many
    files moved, how many were rewritten on the way — and the body lists the
    files, so nothing is hidden that expanding will not show. The alternative
    is twenty blocks whose titles differ only in the filename at the end.

    Kept searchable and filterable like any file block: ``data-path`` holds
    every member path so the filter box still finds one by name, and
    ``data-vendor`` is set only when the whole move is vendored, which is the
    dependency-bump case (``boto3-1.{34.0 → 35.20}.dist-info/``).

    ``data-files`` is how many files this block is the *only* entry for, so the
    "N of M files shown" counter stays a count of files. The edited members are
    left out of it because they follow as blocks of their own and would
    otherwise be counted twice.
    """
    head, was, now, tail = rename_label(*group.display_dirs)
    title = (
        f'{_esc(head)}<span class="ren"><span class="was">{_esc(was)}</span>'
        f' → {_esc(now)}</span>{_esc(tail)}/'
    )
    count = (
        f"{group.moved} files moved" if group.is_whole_dir
        else f"{group.moved} of {group.total_in_old_dir} files moved"
    )
    stat = f"<span>{_esc(count)}</span>"
    if group.edited:
        stat += f'<span class="del">{group.edited} edited</span>'
    if group.added_lines:
        stat += f'<span class="add">+{group.added_lines}</span>'
    if group.removed_lines:
        stat += f'<span class="del">−{group.removed_lines}</span>'
    if group.size_delta:
        stat += f"<span>{signed(group.size_delta)} B</span>"

    searchable = " ".join(c.path for c in group.members)
    searchable += " " + " ".join(c.old_path or "" for c in group.members)
    rows = "".join(
        f'<li class="mono">{_esc(c.path.rpartition("/")[2])}'
        + (' <span class="hint">edited</span>' if c.old and c.new
           and c.old.sha256 != c.new.sha256 else "")
        + "</li>"
        for c in group.members
    )
    return (
        f'<details class="file" data-path="{_esc(searchable)}" '
        f'data-vendor="{1 if group.is_vendor else 0}" '
        f'data-files="{group.moved - group.edited}">'
        "<summary>"
        '<span class="chip renamed">moved</span>'
        f'<span class="path">{icons.file_icon(group.new_dir + "/", "text")}'
        f'<span class="p">{title}</span></span>'
        f'<span class="stat-line">{stat}</span>'
        "</summary>"
        f'<ul class="moved-list">{rows}</ul>'
        "</details>"
    )


def _stats(diff: VersionDiff) -> str:
    """The handful of numbers worth reading before opening a single file."""
    counts = diff.counts()
    size_a = diff.a_meta.get("total_size", 0)
    size_b = diff.b_meta.get("total_size", 0)
    lines = (
        f'<span class="add">+{diff.total_added_lines}</span>'
        f'<span class="del">\u2212{diff.total_removed_lines}</span>'
        if diff.diffs_computed
        else '<span class="delta">not computed</span>'
    )

    # (value markup, label, what rides beside the value, what rides after the
    # label, what hovering the cell explains). Both asides stay on their own
    # line's baseline, so every cell of the rail is exactly two lines tall
    # whatever it has to say — which is why anything longer than a couple of
    # words belongs in the last field rather than the fourth.
    stats: list[tuple[str, str, str, str, str]] = [
        (str(sum(counts.values())), "files changed", "", "", ""),
        (lines, "lines", "", "", ""),
        (str(len(diff.deps)), "dependencies", "", "", ""),
        (_esc(human_size(size_b)), "package size", f"{signed(size_b - size_a)} B", "", ""),
    ]
    if diff.findings_new:
        stats.append((str(len(diff.findings_new)), "new findings", "", "", ""))
    if diff.vendor_files_changed:
        # "not shown" on its own leaves the reader with a number and no way to
        # act on it, and the thing they reach for next is the git mirror, which
        # keeps vendored files and so disagrees with this page about how many
        # files changed. Two lines of rail cannot hold that, so it is the
        # tooltip that says which command produces which answer.
        stats.append((
            str(diff.vendor_files_changed), "vendored files", "", "not shown",
            f"Hidden by diff.ignore_vendor; the dependency table explains the churn "
            f"instead. `lw diff {slugify(diff.function_name)} --vendor --html` rebuilds "
            f"this page with them listed. The git mirror keeps them either way, so "
            f"`lw diff {slugify(diff.function_name)} --mirror` counts more changed files "
            f"than this page does.",
        ))
    if diff.renames_unexamined:
        # Without this the reader has no way to tell a complete rename map from
        # one the pair budget cut short.
        stats.append(
            (str(diff.renames_unexamined), "files", "", "not rename-checked",
             "The pair budget ran out, so some of the added and removed files below "
             "may be halves of the same moved file. Raise diff.max_rename_pairs.")
        )

    cells: list[str] = []
    for value, label, delta, hint, tip in stats:
        beside = f'<span class="delta">{_esc(delta)}</span>' if delta else ""
        after = f' <span class="hint">{_esc(hint)}</span>' if hint else ""
        title = f' title="{_esc(tip)}"' if tip else ""
        cells.append(
            f'<div class="stat"{title}><div class="v">{value}{beside}</div>'
            f'<div class="k">{_esc(label)}{after}</div></div>'
        )
    return f'<div class="stats">{"".join(cells)}</div>'


def _dep_table(diff: VersionDiff) -> str:
    """Render the dependency table, or an empty string when nothing moved.

    Each row shows the package, the versions on both sides, and whether the fact
    came from a manifest (``declared``) or from what was vendored in the zip
    (``installed``).
    """
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
        "<h2>Dependencies</h2><div class='scroll'><table class='grid'>"
        "<thead><tr><th></th><th>package</th><th>from</th><th>to</th>"
        "<th>manager</th><th>origin</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _context_section(diff: VersionDiff) -> str:
    """Render the environment variables and AWS services that came and went.

    Empty string when neither changed, so the section disappears rather than
    appearing empty.
    """
    blocks: list[str] = []

    def listing(label: str, values: list[str], css: str, hint: str = "") -> str:
        """One labelled row of identifiers, or an empty string when there are none.

        These are identifiers out of the function's own code, so they are set in
        the same face the diff sets them in — not as prose in a sentence.
        """
        if not values:
            return ""
        chips = " ".join(f'<span class="tok {css}">{_esc(v)}</span>' for v in values)
        hint_html = f'<div class="sub" style="margin-top:6px">{_esc(hint)}</div>' if hint else ""
        return f"<tr><td class='label'>{_esc(label)}</td><td>{chips}{hint_html}</td></tr>"

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
        blocks.append(
            f"<h2>Configuration impact</h2><table class='grid'><tbody>{rows}</tbody></table>"
        )

    if diff.runtime_change or diff.handler_change:
        entries = []
        if diff.runtime_change:
            entries.append(
                f"<tr><td class='label'>Runtime</td><td class='mono'>"
                f"{_esc(diff.runtime_change[0])} → {_esc(diff.runtime_change[1])}</td></tr>"
            )
        if diff.handler_change:
            before, after = diff.handler_change
            entries.append(
                f"<tr><td class='label'>Handler</td><td class='mono'>"
                f"{_esc(before or '?')} → {_esc(after or '?')}</td></tr>"
            )
        blocks.append(f"<h2>Entry point</h2><table class='grid'><tbody>{''.join(entries)}</tbody></table>")

    return "".join(blocks)


def _findings_section(diff: VersionDiff) -> str:
    """Render new and resolved security findings, or an empty string when there are none.

    Details are already redacted by the scanner, so what lands in the page shows
    the shape of a credential without carrying the credential itself.
    """
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
        "<div class='scroll'><table class='grid'><thead><tr><th>severity</th><th>kind</th>"
        f"<th>where</th><th>detail</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        if rows
        else ""
    )
    return f"<h2>New findings</h2>{table}{resolved}"


def render_html(diff: VersionDiff, generated_by: str = "lambda-watcher") -> str:
    """Render the full report as a single HTML document."""
    title = f"{diff.function_name} · v{diff.a_seq:04d} → v{diff.b_seq:04d}"
    a_when = format_ts(diff.a_meta.get("ingested_at"))
    b_when = format_ts(diff.b_meta.get("ingested_at"))

    blocks: list[str] = []
    for row in diff.file_rows():
        if not isinstance(row, MoveGroup):
            blocks.append(_render_file(row, diff.a_root, diff.b_root))
            continue
        # The group block reports the move; it has no room for a diff, so the
        # members that were rewritten on the way keep their own blocks after it.
        blocks.append(_render_move(row))
        blocks.extend(
            _render_file(c, diff.a_root, diff.b_root) for c in row.edited_members
        )
    file_blocks = "\n".join(blocks)
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
    <h1>{_esc(diff.function_name)}
      <span class="ver">v{diff.a_seq:04d}<span class="arrow">→</span>v{diff.b_seq:04d}</span></h1>
    <div class="sub">
      {_esc(diff.headline())} · v{diff.a_seq:04d} archived {_esc(a_when)},
      v{diff.b_seq:04d} archived {_esc(b_when)}
    </div>
  </header>

  {_stats(diff)}
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
    """Render the diff and write it to ``path``, creating parent directories.

    Returns the path so callers can print it. This is what the background
    ingest calls to leave ``reports/<function>/latest.html`` sitting there
    before anyone thinks to ask what changed.
    """
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
            f'<td class="num">{entry.get("file_count", 0):,}</td>'
            f'<td class="num">{_esc(human_size(entry.get("total_size", 0)))}</td>'
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
  <div class="scroll"><table class="grid">
    <thead><tr>
      <th>version</th><th>archived</th><th>runtime</th><th>handler</th>
      <th class="num">files</th><th class="num">size</th>
      <th>downloaded as</th><th>change from previous</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
  <footer>Generated by {_esc(generated_by)} on {_esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}.</footer>
</div>
</body>
</html>
"""
