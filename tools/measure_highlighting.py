"""Score the report's highlighter against Pygments on real files.

The highlighter in `diffing/highlight.py` is a table of regexes, not a parser,
so "is it good enough" is an empirical question rather than an argument. This
answers it: point the script at a tree of real source files, lex each file both
ways, and count how often we agree with a real lexer about what is a comment,
what is a string, and what is code.

Pygments is the oracle and is *not* a dependency of the package — install it
alongside the dev extras when you want to re-run this:

    .venv/bin/pip install pygments
    .venv/bin/python tools/measure_highlighting.py            # searches / for files
    .venv/bin/python tools/measure_highlighting.py ~/src       # or a tree you choose

Two columns matter. `before` is what the old line-local path scores; `after` is
the whole-file path the report actually uses. The gap between them is the value
of tokenising a file rather than a row.

Only the comment/string/code distinction is scored, because it is the only one
where both lexers certainly mean the same thing. Finer classes (is `self` a
constant? is a `<` part of the tag?) are taste, and counting them would measure
the mapping below rather than the highlighter.
"""

from __future__ import annotations

import collections
import html
import random
import re
import subprocess
import sys
from pathlib import Path

from pygments import lexers
from pygments.token import Comment, String

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lambda_watcher.diffing.highlight import (  # noqa: E402
    FAMILY_BY_LANG,
    GRAMMARS,
    highlight_lines,
)

LANGUAGES = {
    "python": ("py", lexers.PythonLexer), "javascript": ("js", lexers.JavascriptLexer),
    "typescript": ("ts", lexers.TypeScriptLexer), "json": ("json", lexers.JsonLexer),
    "yaml": ("yml", lexers.YamlLexer), "toml": ("toml", lexers.TOMLLexer),
    "ini": ("ini", lexers.IniLexer), "markdown": ("md", lexers.MarkdownLexer),
    "shell": ("sh", lexers.BashLexer), "html": ("html", lexers.HtmlLexer),
    "css": ("css", lexers.CssLexer), "sql": ("sql", lexers.SqlLexer),
    "go": ("go", lexers.GoLexer), "ruby": ("rb", lexers.RubyLexer),
    "xml": ("xml", lexers.XmlLexer),
}

MAX_FILES, MAX_CHARS, MAX_LINE = 60, 400_000, 400
_SPAN = re.compile(r'<span class="tk-(.)">((?:[^<]|<(?!/span>))*)</span>|([^<]+)')


def bucket(cls: str) -> str:
    """Collapse to the distinction both lexers agree on the meaning of."""
    return cls if cls in ("c", "s") else "."


def ours_line(line: str, lang: str) -> list[str]:
    """Per-character classes for one line, lexed with no knowledge of the lines above.

    This is the *old* path, kept only so the report can show what it scored. It is
    what the highlighter used to do, and it gets a line inside a block comment or a
    triple-quoted string wrong every time, because from one row you cannot tell
    that the construct ever opened. The `before` column is this.
    """
    classes = [""] * len(line)
    grammar = GRAMMARS.get(FAMILY_BY_LANG.get(lang, ""))
    if grammar:
        for match in grammar.finditer(line):
            for i in range(*match.span()):
                classes[i] = match.lastgroup[0]
    return classes


def ours_file(text: str, lang: str) -> list[list[str]]:
    """Per-character classes for every line, read back out of the emitted HTML."""
    rows = []
    for line in highlight_lines(text, lang):
        row: list[str] = []
        for match in _SPAN.finditer(line):
            cls = match.group(1) or ""
            row.extend([cls] * len(html.unescape(match.group(2) or match.group(3) or "")))
        rows.append(row)
    return rows


def theirs(text: str, lexer) -> list[str]:
    """Per-character classes from Pygments, collapsed to comment/string/code.

    The oracle. Only these three classes are scored, because they are the only
    ones where both lexers certainly mean the same thing.
    """
    classes = [""] * len(text)
    for pos, token, value in lexer.get_tokens_unprocessed(text):
        cls = "c" if token in Comment else "s" if token in String else ""
        for i in range(pos, min(pos + len(value), len(text))):
            classes[i] = cls
    return classes


def row(label: str, seen: collections.Counter[str]) -> str:
    """One formatted table row: the counts, then agreement before and after."""
    def pct(key: str, over: str) -> float:
        """``key`` as a percentage of ``over``, treating a zero denominator as one."""
        return 100 * seen[key] / (seen[over] or 1)

    return (f"{label:<11}{seen['files']:>6}{seen['lines']:>7}{seen['chars']:>9} │ "
            f"{pct('before', 'chars'):>7.1f}%{pct('after', 'chars'):>7.1f}% │ "
            f"{pct('lb', 'lines'):>12.1f}%{pct('la', 'lines'):>11.1f}%")


def corpus(root: str, ext: str) -> list[Path]:
    """Shuffled list of files with the given extension under ``root``.

    Shelling out to `find` rather than walking in Python: this runs over ``/`` by
    default, where a `rglob` spends most of its time raising `PermissionError`.
    `/proc` and `/sys` are dropped because their contents are not files in any
    sense that matters here, and the size cap keeps a stray minified bundle from
    dominating the character counts.

    Shuffled so a capped sample is spread across the tree instead of being
    whatever the first directory happened to hold.
    """
    found = subprocess.run(
        ["find", root, "-name", f"*.{ext}", "-type", "f", "-size", "-120k"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    ).stdout.split("\n")
    paths = [Path(p) for p in found if p and "/proc/" not in p and "/sys/" not in p]
    random.shuffle(paths)
    return paths


def main() -> int:
    """Lex the corpus both ways, print per-language agreement, and return an exit status."""
    root = sys.argv[1] if len(sys.argv) > 1 else "/"
    random.seed(7)
    print(f"{'language':<11}{'files':>6}{'lines':>7}{'chars':>9} │ "
          f"{'before':>8}{'after':>8} │ {'lines before':>13}{'lines after':>12}")
    print("─" * 78)
    total: collections.Counter[str] = collections.Counter()

    for lang, (ext, lexer_cls) in LANGUAGES.items():
        lexer = lexer_cls()
        seen = collections.Counter()
        for path in corpus(root, ext):
            if seen["files"] >= MAX_FILES or seen["chars"] > MAX_CHARS:
                break
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not text.strip() or len(text) > 120_000:
                continue
            seen["files"] += 1
            reference, painted, offset = theirs(text, lexer), ours_file(text, lang), 0
            for number, line in enumerate(text.splitlines(keepends=True)):
                body = line.rstrip("\n")
                truth = reference[offset:offset + len(body)]
                offset += len(line)
                if not body.strip() or len(body) > MAX_LINE:
                    continue
                after = painted[number] if number < len(painted) else []
                before = ours_line(body, lang)
                seen["lines"] += 1
                seen["chars"] += len(body)
                seen["before"] += sum(bucket(a) == bucket(b) for a, b in zip(before, truth, strict=False))
                seen["after"] += sum(bucket(a) == bucket(b) for a, b in zip(after, truth, strict=False))
                seen["lb"] += all(bucket(a) == bucket(b) for a, b in zip(before, truth, strict=False))
                seen["la"] += all(bucket(a) == bucket(b) for a, b in zip(after, truth, strict=False))
        if not seen["chars"]:
            continue
        total.update(seen)
        print(row(lang, seen))

    print("─" * 78)
    print(row("ALL", total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
