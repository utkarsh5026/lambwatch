"""Self-contained HTML report for a version diff.

No JavaScript frameworks, no CDN, no network: one file you can open, keep,
attach to a change ticket, or send to a colleague.
"""

from __future__ import annotations

import html
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..ai.explanation import PENDING_STALE_SECONDS
from ..utils import format_ts, human_size, read_text, rename_label, signed, slugify
from . import icons, intraline

if TYPE_CHECKING:
    from ..ai.explanation import Explanation
    from ..ai.report import AIPanel
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
  /* Surfaces step up from the page to the card to the sunken well inside it, so
     the sections read as sections without a heavy rule drawn round each one. */
  --page: #f6f7f9; --card: #ffffff; --panel: #fafbfc; --sunken: #f1f3f6;
  --border: #e4e7ec; --rule: #eef0f3; --text: #111827; --muted: #5d6675; --faint: #8b93a1;
  --accent: #4f56d8; --accent-wash: #eef0ff; --accent-edge: #d7dafd;
  --add-bg: #ecf8f1; --add-word: #b8ebcb; --add-gutter: #d9f1e3; --add-fg: #0d7a40;
  --del-bg: #fdf0f1; --del-word: #f7c9cf; --del-gutter: #f8dde1; --del-fg: #b4233a;
  --warn-bg: #fdf5e3; --warn-edge: #f3dfae; --warn-fg: #8a5a00;
  --shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 1px 3px rgba(16, 24, 40, .06);
  --radius: 12px;
  --sans: "Inter", "InterVariable", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
          Roboto, Helvetica, Arial, sans-serif;
  --mono: ui-monospace, "JetBrains Mono", SFMono-Regular, "SF Mono", Menlo, Consolas,
          "Liberation Mono", monospace;
  /* Syntax tokens, One Light. Numbers and constants share a colour on purpose:
     both are literal values, and the eye reads them as the same thing. */
  --tk-c: #8b8f97; --tk-k: #a626a4; --tk-s: #50a14f; --tk-n: #986801;
  --tk-t: #986801; --tk-f: #4078f2; --tk-y: #0184bc;
  /* What a model wrote wears its own colour, so it is never mistaken for
     something the diff engine measured. */
  --ai: #6b4fd8; --ai-wash: #f5f2ff; --ai-edge: #e3dbfd;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #0b0d12; --card: #12151c; --panel: #161a22; --sunken: #1b2029;
    --border: #252b36; --rule: #1d222b; --text: #e6e9ef; --muted: #99a2b0; --faint: #6c7584;
    --accent: #8f95ff; --accent-wash: #1c1f3a; --accent-edge: #30356a;
    --add-bg: #0f2519; --add-word: #1f5a35; --add-gutter: #143020; --add-fg: #6fdc97;
    --del-bg: #2a1319; --del-word: #6d2029; --del-gutter: #3a1920; --del-fg: #ff97a2;
    --warn-bg: #2d2412; --warn-edge: #4a3a16; --warn-fg: #ebc56f;
    --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 0 0 1px rgba(255, 255, 255, .01);
    --tk-c: #7f848e; --tk-k: #c678dd; --tk-s: #98c379; --tk-n: #d19a66;
    --tk-t: #d19a66; --tk-f: #61afef; --tk-y: #56b6c2;
    --ai: #b3a1ff; --ai-wash: #1d1834; --ai-edge: #372d63;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--text);
  font: 14px/1.55 var(--sans); font-feature-settings: "cv11", "ss01";
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 3px; }
.wrap { max-width: 1140px; margin: 0 auto; padding: 0 24px 80px; }

/* ---- top bar --------------------------------------------------------- */
/* Every page carries the same bar, so the three kinds of page read as one
   site: where you are, and the way back to the archive's front page. */
.topbar { position: relative; z-index: 7; background: var(--card);
  border-bottom: 1px solid var(--border); }
.topbar .inner { max-width: 1140px; margin: 0 auto; padding: 0 24px; height: 52px;
  display: flex; align-items: center; gap: 10px; font-size: 13px; }
.brand { display: inline-flex; align-items: center; gap: 9px; font-weight: 600;
  color: var(--text); letter-spacing: -0.01em; white-space: nowrap; }
.brand .logo { display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; border-radius: 7px; color: #fff; font-size: 14px; font-weight: 700;
  background: linear-gradient(135deg, #6d74f2, #4148c9); box-shadow: inset 0 -1px 0 rgba(0,0,0,.18); }
.crumbs { display: flex; align-items: center; gap: 10px; min-width: 0; color: var(--muted); }
.crumbs .sep { color: var(--border); font-size: 18px; font-weight: 300; }
.crumbs a { color: var(--muted); white-space: nowrap; }
.crumbs a:hover { color: var(--text); text-decoration: none; }
.crumbs .here { color: var(--text); font-weight: 500; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.topbar .when { margin-left: auto; color: var(--faint); font-size: 12px; white-space: nowrap; }

/* ---- page header ----------------------------------------------------- */
header.top { padding: 34px 0 26px; }
.eyebrow { font-size: 12px; font-weight: 600; color: var(--accent); letter-spacing: .02em;
  margin-bottom: 8px; }
h1 { font-size: 28px; font-weight: 700; margin: 0; letter-spacing: -0.025em; line-height: 1.2;
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap; overflow-wrap: anywhere; }
h1 .ver { font-family: var(--mono); font-size: 13px; font-weight: 600; letter-spacing: 0;
  color: var(--accent); font-variant-numeric: tabular-nums;
  background: var(--accent-wash); border: 1px solid var(--accent-edge);
  border-radius: 999px; padding: 3px 11px; white-space: nowrap; }
h1 .ver .arrow { color: var(--faint); padding: 0 6px; font-weight: 400; }
.sub { color: var(--muted); font-size: 13px; }
.lead { color: var(--muted); font-size: 15px; margin-top: 8px; }
/* The two versions being compared, as a pair of stamps with the direction of
   travel between them — the header's one piece of real information after the
   name, so it gets drawn rather than buried in a sentence. */
.stamps { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
.stamp { display: inline-flex; align-items: baseline; gap: 8px; padding: 6px 12px;
  background: var(--card); border: 1px solid var(--border); border-radius: 9px;
  box-shadow: var(--shadow); font-size: 12.5px; color: var(--muted); }
.stamp b { font-family: var(--mono); font-weight: 600; color: var(--text); font-size: 12.5px; }
.stamps .to { color: var(--faint); }

/* ---- summary --------------------------------------------------------- */
/* One card divided by hairlines rather than six floating cards. These numbers
   are meant to be read across, and separate boxes put a gutter between every
   pair of them. The label sits above its number, where the eye starts. */
.stats { display: flex; flex-wrap: wrap; background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; margin-bottom: 20px; }
.stat { flex: 1 1 150px; padding: 16px 20px; border-left: 1px solid var(--rule); min-width: 0;
  display: flex; flex-direction: column-reverse; justify-content: space-between; gap: 4px; }
.stat:first-child { border-left: none; }
.stat .v { font-size: 24px; font-weight: 650; letter-spacing: -0.03em; line-height: 1.15;
  font-variant-numeric: tabular-nums; display: flex; align-items: baseline; gap: 8px; }
.stat .v .delta { font-size: 12px; font-weight: 500; color: var(--muted);
  letter-spacing: 0; white-space: nowrap; }
.stat .k { color: var(--muted); font-size: 12.5px; font-weight: 500; }
.stat .k .hint { color: var(--faint); font-weight: 400; }
.stat[title] { cursor: help; }
.stat.alert .v { color: var(--del-fg); }
.add { color: var(--add-fg); } .del { color: var(--del-fg); }

/* ---- cards and their headings ---------------------------------------- */
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); margin-bottom: 20px; }
.card > .scroll, .card > table.grid { padding: 0 20px 6px; }
.sec-head { display: flex; align-items: center; gap: 10px; padding: 14px 20px 12px; }
h2 { font-size: 15px; font-weight: 650; letter-spacing: -0.01em; margin: 0; color: var(--text); }
.count { font-size: 11.5px; font-weight: 600; color: var(--muted); background: var(--sunken);
  border-radius: 999px; padding: 1px 8px; font-variant-numeric: tabular-nums; }
.sec-head .aside { margin-left: auto; color: var(--faint); font-size: 12.5px; }
/* A section that asks the reader to do something before deploying wears a
   tinted edge, so it is the first thing a scroll lands on. */
.card.warn { border-color: var(--warn-edge); }
.card.warn .sec-head { background: var(--warn-bg); border-radius: var(--radius) var(--radius) 0 0;
  border-bottom: 1px solid var(--warn-edge); margin-bottom: 6px; }
.card.warn h2 { color: var(--warn-fg); }
.card.danger { border-color: var(--del-gutter); }
.card.danger .sec-head { background: var(--del-bg); border-radius: var(--radius) var(--radius) 0 0;
  border-bottom: 1px solid var(--del-gutter); margin-bottom: 6px; }
.card.danger h2 { color: var(--del-fg); }
.sec-head .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.card.warn .sec-head .dot { color: var(--warn-fg); }
.card.danger .sec-head .dot { color: var(--del-fg); }
.card .foot { padding: 10px 20px 14px; border-top: 1px solid var(--rule); }

/* ---- tables ---------------------------------------------------------- */
.scroll { overflow-x: auto; }
table.grid { border-collapse: collapse; font-size: 13px; width: 100%; }
.card > table.grid { width: calc(100% - 40px); margin: 0 20px 6px; padding: 0; }
table.grid th { text-align: left; color: var(--faint); font-weight: 500; font-size: 12px;
  padding: 8px 20px 8px 0; border-bottom: 1px solid var(--border); white-space: nowrap; }
table.grid td { padding: 11px 20px 11px 0; border-bottom: 1px solid var(--rule);
  vertical-align: middle; white-space: nowrap; }
table.grid th:last-child, table.grid td:last-child { padding-right: 0; width: 100%;
  white-space: normal; }
table.grid tr:last-child td { border-bottom: none; }
table.grid td.label { width: 230px; color: var(--muted); vertical-align: top; }
table.grid td.label + td { white-space: normal; }
.mono { font-family: var(--mono); font-size: 12.5px; }
/* Code is quoted, not typeset: a font that fuses ``==`` into one glyph would
   show a character the file does not contain. */
.mono, .diff, .wordedit, .tok, .cmd, .p, code { font-variant-ligatures: none; }
.num { font-variant-numeric: tabular-nums; text-align: right; }
table.grid th.num { text-align: right; }
.arrow { color: var(--faint); padding: 0 6px; }
.dim { color: var(--faint); }
.fn-name { font-weight: 600; color: var(--text); }

/* ---- labels ---------------------------------------------------------- */
/* Two different things wear a label here, so they are drawn differently: a
   `chip` classifies a row, a `tok` *is* a name out of the code. A chip leads
   with a dot in its own colour, so the kind still reads at a glance when the
   word is too small to. */
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 2px 9px 2px 8px;
  border-radius: 999px; font-size: 11.5px; font-weight: 600; white-space: nowrap;
  line-height: 1.5; background: var(--sunken); color: var(--muted); }
.chip::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor;
  opacity: .85; flex: 0 0 auto; }
.chip.added   { background: var(--add-gutter); color: var(--add-fg); }
.chip.removed { background: var(--del-gutter); color: var(--del-fg); }
.chip.modified, .chip.changed { background: var(--accent-wash); color: var(--accent); }
.chip.renamed { background: var(--warn-bg); color: var(--warn-fg); }
.chip.high   { background: var(--del-gutter); color: var(--del-fg); }
.chip.medium { background: var(--warn-bg); color: var(--warn-fg); }
.chip.label  { background: var(--accent-wash); color: var(--accent); font-weight: 500; }
.chip.label::before { display: none; }
.tok { display: inline-block; font-family: var(--mono); font-size: 12px; padding: 2px 8px;
  margin: 0 4px 4px 0; border-radius: 6px; background: var(--sunken); border: 1px solid var(--border); }
.tok.added   { background: var(--add-bg); border-color: var(--add-gutter); color: var(--add-fg); }
.tok.removed { background: var(--del-bg); border-color: var(--del-gutter); color: var(--del-fg);
  text-decoration: line-through; text-decoration-color: rgba(180, 35, 58, .45); }
.cmd { font-family: var(--mono); font-size: 12px; background: var(--sunken); color: var(--text);
  border: 1px solid var(--border); padding: 1px 6px; border-radius: 5px; white-space: nowrap; }

/* ---- toolbar --------------------------------------------------------- */
/* Sticks to the top of the window while the list scrolls under it, and stops
   at the bottom of its own card, so it never floats over a different section. */
.toolbar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  position: sticky; top: 0; z-index: 6; padding: 10px 20px; background: var(--card);
  border-top: 1px solid var(--rule); border-bottom: 1px solid var(--border); }
.toolbar #shown-count { margin-left: auto; white-space: nowrap; flex: 0 0 auto; color: var(--faint);
  font-size: 12.5px; font-variant-numeric: tabular-nums; }
.search { position: relative; flex: 1 1 260px; min-width: 160px; display: flex; }
.search svg { position: absolute; left: 10px; top: 50%; width: 15px; height: 15px; margin-top: -7.5px;
  fill: none; stroke: var(--faint); stroke-width: 1.7; stroke-linecap: round; pointer-events: none; }
.search input { width: 100%; padding: 7px 11px 7px 32px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--panel); color: var(--text); font: inherit;
  font-size: 13px; transition: border-color .15s, box-shadow .15s, background .15s; }
.search input::placeholder { color: var(--faint); }
.search input:focus { outline: none; border-color: var(--accent); background: var(--card);
  box-shadow: 0 0 0 3px var(--accent-wash); }
/* The vendored filter is a switch rather than a bare checkbox: it is a view
   setting, on or off, and a switch says so without a sentence. */
.switch { color: var(--muted); font-size: 13px; display: inline-flex; gap: 8px;
  align-items: center; cursor: pointer; user-select: none; white-space: nowrap; }
.switch input { position: absolute; opacity: 0; width: 1px; height: 1px; }
.switch .track { position: relative; width: 30px; height: 18px; border-radius: 999px;
  background: var(--border); transition: background .15s; flex: 0 0 auto; }
.switch .track::after { content: ""; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px;
  border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.25);
  transition: transform .15s ease; }
.switch input:checked + .track { background: var(--accent); }
.switch input:checked + .track::after { transform: translateX(12px); }
.switch input:focus-visible + .track { box-shadow: 0 0 0 3px var(--accent-wash); }

/* ---- the file list --------------------------------------------------- */
/* One panel of rows rather than a stack of cards, because the list is an index:
   the diff it points at opens beside it instead of pushing the rest of the
   list down the page. A row is a button, since that is what it does. */
.files { border-radius: 0 0 var(--radius) var(--radius); overflow: hidden; }
.file { border-top: 1px solid var(--rule); }
/* Transparent rather than absent so every row is the same height, whichever
   one the filter left at the top. */
.file:first-child, .file.first-shown { border-top-color: transparent; }
.row { display: flex; gap: 12px; align-items: center; width: 100%; padding: 10px 20px;
  font: inherit; color: var(--text); text-align: left; background: none; border: 0;
  cursor: pointer; transition: background .12s ease; }
.row:hover { background: var(--panel); }
.row:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
/* One width for every kind, so the paths start in one column and the eye can
   run straight down them. */
.row .chip { min-width: 90px; }
/* The chevron points where the diff will appear: to the side, not downwards.
   Drawn in CSS because a vendored diff runs to thousands of rows, and each one
   would otherwise carry its own copy of the glyph. */
.row::after { content: ""; flex: 0 0 auto; width: 6px; height: 6px; margin-left: 2px;
  border: 1.6px solid var(--faint); border-left: 0; border-bottom: 0;
  transform: rotate(45deg); transition: transform .15s ease, border-color .15s ease; }
.row:hover::after { border-color: var(--accent); transform: translateX(2px) rotate(45deg); }
.file.active .row { background: var(--accent-wash); box-shadow: inset 3px 0 0 var(--accent); }
.file.active .row::after { border-color: var(--accent); transform: translateX(2px) rotate(45deg); }
.row .path { display: flex; align-items: center; gap: 9px; flex: 1; min-width: 0; }
.row .path .p { font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere; }
/* The directory part of a path is quieter than its name, which is the part a
   reader is scanning for. */
.path .dir { color: var(--faint); }
/* The tint hugs the changed part exactly — padding here would open a gap in
   the middle of a path and read as though the name contained a space. */
.path .ren { background: var(--warn-bg); border-radius: 3px; }
.path .was { color: var(--faint); }
.stat-line { font-variant-numeric: tabular-nums; font-size: 12px; white-space: nowrap;
  display: flex; gap: 8px; align-items: center; color: var(--faint); }
.stat-line .add, .stat-line .del { font-weight: 600; }
/* Five squares split between added and removed lines, the way a code host
   draws them: the proportion reads faster than the two numbers beside it. */
.bar { display: inline-flex; gap: 2px; }
.bar i { width: 7px; height: 7px; border-radius: 2px; background: var(--border); }
.bar i.a { background: var(--add-fg); }
.bar i.d { background: var(--del-fg); }

/* ---- the diff itself ------------------------------------------------- */
.diff { overflow-x: auto; border-top: 1px solid var(--border); }
.diff table { border-collapse: collapse; width: 100%; font-family: var(--mono);
  font-size: 12.5px; line-height: 1.6; }
.diff td { padding: 0 8px; white-space: pre; vertical-align: top; }
.diff td.ln { width: 1%; min-width: 44px; padding: 0 10px; text-align: right; color: var(--faint);
  user-select: none; background: var(--panel); font-variant-numeric: tabular-nums; }
.diff td.ln + td.ln { border-right: 1px solid var(--rule); }
/* The sign lives in its own unselectable cell so that copying a block of the
   diff yields the code, not code with markers glued on. It doubles as the
   spine marking how far an added or removed run reaches. */
.diff td.mark { width: 1%; padding: 0 4px 0 8px; text-align: center; color: var(--faint);
  user-select: none; border-left: 2px solid transparent; }
.diff td.code { padding-left: 4px; width: 100%; }
.diff tr.add td.code, .diff tr.add td.mark { background: var(--add-bg); }
.diff tr.add td.mark { color: var(--add-fg); border-left-color: var(--add-fg); }
.diff tr.add td.ln { background: var(--add-gutter); color: var(--add-fg); }
.diff tr.del td.code, .diff tr.del td.mark { background: var(--del-bg); }
.diff tr.del td.mark { color: var(--del-fg); border-left-color: var(--del-fg); }
.diff tr.del td.ln { background: var(--del-gutter); color: var(--del-fg); }
.diff tr.hunk td { background: var(--accent-wash); color: var(--accent); font-size: 11.5px;
  padding: 5px 12px; border-top: 1px solid var(--accent-edge); border-bottom: 1px solid var(--accent-edge); }
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
.wordedit td { padding: 2px 10px; white-space: pre; vertical-align: top; }
.wordedit td.at { width: 1%; text-align: right; color: var(--faint);
  background: var(--panel); border-right: 1px solid var(--rule); }
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
.note { color: var(--muted); font-size: 12.5px; padding: 12px 16px; border-top: 1px solid var(--border);
  background: var(--panel); }
.note code, .sub code { font-family: var(--mono); font-size: 12px; background: var(--sunken);
  padding: 1px 5px; border-radius: 4px; }
/* Nothing to list is a normal answer, not an error, so it is drawn calmly: a
   sentence in the middle of a card, and the command to type next. */
.empty { color: var(--muted); padding: 40px 24px; text-align: center; background: var(--card);
  border: 1px dashed var(--border); border-radius: var(--radius); }
.empty .big { display: block; color: var(--text); font-weight: 600; font-size: 15px; margin-bottom: 4px; }
footer { margin-top: 40px; padding-top: 18px; border-top: 1px solid var(--border);
  color: var(--faint); font-size: 12px; line-height: 1.8; }
.moved-list { margin: 0; padding: 12px 20px 14px 38px; list-style: disc;
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
  backdrop-filter: blur(2px);
  opacity: 0; visibility: hidden; transition: opacity .26s ease, visibility 0s linear .26s; }
body.sheet-open .scrim { opacity: 1; visibility: visible; transition: opacity .26s ease; }
.sheet { position: fixed; top: 0; right: 0; bottom: 0; z-index: 40;
  width: min(100%, var(--sheet-w)); display: flex; flex-direction: column;
  background: var(--card); border-left: 1px solid var(--border);
  box-shadow: -24px 0 60px -30px rgba(6, 11, 18, .45);
  /* `visibility` rather than `display`, so the sheet can animate out and still
     leave nothing behind for the keyboard to land on while it is shut. It turns
     visible on the same frame it is asked to — a transition would leave it
     unfocusable for the length of the slide, which is exactly when the script
     is moving focus into it — and back to hidden only once the slide is over. */
  transform: translateX(100%); visibility: hidden;
  transition: transform .26s cubic-bezier(.22, .61, .36, 1), visibility 0s linear .26s; }
body.sheet-open .sheet { transform: none; visibility: visible;
  transition: transform .26s cubic-bezier(.22, .61, .36, 1); }
.sheet-head { padding: 14px 16px 12px 18px; border-bottom: 1px solid var(--border);
  background: var(--card); display: flex; flex-direction: column; gap: 8px; }
.sheet-bar { display: flex; align-items: center; gap: 10px; }
.sheet-title { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
.sheet-title .path { display: flex; align-items: center; gap: 8px; min-width: 0; }
.sheet-title .p { font-family: var(--mono); font-size: 13px; font-weight: 600;
  overflow-wrap: anywhere; }
/* Wrapping rather than clipping: a file whose counts are a sentence — "missing
   from the archive" — is exactly the one whose reader needs to read them. */
.sheet-sub { display: flex; align-items: center; gap: 12px; min-height: 18px;
  flex-wrap: wrap; color: var(--faint); font-size: 12px; }
.sheet-sub .ver { margin-left: auto; font-family: var(--mono); white-space: nowrap;
  color: var(--muted); background: var(--sunken); border-radius: 999px; padding: 1px 9px; }
.sheet-nav { display: flex; align-items: center; gap: 2px; flex: 0 0 auto; }
.sheet-nav .pos { color: var(--faint); font-size: 12px; padding: 0 4px;
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.iconbtn { display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; padding: 0; border-radius: 8px; border: 1px solid transparent;
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
/* A phone: the rail folds into two columns, and the row drops its counts
   under the path rather than squeezing the path into a sliver. */
@media (max-width: 640px) {
  .wrap, .topbar .inner { padding-left: 16px; padding-right: 16px; }
  .topbar .when, .brand .name, .stamps .to { display: none; }
  .topbar .inner { overflow: hidden; }
  .crumbs { overflow: hidden; }
  .crumbs a, .crumbs span { overflow: hidden; text-overflow: ellipsis; }
  header.top { padding-top: 24px; }
  h1 { font-size: 23px; }
  .stat { flex-basis: 50%; border-top: 1px solid var(--rule); }
  .row { flex-wrap: wrap; padding: 10px 16px; row-gap: 6px; }
  .row .path { flex-basis: calc(100% - 110px); }
  .row .stat-line { margin-left: 102px; flex-wrap: wrap; white-space: normal; }
  .row::after { display: none; }
  .sec-head, .toolbar { padding-left: 16px; padding-right: 16px; }
  .card > table.grid { width: calc(100% - 32px); margin: 0 16px 6px; }
  .card > .scroll { padding: 0 16px 6px; }
  table.grid td.label { width: auto; white-space: normal; }
}
@media (prefers-reduced-motion: reduce) {
  body, .sheet, .scrim, .row::after, .row, .switch .track, .switch .track::after { transition: none; }
}
/* On paper there is no clicking, so every diff is printed under its own row and
   the chrome that only answers a pointer is left out. */
@media print {
  body { background: #fff; }
  .toolbar, .scrim, .sheet-nav, .grip, .topbar { display: none !important; }
  .card, .stats, .stamp { box-shadow: none; }
  .sheet { position: static; transform: none; visibility: visible; width: auto;
    border-left: 0; box-shadow: none; }
  body.sheet-open { padding-right: 0; overflow: visible; }
  .file .body[hidden] { display: block; }
}
.hidden { display: none !important; }

/* ---- the AI summary --------------------------------------------------- */
/* The card a model's explanation is drawn in. It sits above the numbers
   because it is the one part of the page written as sentences — the part to
   read first — and it is tinted violet throughout so nothing in it is ever
   taken for a measurement. Every file it mentions is a button that opens that
   file's diff, so a claim is one click from the lines it is about. */
.card.ai { border-color: var(--ai-edge); overflow: hidden; }
.card.ai > .sec-head { background: var(--ai-wash); border-bottom: 1px solid var(--ai-edge); }
.card.ai h2 { color: var(--ai); }
.ai-mark { color: var(--ai); font-size: 15px; line-height: 1; }
.ai-body { padding: 16px 20px 6px; }
.ai-headline { font-size: 17px; font-weight: 650; letter-spacing: -0.015em; line-height: 1.4;
  margin: 0 0 8px; color: var(--text); }
.ai-summary { margin: 0 0 10px; color: var(--text); max-width: 78ch; white-space: pre-wrap; }
.ai-why { margin: 0 0 14px; color: var(--muted); font-size: 13px; }
.ai-cols { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 8px 28px;
  margin-top: 6px; }
.ai-cols.single { grid-template-columns: minmax(0, 1fr); }
.ai-block h3 { font-size: 11.5px; font-weight: 650; text-transform: uppercase; letter-spacing: .06em;
  color: var(--faint); margin: 12px 0 8px; display: flex; align-items: center; gap: 8px; }
.ai-list { list-style: none; margin: 0 0 8px; padding: 0; }
.ai-list li { display: flex; gap: 10px; align-items: flex-start; padding: 9px 0;
  border-top: 1px solid var(--rule); }
.ai-list li:first-child { border-top: none; padding-top: 2px; }
.ai-list .chip { min-width: 98px; justify-content: flex-start; margin-top: 1px; }
.ai-block h3 .count { text-transform: none; letter-spacing: 0; }
.ai-list .what { min-width: 0; }
.ai-list b { font-weight: 600; }
.ai-list p { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
.ai-files { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.ai-file { font: 500 11.5px/1.5 var(--mono); padding: 1px 8px; border-radius: 6px; cursor: pointer;
  background: var(--sunken); border: 1px solid var(--border); color: var(--text);
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ai-file:hover { border-color: var(--ai); color: var(--ai); }
.ai-file:focus-visible { outline: 2px solid var(--ai); outline-offset: 1px; }
span.ai-file { cursor: default; }
span.ai-file:hover { border-color: var(--border); color: var(--text); }
.chip.risk-high, .chip.k-security, .chip.k-removal { background: var(--del-gutter); color: var(--del-fg); }
.chip.risk-medium, .chip.k-config, .chip.k-dependency { background: var(--warn-bg); color: var(--warn-fg); }
.chip.risk-low, .chip.k-feature { background: var(--add-gutter); color: var(--add-fg); }
.chip.k-fix, .chip.k-behaviour { background: var(--accent-wash); color: var(--accent); }
.chip.ai-chip { background: var(--ai-wash); color: var(--ai); }
.ai-check { list-style: none; margin: 0 0 8px; padding: 0; }
.ai-check li { padding: 5px 0; }
.ai-check label { display: flex; gap: 10px; align-items: flex-start; cursor: pointer; }
.ai-check input { margin: 3px 0 0; accent-color: var(--ai); width: 15px; height: 15px; flex: 0 0 auto; }
.ai-check input:checked + span { color: var(--faint); text-decoration: line-through; }
.ai-foot { padding: 10px 20px 12px; border-top: 1px solid var(--rule); background: var(--panel);
  color: var(--faint); font-size: 12px; display: flex; flex-wrap: wrap; gap: 6px 14px;
  align-items: center; }
.ai-foot .grow { flex: 1 1 320px; }
.cmdcopy { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; }
.cmdcopy .cmd { overflow: hidden; text-overflow: ellipsis; }
.copy { font: 600 11.5px/1.4 var(--sans); padding: 3px 9px; border-radius: 6px; cursor: pointer;
  background: var(--card); color: var(--muted); border: 1px solid var(--border); white-space: nowrap; }
.copy:hover { color: var(--ai); border-color: var(--ai); }
.copy.copied { color: var(--add-fg); border-color: var(--add-fg); }
/* A card with nothing written yet is one line: what would appear, and the
   command that makes it. It sits under the numbers, not over them, since it
   is an offer rather than an answer. */
.ai-offer { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; padding: 14px 20px; }
.ai-offer .grow { flex: 1 1 360px; color: var(--muted); }
.ai-offer .grow b { color: var(--text); font-weight: 600; }
.ai-offer .cmds { display: flex; flex-direction: column; gap: 6px; }
.ai-status { padding: 12px 20px; border-bottom: 1px solid var(--ai-edge); background: var(--panel);
  color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; }
.ai-status.bad { background: var(--warn-bg); color: var(--warn-fg); border-bottom-color: var(--warn-edge); }
.card.ai.failed { border-color: var(--warn-edge); }
.card.ai.failed > .sec-head { background: var(--warn-bg); border-bottom-color: var(--warn-edge); }
.card.ai.failed h2, .card.ai.failed .ai-mark { color: var(--warn-fg); }
/* While an explanation is being written the mark pulses, so an open page
   visibly has something coming rather than looking finished and sparse. */
.ai-wait .ai-mark { animation: ai-pulse 1.4s ease-in-out infinite; }
@keyframes ai-pulse { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }
.ai-late .ai-wait-on { display: none; }
.ai-wait-late, .ai-offer .cmds.ai-wait-late { display: none; }
.ai-late .ai-wait-late { display: inline; }
.ai-late .ai-offer .cmds.ai-wait-late { display: flex; }
.cmds .step { color: var(--faint); font-size: 11.5px; font-weight: 600; width: 14px;
  display: inline-block; }
/* The model's one-line note on a file, under its path in the list and in the
   sheet's heading. */
.row .path, .sheet-title .path { flex-wrap: wrap; row-gap: 2px; }
.ai-note { flex-basis: 100%; padding-left: 25px; color: var(--muted); font-size: 12.5px;
  line-height: 1.45; font-family: var(--sans); }
.ai-note::before { content: "✦ "; color: var(--ai); }
.ai-line { margin-top: 3px; color: var(--muted); font-size: 12.5px; white-space: normal; }
.ai-line::before { content: "✦ "; color: var(--ai); }
.ai-line .chip { margin-left: 6px; vertical-align: 1px; }
@media (max-width: 860px) {
  .ai-cols { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 640px) {
  .ai-body, .ai-foot, .ai-offer, .ai-status { padding-left: 16px; padding-right: 16px; }
  .ai-list li { flex-direction: column; gap: 4px; }
  .ai-list .chip { min-width: 0; align-self: flex-start; }
  .ai-note { padding-left: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .ai-wait .ai-mark { animation: none; }
}
@media print {
  .copy, .ai-offer { display: none !important; }
}
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

  // ---- the AI summary --------------------------------------------------
  // Everything below is found by class rather than by id: a page without an
  // AI card has none of it, and nothing here may assume otherwise.

  // A file named in the explanation opens that file's diff. The row may be
  // hidden by the filter or the vendored switch, so both are cleared first —
  // a button that did nothing because of a filter the reader forgot about
  // would read as a broken link.
  function blockFor(path) {
    var exact = null, within = null;
    files.forEach(function (el) {
      if (el.getAttribute('data-key') === path) { exact = exact || el; }
      var tokens = (el.getAttribute('data-path') || '').split(' ');
      if (!within && tokens.indexOf(path) !== -1) { within = el; }
    });
    return exact || within;
  }
  Array.prototype.slice.call(document.querySelectorAll('button.ai-file')).forEach(function (btn) {
    btn.addEventListener('click', function () {
      var file = blockFor(btn.getAttribute('data-open'));
      if (!file) { return; }
      if (file.classList.contains('hidden')) {
        if (search) { search.value = ''; }
        if (vendorToggle) { vendorToggle.checked = true; }
        apply();
      }
      var row = file.querySelector('.row');
      if (row && row.scrollIntoView) { row.scrollIntoView({block: 'center'}); }
      opener = row;
      show(file, true);
    });
  });

  // Copy a command to the clipboard. A page opened from disk is a secure
  // context in current browsers, but not in every one, so the old
  // select-and-copy route is kept as the fallback.
  function copied(btn) {
    btn.classList.add('copied');
    btn.textContent = 'Copied';
    setTimeout(function () { btn.classList.remove('copied'); btn.textContent = 'Copy'; }, 1600);
  }
  function copyText(text, btn) {
    function fallback() {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try { if (document.execCommand('copy')) { copied(btn); } } catch (e) { /* nothing to do */ }
      document.body.removeChild(area);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { copied(btn); }, fallback);
    } else {
      fallback();
    }
  }
  Array.prototype.slice.call(document.querySelectorAll('button.copy')).forEach(function (btn) {
    btn.addEventListener('click', function () { copyText(btn.getAttribute('data-copy') || '', btn); });
  });

  // The deploy checklist keeps its ticks across reloads, keyed by the pair of
  // versions and by each item's own words: a refreshed explanation that
  // reorders the list keeps the right boxes ticked, and one that rewrites an
  // item starts that item afresh.
  Array.prototype.slice.call(document.querySelectorAll('.ai-check')).forEach(function (list) {
    var key = list.getAttribute('data-key') || 'lw-ai';
    var tally = list.parentNode.querySelector('.ai-done');
    var boxes = Array.prototype.slice.call(list.querySelectorAll('input[type=checkbox]'));
    var saved = {};
    try { saved = JSON.parse(window.localStorage.getItem(key) || '{}') || {}; } catch (e) { saved = {}; }
    function count() {
      var done = boxes.filter(function (b) { return b.checked; }).length;
      if (tally) { tally.textContent = done + ' of ' + boxes.length + ' done'; }
    }
    boxes.forEach(function (box) {
      var item = box.getAttribute('data-item') || '';
      box.checked = !!saved[item];
      box.addEventListener('change', function () {
        saved[item] = box.checked;
        try { window.localStorage.setItem(key, JSON.stringify(saved)); } catch (e) { /* private window */ }
        count();
      });
    });
    count();
  });

  // While an explanation is being written the page reloads itself every few
  // seconds, so the answer appears on a page already open. Never while a
  // diff is open in the sheet — that would snatch it from under the reader —
  // and not for ever: past the limit the card says what to type instead.
  var waiting = document.querySelector('[data-ai-started]');
  if (waiting) {
    var started = Date.parse(waiting.getAttribute('data-ai-started'));
    var limit = parseInt(waiting.getAttribute('data-ai-limit') || '1200', 10) * 1000;
    var overdue = function () { return isNaN(started) || Date.now() - started > limit; };
    var tick = function () {
      if (overdue()) { waiting.classList.add('ai-late'); return; }
      if (!document.body.classList.contains('sheet-open')) { window.location.reload(); return; }
      setTimeout(tick, 5000);
    };
    // A page opened long after the request began says so at once, rather
    // than after one more pointless reload.
    if (overdue()) { waiting.classList.add('ai-late'); } else { setTimeout(tick, 5000); }
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


def _split_path(path: str) -> str:
    """A path with its directory set quieter than its name: ``helpers/`` dim, ``db.py`` not.

    The name is what a reader scanning the list is looking for; the directory
    is context. A path with no directory is just its escaped self.
    """
    folder, slash, name = path.rpartition("/")
    if not slash:
        return _esc(path)
    return f'<span class="dir">{_esc(folder)}/</span>{_esc(name)}'


#: How many squares the diffstat bar draws, split between added and removed.
BAR_CELLS = 5


def _bar(added: int, removed: int) -> str:
    """Five squares showing how a file's changed lines split: ``+9 −2`` → ■■■■□.

    Green for added, red for removed, grey for the rest, the way a code host
    draws it. A change of a few lines fills only as many squares as it has
    lines, so a one-line tweak does not look as heavy as a rewrite. Empty when
    no lines were counted — a binary or skipped file has nothing to split.
    """
    total = added + removed
    if total <= 0:
        return ""
    filled = min(BAR_CELLS, total)
    adds = round(filled * added / total)
    # Either side that moved at all keeps at least one square, or a 40:1
    # change would hide its one removal entirely.
    if removed and adds == filled:
        adds -= 1
    if added and adds == 0:
        adds = 1
    dels = filled - adds
    cells = "a" * adds + "d" * dels + "n" * (BAR_CELLS - filled)
    return '<span class="bar" aria-hidden="true">' + "".join(
        f'<i class="{c}"></i>' for c in cells) + "</span>"


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
    key: str = "",
    note: str = "",
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
    ``key`` is the one path that names this block, which is how a file button
    in the AI summary finds the diff it points at. ``note`` is the model's one
    line about the file, drawn under the path — and so also in the sheet's
    heading, which is copied from the row.
    """
    extra = f' data-files="{files}"' if files is not None else ""
    if key:
        extra += f' data-key="{_esc(key)}"'
    note_html = f'<span class="ai-note">{_esc(note)}</span>' if note else ""
    return (
        f'<article class="file" data-path="{_esc(path_key)}" '
        f'data-vendor="{1 if is_vendor else 0}"{extra}>'
        '<button class="row" type="button" aria-expanded="false" aria-controls="sheet">'
        f'<span class="chip {_esc(chip_class)}">{_esc(chip_text)}</span>'
        f'<span class="path">{icon}<span class="p">{title}</span>{note_html}</span>'
        f'<span class="stat-line">{stat}</span>'
        "</button>"
        f'<div class="body" hidden>{body}</div>'
        "</article>"
    )


def _render_file(change: FileChange, a_root: Path | None = None, b_root: Path | None = None,
                 ai_note: str = "") -> str:
    """Render one file's change as a list row and the diff table behind it.

    A rename is titled as one file with only the moved part written twice
    (``boto3-{1.34.0 → 1.35.20}.dist-info/METADATA``), rather than as two
    near-identical 90-character paths the reader has to compare by eye.

    The version directories are passed through so the syntax highlighter can
    read the whole file: colouring a hunk correctly means knowing what was
    happening above it, since a line inside a block comment only looks like a
    comment if you can see where it opened. ``ai_note`` is the AI summary's
    line about this file, when there is one.
    """
    lang = language_of(change.path, change.lang)
    # A rename is one file, not two. Written out in full twice, the two paths
    # are near-identical and the reader has to diff 90 characters by eye to
    # find the part that moved; so only that part is written twice.
    title = _split_path(change.path)
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
    stat += _bar(change.added_lines, change.removed_lines)
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
        key=change.path,
        note=ai_note,
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
    stat += _bar(group.added_lines, group.removed_lines)
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
        # New findings are the one number on the rail that asks for action.
        css = "stat alert" if label == "new findings" else "stat"
        cells.append(
            f'<div class="{css}"{title}><div class="v">{value}{beside}</div>'
            f'<div class="k">{_esc(label)}{after}</div></div>'
        )
    return f'<div class="stats">{"".join(cells)}</div>'


def _section(title: str, body: str, *, count: int | None = None, tone: str = "",
             aside: str = "") -> str:
    """One titled card: a heading, an optional count beside it, and the body under it.

    ``tone`` is ``warn`` or ``danger`` for a section that asks the reader to do
    something before deploying; it tints the heading so a scroll lands there
    first. ``aside`` is already-escaped markup set at the heading's far end.
    """
    dot = '<span class="dot"></span>' if tone else ""
    badge = f'<span class="count">{count:,}</span>' if count is not None else ""
    side = f'<span class="aside">{aside}</span>' if aside else ""
    css = f"card {tone}" if tone else "card"
    return (f'<section class="{css}"><div class="sec-head">{dot}<h2>{_esc(title)}</h2>'
            f"{badge}{side}</div>{body}</section>")


def _dep_table(diff: VersionDiff) -> str:
    """Render the dependency table, or an empty string when nothing moved.

    Each row shows the package, the move between versions as one phrase
    (``1.34.0 → 1.35.20``, or just ``2.9.0`` for a newcomer), and whether the
    fact came from a manifest (``declared``) or from what was vendored in the
    zip (``installed``).
    """
    if not diff.deps:
        return ""
    rows = []
    for change in diff.deps:
        old, new = change.old_version, change.new_version
        if old and new:
            move = (f'<span class="del">{_esc(old)}</span><span class="arrow">→</span>'
                    f'<span class="add">{_esc(new)}</span>')
        elif new:
            move = f'<span class="add">{_esc(new)}</span>'
        else:
            move = f'<span class="del">{_esc(old or "—")}</span>'
        rows.append(
            "<tr>"
            f'<td><span class="chip {_esc(change.kind)}">{_esc(change.kind)}</span></td>'
            f'<td class="mono"><strong>{_esc(change.name)}</strong></td>'
            f'<td class="mono">{move}</td>'
            f'<td class="dim">{_esc(change.manager)} · '
            f'{"declared" if change.is_declared else "installed"}</td>'
            "</tr>"
        )
    table = (
        "<div class='scroll'><table class='grid'>"
        "<thead><tr><th>change</th><th>package</th><th>version</th><th>source</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    return _section("Dependencies", table, count=len(diff.deps))


def _context_section(diff: VersionDiff) -> str:
    """Render the environment variables, AWS services and entry point that moved.

    These are the changes a file diff makes look harmless and a deploy makes
    expensive, so they share one tinted card titled for what the reader has to
    do about them. Empty string when none changed, so the card disappears
    rather than appearing empty.
    """

    def listing(label: str, values: list[str], css: str, hint: str = "") -> str:
        """One labelled row of identifiers, or an empty string when there are none.

        These are identifiers out of the function's own code, so they are set in
        the same face the diff sets them in — not as prose in a sentence.
        """
        if not values:
            return ""
        chips = "".join(f'<span class="tok {css}">{_esc(v)}</span>' for v in values)
        hint_html = f'<div class="sub">{_esc(hint)}</div>' if hint else ""
        return f"<tr><td class='label'>{_esc(label)}</td><td>{chips}{hint_html}</td></tr>"

    def moved(label: str, before: str | None, after: str | None) -> str:
        """One labelled ``old → new`` row for the runtime or the handler."""
        return (f"<tr><td class='label'>{_esc(label)}</td><td class='mono'>"
                f"<span class='del'>{_esc(before or '?')}</span><span class='arrow'>→</span>"
                f"<span class='add'>{_esc(after or '?')}</span></td></tr>")

    rows = [
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
    if diff.runtime_change:
        rows.append(moved("Runtime", *diff.runtime_change))
    if diff.handler_change:
        rows.append(moved("Handler", *diff.handler_change))
    body = "".join(rows)
    if not body:
        return ""
    return _section("Before you deploy", f"<table class='grid'><tbody>{body}</tbody></table>",
                    tone="warn", aside="configuration impact")


def _findings_section(diff: VersionDiff) -> str:
    """Render new and resolved security findings, or an empty string when there are none.

    Details are already redacted by the scanner, so what lands in the page shows
    the shape of a credential without carrying the credential itself. The card
    is tinted red when any finding is ``high``, amber otherwise, and plain when
    the only news is findings that went away.
    """
    if not diff.findings_new and not diff.findings_fixed:
        return ""
    rows = []
    for finding in diff.findings_new:
        rows.append(
            "<tr>"
            f'<td><span class="chip {_esc(finding["severity"])}">{_esc(finding["severity"])}</span></td>'
            f'<td>{_esc(finding["kind"])}</td>'
            f'<td class="mono">{_esc(finding["path"])}<span class="dim">:{_esc(finding["line"])}</span></td>'
            f'<td class="mono dim">{_esc(finding["detail"])}</td>'
            "</tr>"
        )
    resolved = (
        f'<div class="foot sub">{len(diff.findings_fixed)} finding(s) '
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
    if any(f["severity"] == "high" for f in diff.findings_new):
        tone = "danger"
    else:
        tone = "warn" if diff.findings_new else ""
    return _section("New findings", f"{table}{resolved}", count=len(diff.findings_new),
                    tone=tone, aside="values are redacted")


def _inline_code(text: str) -> str:
    """Escape prose and set any ```lw …``` span in it as a command: the hints are written that way."""
    return re.sub(r"`([^`]+)`", r'<span class="cmd">\1</span>', _esc(text))


def _copyable(command: str) -> str:
    """A command set in code type, with a button beside it that copies it.

    The page cannot run anything — it is a file on disk — so the next best
    thing is making the command one click from the terminal.
    """
    return (f'<span class="cmdcopy"><span class="cmd">{_esc(command)}</span>'
            f'<button type="button" class="copy" data-copy="{_esc(command)}" '
            f'title="Copy this command">Copy</button></span>')


def _file_buttons(paths: list[str], linkable: set[str]) -> str:
    """The files a point is about, each a button that opens its diff when the page has one.

    A path the page does not list — a vendored file hidden by the switch at
    build time — is drawn as plain text rather than a button that would do
    nothing.
    """
    if not paths:
        return ""
    parts = []
    for path in paths:
        if path in linkable:
            parts.append(f'<button type="button" class="ai-file" data-open="{_esc(path)}" '
                         f'title="Open the diff of {_esc(path)}">{_esc(path)}</button>')
        else:
            parts.append(f'<span class="ai-file" title="{_esc(path)}">{_esc(path)}</span>')
    return f'<div class="ai-files">{"".join(parts)}</div>'


def _provenance(ex: Explanation) -> str:
    """The footer line saying who wrote an explanation and what it was shown.

    "Written by claude-sonnet-5 (Anthropic) on 2026-09-24 10:02 · from 14 of
    16 changed files · 2 values redacted". What the model was *not* shown is
    the part that matters most: an explanation of 3 files out of 40 reads
    exactly as confidently as one of all 40.
    """
    from ..ai.settings import PROVIDERS

    service = PROVIDERS[ex.provider].label if ex.provider in PROVIDERS else ex.provider
    parts = [f"Written by <b>{_esc(ex.model or ex.model_name or 'a model')}</b>"
             + (f" ({_esc(service)})" if service else "")
             + (f" on {_esc(format_ts(ex.created_at))}" if ex.created_at else "")]
    if not ex.send_code:
        parts.append("from the structure only — no code was sent")
    elif ex.files_total and ex.files_sent < ex.files_total:
        parts.append(f"from {ex.files_sent} of {ex.files_total} changed files"
                     + (" (the rest left out for length)" if ex.omitted else ""))
    elif ex.files_total:
        parts.append(f"from all {ex.files_total} changed file{'s' if ex.files_total != 1 else ''}")
    if ex.withheld:
        parts.append(f"{len(ex.withheld)} credential file{'s' if len(ex.withheld) != 1 else ''} withheld")
    if ex.redactions:
        parts.append(f"{ex.redactions} credential-like value{'s' if ex.redactions != 1 else ''} "
                     "redacted before sending")
    return " · ".join(parts) + ". AI can be wrong — the diffs below are the record."


def _explanation_card(panel: AIPanel, ex: Explanation, linkable: set[str]) -> str:
    """The full card: headline, summary, changes, risks and a checklist that remembers its ticks.

    Changes go on the left and the things to act on — risks, then the
    checklist — on the right, so on a wide screen the reader sees what
    happened and what to do about it side by side. A refresh in flight, or one
    that failed, is a strip across the top: the answer already on screen stays
    readable either way.
    """
    risk = (f'<span class="chip risk-{_esc(ex.risk)}">{_esc(ex.risk)} risk</span>'
            if ex.risk else "")
    head = (f'<div class="sec-head"><span class="ai-mark" aria-hidden="true">✦</span>'
            f'<h2>What changed</h2>{risk}'
            f'<span class="aside">AI summary</span></div>')
    strip = ""
    if panel.state == "pending":
        strip = (f'<div class="ai-status ai-wait" data-ai-started="{_esc(panel.started_at)}" '
                 f'data-ai-limit="{PENDING_STALE_SECONDS}">'
                 f'<span class="ai-wait-on"><span class="ai-mark">✦</span> A fresh summary is being '
                 f'written by {_esc(panel.model)}; this page updates itself when it is ready.</span>'
                 f'<span class="ai-wait-late">That is taking longer than it should. '
                 f'{_copyable(panel.command + " --refresh")}</span></div>')
    elif panel.state == "failed":
        hint = f" {_inline_code(panel.hint.rstrip('.'))}." if panel.hint else ""
        strip = (f'<div class="ai-status bad"><span>Writing a fresh summary failed: '
                 f'{_esc(panel.message.rstrip("."))}.{hint}</span>'
                 f'{_copyable(panel.command + " --refresh")}</div>')

    body = []
    if ex.headline:
        body.append(f'<p class="ai-headline">{_esc(ex.headline)}</p>')
    if ex.summary and ex.summary != ex.headline:
        body.append(f'<p class="ai-summary">{_esc(ex.summary)}</p>')
    if ex.risk_reason:
        body.append(f'<p class="ai-why">Why {_esc(ex.risk or "this")} risk: {_esc(ex.risk_reason)}</p>')

    left, right = [], []
    if ex.changes:
        items = "".join(
            f'<li><span class="chip k-{_esc(p.kind)}">{_esc(p.kind)}</span><div class="what">'
            f'<b>{_esc(p.title)}</b>' + (f"<p>{_esc(p.detail)}</p>" if p.detail else "")
            + _file_buttons(p.files, linkable) + "</div></li>"
            for p in ex.changes
        )
        left.append(f'<div class="ai-block"><h3>Changes</h3><ol class="ai-list">{items}</ol></div>')
    if ex.risks:
        items = "".join(
            f'<li><span class="chip risk-{_esc(p.kind)}">{_esc(p.kind)}</span><div class="what">'
            f'<b>{_esc(p.title)}</b>' + (f"<p>{_esc(p.detail)}</p>" if p.detail else "")
            + _file_buttons(p.files, linkable) + "</div></li>"
            for p in ex.risks
        )
        right.append(f'<div class="ai-block"><h3>Worth checking</h3><ul class="ai-list">{items}</ul></div>')
    if ex.checklist:
        items = "".join(
            f'<li><label><input type="checkbox" data-item="{_esc(step)}"><span>{_esc(step)}</span>'
            "</label></li>"
            for step in ex.checklist
        )
        right.append(f'<div class="ai-block"><h3>Deploy checklist <span class="count ai-done"></span></h3>'
                     f'<ul class="ai-check" data-key="{_esc(panel.storage_key)}">{items}</ul></div>')
    if left and right:
        body.append(f'<div class="ai-cols"><div>{"".join(left)}</div><div>{"".join(right)}</div></div>')
    elif left or right:
        body.append(f'<div class="ai-cols single"><div>{"".join(left + right)}</div></div>')

    foot = (f'<div class="ai-foot"><span class="grow">{_provenance(ex)}</span>'
            f'{_copyable(panel.command + " --refresh")}</div>')
    return (f'<section class="card ai" id="ai-summary">{head}{strip}'
            f'<div class="ai-body">{"".join(body)}</div>{foot}</section>')


def _ai_cards(panel: AIPanel | None, diff: VersionDiff) -> tuple[str, str]:
    """The AI card for a comparison page, as ``(above the numbers, below them)``.

    An answer — or one on its way, or one that failed — goes above the stats
    rail, because a paragraph of plain English is what a reader should meet
    first. An *offer* to write one goes below: it is not an answer, and a page
    should not open on a suggestion. At most one of the two is ever non-empty,
    and both are empty when AI is switched off.
    """
    if panel is None:
        return "", ""
    linkable = {c.path for c in diff.files} | {c.old_path for c in diff.files if c.old_path}
    if panel.explanation is not None and not panel.explanation.is_empty:
        return _explanation_card(panel, panel.explanation, linkable), ""
    mark = '<span class="ai-mark" aria-hidden="true">✦</span>'
    if panel.state == "pending":
        model = f" by {_esc(panel.model)}" if panel.model else ""
        return (
            f'<section class="card ai ai-wait" id="ai-summary" data-ai-started="{_esc(panel.started_at)}" '
            f'data-ai-limit="{PENDING_STALE_SECONDS}"><div class="ai-offer">{mark}<div class="grow">'
            f'<span class="ai-wait-on"><b>A plain-English summary of this change is being written'
            f'{model}.</b> This page reloads itself when it is ready — usually within a minute.</span>'
            f'<span class="ai-wait-late"><b>This is taking longer than it should.</b> The request may '
            f'have been interrupted; the command beside this tries again.</span></div>'
            f'<div class="cmds ai-wait-late">{_copyable(panel.command)}</div></div></section>',
            "",
        )
    if panel.state == "failed":
        hint = f" {_inline_code(panel.hint.rstrip('.'))}." if panel.hint else ""
        model = f" ({_esc(panel.model)})" if panel.model else ""
        return (
            f'<section class="card ai failed" id="ai-summary"><div class="sec-head">{mark}'
            f'<h2>The AI summary could not be written</h2><span class="aside">{model}</span></div>'
            f'<div class="ai-offer"><div class="grow"><b>{_esc(panel.message.rstrip("."))}.</b>{hint} '
            f'The diff below is complete; only the summary is missing.</div>'
            f'<div class="cmds">{_copyable(panel.command)}</div></div></section>',
            "",
        )
    if panel.state == "missing":
        return "", (
            f'<section class="card ai" id="ai-summary"><div class="ai-offer">{mark}<div class="grow">'
            f'<b>Get this change explained in plain English.</b> A model reads the diff below and '
            f'writes what the function now does differently, what could break, and what to do '
            f'before deploying.</div><div class="cmds">{_copyable(panel.command)}</div></div></section>'
        )
    return "", (
        f'<section class="card ai" id="ai-summary"><div class="ai-offer">{mark}<div class="grow">'
        f'<b>Want this change explained in plain English?</b> Set up a model once — Anthropic, '
        f'OpenAI, Azure OpenAI, or one running on your own machine — then explain this change. '
        f'<span class="dim"><span class="cmd">lw ai off</span> hides this.</span></div>'
        f'<div class="cmds"><span><span class="step">1</span>{_copyable("lw ai add")}</span>'
        f'<span><span class="step">2</span>{_copyable(panel.command)}</span></div></div></section>'
    )


#: Without the script the sheet can never open, so the diffs sit under their own
#: rows instead and the controls that would do nothing are taken off the page.
NOSCRIPT = """
.toolbar, .sheet, .scrim { display: none; }
.row { cursor: default; }
.row::after { display: none; }
.file .body[hidden] { display: block; }
.copy { display: none; }
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


#: The magnifier drawn inside the filter box.
SEARCH_GLYPH = "M7 12.2a5.2 5.2 0 1 0 0-10.4 5.2 5.2 0 0 0 0 10.4zM10.8 10.8 14 14"

#: A crumb in the top bar: its text, and where it links (None for the page you are on).
Crumb = tuple[str, str | None]


def _stamp_now() -> str:
    """When the page is being written, to the minute: ``2026-09-23 01:07``."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _page(title: str, body: str, crumbs: list[Crumb], footer: str, *,
          head: str = "", before: str = "", after: str = "") -> str:
    """Wrap one page's content in the shell every report page shares.

    All three pages — the archive index, a function's history and a
    comparison — carry the same top bar, so they read as one site rather than
    three unrelated printouts: the tool's name on the left, where you are in
    the archive beside it, and when the page was written on the right.

    A crumb links only when its caller knows the target exists. A comparison
    written with ``lw diff --output`` can land anywhere on disk, so a link
    guessed at ``../index.html`` would be a link to nothing. ``footer`` is
    already-escaped markup; ``head`` goes in ``<head>``, ``before`` at the top
    of the body (the icon sprite) and ``after`` at the bottom (the sheet and
    its script).
    """
    trail = []
    for index, (label, href) in enumerate(crumbs):
        if href:
            trail.append(f'<a href="{_esc(href)}">{_esc(label)}</a>')
        elif index == len(crumbs) - 1:
            trail.append(f'<span class="here">{_esc(label)}</span>')
        else:
            trail.append(f"<span>{_esc(label)}</span>")
    path = '<span class="sep">/</span>'.join(trail)
    crumb_bar = f'<span class="sep">/</span><div class="crumbs">{path}</div>' if trail else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{CSS}{ICON_CSS}</style>
{head}
</head>
<body>
{before}
<nav class="topbar"><div class="inner">
  <span class="brand"><span class="logo" aria-hidden="true">λ</span><span class="name">lambda-watcher</span></span>
  {crumb_bar}
  <span class="when">Generated {_esc(_stamp_now())}</span>
</div></nav>
<div class="wrap">
{body}
  <footer>{footer}</footer>
</div>
{after}
</body>
</html>
"""


def render_html(
    diff: VersionDiff,
    generated_by: str = "lambda-watcher",
    *,
    archive_href: str | None = None,
    history_href: str | None = None,
    ai: AIPanel | None = None,
) -> str:
    """Render the full report as a single HTML document.

    What the reader gets is a summary they can take in without scrolling —
    the two versions, the numbers, then anything that needs doing before a
    deploy — and a list of every changed file under it. Clicking one opens its
    diff in the sheet beside the list rather than underneath it — see
    :func:`_sheet` — so the list keeps its place and the code gets the width
    it wants.

    ``archive_href`` and ``history_href`` point the top bar at the archive's
    front page and this function's history, relative to where the page is
    being written. Callers pass them only where they know those pages sit;
    left out, the crumbs are plain text.

    ``ai`` is the page's AI card, from :func:`~lambda_watcher.ai.report.panel_for`:
    an explanation with its per-file notes, one on its way, one that failed,
    or an offer to write one. ``None`` leaves AI off the page entirely.
    """
    versions = f"v{diff.a_seq:04d} → v{diff.b_seq:04d}"
    title = f"{diff.function_name} · {versions}"
    a_when = format_ts(diff.a_meta.get("ingested_at"))
    b_when = format_ts(diff.b_meta.get("ingested_at"))
    notes = ai.explanation.file_notes if ai is not None and ai.explanation is not None else {}
    ai_top, ai_offer = _ai_cards(ai, diff)

    blocks: list[str] = []
    for row in diff.file_rows():
        if not isinstance(row, MoveGroup):
            blocks.append(_render_file(row, diff.a_root, diff.b_root, notes.get(row.path, "")))
            continue
        # The group block reports the move; it has no room for a diff, so the
        # members that were rewritten on the way keep their own blocks after it.
        blocks.append(_render_move(row))
        blocks.extend(
            _render_file(c, diff.a_root, diff.b_root, notes.get(c.path, "")) for c in row.edited_members
        )
    vendor_toggle = (
        '<label class="switch"><input type="checkbox" id="vendor" checked>'
        '<span class="track"></span>Show vendored files</label>'
        if any(c.is_vendor for c in diff.files)
        else ""
    )
    # Two versions with the same tree have nothing to filter and nothing to
    # open, so that page is one sentence: a search box above it would only
    # offer to narrow an empty list.
    if diff.files:
        toolbar = (
            '<div class="toolbar">'
            '<label class="search"><svg viewBox="0 0 16 16" aria-hidden="true">'
            f'<path d="{SEARCH_GLYPH}"/></svg>'
            '<input type="search" id="filter" placeholder="Filter by path…" autocomplete="off"'
            ' aria-label="Filter files by path"></label>'
            f'{vendor_toggle}<span class="sub" id="shown-count"></span></div>'
        )
        listing = '<div class="files">{}</div>'.format("\n".join(blocks))
        files = _section("File changes", toolbar + listing, count=sum(diff.counts().values()),
                         aside="click a file to open its diff")
    else:
        files = ('<div class="empty"><span class="big">No file-level changes '
                 "between these versions.</span></div>")

    body = f"""  <header class="top">
    <div class="eyebrow">Version comparison</div>
    <h1>{_esc(diff.function_name)}
      <span class="ver">v{diff.a_seq:04d}<span class="arrow">→</span>v{diff.b_seq:04d}</span></h1>
    <div class="lead">{_esc(diff.headline().capitalize())}</div>
    <div class="stamps">
      <span class="stamp"><b>v{diff.a_seq:04d}</b>archived {_esc(a_when)}</span>
      <span class="to" aria-hidden="true">→</span>
      <span class="stamp"><b>v{diff.b_seq:04d}</b>archived {_esc(b_when)}</span>
    </div>
  </header>

  {ai_top}
  {_stats(diff)}
  {ai_offer}
  {_findings_section(diff)}
  {_context_section(diff)}
  {_dep_table(diff)}
  {files}"""
    crumbs: list[Crumb] = [
        ("All functions", archive_href), (diff.function_name, history_href), (versions, None),
    ]
    footer = (
        f"Generated by {_esc(generated_by)} on {_esc(_stamp_now())}. Content hashes ignore zip "
        "timestamps, so re-downloading unchanged code does not create a new version."
    )
    return _page(
        title, body, crumbs, footer,
        head=f"<noscript><style>{NOSCRIPT}</style></noscript>",
        before=icons.sprite(),
        after=f"{_sheet(diff)}\n<script>{JS}</script>",
    )


def write_html(
    diff: VersionDiff,
    path: Path,
    generated_by: str = "lambda-watcher",
    *,
    archive_href: str | None = None,
    history_href: str | None = None,
    ai: AIPanel | None = None,
) -> Path:
    """Render the diff and write it to ``path``, creating parent directories.

    Returns the path so callers can print it. This is what the background
    ingest calls to leave ``reports/<function>/latest.html`` sitting there
    before anyone thinks to ask what changed. The hrefs and the AI card are
    passed straight to :func:`render_html`.

    Written beside the target and renamed over it: a page that is open while
    an explanation is being written reloads itself every few seconds, and a
    reload landing halfway through a plain rewrite would show half a page. The
    scratch name carries the thread as well as the process, because the
    watcher's ingest thread and its explainer can rewrite ``latest.html`` at
    the same moment.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    page = render_html(diff, generated_by, archive_href=archive_href, history_href=history_href, ai=ai)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        scratch.write_text(page, encoding="utf-8")
        os.replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)
    return path


def _ai_line(entry: dict[str, Any]) -> str:
    """The AI headline for one row of the history or the archive index, with its risk. Empty without one."""
    headline = entry.get("ai_headline")
    if not headline:
        return ""
    risk = entry.get("ai_risk")
    chip = f'<span class="chip risk-{_esc(risk)}">{_esc(risk)} risk</span>' if risk else ""
    return f'<div class="ai-line">{_esc(headline)}{chip}</div>'


def render_timeline(
    function_name: str,
    versions: list[dict[str, Any]],
    generated_by: str = "lambda-watcher",
    *,
    archive_href: str | None = None,
) -> str:
    """Index page: every archived version of one function, newest first.

    ``versions`` entries carry the per-version stats plus ``diff_href`` /
    ``diff_summary`` describing the step from the previous version, and
    ``ai_headline`` / ``ai_risk`` when that step has been explained — which
    turns the history into a list of what each release *did*, not only how
    many files it touched. ``archive_href`` links the top bar back to the
    archive's front page, when the caller knows where that is.
    """
    rows: list[str] = []
    for entry in versions:
        seq = entry["seq"]
        href = entry.get("diff_href")
        step = (
            f'<a href="{_esc(href)}">{_esc(entry.get("diff_summary") or "view diff")}</a>'
            if href
            else '<span class="dim">first version</span>'
        )
        step += _ai_line(entry)
        label = (f' <span class="chip label">{_esc(entry["label"])}</span>'
                 if entry.get("label") else "")
        rows.append(
            "<tr>"
            f'<td class="mono"><strong>v{seq:04d}</strong>{label}</td>'
            f'<td>{_esc(format_ts(entry.get("ingested_at")))}</td>'
            f'<td class="mono">{_esc(entry.get("runtime") or "?")}</td>'
            f'<td class="mono">{_esc(entry.get("handler") or "?")}</td>'
            f'<td class="num">{entry.get("file_count", 0):,}</td>'
            f'<td class="num">{_esc(human_size(entry.get("total_size", 0)))}</td>'
            f'<td class="mono dim">{_esc(str(entry.get("source_name") or ""))}</td>'
            f"<td>{step}</td>"
            "</tr>"
        )

    count = len(versions)
    table = f"""<div class="scroll"><table class="grid">
    <thead><tr>
      <th>version</th><th>archived</th><th>runtime</th><th>handler</th>
      <th class="num">files</th><th class="num">size</th>
      <th>downloaded as</th><th>change from previous</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>"""
    body = f"""  <header class="top">
    <div class="eyebrow">Version history</div>
    <h1>{_esc(function_name)}</h1>
    <div class="lead">{count} archived version{'s' if count != 1 else ''} · newest first</div>
  </header>
  {_section("Versions", table, count=count)}"""
    crumbs: list[Crumb] = [("All functions", archive_href), (function_name, None)]
    footer = f"Generated by {_esc(generated_by)} on {_esc(_stamp_now())}."
    return _page(f"{function_name} · version history", body, crumbs, footer)


def render_archive_index(
    functions: list[dict[str, Any]],
    generated_by: str = "lambda-watcher",
) -> str:
    """The front page of ``reports/``: every archived function, one row each.

    Each function's comparison is already on disk by the time anyone looks, but
    nothing tied those pages together, so reading one meant knowing its folder
    name. This is the page to bookmark instead: what is archived, what changed
    last, and which functions ship a credential right now.

    ``functions`` entries come from
    :func:`~lambda_watcher.diffing.build.archive_index_entries`, which has
    already decided the links — a row only links a page that exists, and names
    the command that would write it otherwise (``lw report "orders"``).

    Times are absolute, unlike the dashboard's "3 hours ago": this page is
    written once and read later, and a relative time would be wrong by then.
    """
    rows: list[str] = []
    for entry in functions:
        name = entry["name"]
        label = (f' <span class="chip label">{_esc(entry["label"])}</span>'
                 if entry.get("label") else "")
        secrets = " ".join(
            f'<span class="chip {_esc(severity)}">{count} {_esc(severity)}</span>'
            for severity, count in entry["secrets"].items()
        ) or '<span class="dim">none</span>'
        if entry.get("change_href"):
            change = (f'<a href="{_esc(entry["change_href"])}">'
                      f'v{entry["previous_seq"]:04d} → v{entry["seq"]:04d}</a>')
        elif entry.get("previous_seq") is None:
            change = '<span class="dim">first version</span>'
        else:
            # Versions archived with automatic reports switched off, or before
            # there were any: the comparison exists only once someone asks.
            change = (f'<span class="dim">not written yet:</span> '
                      f'<span class="cmd">lw report "{_esc(name)}"</span>')
        if entry.get("history_href"):
            change += (f' <span class="dim">·</span> '
                       f'<a href="{_esc(entry["history_href"])}">full history</a>')
        change += _ai_line(entry)
        rows.append(
            "<tr>"
            f'<td><span class="fn-name">{_esc(name)}</span></td>'
            f'<td class="num">{entry["versions"]:,}</td>'
            f'<td class="mono">v{entry["seq"]:04d}{label}</td>'
            f'<td>{_esc(format_ts(entry.get("ingested_at")))}</td>'
            f'<td class="mono">{_esc(entry.get("runtime") or "?")}</td>'
            f"<td>{secrets}</td>"
            f"<td>{change}</td>"
            "</tr>"
        )

    version_total = sum(int(entry["versions"]) for entry in functions)
    if rows:
        lead = "Every function archived so far, most recently archived first."
        leaking = sum(1 for entry in functions if entry["secrets"])
        # The same rail the comparison page opens with, so the front page
        # answers "how much is in here" before the reader scans a table.
        cells = [
            (f"{len(functions):,}", "functions", ""),
            (f"{version_total:,}", "versions archived", ""),
            (f"{leaking:,}", "ship a secret", " alert" if leaking else ""),
        ]
        stats = '<div class="stats">' + "".join(
            f'<div class="stat{css}"><div class="v">{value}</div><div class="k">{label}</div></div>'
            for value, label, css in cells
        ) + "</div>"
        table = f"""<div class="scroll"><table class="grid">
    <thead><tr>
      <th>function</th><th class="num">versions</th><th>latest</th><th>archived</th>
      <th>runtime</th><th title="In first-party code, in the latest version">secrets</th>
      <th>latest change</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>"""
        content = stats + _section("Functions", table, count=len(functions))
    else:
        lead = "Every function lambda-watcher archives will be listed here."
        content = ('<div class="empty"><span class="big">Nothing is archived yet.</span>'
                   '<span class="cmd">lw setup</span> watches your downloads folder from now on.'
                   "</div>")

    body = f"""  <header class="top">
    <div class="eyebrow">Archive</div>
    <h1>Lambda archive</h1>
    <div class="lead">{lead}</div>
  </header>
  {content}"""
    footer = (
        f"Generated by {_esc(generated_by)} on {_esc(_stamp_now())}, and rewritten each time a "
        'version is archived. <span class="cmd">lw report</span> rewrites it by hand; '
        '<span class="cmd">lw report "&lt;function&gt;"</span> writes that function\'s full history.'
    )
    return _page("Lambda archive · every function", body, [("All functions", None)], footer)
