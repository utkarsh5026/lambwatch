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

Two limits are deliberate:

* **Highlighting is line-local.** Diff hunks are discontinuous, so there is no
  state worth carrying between the lines a report shows. A docstring or a
  ``/* … */`` block that spans lines is coloured on the line it opens and then
  stops — the honest answer for a view that only ever shows fragments.
* **It is approximate and never authoritative.** Nothing downstream reads these
  classes; getting a token wrong costs a colour, not a fact.

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
# would do real work and the spans would outweigh the code they wrap.
MAX_LINE = 4000


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
    pattern = re.compile("|".join(f"(?P<{cls}{i}>{rule})" for i, (cls, rule) in enumerate(rules)))
    if pattern.groups != len(rules):
        raise ValueError("a highlight rule opened a capturing group; use (?:…)")
    return pattern


def _words(cls: str, words: str) -> tuple[str, str]:
    """A word-boundary alternation over a whitespace-separated vocabulary."""
    return cls, r"\b(?:" + "|".join(words.split()) + r")\b"


# --------------------------------------------------------------- shared rules
# Quoted strings accept an unterminated tail so that the opening line of a
# multi-line string still reads as one, rather than dissolving into keywords.
_HASH      = ("c", r"#[^\n]*")
_SEMI      = ("c", r";[^\n]*")
_SLASHES   = ("c", r"//[^\n]*")
_BLOCK     = ("c", r"/\*.*?(?:\*/|$)")
_SQL_DASH  = ("c", r"--[^\n]*")
_SGML      = ("c", r"<!--.*?(?:-->|$)")
_DQUOTE    = ("s", r'"(?:\\.|[^"\\])*"?')
_SQUOTE    = ("s", r"'(?:\\.|[^'\\])*'?")
_BACKTICK  = ("s", r"`(?:\\.|[^`\\])*`?")
_TRIPLE    = ("s", r'[rbfuRBFU]{0,2}(?:"""(?:.*?"""|.*)|\'\'\'(?:.*?\'\'\'|.*))')
_PY_QUOTE  = ("s", r'[rbfuRBFU]{0,2}(?:"(?:\\.|[^"\\])*"?|\'(?:\\.|[^\'\\])*\'?)')
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
        parts.append(f'<span class="tk-{match.lastgroup[0]}">{token}</span>')
        pos = end
    parts.append(html.escape(text[pos:], quote=True))
    return "".join(parts)
