"""Which *words* actually changed, when the line is the wrong unit.

A diff that paints a whole line green says "this line is new". Most of the time
it is not new — three characters of it are, and the eye has to re-read forty to
find them. This module finds those characters.

It does that at two scales, and the second is the reason the first exists at
all. `pair_rows` and `mark` work *inside* a diff that already found its lines,
narrowing an add/remove pair down to the words between them. `long_line_edits`
works where there are no usable lines to find: a minified bundle is one
8,000-character line, so a line diff quotes the entire file to show a changed
digit, and the only readable answer is the changed run plus enough either side
to place it.

Three problems, kept apart:

`pair_rows` decides *which* removed line a given added line is a rewrite of.
Getting that wrong is worse than not marking at all — a confident mark on an
unrelated pair sends the reader hunting for a change that is not there — so the
pairing is deliberately shy: it takes the strongest match for each line, and
only when the two are more alike than not.

`mark` then wraps the differing ranges. The line reaching it is already HTML,
so the wrapper is split at every tag boundary rather than straddling one: two
adjacent marks paint one continuous background, and the nesting stays
well-formed whatever `highlight` decided to emit.

`long_line_edits` is the whole-file case, and is deliberately not a diff: it
returns quotable excerpts, not a patch, because a patch of a one-line file is
the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    """Split a line into words, runs of whitespace and single punctuation marks.

    Word-level rather than character-level, so a changed identifier is marked as
    one unit instead of as the three letters inside it that happen to differ.
    """
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
        """The parts of ``[start, stop)`` that fall inside a marked span.

        Clipped to the piece being examined, so a span reaching across a syntax
        boundary comes back as the portion belonging to this piece.
        """
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


#: A file whose lines average more than this many characters has no usable
#: lines. Mean rather than maximum, on purpose: one 900-character embedded data
#: literal in an otherwise ordinary module is still best read as a line diff,
#: while a bundle is *every* line that wide.
LONG_LINE_MEAN = 400

#: Characters of unchanged text quoted either side of an edit. Enough to place
#: the change in a file the reader has never opened, short enough that twenty
#: edits still fit on a screen.
EDIT_CONTEXT = 24

#: How far apart the first and last difference may be before quoting the
#: differences stops being an excerpt and starts being the file again. It is a
#: span rather than an edit count because it also caps the token match below,
#: which is quadratic: measured on random text, the middle costs 0.02s at this
#: width, 0.07s at 8 KB, 0.3s at 16 KB and 3.5s at 64 KB. Reports render every
#: consecutive pair of versions, so the ceiling has to sit where it does.
MAX_EDIT_SPAN = 4000

#: Past this many separate edits the reader is scrolling a list instead of
#: reading a change, and the count itself is the more useful fact.
MAX_EDITS = 20


@dataclass
class WordEdit:
    """One changed run of characters in a file with no usable lines, in context.

    ``…e){var t=`` ``1`` → ``2`` ``;a=1;a=1;a=1…`` — the bundle around it is
    8,000 identical characters, and this is the fifty that carry the change.
    ``lead`` and ``trail`` are quoted from the *new* text and are never part of
    what changed; they exist so the reader can find the spot.

    ``before`` is empty for a pure insertion and ``after`` for a pure deletion,
    which is how a renderer tells the three cases apart. ``at`` is the character
    offset in the new text, and is what distinguishes two edits whose context
    happens to read alike — in a minified bundle that is most of them.
    """

    at: int
    before: str
    after: str
    lead: str
    trail: str

    def as_dict(self) -> dict:
        """This edit as plain JSON-ready data, for the machine-readable diff."""
        return {"at": self.at, "before": self.before, "after": self.after,
                "lead": self.lead, "trail": self.trail}


def mean_line_length(text: str) -> float:
    """Average characters per line, newline excluded. ``0.0`` for empty text.

    The test for whether a file can be diffed by line at all: an 8 KB minified
    bundle on one line scores 8,000, a Python module scores about 30. Compare
    against :data:`LONG_LINE_MEAN`.
    """
    lines = text.splitlines()
    if not lines:
        return 0.0
    return sum(len(line) for line in lines) / len(lines)


def _common_edges(before: str, after: str) -> tuple[int, int]:
    """How many characters the two texts share at the start and at the end.

    The cheap half of the work, and for the case this module exists to serve —
    a rebuilt bundle where one constant moved — very nearly all of it: 8,000
    characters collapse to a two-character middle in one linear pass, before
    the quadratic matcher sees anything.

    The two counts never overlap, so ``aaa`` → ``aaaa`` reports a shared prefix
    and a one-character insertion rather than counting the same ``a`` twice.
    """
    limit = min(len(before), len(after))
    head = 0
    while head < limit and before[head] == after[head]:
        head += 1
    tail = 0
    while tail < limit - head and before[-1 - tail] == after[-1 - tail]:
        tail += 1
    return head, tail


def _offsets(tokens: list[str]) -> list[int]:
    """Where each token starts, plus one past the end.

    Precomputed because the alternative — re-joining the prefix at every opcode
    — is quadratic in a token list that can run to thousands of entries.
    """
    starts, at = [], 0
    for token in tokens:
        starts.append(at)
        at += len(token)
    starts.append(at)
    return starts


def long_line_edits(before: str, after: str) -> tuple[list[WordEdit], str | None]:
    """The changed runs of two texts too wide to diff by line, and why not more.

    Returns ``(edits, reason)`` with exactly one of them meaningful: a reason is
    what to tell the reader in place of the edits, in the same voice as
    :attr:`~.compare.FileChange.skipped_reason` — ``changes span 412,003
    characters``. That reason reports the distance between the first difference
    and the last, and deliberately not how much of the file differs: a rebuilt
    bundle and four changed bytes at opposite ends of an intact one look the
    same from here, and telling them apart costs the match this is declining to
    run. Callers print the size delta alongside either way.

    The shared prefix and suffix come off first, which is the whole reason this
    is affordable: matching 8,000 tokens against 8,000 tokens is quadratic, but
    a bundle rebuilt with one constant changed has a two-character middle. Only
    that middle reaches the matcher, and past :data:`MAX_EDIT_SPAN` it does not
    reach it at all.

    See :func:`word_diff` for the same idea one line at a time.
    """
    if before == after:
        return [], "no textual change"

    head, tail = _common_edges(before, after)
    old_mid = before[head : len(before) - tail]
    new_mid = after[head : len(after) - tail]
    span = max(len(old_mid), len(new_mid))
    if span > MAX_EDIT_SPAN:
        # Says how far apart the differences are, not how much differs, because
        # only the first is known here: a wholly rebuilt bundle and four changed
        # bytes at opposite ends of an intact one produce the same span, and
        # measuring which is which costs the match this branch exists to skip.
        return [], f"changes span {span:,} characters"

    old, new = _tokens(old_mid), _tokens(new_mid)
    old_at, new_at = _offsets(old), _offsets(new)

    edits: list[WordEdit] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if len(edits) == MAX_EDITS:
            return [], f"more than {MAX_EDITS} separate changes"
        removed = old_mid[old_at[i1] : old_at[i2]]
        added = new_mid[new_at[j1] : new_at[j2]]
        at = head + new_at[j1]
        edits.append(WordEdit(
            at=at,
            before=removed,
            after=added,
            lead=after[max(0, at - EDIT_CONTEXT) : at],
            trail=after[at + len(added) : at + len(added) + EDIT_CONTEXT],
        ))
    return edits, None
