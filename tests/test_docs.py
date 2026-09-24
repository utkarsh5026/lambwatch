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
    # Seconds are optional: `lw logs` prints the log file's own stamps, to the second.
    (re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?"), "<archived>"),
    (re.compile(r"^[0-9a-f]{7,40}(?= order-processor v\d)"), "<commit>"),
    # How long ago something happened depends on how quickly the capture ran
    # after it. The builder takes `status` straight after the ingest it describes,
    # but a slow machine can still tip "just now" over into "1 minute ago".
    (re.compile(r"\bjust now\b|\b\d+ minutes? ago\b"), "<recently>"),
    # `doctor` reports the machine's free space, which is not a property of the
    # tool at all: the page quotes whatever the run that produced it saw.
    (re.compile(r"^(disk free)\s+\S+\s+.*$"), r"\1 <this machine>"),
    # The banner carries the version, which moves with every release while the
    # captures around it do not. Pinning it here would make a version bump a
    # documentation edit, and the captures are demonstrating the watcher's
    # output rather than which release printed it. This matched only the
    # watcher's own `— archiving into` banner until `setup`, `status` and `demo`
    # grew banners of their own, and the 0.3.0 → 0.4.0 bump duly failed three
    # lines on the site; `test_a_version_bump_is_not_a_documentation_edit`
    # is what keeps a sixth banner from doing it again.
    (re.compile(r"^lambda-watcher \d+\.\d+\.\d+\S*"), "lambda-watcher <version>"),
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


def _banner_suffixes() -> set[str]:
    """What each ``lambda-watcher <version>`` banner in ``cli.py`` prints after the version.

    ``setup`` follows it with ``— setting up``, ``demo`` with ``— a demo, on a
    sample Lambda``, the watcher with ``— archiving into <root>``; ``status`` and
    ``--version`` print the version and stop. Reading the suffixes out of the
    source rather than listing them here is the point: a sixth banner added later
    lands in this set without anyone remembering to add it.
    """
    source = (REPO / "src" / "lambda_watcher" / "cli.py").read_text(encoding="utf-8")
    found = re.findall(r"lambda-watcher(?:\[/bold\])? \{__version__\}([^\"\n]*)", source)
    # The suffixes come from source text, so a trailing newline is the two
    # characters `\` and `n`, and `{cfg.root}` is still an unfilled f-string slot.
    return {re.sub(r"\{[^}]+\}", "~/.lambda-watcher", s).removesuffix(r"\n") for s in found}


def test_a_version_bump_is_not_a_documentation_edit() -> None:
    """Every banner's version normalises away in :func:`_comparable`, not just the watcher's.

    ``_VARIABLE`` matched the ``— archiving into`` banner alone, so the ``setup``,
    ``status`` and ``demo`` captures kept a literal ``0.3.0`` on the site and the
    0.4.0 bump failed :func:`test_site_terminal_blocks_are_real_output` on every
    POSIX leg — which is the whole suite the release workflow re-runs before it
    publishes, so a version bump could not ship itself. Which release printed a
    capture is not what the capture demonstrates; a banner that survives here is
    one that breaks the next release instead.
    """
    suffixes = _banner_suffixes()
    assert suffixes, "no version banners found in cli.py — has the banner moved?"

    for suffix in suffixes:
        banner = f"lambda-watcher 9.9.9{suffix}"
        assert "9.9.9" not in _comparable(banner), (
            f"{banner!r} keeps its version through _comparable(), so the next "
            "release turns this capture into a documentation edit"
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
    real = _registered_commands()
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


def _registered_commands() -> set[str]:
    """Every command name the CLI actually registers, command groups (``lw ai``) included."""
    from lambda_watcher.cli import app

    # `callback` is Optional on Typer's CommandInfo; a registered command always
    # has one, and a name no `lw <cmd>` can match is the harmless way to say so.
    names = {c.name or (c.callback.__name__ if c.callback else "?") for c in app.registered_commands}
    return names | {g.name for g in app.registered_groups if isinstance(g.name, str)}


def test_every_command_is_named_in_the_site_reference() -> None:
    """The reference covers every command, not just some of them.

    The check above only runs one way — nothing documented is missing from the
    CLI — which is how the whole service lifecycle (``setup``, ``status``,
    ``start``, ``stop``, ``restart``) went missing from the site while every test
    passed. A command counts as documented when an entry's name or description
    says ``lw <command>``; appearing incidentally inside some other command's
    captured output does not count.
    """
    markup = SITE.read_text(encoding="utf-8")
    reference = re.search(r'<div class="cmdlist">.*?\n    </div>', markup, re.S)
    assert reference, "the command reference has moved"
    spans = re.findall(r'<span class="cmd__(?:name|what)">(.*?)</span>', reference.group(0), re.S)
    described = " ".join(spans)
    named = set(re.findall(r"\blw ([a-z]+)", html.unescape(re.sub(r"<[^>]+>", "", described))))
    missing = sorted(_registered_commands() - named)
    assert not missing, f"commands the site reference never names: {missing}"


def test_every_command_has_a_row_in_the_readme() -> None:
    """The README's command table lists every command the CLI has."""
    table = [ln for ln in README.read_text(encoding="utf-8").splitlines() if ln.startswith("| `")]
    listed = {name for row in table for name in re.findall(r"`([a-z]+)[ `]", row.split("|")[1])}
    missing = sorted(_registered_commands() - listed)
    assert not missing, f"commands missing from the README's command table: {missing}"
