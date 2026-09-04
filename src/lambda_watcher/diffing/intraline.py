"""Which *words* of a changed line actually changed.

A diff that paints a whole line green says "this line is new". Most of the time
it is not new — three characters of it are, and the eye has to re-read forty to
find them. This module finds those characters, and marks them inside a line the
highlighter has already coloured.

Two problems, kept apart:

`pair_rows` decides *which* removed line a given added line is a rewrite of.
Getting that wrong is worse than not marking at all — a confident mark on an
unrelated pair sends the reader hunting for a change that is not there — so the
pairing is deliberately shy: it takes the strongest match for each line, and
only when the two are more alike than not.

`mark` then wraps the differing ranges. The line reaching it is already HTML,
so the wrapper is split at every tag boundary rather than straddling one: two
adjacent marks paint one continuous background, and the nesting stays
well-formed whatever `highlight` decided to emit.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

#: Words, runs of whitespace, and every other character on its own. Splitting
#: this way means a renamed identifier marks as one unit instead of dissolving
#: into the letters it happens to share with the old name.
_TOKEN = re.compile(r"\w+|\s+|.")

#: A tag, an entity, or a run of neither — the three things a highlighted line
#: is made of. Entities matter because `&amp;` is five characters of markup and
#: one character of the file.
_PIECE = re.compile(r"<[^>]*>|&[#A-Za-z0-9]+;|[^<&]+|.")

#: Below this, "rewrite" is a fiction: the two lines have little in common and
#: marking their scattered shared characters would be confetti, not information.
MIN_SIMILARITY = 0.5

#: Above this, almost the whole line is marked and the marks stop distinguishing
#: anything — the plain add/remove wash already said it.
MAX_MARKED = 0.75

#: Pairing is quadratic in the size of a replaced block. Blocks this large are
#: wholesale rewrites, where per-word marks would not help anyone anyway.
MAX_BLOCK = 40


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _ranges(tokens: list[str], opcodes: list[tuple], side: int) -> list[tuple[int, int]]:
    """Character ranges of the tokens this side does not share with the other."""
    starts, offset = [], 0
    for token in tokens:
        starts.append(offset)
        offset += len(token)
    starts.append(offset)

    spans: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        lo, hi = (i1, i2) if side == 0 else (j1, j2)
        if lo == hi:
            continue
        if spans and spans[-1][1] == starts[lo]:
            spans[-1] = (spans[-1][0], starts[hi])
        else:
            spans.append((starts[lo], starts[hi]))
    return spans


def word_diff(before: str, after: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """The differing character ranges of two lines, as ``(before, after)``.

    Returns empty lists when marking would not help: when the lines are too
    unlike to be a rewrite of each other, and when so much of them differs that
    the marks would cover the line they are supposed to pick things out of.
    """
    if before == after:
        return [], []
    old, new = _tokens(before), _tokens(after)
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    if matcher.ratio() < MIN_SIMILARITY:
        return [], []

    opcodes = matcher.get_opcodes()
    old_spans = _ranges(old, opcodes, 0)
    new_spans = _ranges(new, opcodes, 1)
    marked = sum(hi - lo for lo, hi in old_spans) + sum(hi - lo for lo, hi in new_spans)
    if marked > MAX_MARKED * (len(before) + len(after)):
        return [], []
    return old_spans, new_spans


def pair_rows(removed: list[str], added: list[str]) -> dict[tuple[int, int], None]:
    """Which removed line each added line rewrites, as ``{(old_i, new_i)}``.

    Strongest pair first, each line spoken for once. A line whose best partner
    is still mostly different stays unpaired, and is painted the plain way.
    """
    if not removed or not added or len(removed) > MAX_BLOCK or len(added) > MAX_BLOCK:
        return {}

    scored = []
    for i, old in enumerate(removed):
        for j, new in enumerate(added):
            ratio = SequenceMatcher(None, old, new, autojunk=False).ratio()
            if ratio >= MIN_SIMILARITY:
                scored.append((ratio, i, j))

    pairs: dict[tuple[int, int], None] = {}
    used_old: set[int] = set()
    used_new: set[int] = set()
    for _, i, j in sorted(scored, key=lambda s: (-s[0], s[1], s[2])):
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        pairs[(i, j)] = None
    return pairs


def mark(rendered: str, spans: list[tuple[int, int]], css: str = "wd") -> str:
    """Wrap the given character ranges of an already-highlighted line.

    ``spans`` index the *plain text* of the line — the same offsets `word_diff`
    returned — while ``rendered`` is what `highlight` made of it. Walking the
    two together is the whole job: markup is stepped over, an entity counts as
    the one character it stands for, and a range that reaches across a syntax
    span is emitted as one wrapper per piece it covers.
    """
    if not spans:
        return rendered

    def covered(start: int, stop: int) -> list[tuple[int, int]]:
        return [(max(lo, start), min(hi, stop)) for lo, hi in spans if lo < stop and hi > start]

    out: list[str] = []
    at = 0
    for piece in _PIECE.findall(rendered):
        if piece.startswith("<"):
            out.append(piece)
            continue
        width = 1 if piece.startswith("&") else len(piece)
        hits = covered(at, at + width)
        if not hits:
            out.append(piece)
        elif piece.startswith("&"):
            out.append(f'<span class="{css}">{piece}</span>')
        else:
            cursor = at
            for lo, hi in hits:
                out.append(piece[cursor - at : lo - at])
                out.append(f'<span class="{css}">{piece[lo - at : hi - at]}</span>')
                cursor = hi
            out.append(piece[cursor - at :])
        at += width
    return "".join(out)
