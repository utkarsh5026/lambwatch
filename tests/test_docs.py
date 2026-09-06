"""The documentation quotes real output, and this is what keeps it honest.

``docs/examples/build_demo.py`` builds a demo Lambda and runs the real pipeline
over it. Every terminal block on the GitHub Pages site and in the README is
copied from its output, so the two can silently drift apart — a renderer tweak
that moves a column, a scanner rule that stops firing. These tests fail when
they do, and name the command to regenerate from.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# Rich renders the same output differently on a Windows console: it substitutes
# the rounded box corners the diff panel is drawn with (╭ becomes ┌, while │ is
# left alone) and sizes some columns differently. That is a property of the
# terminal, not a defect in the documentation, and Rich offers no way to turn it
# off from the environment. The docs are generated on POSIX, so the captures are
# compared there; the structural checks below still run everywhere.
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Rich substitutes box characters on Windows consoles by design",
)

REPO = Path(__file__).resolve().parents[1]
SITE = REPO / "docs" / "index.html"
README = REPO / "README.md"
BUILDER = REPO / "docs" / "examples" / "build_demo.py"


# Three things legitimately differ between runs: when a version was archived,
# the git mirror's commit ids (git hashes the commit time), and the free disk
# space `doctor` reports. Every other character of a documented capture has to
# match what the tool printed.
_VARIABLE = [
    (re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"), "<archived>"),
    (re.compile(r"^[0-9a-f]{7,40}(?= order-processor v\d)"), "<commit>"),
    # `doctor` reports the machine's free space, which is not a property of the
    # tool at all: the page quotes whatever the run that produced it saw.
    (re.compile(r"^(disk free)\s+\S+\s+.*$"), r"\1 <this machine>"),
    # The banner carries the version, which moves with every release while the
    # captures around it do not. Pinning it here would make a version bump a
    # documentation edit, and the captures are demonstrating the watcher's
    # output rather than which release printed it.
    (re.compile(r"^lambda-watcher \d+\.\d+\.\d+\S*(?= — archiving into)"), "lambda-watcher <version>"),
]


def _comparable(line: str) -> str:
    line = line.rstrip()
    for pattern, placeholder in _VARIABLE:
        line = pattern.sub(placeholder, line)
    return line


@pytest.fixture(scope="module")
def captures() -> set[str]:
    """Every line the demo builder prints, from one real run of the pipeline."""
    # The captures are UTF-8 (box characters, arrows); Windows would otherwise
    # decode this pipe as cp1252 and mangle every frame.
    proc = subprocess.run(
        [sys.executable, str(BUILDER)], cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return {_comparable(line) for line in proc.stdout.split("\n")}


def _site_captures() -> dict[str, list[str]]:
    """Terminal slabs on the page, keyed by the caption naming their command.

    Only slabs captioned with a command are checked; the one illustrating a
    plain ``diff -rq`` is composed prose and is covered by its own test below.
    """
    blocks: dict[str, list[str]] = {}
    markup = SITE.read_text(encoding="utf-8")
    for block in re.findall(r'<div class="slab">(.*?)</pre>', markup, re.S):
        caption = re.search(r'<span class="slab-cap">(.*?)</span>', block, re.S)
        if not caption or not caption.group(1).startswith(("lambda-watcher", "one hunk")):
            continue
        body = re.sub(r"<[^>]+>", "", block.split("<pre>", 1)[1])
        blocks[caption.group(1)] = html.unescape(body).split("\n")
    return blocks


def _readme_captures() -> dict[str, list[str]]:
    """Fenced blocks in the README that open with a shell prompt."""
    blocks: dict[str, list[str]] = {}
    for fence in re.findall(r"^```[a-z]*\n(.*?)^```", README.read_text(encoding="utf-8"), re.S | re.M):
        lines = fence.rstrip("\n").split("\n")
        if lines and lines[0].startswith("$ "):
            blocks[lines[0]] = lines[1:]
    return blocks


@posix_only
def test_site_terminal_blocks_are_real_output(captures: set[str]) -> None:
    blocks = _site_captures()
    assert len(blocks) >= 4, f"expected the page's captures, found {sorted(blocks)}"
    _assert_all_produced(blocks, captures, "docs/index.html")


@posix_only
def test_readme_terminal_blocks_are_real_output(captures: set[str]) -> None:
    blocks = _readme_captures()
    assert len(blocks) >= 2, f"expected the README's captures, found {sorted(blocks)}"
    _assert_all_produced(blocks, captures, "README.md")


def _assert_all_produced(blocks: dict[str, list[str]], captures: set[str], where: str) -> None:
    invented = [
        f"[{name}] {line}"
        for name, lines in blocks.items()
        for line in (_comparable(ln) for ln in lines)
        if line and line not in captures
    ]
    assert not invented, (
        f"{len(invented)} line(s) in {where} are not output the tool produced. "
        f"Regenerate with `python {BUILDER.relative_to(REPO)}`:\n  "
        + "\n  ".join(invented[:15])
    )


@posix_only
def test_the_noise_the_docs_promise_is_the_noise_that_exists(captures: set[str]) -> None:
    """The page and README both claim 61 changed files, 56 of them vendored."""
    counts = next(ln for ln in captures if ln.startswith("...of which"))
    total = next(ln for ln in captures if ln.strip().isdigit())


    assert total == "61" and "56 lines are site-packages/" in counts, (
        f"the demo now reports {total} changed files ({counts.strip()}); "
        "update the two prose claims in docs/index.html and README.md"
    )
    for path in (SITE, README):
        text = path.read_text(encoding="utf-8")
        assert "61" in text and "56" in text, f"{path.name} no longer quotes the real counts"


def test_site_command_reference_only_lists_real_commands() -> None:
    """Every `lw <cmd>` the site advertises is a command the CLI actually has."""
    from lambda_watcher.cli import app

    # `callback` is Optional on Typer's CommandInfo; a registered command always
    # has one, and a name no `lw <cmd>` can match is the harmless way to say so.
    real = {c.name or (c.callback.__name__ if c.callback else "?")
            for c in app.registered_commands}
    markup = SITE.read_text(encoding="utf-8")
    reference = re.search(r'<div class="cmdlist">.*?\n    </div>', markup, re.S)
    assert reference, "the command reference has moved"

    advertised = set(re.findall(r'<span class="cmd__name">lw ([a-z]+)', reference.group(0)))
    assert advertised, "no commands parsed out of the reference"
    assert advertised <= real, f"documented but missing from the CLI: {sorted(advertised - real)}"


def test_every_command_reference_entry_shows_its_output() -> None:
    """A command listed without the output it produces is the thing this page avoids."""
    markup = SITE.read_text(encoding="utf-8")
    entries = re.findall(r'<details class="cmd".*?</details>', markup, re.S)
    assert len(entries) >= 15, f"expected the full command reference, found {len(entries)}"

    missing = []
    for entry in entries:
        if "<pre>" in entry:
            continue
        name = re.search(r'<span class="cmd__name">(.*?)</span>', entry)
        assert name, "a command reference entry carries no name"
        missing.append(name.group(1))
    assert not missing, f"listed with no example output: {missing}"
