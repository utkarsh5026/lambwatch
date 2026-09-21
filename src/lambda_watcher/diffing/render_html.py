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
.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px;
  position: sticky; top: 0; background: var(--bg); padding: 10px 0; z-index: 6;
  box-shadow: 0 1px 0 var(--border); }
.toolbar #shown-count { margin-left: auto; white-space: nowrap; flex: 0 0 auto; color: var(--faint); }
.toolbar input[type=search] { flex: 1 1 240px; min-width: 150px; padding: 7px 11px; border-radius: 7px;
  border: 1px solid var(--border); background: var(--panel); color: var(--text); font-size: 13px; }
.toolbar input[type=search]:focus { outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-wash); }
.toolbar label { color: var(--muted); font-size: 13px; display: inline-flex; gap: 6px;
  align-items: center; cursor: pointer; user-select: none; }

/* ---- the file list --------------------------------------------------- */
/* One panel of rows rather than a stack of cards, because the list is an index
   now: the diff it points at opens beside it instead of pushing the rest of
   the list down the page. A row is a button, since that is what it does. */
.files { border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
  background: var(--panel); }
.file { border-top: 1px solid var(--rule); }
/* Transparent rather than absent so every row is the same height, whichever
   one the filter left at the top. */
.file:first-child, .file.first-shown { border-top-color: transparent; }
.row { display: flex; gap: 10px; align-items: center; width: 100%; padding: 9px 14px;
  font: inherit; color: var(--text); text-align: left; background: none; border: 0;
  cursor: pointer; }
.row:hover { background: var(--sunken); }
.row:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
/* The chevron points where the diff will appear: to the side, not downwards.
   Drawn in CSS because a vendored diff runs to thousands of rows, and each one
   would otherwise carry its own copy of the glyph. */
.row::after { content: ""; flex: 0 0 auto; width: 6px; height: 6px; margin-left: 2px;
  border: 1.6px solid var(--faint); border-left: 0; border-bottom: 0;
  transform: rotate(45deg); transition: transform .15s ease, border-color .15s ease; }
.row:hover::after { border-color: var(--accent); transform: translateX(2px) rotate(45deg); }
.file.active .row { background: var(--accent-wash); box-shadow: inset 3px 0 0 var(--accent); }
.file.active .row::after { border-color: var(--accent); transform: translateX(2px) rotate(45deg); }
.row .path { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }
.row .path .p { font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere; }
/* The tint hugs the changed part exactly — padding here would open a gap in
   the middle of a path and read as though the name contained a space. */
.path .ren { background: var(--sunken); border-radius: 3px; }
.path .was { color: var(--faint); }
.stat-line { font-variant-numeric: tabular-nums; font-size: 12px; white-space: nowrap;
  display: flex; gap: 8px; align-items: baseline; color: var(--faint); }

/* ---- the diff itself ------------------------------------------------- */
.diff { overflow-x: auto; border-top: 1px solid var(--border); }
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
/* ---- the sheet ------------------------------------------------------- */
/* Where the diff lands when a row is clicked. Code wants width, and a block
   that opened downwards spent the page's widest dimension on the file list it
   had just pushed out of view; this keeps the list where it was, so the next
   file is one click away rather than one scroll back.

   A wide window docks the sheet and the page makes room beside it. A narrow
   one slides it over the page with a scrim, because a laptop in portrait has
   no room to dock and half a diff is worse than a covered list. */
:root { --sheet-w: 760px; }
.scrim { position: fixed; inset: 0; z-index: 30; background: rgba(8, 13, 20, .44);
  opacity: 0; visibility: hidden; transition: opacity .26s ease, visibility 0s linear .26s; }
body.sheet-open .scrim { opacity: 1; visibility: visible; transition: opacity .26s ease; }
.sheet { position: fixed; top: 0; right: 0; bottom: 0; z-index: 40;
  width: min(100%, var(--sheet-w)); display: flex; flex-direction: column;
  background: var(--bg); border-left: 1px solid var(--border);
  box-shadow: -24px 0 60px -30px rgba(6, 11, 18, .5);
  /* `visibility` rather than `display`, so the sheet can animate out and still
     leave nothing behind for the keyboard to land on while it is shut. It turns
     visible on the same frame it is asked to — a transition would leave it
     unfocusable for the length of the slide, which is exactly when the script
     is moving focus into it — and back to hidden only once the slide is over. */
  transform: translateX(100%); visibility: hidden;
  transition: transform .26s cubic-bezier(.22, .61, .36, 1), visibility 0s linear .26s; }
body.sheet-open .sheet { transform: none; visibility: visible;
  transition: transform .26s cubic-bezier(.22, .61, .36, 1); }
.sheet-head { padding: 12px 14px 11px; border-bottom: 1px solid var(--border);
  background: var(--panel); display: flex; flex-direction: column; gap: 7px; }
.sheet-bar { display: flex; align-items: center; gap: 10px; }
.sheet-title { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
.sheet-title .path { display: flex; align-items: center; gap: 8px; min-width: 0; }
.sheet-title .p { font-family: var(--mono); font-size: 13px; font-weight: 600;
  overflow-wrap: anywhere; }
/* Wrapping rather than clipping: a file whose counts are a sentence — "missing
   from the archive" — is exactly the one whose reader needs to read them. */
.sheet-sub { display: flex; align-items: baseline; gap: 12px; min-height: 18px;
  flex-wrap: wrap; color: var(--faint); font-size: 12px; }
.sheet-sub .ver { margin-left: auto; font-family: var(--mono); white-space: nowrap; }
.sheet-nav { display: flex; align-items: center; gap: 2px; flex: 0 0 auto; }
.sheet-nav .pos { color: var(--faint); font-size: 12px; padding: 0 3px;
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.iconbtn { display: inline-flex; align-items: center; justify-content: center;
  width: 28px; height: 28px; padding: 0; border-radius: 7px; border: 1px solid transparent;
  background: none; color: var(--muted); cursor: pointer; }
.iconbtn:hover:not(:disabled) { background: var(--sunken); border-color: var(--border);
  color: var(--text); }
.iconbtn:disabled { opacity: .3; cursor: default; }
.iconbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.iconbtn svg { width: 16px; height: 16px; fill: none; stroke: currentColor;
  stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.iconbtn.close { margin-left: 5px; }
.sheet-body { flex: 1; overflow: auto; overscroll-behavior: contain; }
.sheet-body:focus { outline: none; }
/* The panel's own top rule would double the header's. */
.sheet-body > .body > :first-child { border-top: none; }
/* One file wants sixty columns and the next wants two hundred, and which one
   is on screen is not something the page can know — so the edge is draggable
   wherever there is a pointer and room to dock. */
.grip { position: absolute; left: 0; top: 0; bottom: 0; width: 11px; display: none;
  cursor: col-resize; }
.grip::before { content: ""; position: absolute; left: 4px; top: 50%; width: 3px; height: 44px;
  margin-top: -22px; border-radius: 3px; background: var(--border); transition: background .15s; }
.grip:hover::before, body.dragging .grip::before { background: var(--accent); }
body.dragging { user-select: none; cursor: col-resize; }
body.dragging, body.dragging .sheet { transition: none; }

@media (min-width: 1280px) {
  :root { --sheet-w: min(50vw, 960px); }
  .scrim { display: none; }
  .grip { display: block; }
  body { transition: padding-right .26s cubic-bezier(.22, .61, .36, 1); }
  body.sheet-open { padding-right: min(var(--sheet-w), 76vw); }
}
/* Nothing to dock into: the sheet is over the page, so the page stops scrolling
   underneath it. */
@media (max-width: 1279.98px) {
  body.sheet-open { overflow: hidden; }
}
@media (prefers-reduced-motion: reduce) {
  body, .sheet, .scrim, .row::after { transition: none; }
}
/* On paper there is no clicking, so every diff is printed under its own row and
   the chrome that only answers a pointer is left out. */
@media print {
  .toolbar, .scrim, .sheet-nav, .grip { display: none !important; }
  .sheet { position: static; transform: none; visibility: visible; width: auto;
    border-left: 0; box-shadow: none; }
  body.sheet-open { padding-right: 0; overflow: visible; }
  .file .body[hidden] { display: block; }
}
.hidden { display: none !important; }
"""

#: What the page does once it is open: narrow the list, and move one file's diff
#: into the sheet and back out again. Moving rather than copying is the whole
#: design — the diff exists once, so the sheet cannot drift from the row, and
#: closing it leaves the page exactly as it was rendered. One ``<script>`` at the
#: end of the body, so everything it binds to is already parsed.
JS = """
(function () {
  var search = document.getElementById('filter');
  var vendorToggle = document.getElementById('vendor');
  var counter = document.getElementById('shown-count');
  var files = Array.prototype.slice.call(document.querySelectorAll('.files .file'));
  var scrim = document.getElementById('scrim');
  var host = document.getElementById('sheet-body');
  var titleBox = document.getElementById('sheet-title');
  var statBox = document.getElementById('sheet-stat');
  var posBox = document.getElementById('sheet-pos');
  var prevBtn = document.getElementById('sheet-prev');
  var nextBtn = document.getElementById('sheet-next');
  var closeBtn = document.getElementById('sheet-close');
  var grip = document.getElementById('sheet-grip');
  var openFile = null;   // whose diff is sitting in the sheet right now
  var opener = null;     // the row that put it there, to hand focus back to

  function listed() {
    return files.filter(function (el) { return !el.classList.contains('hidden'); });
  }

  function empty(node) {
    while (node && node.firstChild) { node.removeChild(node.firstChild); }
  }

  // The sheet header says the same things the row does, so it is built from a
  // copy of the row rather than from a second set of markup on every file.
  function copyInto(node, source) {
    empty(node);
    if (node && source) { node.appendChild(source.cloneNode(true)); }
  }

  function position() {
    var rows = listed();
    var at = openFile ? rows.indexOf(openFile) : -1;
    if (posBox) { posBox.textContent = at < 0 ? '' : (at + 1) + ' / ' + rows.length; }
    if (prevBtn) { prevBtn.disabled = at <= 0; }
    if (nextBtn) { nextBtn.disabled = at < 0 || at >= rows.length - 1; }
  }

  // The diff is moved into the sheet rather than copied, so this puts it back
  // where it came from. One diff exists at a time, wherever it is showing.
  function park() {
    if (!openFile) { return; }
    var body = host.firstElementChild;
    if (body) { body.hidden = true; openFile.appendChild(body); }
    openFile.classList.remove('active');
    var row = openFile.querySelector('.row');
    if (row) { row.setAttribute('aria-expanded', 'false'); }
    openFile = null;
  }

  function show(file, takeFocus) {
    var row = file && file.querySelector('.row');
    if (!row || !host) { return; }
    park();
    openFile = file;
    file.classList.add('active');
    row.setAttribute('aria-expanded', 'true');
    var body = file.querySelector('.body');
    if (body) { body.hidden = false; host.appendChild(body); }
    host.scrollTop = 0;
    empty(titleBox);
    ['.chip', '.path'].forEach(function (part) {
      var found = row.querySelector(part);
      if (found && titleBox) { titleBox.appendChild(found.cloneNode(true)); }
    });
    copyInto(statBox, row.querySelector('.stat-line'));
    document.body.classList.add('sheet-open');
    position();
    if (takeFocus) { host.focus(); }
  }

  function close() {
    park();
    document.body.classList.remove('sheet-open');
    empty(titleBox);
    empty(statBox);
    position();
    if (opener) { opener.focus(); opener = null; }
  }

  function step(delta) {
    var rows = listed();
    var at = openFile ? rows.indexOf(openFile) : -1;
    var next = at < 0 ? null : rows[at + delta];
    if (next) {
      // Focus stays on the button that is walking the list, so the next press
      // lands on it too.
      show(next, false);
      opener = next.querySelector('.row');
    }
  }

  function apply() {
    var term = (search ? search.value : '').toLowerCase();
    var showVendor = vendorToggle ? vendorToggle.checked : true;
    var shown = 0;
    var total = 0;
    var first = true;
    files.forEach(function (el) {
      var path = (el.getAttribute('data-path') || '').toLowerCase();
      var isVendor = el.getAttribute('data-vendor') === '1';
      var ok = (!term || path.indexOf(term) !== -1) && (showVendor || !isVendor);
      // A move block stands for every file it folded up, so both halves of the
      // count are sums of `data-files` rather than counts of rows.
      var covers = parseInt(el.getAttribute('data-files') || '1', 10);
      el.classList.toggle('hidden', !ok);
      el.classList.toggle('first-shown', ok && first);
      total += covers;
      if (ok) { shown += covers; first = false; }
    });
    if (counter) { counter.textContent = shown + ' of ' + total + ' files shown'; }
    // Leaving the sheet open on a file the filter just took off the list would
    // show a diff with no row to close it from.
    if (openFile && openFile.classList.contains('hidden')) { close(); } else { position(); }
  }

  files.forEach(function (file) {
    var row = file.querySelector('.row');
    if (!row) { return; }
    row.addEventListener('click', function () {
      if (file === openFile) { close(); return; }
      opener = row;
      show(file, true);
    });
  });

  if (search) { search.addEventListener('input', apply); }
  if (vendorToggle) { vendorToggle.addEventListener('change', apply); }
  if (closeBtn) { closeBtn.addEventListener('click', close); }
  if (scrim) { scrim.addEventListener('click', close); }
  if (prevBtn) { prevBtn.addEventListener('click', function () { step(-1); }); }
  if (nextBtn) { nextBtn.addEventListener('click', function () { step(1); }); }

  document.addEventListener('keydown', function (ev) {
    if (!openFile || ev.ctrlKey || ev.metaKey || ev.altKey) { return; }
    if (ev.key === 'Escape') { close(); return; }
    var tag = (ev.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea') { return; }
    if (ev.key === 'j') { step(1); ev.preventDefault(); }
    if (ev.key === 'k') { step(-1); ev.preventDefault(); }
  });

  if (grip && window.PointerEvent) {
    grip.addEventListener('pointerdown', function (down) {
      down.preventDefault();
      grip.setPointerCapture(down.pointerId);
      document.body.classList.add('dragging');
      function drag(move) {
        // Clamped so a drag can neither shut the sheet nor bury the list
        // behind it, whichever direction it is thrown in.
        var width = Math.min(Math.max(window.innerWidth - move.clientX, 380),
                             Math.max(window.innerWidth - 320, 380));
        document.documentElement.style.setProperty('--sheet-w', Math.round(width) + 'px');
      }
      function drop() {
        document.body.classList.remove('dragging');
        grip.removeEventListener('pointermove', drag);
        grip.removeEventListener('pointerup', drop);
        grip.removeEventListener('pointercancel', drop);
      }
      grip.addEventListener('pointermove', drag);
      grip.addEventListener('pointerup', drop);
      grip.addEventListener('pointercancel', drop);
    });
  }

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


def _file_block(
    *,
    path_key: str,
    chip_class: str,
    chip_text: str,
    icon: str,
    title: str,
    stat: str,
    body: str,
    is_vendor: bool,
    files: int | None = None,
) -> str:
    """One row of the file list, carrying the diff the sheet opens when it is clicked.

    The row is what the reader scans — kind, icon, path, counts — and ``body``
    is what opens beside it. The body ships inside the row's own element rather
    than in a second list keyed by path, so a block stays one self-contained
    thing to filter, hide or print; the script moves that element into the sheet
    and back out again, which is why no diff is ever on the page twice.

    ``path_key`` is what the filter box matches on: a file's own path and the
    one it was renamed from, or every member path of a move. ``files`` is how
    many files the block is the only entry for, left off when that is one.
    """
    extra = f' data-files="{files}"' if files is not None else ""
    return (
        f'<article class="file" data-path="{_esc(path_key)}" '
        f'data-vendor="{1 if is_vendor else 0}"{extra}>'
        '<button class="row" type="button" aria-expanded="false" aria-controls="sheet">'
        f'<span class="chip {_esc(chip_class)}">{_esc(chip_text)}</span>'
        f'<span class="path">{icon}<span class="p">{title}</span></span>'
        f'<span class="stat-line">{stat}</span>'
        "</button>"
        f'<div class="body" hidden>{body}</div>'
        "</article>"
    )


def _render_file(change: FileChange, a_root: Path | None = None, b_root: Path | None = None) -> str:
    """Render one file's change as a list row and the diff table behind it.

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
        # The diff is behind a click, so a row with no ``+`` and no ``−`` is all
        # most readers ever see of this file. The note is the difference between
        # "measured differently" and "unchanged".
        stat += f'<span class="skipped">{_esc(note)}</span>'

    parts: list[str] = []
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
            mark = {"add": "+", "del": "−"}.get(row.css, "")
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
        elif change.missing:
            # The one reason here that is not about the file: nothing is wrong
            # with it, the archive just does not have it where the index says.
            note += (' The index lists this file but the version directory does not '
                     'hold it — <code>lw reindex</code> rebuilds the index from what '
                     'is on disk.')
        parts.append(f'<div class="note">{note}</div>')

    return _file_block(
        path_key=f'{change.path} {change.old_path or ""}',
        chip_class=change.kind,
        chip_text=change.kind,
        icon=icons.file_icon(change.path, lang),
        title=title,
        stat=stat,
        body="\n".join(parts),
        is_vendor=change.is_vendor,
    )


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

    Kept searchable and filterable like any file block: the filter box matches
    on every member path, and the block counts as vendored only when the whole
    move is, which is the dependency-bump case
    (``boto3-1.{34.0 → 35.20}.dist-info/``).

    The file count it reports to :func:`_file_block` leaves out the edited
    members, so the "N of M files shown" counter stays a count of files: those
    members follow as blocks of their own and would otherwise be counted twice.
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
    return _file_block(
        path_key=searchable,
        chip_class="renamed",
        chip_text="moved",
        icon=icons.file_icon(group.new_dir + "/", "text"),
        title=title,
        stat=stat,
        body=f'<ul class="moved-list">{rows}</ul>',
        is_vendor=group.is_vendor,
        files=group.moved - group.edited,
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


#: Without the script the sheet can never open, so the diffs sit under their own
#: rows instead and the controls that would do nothing are taken off the page.
NOSCRIPT = """
.toolbar, .sheet, .scrim { display: none; }
.row { cursor: default; }
.row::after { display: none; }
.file .body[hidden] { display: block; }
"""


def _nav_button(ident: str, label: str, glyph: str, extra: str = "") -> str:
    """One of the sheet's three controls: a 16×16 stroked glyph with a spoken name.

    ``label`` names the keystroke as well as the action (``Next file (j)``),
    because a shortcut on a page with no menu has nowhere else to be found.
    """
    return (
        f'<button type="button" class="iconbtn{extra}" id="{ident}" '
        f'title="{_esc(label)}" aria-label="{_esc(label)}">'
        f'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="{glyph}"/></svg></button>'
    )


def _sheet(diff: VersionDiff) -> str:
    """The panel a file's diff opens into, and the scrim behind it on a narrow window.

    One frame per page rather than one per file: the script moves the clicked
    file's diff into it, so what is written here is everything around that diff
    — the path, its counts, which two versions are being compared, and the
    controls that walk the list without going back to it.

    The heading is left empty because it is filled from the row that was
    clicked; see the ``show`` function in :data:`JS`.
    """
    return (
        '<div class="scrim" id="scrim"></div>'
        '<aside class="sheet" id="sheet" role="dialog" aria-labelledby="sheet-title">'
        '<div class="grip" id="sheet-grip" title="Drag to resize"></div>'
        '<header class="sheet-head"><div class="sheet-bar">'
        '<div class="sheet-title" id="sheet-title"></div>'
        '<div class="sheet-nav">'
        + _nav_button("sheet-prev", "Previous file (k)", "m4.5 9.75 3.5-3.5 3.5 3.5")
        + '<span class="pos" id="sheet-pos"></span>'
        + _nav_button("sheet-next", "Next file (j)", "m4.5 6.25 3.5 3.5 3.5-3.5")
        + _nav_button("sheet-close", "Close (Esc)", "m4.5 4.5 7 7m0-7-7 7", " close")
        + "</div></div>"
        '<div class="sheet-sub"><span id="sheet-stat"></span>'
        f'<span class="ver">v{diff.a_seq:04d} → v{diff.b_seq:04d}</span>'
        "</div></header>"
        '<div class="sheet-body" id="sheet-body" tabindex="-1"></div>'
        "</aside>"
    )


def render_html(diff: VersionDiff, generated_by: str = "lambda-watcher") -> str:
    """Render the full report as a single HTML document.

    What the reader gets is a summary they can take in without scrolling, then
    a list of every changed file. Clicking one opens its diff in the sheet
    beside the list rather than underneath it — see :func:`_sheet` — so the
    list keeps its place and the code gets the width it wants.
    """
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
    vendor_toggle = (
        '<label><input type="checkbox" id="vendor" checked> show vendored files</label>'
        if any(c.is_vendor for c in diff.files)
        else ""
    )
    # Two versions with the same tree have nothing to filter and nothing to
    # open, so that page is one sentence: a search box above it would only
    # offer to narrow an empty list.
    toolbar = listing = ""
    if diff.files:
        toolbar = (
            '<div class="toolbar">'
            '<input type="search" id="filter" placeholder="Filter by path…" autocomplete="off">'
            f'{vendor_toggle}<span class="sub" id="shown-count"></span></div>'
        )
        listing = '<div class="files">{}</div>'.format("\n".join(blocks))
    else:
        listing = '<div class="empty">No file-level changes between these versions.</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{CSS}{ICON_CSS}</style>
<noscript><style>{NOSCRIPT}</style></noscript>
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
  {toolbar}
  {listing}

  <footer>
    Generated by {_esc(generated_by)} on {_esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}.
    Content hashes ignore zip timestamps, so re-downloading unchanged code does not create a new version.
  </footer>
</div>
{_sheet(diff)}
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
