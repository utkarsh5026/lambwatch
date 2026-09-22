"""What each command's ``--help`` says to the person reading it.

The docstrings in :mod:`lambda_watcher.cli` are written for whoever maintains a
command; this is written for whoever runs it. Keeping the two apart is what lets
a docstring explain why ``doctor`` exits nonzero, or what ``demo`` replaced,
without that history turning up in the middle of someone's terminal.

Each entry is three things: the one-line summary ``lw --help`` lists the command
by, a paragraph or two of plain explanation, and worked examples, which
``lw <command> --help`` draws in a panel of their own beneath the options.
Every example is a real invocation — ``tests/test_helptext.py`` parses each one
against the command it names — so renaming an option in :mod:`cli` fails the
suite rather than leaving an example behind that no longer works.

The text is Rich markup, because Typer renders it that way (``cli.app`` pins
``rich_markup_mode="rich"``): a literal square bracket has to be escaped.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typer.core import TyperCommand

#: What every function argument accepts: ``order`` finds ``order-processor``.
FUNCTION_HELP = "The function, as `lw ls` lists it. Any unique part of the name works."
#: What a version argument accepts where one has to be given.
VERSION_HELP = "Which version: 7, v7, latest or first."
#: The same, where leaving it out means the newest.
LATEST_VERSION_HELP = "Which version: 7, v7, latest or first. Leave it out for the newest."


@dataclass(frozen=True)
class CommandHelp:
    """What one command's ``--help`` says.

    ``summary`` is the line ``lw --help`` lists the command by, so it has to make
    sense on its own. ``about`` is plain prose, hard-wrapped here for reading and
    rejoined by :meth:`text`. ``examples`` are ``(what it does, the command)``
    pairs, drawn by :class:`ExamplesCommand`.
    """

    summary: str
    about: str = ""
    examples: tuple[tuple[str, str], ...] = ()

    def text(self) -> str:
        """The summary and explanation as Typer's ``help=``, one line per paragraph.

        Typer keeps the line breaks inside every paragraph after the first, so a
        paragraph wrapped at 80 columns in this file turns ragged in a 60-column
        terminal: each line stops short and the next starts on a new one.
        Joining them first leaves the wrapping to the terminal, which knows its
        own width.
        """
        paragraphs = inspect.cleandoc(self.about).split("\n\n") if self.about else []
        return "\n\n".join([self.summary, *(" ".join(p.split()) for p in paragraphs)])


class ExamplesCommand(TyperCommand):
    """A command whose ``--help`` ends in a panel of worked examples.

    Typer draws the usage, the explanation, the arguments and the options; this
    adds one more panel underneath, in the same style, from the command's entry
    in :data:`COMMANDS`. Underneath rather than above, because the bottom of the
    help is the part still on screen once it has printed — which is where
    someone looking for "how do I type this" is looking.

    It prints straight after Typer does, which relies on Typer's Rich renderer
    printing as it goes; ``cli.app`` pins ``rich_markup_mode="rich"`` so that
    Click's buffered plain formatter, which would print the examples first, is
    never the one in use.
    """

    def format_help(self, ctx: typer.Context, formatter: Any) -> None:
        """Typer's help as usual, then this command's examples, if it has any."""
        super().format_help(ctx, formatter)
        entry = COMMANDS.get(self.name or "")
        if entry is not None and entry.examples:
            console = Console()
            console.print(examples_panel(entry.examples, console.width))


#: Narrowest the description column may get before the examples stack instead.
_MIN_DESCRIPTION_WIDTH = 30


def examples_panel(examples: tuple[tuple[str, str], ...], width: int) -> Panel:
    """The examples as a panel drawn like Typer's own ``Options`` box.

    The command sits on the left in the bold cyan Typer gives option names, and
    what it does on the right, so the panel reads as part of the help rather
    than as something bolted on. A command never wraps — one broken across two
    lines is one nobody can copy — so when the terminal is too narrow to leave
    the description :data:`_MIN_DESCRIPTION_WIDTH` columns beside the longest
    command, each description goes on its own line with its command indented
    beneath it, instead of folding into a column three words wide. Cells are
    :class:`~rich.text.Text` rather than strings so that nothing in an example
    is read as markup.
    """
    widest = max(len(command) for _, command in examples)
    # Two borders, two of padding, and the three-column gap between the columns.
    if width - widest - 7 >= _MIN_DESCRIPTION_WIDTH:
        body: Table | Group = Table.grid(padding=(0, 3))
        body.add_column(style="bold cyan", no_wrap=True)
        body.add_column()
        for what, command in examples:
            body.add_row(Text(command), Text(what))
    else:
        body = Group(*(
            line
            for what, command in examples
            for line in (Text(what), Text(f"  {command}", style="bold cyan"))
        ))
    return Panel(body, title="Examples", title_align="left", border_style="dim")


def for_command(name: str) -> dict[str, Any]:
    """The ``@app.command`` keyword arguments that give ``name`` its help and examples.

    ``for_command("diff")`` → ``{"help": "Compare two versions…", "cls":
    ExamplesCommand}``, spread into the decorator as
    ``@app.command(rich_help_panel="Everyday", **for_command("diff"))`` so each
    command in :mod:`cli` names where its help lives. A name with no entry is a
    ``KeyError`` at import, which is the point: a new command cannot ship with
    no help at all.
    """
    return {"help": COMMANDS[name].text(), "cls": ExamplesCommand}


#: Every command's help, keyed by the name it is typed as, in ``lw --help`` order.
COMMANDS: dict[str, CommandHelp] = {
    # ------------------------------------------------------------ Everyday
    "status": CommandHelp(
        summary="Is the watcher running, and what has it archived? (Also plain `lw`.)",
        about="""
            Shows whether anything is watching your downloads folder, how much has
            been archived, the functions seen most recently, and the command worth
            typing next. Running `lw` on its own shows exactly the same thing.
        """,
        examples=(
            ("Check it is running and see what it has caught", "lw"),
            ("The same thing, spelled out", "lw status"),
        ),
    ),
    "setup": CommandHelp(
        summary="Set everything up: config, background watcher, and any history already on disk.",
        about="""
            The one command to run after installing. It writes a config file if
            there is none, creates the archive, offers to import any Lambda zips
            already in your downloads folder, and starts a watcher that runs in the
            background and comes back after every reboot. There is nothing to
            configure first: it finds your downloads folder on its own.

            Running it again is safe. An existing config and archive are kept, and
            only what is missing is put back.
        """,
        examples=(
            ("Set up, answering the questions as they come", "lw setup"),
            ("Say yes to everything, including importing old zips", "lw setup --yes"),
            ("Set up without a background watcher", "lw setup --no-service"),
        ),
    ),
    "demo": CommandHelp(
        summary="See the whole thing work on a sample Lambda, without touching your archive.",
        about="""
            Runs three downloads of a sample function, order-processor, through the
            same steps the watcher uses: a release, the next release, and that
            release downloaded again. You see what `lw diff` prints and the HTML
            report that comes with it, without waiting for a real deploy.

            It all happens in a separate demo archive, so nothing it does shows up
            in `lw ls`, and each run starts from scratch.
        """,
        examples=(
            ("Run the demo", "lw demo"),
            ("Run it and go straight to the report", "lw demo --open"),
            ("Remove the demo archive afterwards", "lw demo --clean"),
        ),
    ),
    "doctor": CommandHelp(
        summary="Check that everything is in place, and say how to fix what is not.",
        about="""
            Checks the config file, the archive, every watched folder, the
            background watcher and whether it has reported in lately, git, the
            index and free disk space. Every row that is not ok ends in the command
            that fixes it.

            It exits with status 1 when something is wrong, so it also works as a
            check in a script or a scheduled job.
        """,
        examples=(
            ("Find out why nothing is being archived", "lw doctor"),
        ),
    ),
    "ls": CommandHelp(
        summary="List every Lambda function that has been archived.",
        about="""
            One row per function: how many versions are archived, the newest
            version, when it was last downloaded, and its runtime. The names in the
            first column are what every other command takes, and any unique part
            of one will do.
        """,
        examples=(
            ("See what has been archived", "lw ls"),
            ("Then look at one function's versions", "lw versions order-processor"),
        ),
    ),
    "diff": CommandHelp(
        summary="Compare two versions of a function. Defaults to the last two.",
        about="""
            Your own code is compared line by line. Vendored packages
            (node_modules, site-packages and the like) are left out and summarised
            as version changes instead, such as boto3 1.34.0 → 1.35.20, so a
            dependency upgrade does not bury the one line you changed. Environment
            variables, AWS services and possible secrets that came or went are
            listed as well.

            Versions can be given as 7, v7, latest or first. --from and --to also
            count back from the newest: -1 is the newest, -2 the one before it.
        """,
        examples=(
            ("What changed in the newest version", "lw diff order-processor"),
            ("Two particular versions", "lw diff order-processor --from 3 --to 7"),
            ("Everything since the first version", "lw diff order-processor --from first"),
            ("The same comparison as a page in your browser", "lw diff order-processor --html --open"),
            ("Just the summary, without the changed lines", "lw diff order-processor --no-patch"),
        ),
    ),
    "report": CommandHelp(
        summary="Build a browsable HTML history: every version plus a diff for each step.",
        about="""
            Writes an index page listing every version of a function and what
            changed at each step, with one comparison page per step, then opens it
            in your browser when you are at a desktop. It goes in the reports
            folder inside the archive unless --output says otherwise.

            Leave out the function for one page covering every function: its
            latest change and anything that looks like a secret, linking to the
            pages already written. The watcher writes the newest comparison each
            time a version arrives; this is for the whole history at once.
        """,
        examples=(
            ("Build a function's history and open it", "lw report order-processor"),
            ("One page for every function", "lw report"),
            ("Only the ten newest versions", "lw report order-processor --limit 10"),
            ("Write it to a folder to share, without opening it",
             "lw report order-processor -o ./history --no-open"),
        ),
    ),
    # ------------------------------------------------------------ Watching
    "start": CommandHelp(
        summary="Watch in the background, now and after every reboot.",
        about="""
            Registers the watcher with your system's own service manager — launchd
            on macOS, systemd on Linux, Task Scheduler on Windows — as your own
            user, never as an administrator, and starts it. If that is refused, it
            falls back to an arrangement that still works and says what that one
            cannot do.

            `lw setup` already does this. Use start to bring the watcher back after
            `lw stop`.
        """,
        examples=(
            ("Start watching in the background", "lw start"),
            ("Then check that it is running", "lw status"),
        ),
    ),
    "stop": CommandHelp(
        summary="Stop the background watcher.",
        about="""
            It starts again at your next login unless you add --remove, which
            unregisters it so it stays off until `lw start`. Nothing archived is
            deleted either way.
        """,
        examples=(
            ("Stop watching until the next login", "lw stop"),
            ("Stop, and stay stopped", "lw stop --remove"),
        ),
    ),
    "restart": CommandHelp(
        summary="Stop and start the background watcher — use it after editing the config.",
        about="""
            The watcher reads the config once, when it starts, so a change to the
            watched folders or any other setting takes effect after a restart.
        """,
        examples=(
            ("Pick up an edited config", "lw restart"),
        ),
    ),
    "watch": CommandHelp(
        summary="Watch the downloads folder and archive every Lambda zip that lands in it.",
        about="""
            Runs in this terminal until you press Ctrl-C, printing a line for each
            zip it archives. It is what the background watcher from `lw start`
            runs, so use it to see what happens as you download, or where a
            background watcher is not an option. Zips that arrived in the last day
            are picked up as it starts.
        """,
        examples=(
            ("Watch in this terminal", "lw watch"),
            ("Watch a different folder", "lw watch --dir ~/Desktop/lambdas"),
            ("Archive what is already there, then stop", "lw watch --once"),
        ),
    ),
    "ingest": CommandHelp(
        summary="Archive one or more zip files by hand.",
        about="""
            For a zip that did not arrive through a watched folder. The function's
            name is worked out from the file the same way the watcher does it; use
            --as when the filename says nothing useful. A zip whose contents match
            the newest archived version is reported as unchanged and not stored
            again.
        """,
        examples=(
            ("Archive one zip", "lw ingest ~/Desktop/order-processor.zip"),
            ("Archive it under a name you choose", "lw ingest code.zip --as order-processor"),
            ("Attach a note as you archive it", 'lw ingest order-processor.zip --label "retry hotfix"'),
            ("Archive several, in the order given", "lw ingest release-1.zip release-2.zip"),
        ),
    ),
    "backfill": CommandHelp(
        summary="Import a folder of old backups, oldest first, so version order matches history.",
        about="""
            Zips are archived in the order they were last modified, so version 1 is
            the oldest and the numbering follows what actually happened. Nothing in
            the folder is moved or deleted. Try --dry-run first to see which
            function each zip would be filed under.
        """,
        examples=(
            ("See what would be imported, without importing", "lw backfill ~/lambda-backups --dry-run"),
            ("Import a folder", "lw backfill ~/lambda-backups"),
            ("Include the folders inside it", "lw backfill ~/lambda-backups --recursive"),
            ("Only the zips whose names match", 'lw backfill ~/lambda-backups --pattern "order-*.zip"'),
        ),
    ),
    # ------------------------------------------------- Reading the archive
    "versions": CommandHelp(
        summary="List the archived versions of one function.",
        about="""
            Newest first: each version's number, when it was archived, how many
            files and how big, its handler, the name it was downloaded as, and any
            label you gave it. The numbers are what `lw diff`, `lw show` and
            `lw export` take.
        """,
        examples=(
            ("Every version of a function", "lw versions order-processor"),
            ("Only the ten newest", "lw versions order-processor -n 10"),
        ),
    ),
    "show": CommandHelp(
        summary="Show what one archived version contains.",
        about="""
            Its runtime and handler, how many files and how big, the dependencies
            it shipped with, the environment variables its code reads, the AWS
            services it calls, and anything that looks like a secret. Leave out
            the version to see the newest.
        """,
        examples=(
            ("The newest version", "lw show order-processor"),
            ("A particular version", "lw show order-processor 3"),
            ("Every file in the package as well", "lw show order-processor --files"),
            ("Everything recorded about it, as JSON", "lw show order-processor --json"),
        ),
    ),
    "export": CommandHelp(
        summary="Get a version back out — a deployable zip or a plain folder.",
        about="""
            The zip holds exactly the files that were archived, so it can be
            uploaded to Lambda again, to roll back for instance. --tree copies them
            into a plain folder instead. Either lands in the current folder unless
            --output says otherwise.
        """,
        examples=(
            ("The newest version as a zip", "lw export order-processor"),
            ("Version 3 as a zip to roll back to", "lw export order-processor 3 --output rollback.zip"),
            ("Version 3 as a plain folder", "lw export order-processor 3 --tree"),
        ),
    ),
    "open": CommandHelp(
        summary="Open a function's archived code in your editor.",
        about="""
            With no version, this opens a git repository holding the newest code,
            where every earlier version is a commit tagged v0001, v0002 and so on,
            so your editor's own history and compare views cover the whole archive.
            Name a version to open only that version's files.

            It uses the editor from your config, or else the first of VS Code,
            Cursor, Zed and similar editors it finds.
        """,
        examples=(
            ("Open the code and its history", "lw open order-processor"),
            ("Open one version's files", "lw open order-processor 3"),
            ("Use the editor window you already have open", "lw open order-processor --reuse"),
            ("Print the folder instead of opening it", "lw open order-processor --print"),
        ),
    ),
    "git": CommandHelp(
        summary="Run git against a function's history, where every version is a commit.",
        about="""
            Every archived version is also a commit in a small git repository kept
            for each function, tagged v0001, v0002 and so on. Everything after the
            function name goes to git as it is; with nothing after it, this shows
            the last 20 versions.

            Unlike `lw diff`, git shows vendored packages file by file.
        """,
        examples=(
            ("The history, one line per version", "lw git order-processor log --oneline"),
            ("Git's own diff between two versions", "lw git order-processor diff v0003 v0007"),
            ("Which files each version changed", "lw git order-processor log --stat"),
            ("One file as it was in version 3", "lw git order-processor show v0003:lambda_function.py"),
        ),
    ),
    "search": CommandHelp(
        summary="Search across everything archived.",
        about="""
            Finds files whose path contains the term, and packages whose name
            contains it, in every version of every function. Good for questions
            like "which functions still ship requests?" or "where did this file
            first appear?".
        """,
        examples=(
            ("Files and packages matching a word", "lw search requests"),
            ("Which versions ship boto3", "lw search boto3 --kind deps"),
            ("Find a file by part of its path", "lw search lambda_function.py --kind files"),
        ),
    ),
    # --------------------------------------------------------- Housekeeping
    "rename": CommandHelp(
        summary="Fix a misidentified function name (and optionally remember the mapping).",
        about="""
            A function's name is guessed from the file you downloaded, and not
            every download is named well. This renames it everywhere: the index,
            its folder in the archive and its git repository. With --alias, later
            downloads whose filename contains that text are filed under the new
            name automatically.
        """,
        examples=(
            ("Give a function its real name", "lw rename order-proc order-processor"),
            ("Rename it, and file later downloads named order-proc under it",
             "lw rename order-proc order-processor --alias order-proc"),
        ),
    ),
    "merge": CommandHelp(
        summary="Combine two entries that are really the same Lambda, renumbering by time.",
        about="""
            When one function has been archived under two names, this moves every
            version of the first into the second and renumbers them all in the
            order they were archived. The first name is gone afterwards; its
            versions are not.
        """,
        examples=(
            ("Fold order-processor-v2 into order-processor",
             "lw merge order-processor-v2 order-processor"),
        ),
    ),
    "label": CommandHelp(
        summary="Annotate a version so you can recognise it later.",
        about="""
            The label appears beside the version in `lw versions` and in reports:
            which one is in production, say, or what a release was for. Labelling
            a version again replaces its label, and an empty one, "", removes it.
        """,
        examples=(
            ("Mark the newest version as the one in production",
             'lw label order-processor latest "prod since 2026-03-01"'),
            ("Label an older version", 'lw label order-processor 3 "before the retry change"'),
            ("Remove a label", 'lw label order-processor 3 ""'),
        ),
    ),
    "rm": CommandHelp(
        summary="Delete a function and everything archived for it.",
        about="""
            Removes every archived version, its git repository and its place in
            the index. This cannot be undone, so it asks first. `lw export` gets a
            version out beforehand if there is one worth keeping.
        """,
        examples=(
            ("Delete a function, after confirming", "lw rm order-processor-test"),
            ("Delete it without being asked", "lw rm order-processor-test --yes"),
        ),
    ),
    "path": CommandHelp(
        summary="Print where something lives on disk (handy for `cd $(...)`).",
        about="""
            With just a function, prints its folder in the archive. With a version,
            prints the folder holding that version's code, so
            cd "$(lw path order-processor 3)" takes your shell there. --repo prints
            the function's git repository instead.
        """,
        examples=(
            ("A function's folder in the archive", "lw path order-processor"),
            ("Where version 3's code is", "lw path order-processor 3"),
            ("Open the newest code in your file manager", "lw path order-processor latest --open"),
            ("The function's git repository", "lw path order-processor --repo"),
        ),
    ),
    "logs": CommandHelp(
        summary="Show the watcher's log file — what it saw, and what it made of it.",
        about="""
            The place to look when a download did not show up: every file the
            watcher noticed, and why it archived or skipped it. --service shows the
            service manager's output instead, which is where a watcher that fails
            to start says why.

            Not to be confused with `lw log`, which lists what the archive
            recorded.
        """,
        examples=(
            ("The last 40 lines", "lw logs"),
            ("Keep watching it while you download", "lw logs --follow"),
            ("Further back", "lw logs -n 200"),
            ("Why the background watcher will not start", "lw logs --service"),
        ),
    ),
    "log": CommandHelp(
        summary="Recent activity, including downloads that were skipped and why.",
        about="""
            The archive's own record of what happened: new versions, re-downloads
            that changed nothing, duplicates, failures, and when the watcher
            started and stopped. For what the watcher itself was doing, including
            files it never considered, see `lw logs`.
        """,
        examples=(
            ("The last 25 events", "lw log"),
            ("The last 100", "lw log -n 100"),
        ),
    ),
    "init": CommandHelp(
        summary="Write a commented config file you can edit.",
        about="""
            Every setting already has a working default, so a config is only
            needed to change one, such as which folders are watched. The file
            explains each setting beside it, and `lw setup` writes the same one if
            there is none. After editing it, run `lw restart` so the background
            watcher picks up the change.
        """,
        examples=(
            ("Write the config file", "lw init"),
            ("Start again from a fresh one", "lw init --force"),
        ),
    ),
    "reindex": CommandHelp(
        summary="Rebuild the index from the manifests on disk (the archive is the source of truth).",
        about="""
            The index is a fast lookup built from the archive, and every archived
            version keeps a complete record of itself beside its files. If the
            index is damaged or out of step — `lw doctor` says so — this rebuilds
            it from those records. Nothing archived is changed.
        """,
        examples=(
            ("Rebuild the index", "lw reindex"),
            ("Without the confirmation", "lw reindex --yes"),
        ),
    ),
}
