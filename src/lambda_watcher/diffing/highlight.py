"""Syntax highlighting for the HTML report, without a highlighting library.

A report is meant to survive being emailed, dropped in a bucket or attached to
a change ticket, so it cannot pull Prism or highlight.js off a CDN — and
vendoring one would be a lot of borrowed code to carry for a diff view. What a
diff actually needs is much smaller: enough colour to tell comments, strings,
numbers and keywords apart so the eye can skip to the line that matters.

So each language family is one combined regular expression whose named groups
carry the token class, scanned left to right; whatever falls between two
matches is plain text. The first letter of the group name *is* the class, which
is what keeps the rule tables below readable.

There are two ways in:

* ``highlight_lines(text, lang)`` tokenises a **whole file** and hands back one
  string per line. This is the one the report uses, because a docstring or a
  licence header only comes out right when the lexer can see where the block
  opened. Multi-line rules are written to span newlines, so the same grammar
  serves both entry points.
* ``highlight(text, lang)`` tokenises **one line** on its own. A diff row falls
  back to this when its source file cannot be read or no longer matches, so a
  report degrades to line-local colour rather than to none.

It is approximate and never authoritative either way: nothing downstream reads
these classes, and getting a token wrong costs a colour, not a fact.

The one invariant that has to hold is the escaping one: every character of the
input comes back HTML-escaped, whether it landed inside a span or not.
``tests/test_highlight.py`` asserts exactly that.
"""

from __future__ import annotations

import html
import re
from pathlib import PurePosixPath

# Token classes. The letter is the CSS class suffix (`tk-k`) and the first
# character of every named group that produces it:
#
#   c  comment                     k  keyword
#   s  string                      t  literal constant, built-in type
#   n  number                      f  name — call, decorator, variable, attribute
#   y  key, tag or heading

# A line long enough to be minified output is not worth tokenising: the regex
# would do real work and the spans would outweigh the code they wrap. The file
# bound catches the same thing from the other side — a bundle that *is* one
# enormous line, or a generated file where the win is not worth the scan.
MAX_LINE = 4000
MAX_FILE = 1 << 20


def _grammar(*rules: tuple[str, str]) -> re.Pattern[str]:
    """Combine ``(class, pattern)`` rules into one alternation.

    Order matters — the leftmost match wins, and ties at the same position go to
    the earlier rule, so comments and strings come before anything that could
    also match inside one.

    Every group a rule opens must be non-capturing: ``lastgroup`` reports the
    last *capturing* group that matched, so one stray ``(…)`` would hand back a
    group with no class letter. The count check makes that a startup failure
    rather than a rendering one.
    """
    joined = "|".join(f"(?P<{cls}{i}>{rule})" for i, (cls, rule) in enumerate(rules))
    # MULTILINE so that a rule anchored with ^ (a YAML key, a heading, an INI
    # section) keeps meaning "start of line" when the grammar is handed a whole
    # file. On a single line it changes nothing.
    pattern = re.compile(joined, re.MULTILINE)
    if pattern.groups != len(rules):
        raise ValueError("a highlight rule opened a capturing group; use (?:…)")
    return pattern


def _words(cls: str, words: str) -> tuple[str, str]:
    """A word-boundary alternation over a whitespace-separated vocabulary."""
    return cls, r"\b(?:" + "|".join(words.split()) + r")\b"


def _token_class(match: re.Match[str]) -> str:
    """The one-letter token class of the rule that matched, ``""`` when none did.

    ``re.Match.lastgroup`` is ``str | None`` because a pattern can match with no
    named group taking part. A grammar built by :func:`_grammar` cannot: it names
    every alternative and refuses to compile if a rule opens a capturing group of
    its own, so exactly one named group is set on every match.

    The empty fallback is therefore unreachable, and it is an empty string rather
    than a raise or a guessed class because both callers already treat ``""`` as
    "emit this uncoloured" - a report that has been archived must still render.
    """
    return match.lastgroup[0] if match.lastgroup else ""


# --------------------------------------------------------------- shared rules
# Quoted strings accept an unterminated tail so that the opening line of a
# multi-line string still reads as one, rather than dissolving into keywords.
# Rules that must stay on one line say so with an explicit ``\n`` exclusion:
# scanning a whole file, an unterminated quote would otherwise swallow the rest
# of it. Rules that legitimately span lines end at ``\Z``, the end of the text —
# not ``$``, which under re.MULTILINE would stop at the first newline.
_HASH      = ("c", r"#[^\n]*")
_SEMI      = ("c", r";[^\n]*")
_SLASHES   = ("c", r"//[^\n]*")
_BLOCK     = ("c", r"/\*[\s\S]*?(?:\*/|\Z)")
_SQL_DASH  = ("c", r"--[^\n]*")
_SGML      = ("c", r"<!--[\s\S]*?(?:-->|\Z)")
_DQUOTE    = ("s", r'"(?:\\.|[^"\\\n])*"?')
_SQUOTE    = ("s", r"'(?:\\.|[^'\\\n])*'?")
_BACKTICK  = ("s", r"`(?:\\.|[^`\\])*`?")
_TRIPLE    = ("s", r'[rbfuRBFU]{0,2}(?:"""[\s\S]*?(?:"""|\Z)|\'\'\'[\s\S]*?(?:\'\'\'|\Z))')
_PY_QUOTE  = ("s", r'[rbfuRBFU]{0,2}(?:"(?:\\.|[^"\\\n])*"?|\'(?:\\.|[^\'\\\n])*\'?)')
_NUMBER    = ("n", r"\b(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)\w*")
_CALL      = ("f", r"[A-Za-z_]\w*(?=\s*\()")


# ------------------------------------------------------------------ languages
_PY_KEYWORDS = """
    and as assert async await break case class continue def del elif else except finally for from
    global if import in is lambda match nonlocal not or pass raise return try while with yield
"""
_PY_CONSTANTS = "True False None self cls NotImplemented Ellipsis"

_C_KEYWORDS = """
    abstract as async await break case catch class const constexpr continue debugger default defer
    delegate delete do else enum event export extends fallthrough final finally fn for func function
    go goto if impl implements import in instanceof interface internal let lock loop mod module move
    mut namespace new operator out override package private protected pub public range readonly
    record ref return sealed select static struct super switch synchronized template throw throws
    trait transient try type typealias typedef typeof union unsafe use using val var virtual void
    volatile when where while with yield
"""
_C_CONSTANTS = """
    any bool boolean byte char chan complex64 complex128 double error false float float32 float64
    Infinity int int8 int16 int32 int64 isize long map NaN never nil null number object rune sbyte
    self short str string symbol this true u8 u16 u32 u64 uint uint8 uint16 uint32 uint64 uintptr
    undefined unknown usize
"""

_RB_KEYWORDS = """
    alias and begin break case class def do else elsif end ensure for if in module next not or redo
    require require_relative rescue retry return super then undef unless until when while yield
"""
_RB_CONSTANTS = "true false nil self __FILE__ __dir__"

_SH_KEYWORDS = """
    alias break case continue declare do done elif else esac eval exec exit export fi for function
    if in local readonly return select set shift source then trap unset until while
    ADD ARG CMD COPY ENTRYPOINT ENV EXPOSE FROM HEALTHCHECK LABEL ONBUILD RUN SHELL STOPSIGNAL USER
    VOLUME WORKDIR
"""

_SQL_KEYWORDS = """
    add all alter and as asc begin between by case column commit constraint create cross default
    delete desc distinct drop else end exists foreign from full group having if in index inner
    insert into is join key left like limit not null offset on or order outer primary references
    returning right rollback select set table then union unique update values view when where with
"""

GRAMMARS: dict[str, re.Pattern[str]] = {
    "python": _grammar(
        _HASH, _TRIPLE, _PY_QUOTE, _NUMBER,
        _words("k", _PY_KEYWORDS), _words("t", _PY_CONSTANTS),
        ("f", r"@[\w.]+"), _CALL,
    ),
    "clike": _grammar(
        _SLASHES, _BLOCK, _DQUOTE, _SQUOTE, _BACKTICK, _NUMBER,
        _words("k", _C_KEYWORDS), _words("t", _C_CONSTANTS),
        ("f", r"@[\w.]+"), _CALL,
    ),
    "ruby": _grammar(
        ("c", r"^=begin[\s\S]*?(?:^=end[^\n]*|\Z)"),
        _HASH, _DQUOTE, _SQUOTE, _NUMBER,
        _words("k", _RB_KEYWORDS), _words("t", _RB_CONSTANTS),
        ("t", r":[A-Za-z_]\w*[?!]?"), ("f", r"[@$]{1,2}[A-Za-z_]\w*"), _CALL,
    ),
    "shell": _grammar(
        _HASH, _DQUOTE, _SQUOTE, _BACKTICK, _NUMBER,
        _words("k", _SH_KEYWORDS),
        ("f", r"\$\{[^}\n]*\}?|\$[A-Za-z_]\w*|\$[@*#?!$0-9-]"),
    ),
    "json": _grammar(
        ("y", r'"(?:\\.|[^"\\])*"(?=\s*:)'), _DQUOTE, _NUMBER,
        _words("t", "true false null"),
    ),
    "yaml": _grammar(
        _HASH,
        ("y", r"^[ \t]*(?:-[ \t]+)?[A-Za-z_0-9.$/-]+(?=[ \t]*:(?:[ \t]|$))"),
        _DQUOTE, _SQUOTE, _NUMBER,
        _words("t", "true false null yes no on off"),
        ("f", r"[&*][A-Za-z_]\w*|<<"),
    ),
    "ini": _grammar(
        _HASH, _SEMI,
        ("y", r"^[ \t]*\[[^\]\n]*\]?"),
        ("y", r"^[ \t]*[A-Za-z_0-9.$-]+(?=[ \t]*=)"),
        _DQUOTE, _SQUOTE, _NUMBER,
        _words("t", "true false null"),
    ),
    "dotenv": _grammar(
        _HASH,
        ("y", r"^[ \t]*(?:export[ \t]+)?[A-Za-z_][\w.]*(?==)"),
        _DQUOTE, _SQUOTE, _NUMBER,
    ),
    "markup": _grammar(
        _SGML,
        ("c", r"<[?!][^>\n]*>?"),
        ("y", r"</?[A-Za-z][\w:.-]*|/?>"),
        ("f", r"[A-Za-z_:][\w:.-]*(?=\s*=)"),
        _DQUOTE, _SQUOTE,
        ("t", r"&\#?\w+;"),
    ),
    "css": _grammar(
        _BLOCK, _DQUOTE, _SQUOTE,
        ("k", r"@[-\w]+"),
        ("f", r"[-a-zA-Z][-\w]*(?=[ \t]*:)"),
        ("n", r"\#[0-9a-fA-F]{3,8}\b"), _NUMBER,
        ("y", r"\.[-\w]+|:{1,2}[-\w]+"),
    ),
    "sql": _grammar(
        _SQL_DASH, _BLOCK, _SQUOTE, _DQUOTE, _NUMBER,
        ("k", r"(?i:" + _words("k", _SQL_KEYWORDS)[1] + r")"),
    ),
    "markdown": _grammar(
        ("y", r"^\#{1,6}[ \t].*"),
        ("k", r"^[ \t]*(?:```|~~~).*|^[ \t]*(?:[-*+]|\d+\.)(?=[ \t])|^[ \t]*>"),
        ("s", r"`[^`\n]*`?"),
        ("f", r"\[[^\]\n]*\]\([^)\n]*\)?"),
    ),
}

FAMILY_BY_LANG: dict[str, str] = {
    "python": "python", "javascript": "clike", "typescript": "clike", "java": "clike",
    "kotlin": "clike", "go": "clike", "rust": "clike", "csharp": "clike", "php": "clike",
    "scala": "clike", "swift": "clike", "c": "clike", "cpp": "clike",
    "ruby": "ruby",
    "shell": "shell", "powershell": "shell", "dockerfile": "shell", "makefile": "shell",
    "json": "json", "yaml": "yaml", "toml": "ini", "ini": "ini", "dotenv": "dotenv",
    "html": "markup", "xml": "markup", "css": "css", "sql": "sql", "markdown": "markdown",
}


# `utils.language_for` reads an extension, and a dotfile has none: `.env.production`
# and `.npmrc` are both indexed as plain text. Correcting that here rather than in
# the analysis layer keeps it a presentation choice — the label already written into
# every manifest on disk stays what it was.
_BY_NAME: tuple[tuple[str, str], ...] = ((".env", "dotenv"), (".npmrc", "dotenv"))


def language_of(path: str, lang: str) -> str:
    """The language to render a file as: what the index recorded, corrected by name."""
    name = PurePosixPath(path).name.lower()
    for prefix, corrected in _BY_NAME:
        if name.startswith(prefix):
            return corrected
    return lang


def highlight_lines(text: str, lang: str) -> list[str]:
    """A whole file as escaped HTML, one entry per line.

    This is where the cross-line constructs are won: the grammar sees the whole
    text, so a docstring, a licence header or an HTML comment is one match no
    matter how many lines it covers, and each line it crosses gets its own span.

    The result always has exactly ``len(text.split("\n"))`` entries, so a caller
    can index it by line number and check its own work against the same split.

    The cost is scanning a whole file to colour the handful of lines a hunk
    quotes from it — about 90ms for a 220 KB file, and `DiffConfig` already
    refuses to diff anything past 512 KB. Skipping the scan for files with no
    spanning construct in them would save that, at the price of a second path
    through here that a later rule could silently fall out of sync with.
    """
    raw = text.split("\n")
    grammar = GRAMMARS.get(FAMILY_BY_LANG.get(lang, ""))
    if grammar is None or len(text) > MAX_FILE or any(len(line) > MAX_LINE for line in raw):
        return [html.escape(line, quote=True) for line in raw]

    lines: list[list[str]] = [[]]

    def emit(chunk: str, cls: str) -> None:
        """Append one grammar match to the line buffer, wrapped in its token class.

        Splits on newlines as it goes, because a single match can span lines — a
        block comment or a triple-quoted string — and the output is built per line.
        An empty ``cls`` emits escaped text with no wrapper.
        """
        for i, piece in enumerate(chunk.split("\n")):
            if i:
                lines.append([])
            if piece:
                escaped = html.escape(piece, quote=True)
                lines[-1].append(f'<span class="tk-{cls}">{escaped}</span>' if cls else escaped)

    pos = 0
    for match in grammar.finditer(text):
        start, end = match.span()
        if start == end:
            continue
        if start > pos:
            emit(text[pos:start], "")
        emit(match.group(), _token_class(match))
        pos = end
    emit(text[pos:], "")
    return ["".join(parts) for parts in lines]


def highlight(text: str, lang: str) -> str:
    """One line of source as escaped HTML, with token spans where we know the language.

    Falls back to plain escaped text for anything unrecognised, so the caller
    never needs to ask whether a language is supported.
    """
    grammar = GRAMMARS.get(FAMILY_BY_LANG.get(lang, ""))
    if grammar is None or len(text) > MAX_LINE:
        return html.escape(text, quote=True)

    parts: list[str] = []
    pos = 0
    for match in grammar.finditer(text):
        start, end = match.span()
        if start == end:  # a zero-width rule would colour nothing and stall nothing
            continue
        if start > pos:
            parts.append(html.escape(text[pos:start], quote=True))
        token = html.escape(match.group(), quote=True)
        cls = _token_class(match)
        parts.append(f'<span class="tk-{cls}">{token}</span>' if cls else token)
        pos = end
    parts.append(html.escape(text[pos:], quote=True))
    return "".join(parts)
