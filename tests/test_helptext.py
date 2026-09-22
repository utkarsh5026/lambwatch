"""Every command's ``--help`` explains itself, and every example in it works.

The examples are the part of the help people copy, so an example that names an
option since renamed is worse than no example at all. These tests parse each one
against the command it names, the same way the shell would hand it over.
"""

from __future__ import annotations

import shlex

import pytest
import typer
from typer.testing import CliRunner

from lambda_watcher import helptext
from lambda_watcher.cli import app

runner = CliRunner()
commands = typer.main.get_group(app).commands


def test_every_command_takes_its_help_from_helptext():
    """No command falls back to its docstring, which is written for maintainers."""
    assert set(commands) == set(helptext.COMMANDS)
    for name, command in commands.items():
        assert command.help == helptext.COMMANDS[name].text(), name
        assert helptext.COMMANDS[name].examples, f"{name} has no examples"


@pytest.mark.parametrize(
    "example",
    [example for entry in helptext.COMMANDS.values() for _, example in entry.examples],
)
def test_every_example_is_a_command_line_lw_accepts(example: str):
    """An unknown option or a missing argument fails here, not in someone's terminal."""
    words = shlex.split(example)
    assert words[0] == "lw", example
    if len(words) == 1:
        return                                   # bare `lw`, the status dashboard
    assert words[1] in commands, f"{example}: no such command"
    commands[words[1]].make_context(words[1], words[2:])


def test_help_ends_in_the_examples(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(app, ["diff", "--help"])
    assert result.exit_code == 0, result.output
    assert "Examples" in result.output
    assert "lw diff order-processor --from 3 --to 7" in result.output
    # Below the options rather than above them: the bottom is what stays on screen.
    assert result.output.index("Examples") > result.output.index("--no-patch")


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
