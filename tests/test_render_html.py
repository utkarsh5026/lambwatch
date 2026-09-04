"""What the HTML report paints: the highlighter, the icons, and the page they land on.

The highlighter is the part worth pinning down. It writes markup around text
that came out of somebody's Lambda, so the test that matters is not which token
turned purple — it is that every character of the input still arrives escaped,
whatever the grammar decided to do with it.
"""

from __future__ import annotations

import html as html_lib
import re

import pytest

from lambda_watcher.diffing import icons
from lambda_watcher.diffing.highlight import (
    FAMILY_BY_LANG,
    GRAMMARS,
    MAX_FILE,
    MAX_LINE,
    highlight,
    highlight_lines,
    language_of,
)
from lambda_watcher.diffing.render_html import CSS, _Row, _row_code, render_html
from lambda_watcher.ingest import Ingestor
from lambda_watcher.utils import LANG_BY_EXT

from tests.conftest import PY_V1, PY_V2
from tests.test_diff import _diff

_TAG = re.compile(r"<[^>]+>")

# One line per grammar, each carrying the part of it most likely to go wrong:
# an unterminated string, a comment that opens inside one, a key next to a value.
CORPUS: list[tuple[str, str]] = [
    ("python",     'msg = f"unterminated {name!r}  # not a comment'),
    ("python",     'DOC = """opens here and never closes'),
    ("python",     "@app.route\ndef handler(event, ctx=None): return True  # done"),
    ("javascript", 'const re = /a"b/; // "not a string"'),
    ("typescript", "export const n: number = 0x1f;  /* block */"),
    ("go",         'func main() { var n int64 = 1; fmt.Println("x", nil) }'),
    ("ruby",       "@count = :ok unless event.nil?  # frozen"),
    ("shell",      'for f in "$@"; do echo "${f}" || exit 1; done  # deploy'),
    ("json",       '{"region": "us-east-1", "n": 12, "debug": false, "arn": null}'),
    ("yaml",       "  MemorySize: 512  # bytes"),
    ("toml",       '[tool.demo]\nname = "orders"   # comment'),
    ("ini",        "version = 1.0.1  ; legacy"),
    ("html",       '<div class="row" id="x">&amp;ok</div><!-- banner -->'),
    ("css",        "@media print { .card { color: #4b8bbe; padding: 2px; } }"),
    ("sql",        "select id from orders WHERE total > 1 -- orders"),
    ("markdown",   "# Orders\n- see [docs](./x) and `code`"),
    ("dotenv",     '# never commit\nexport STAGE=prod\nNAME="orders"'),
    ("text",       "no grammar for this one"),
    ("binary",     "\x00\x01 not really text"),
]

# Text that would be an injection if a span were ever built around raw input.
HOSTILE: list[str] = [
    '<script>alert(1)</script>',
    '"><img src=x onerror=alert(1)>',
    "</span><span class='tk-k'>",
    'KEY = "</style><script>fetch(\'//evil\')</script>"',
    "&lt;already escaped&gt; & bare ampersand",
    "back\\slash \\\" quote 'single' `tick`",
    "emoji 🐍 and ünïcødé",
    "",
    "   ",
]


@pytest.mark.parametrize("lang,line", CORPUS)
def test_highlighting_returns_every_character_escaped(lang: str, line: str) -> None:
    assert _TAG.sub("", highlight(line, lang)) == html_lib.escape(line, quote=True)


@pytest.mark.parametrize("line", HOSTILE)
@pytest.mark.parametrize("lang", sorted({lang for lang, _ in CORPUS}))
def test_hostile_input_cannot_escape_its_span(lang: str, line: str) -> None:
    rendered = highlight(line, lang)
    assert _TAG.sub("", rendered) == html_lib.escape(line, quote=True)
    assert "<script" not in rendered
    assert not re.search(r"<(?!/?span\b)", rendered)


# Deliberately ungrammared: plain text has nothing to colour, and a binary file
# never reaches the highlighter with anything worth reading.
PLAIN = {"text", "binary"}


@pytest.mark.parametrize("lang,line", [pair for pair in CORPUS if pair[0] not in PLAIN])
def test_a_known_language_actually_gets_coloured(lang: str, line: str) -> None:
    assert 'class="tk-' in highlight(line, lang)


@pytest.mark.parametrize("lang,line", [pair for pair in CORPUS if pair[0] in PLAIN])
def test_a_language_without_a_grammar_is_left_alone(lang: str, line: str) -> None:
    assert highlight(line, lang) == html_lib.escape(line, quote=True)


def test_an_unknown_language_falls_back_to_plain_escaping() -> None:
    line = 'weird <thing> "quoted"'
    assert highlight(line, "brainfuck") == html_lib.escape(line, quote=True)


def test_a_minified_line_is_not_tokenised() -> None:
    # Bundled JavaScript arrives as one enormous line; spans would outweigh it.
    line = "var a=1;" * (MAX_LINE // 4)
    assert highlight(line, "javascript") == html_lib.escape(line, quote=True)
    assert 'class="tk-' in highlight("var a=1;", "javascript")


def test_no_empty_spans_are_emitted() -> None:
    for lang, line in CORPUS:
        assert '"></span>' not in highlight(line, lang)


def test_every_family_a_language_names_exists() -> None:
    assert set(FAMILY_BY_LANG.values()) == set(GRAMMARS)


def test_every_token_class_a_grammar_emits_has_a_colour() -> None:
    emitted = {name[0] for grammar in GRAMMARS.values() for name in grammar.groupindex}
    assert emitted, "no grammar declared any token class"
    for letter in sorted(emitted):
        assert f".tk-{letter} {{" in CSS, f"class tk-{letter} has no rule"
        assert f"--tk-{letter}:" in CSS, f"class tk-{letter} has no variable"


def test_a_dotfile_is_read_by_name_not_by_extension() -> None:
    # `language_for` sees no extension on these, so the index calls them text.
    assert language_of(".env.production", "text") == "dotenv"
    assert language_of("config/.npmrc", "text") == "dotenv"
    assert language_of("handler.py", "python") == "python"
    assert language_of("notes.txt", "text") == "text"


# ------------------------------------------------- the whole-file path
# Files whose point is a construct that spans lines. The middle line is the one
# a line-local lexer gets wrong, so that is the line each case checks.
SPANNING = [
    ("python", '"""Runtime configuration.\n\nTODO: move to Secrets Manager.\n"""\nimport os\n', 2, "s"),
    ("javascript", "/**\n * @param {object} event\n */\nconst h = 1;\n", 1, "c"),
    ("css", "/* theme\n   licence\n*/\n.card { color: red; }\n", 1, "c"),
    ("html", "<!--\n  banner\n-->\n<div>x</div>\n", 1, "c"),
    ("sql", "/* orders\n   report\n*/\nSELECT 1;\n", 1, "c"),
    ("ruby", "=begin\nthe explanation\n=end\nputs 1\n", 1, "c"),
]


@pytest.mark.parametrize("lang,text,line,cls", SPANNING)
def test_a_construct_that_spans_lines_is_coloured_on_every_line(lang, text, line, cls) -> None:
    painted = highlight_lines(text, lang)
    assert f'class="tk-{cls}"' in painted[line], painted[line]
    # ...which is exactly what tokenising the line on its own cannot know: it
    # either leaves the line bare or, worse, speckles the prose inside a comment
    # block with keyword colours. Either way it never reaches the right class.
    assert f'class="tk-{cls}"' not in highlight(text.split("\n")[line], lang)


@pytest.mark.parametrize("lang,text", [(lang, text) for lang, text, _, _ in SPANNING])
def test_whole_file_highlighting_returns_one_entry_per_line(lang: str, text: str) -> None:
    # `_paint` zips these against the same split with strict=True: if this ever
    # broke, every row below the break would borrow colour from its neighbour.
    assert len(highlight_lines(text, lang)) == len(text.split("\n"))


@pytest.mark.parametrize("lang,line", CORPUS)
def test_whole_file_highlighting_escapes_everything_too(lang: str, line: str) -> None:
    text = f"{line}\n{line}"
    rendered = "\n".join(highlight_lines(text, lang))
    assert _TAG.sub("", rendered) == html_lib.escape(text, quote=True)


@pytest.mark.parametrize("text", HOSTILE)
def test_whole_file_highlighting_cannot_be_escaped_either(text: str) -> None:
    rendered = "\n".join(highlight_lines(text, "python"))
    assert _TAG.sub("", rendered) == html_lib.escape(text, quote=True)
    assert not re.search(r"<(?!/?span\b)", rendered)


def test_a_file_too_big_or_too_wide_to_tokenise_is_left_plain() -> None:
    wide = "var a=1;" * MAX_LINE  # one minified bundle line
    assert highlight_lines(wide, "javascript") == [html_lib.escape(wide, quote=True)]
    big = "x = 1\n" * (MAX_FILE // 4)
    assert 'class="tk-' not in highlight_lines(big, "python")[0]
    assert 'class="tk-' in highlight_lines("x = 1\n", "python")[0]


def test_a_row_takes_its_colour_from_the_right_side_of_the_diff() -> None:
    old = [("gone = 1", '<span class="tk-OLD">gone = 1</span>')]
    new = [("kept = 2", '<span class="tk-NEW">kept = 2</span>')]
    removed = _Row("del", "1", "", "gone = 1")
    added = _Row("add", "", "1", "kept = 2")
    assert _row_code(removed, "python", old, new) == old[0][1]
    assert _row_code(added, "python", old, new) == new[0][1]


def test_a_row_that_does_not_match_its_file_falls_back_instead_of_borrowing() -> None:
    # The file moved on, or was rewritten between the diff and the render. The
    # wrong colour on the right line is a bug; the right colour on the wrong
    # line is a lie, so this must not reach for the painted line at all.
    stale = [("something else entirely", '<span class="tk-s">something else entirely</span>')]
    row = _Row("add", "", "1", "import os")
    assert _row_code(row, "python", None, stale) == highlight("import os", "python")
    # Out of range, and no file at all, take the same path.
    assert _row_code(_Row("add", "", "99", "import os"), "python", None, stale) == \
        highlight("import os", "python")
    assert _row_code(_Row("add", "", "1", "import os"), "python", None, None) == \
        highlight("import os", "python")


def test_diff_metadata_is_never_highlighted_as_code() -> None:
    row = _Row("meta", "", "", "\\ No newline at end of file")
    assert _row_code(row, "python", None, None) == html_lib.escape(row.text, quote=True)


# ------------------------------------------------------------------- icons
def test_every_icon_points_at_a_glyph_the_sprite_defines() -> None:
    sheet = icons.sprite()
    for key, (glyph, _) in icons.ICONS.items():
        assert glyph in icons.GLYPHS, f"{key} names a glyph that does not exist"
        assert f'id="g-{glyph}"' in sheet


def test_every_icon_key_has_a_colour_rule() -> None:
    generated = icons.css()
    for key in icons.ICONS:
        assert f".fic-{key} {{" in generated


def test_every_language_the_index_can_record_has_an_icon() -> None:
    for lang in set(LANG_BY_EXT.values()):
        assert lang in icons.ICONS, f"{lang} would fall back to the generic icon"


def test_a_language_worth_colouring_is_worth_an_icon() -> None:
    for lang in FAMILY_BY_LANG:
        assert lang in icons.ICONS, f"{lang} is highlighted but has no icon"


def test_the_path_overrides_the_language_where_it_knows_better() -> None:
    assert icons.icon_key("assets/logo.png", "binary") == "image"
    assert icons.icon_key("certs/server.pem", "pem") == "dotenv"
    assert icons.icon_key(".env.production", "dotenv") == "dotenv"
    assert icons.icon_key("handler.py", "python") == "python"
    assert icons.icon_key("something.qqq", "qqq") == "_other"


def test_icons_reference_the_sprite_rather_than_repeating_it() -> None:
    mark = icons.file_icon("handler.py", "python")
    assert '<use href="#g-code"/>' in mark
    assert "<path" not in mark  # the drawing lives in the sprite, once


# ------------------------------------------------------------- on the page
def test_the_report_paints_the_code_and_carries_one_sprite(cfg, db, ingestor: Ingestor, make_zip):
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V2}))
    page = render_html(_diff(cfg, db, ingestor))

    assert page.count('<svg class="sprite"') == 1
    assert 'id="g-code"' in page
    assert 'class="fic fic-python"' in page
    assert '<span class="tk-k">import</span>' in page
    assert '<td class="mark">+</td>' in page
    # The sign column widened every row, hunk headers included.
    assert 'colspan="4"' in page and 'colspan="3"' not in page
    # Still one file, no network.
    assert "http://" not in page and "https://" not in page
