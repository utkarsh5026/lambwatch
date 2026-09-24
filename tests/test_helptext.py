"""Every command's ``--help`` explains itself, and every example in it works.

The examples are the part of the help people copy, so an example that names an
option since renamed is worse than no example at all. These tests parse each one
against the command it names, the same way the shell would hand it over.
"""

from __future__ import annotations

import shlex

import pytest
import typer
from rich.text import Text
from typer.testing import CliRunner

from lambda_watcher import helptext
from lambda_watcher.cli import app

runner = CliRunner()
commands = typer.main.get_group(app).commands


#: Every command inside a group, keyed the way helptext files it: ``"ai add"``.
subcommands = {
    f"{group} {name}": command
    for group, parent in commands.items()
    for name, command in getattr(parent, "commands", {}).items()
}


def test_every_command_takes_its_help_from_helptext():
    """No command falls back to its docstring, which is written for maintainers."""
    assert set(commands) == set(helptext.COMMANDS)
    for name, command in commands.items():
        assert command.help == helptext.COMMANDS[name].text(), name
        assert helptext.COMMANDS[name].examples, f"{name} has no examples"


def test_every_command_inside_a_group_takes_its_help_from_helptext():
    """``lw ai add --help`` is read as often as ``lw diff --help``, so it gets the same rule."""
    assert subcommands, "no command groups found"
    assert set(subcommands) == set(helptext.SUBCOMMANDS)
    for key, command in subcommands.items():
        assert command.help == helptext.SUBCOMMANDS[key].text(), key
        assert helptext.SUBCOMMANDS[key].examples, f"{key} has no examples"


@pytest.mark.parametrize(
    "example",
    [example for entry in (*helptext.COMMANDS.values(), *helptext.SUBCOMMANDS.values())
     for _, example in entry.examples],
)
def test_every_example_is_a_command_line_lw_accepts(example: str):
    """An unknown option or a missing argument fails here, not in someone's terminal.

    An example for a group is parsed by the subcommand it names, so ``lw ai add
    --modle x`` fails here too rather than being waved through by the group.
    """
    words = shlex.split(example)
    assert words[0] == "lw", example
    if len(words) == 1:
        return                                   # bare `lw`, the status dashboard
    assert words[1] in commands, f"{example}: no such command"
    command = commands[words[1]]
    nested = getattr(command, "commands", None)
    if nested and len(words) > 2 and not words[2].startswith("-"):
        assert words[2] in nested, f"{example}: no such command"
        nested[words[2]].make_context(words[2], words[3:])
        return
    command.make_context(words[1], words[2:])


def test_a_group_ends_its_help_in_examples_too(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    for args, example in ((["ai", "--help"], "lw ai add anthropic --model claude-sonnet-5"),
                          (["ai", "add", "--help"], "lw ai add local --model llama3.1")):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        output = Text.from_ansi(result.output).plain
        assert "Examples" in output and example in output, args


def test_help_ends_in_the_examples(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(app, ["diff", "--help"])
    assert result.exit_code == 0, result.output
    # Typer forces colour when GITHUB_ACTIONS is set, which splits `--no-patch` in
    # the options table with escape codes and leaves only the one in the examples.
    output = Text.from_ansi(result.output).plain
    assert "Examples" in output
    assert "lw diff order-processor --from 3 --to 7" in output
    # Below the options rather than above them: the bottom is what stays on screen.
    assert output.index("Examples") > output.index("--no-patch")


def test_an_explanation_is_left_for_the_terminal_to_wrap():
    """Hard wraps in the source would print as ragged lines in a narrow terminal."""
    entry = helptext.CommandHelp(
        summary="Do the thing.",
        about="""
            First paragraph,
            wrapped in the source.

            Second.
        """,
    )
    assert entry.text() == "Do the thing.\n\nFirst paragraph, wrapped in the source.\n\nSecond."
