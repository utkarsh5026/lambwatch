"""Smoke tests for the command line surface."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from importlib import reload
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lambda_watcher import cli
from lambda_watcher.cli import app
from lambda_watcher.gitmirror import git_available
from lambda_watcher.utils import rmtree
from tests.conftest import PY_V1, PY_V2

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    store = tmp_path / "store"
    monkeypatch.setenv("LAMBDA_WATCHER_HOME", str(store))
    monkeypatch.setenv("COLUMNS", "200")
    return store


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    path = tmp_path / "downloads"
    path.mkdir()
    return path


def _zip(directory: Path, name: str, files: dict[str, str]) -> Path:
    path = directory / name
    with zipfile.ZipFile(path, "w") as zf:
        for member, content in files.items():
            zf.writestr(member, content)
    return path


def _run(*args: str):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, f"`{' '.join(args)}` failed:\n{result.output}\n{result.exception}"
    return result


def _config_watching(config: Path, directory: Path) -> Path:
    """A config that watches one folder and nothing else.

    Without it `setup` falls back to the real ``~/Downloads``, and `--yes`
    would archive whatever the person running the suite has in there.
    """
    config.write_text(f'watch:\n  dirs: ["{directory.as_posix()}"]\n', encoding="utf-8")
    return config


@pytest.fixture
def archived(home: Path, downloads: Path):
    _run("ingest", str(_zip(downloads, "order-processor.zip", {
        "lambda_function.py": PY_V1, "requirements.txt": "boto3==1.34.0\n"})))
    _run("ingest", str(_zip(downloads, "order-processor-2026-02-01.zip", {
        "lambda_function.py": PY_V2, "requirements.txt": "boto3==1.35.20\n"})))
    return home


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "lambda-watcher" in result.output


def test_init_writes_a_config(home: Path, tmp_path: Path):
    config = tmp_path / "config.yaml"
    result = _run("--config", str(config), "init")
    assert config.exists()
    assert "watch:" in config.read_text()
    assert "wrote" in result.output


def test_bare_invocation_reports_status_rather_than_a_wall_of_commands(home: Path):
    """The first thing a new user sees should be their situation, not the manual."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "not watching" in result.output
    assert "nothing archived yet" in result.output
    assert "lw setup" in result.output, "an empty archive has to say what to do next"


def test_status_names_the_archive_and_what_to_do_with_it(archived: Path):
    output = _run("status").output
    assert "order-processor" in output
    assert "1 function" in output and "2 versions" in output
    assert 'lw diff "order-processor"' in output


def test_status_points_at_the_page_linking_every_report(archived: Path):
    output = _run("status").output
    assert "index.html · lw report" in output


def test_status_still_names_a_report_in_an_archive_from_before_the_front_page(archived: Path):
    """back-compat: an older release wrote each function's latest.html and nothing above it."""
    (archived / "reports" / "index.html").unlink()
    output = _run("status").output
    assert "latest report:" in output and "latest.html" in output


def test_a_running_watcher_suggests_the_page_for_every_function():
    """Not the newest function's history: the dashboard should not guess which one you meant."""
    from lambda_watcher.service import ServiceStatus

    running = ServiceStatus("systemd", installed=True, running=True)
    commands = [command for command, _ in cli._next_steps(running, "order-processor")]
    assert commands == ['lw diff "order-processor"', "lw report"]


def test_setup_survives_a_machine_that_will_not_take_a_service(
    home: Path, downloads: Path, tmp_path: Path, monkeypatch
):
    """Everything else setup did still stands; a refused service is not fatal."""
    from lambda_watcher import service

    def refuse(*_a, **_k):
        raise service.ServiceError("ERROR: Access is denied.")

    monkeypatch.setattr(cli, "install_service", refuse)
    config = _config_watching(tmp_path / "config.yaml", downloads)
    result = runner.invoke(app, ["--config", str(config), "setup"])
    assert result.exit_code == 0, "the archive was set up; only the service was refused"
    assert config.exists()
    assert "could not install a background watcher" in result.output
    assert "lw watch" in result.output, "it has to name the way forward"


def test_start_still_fails_loudly_when_no_service_can_be_installed(home: Path, monkeypatch):
    """`setup` shrugs it off, but `start` has one job."""
    from lambda_watcher import service

    def refuse(*_a, **_k):
        raise service.ServiceError("nothing doing")

    monkeypatch.setattr(cli, "install_service", refuse)
    assert runner.invoke(app, ["start"]).exit_code == 1


def test_paths_are_shortened_with_the_platforms_own_separator(monkeypatch, tmp_path: Path):
    """`~/` pasted onto a Windows relative path produced `~/.lambda-watcher\\config.yaml`."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    import os

    assert cli._home_relative(tmp_path / "a" / "b") == f"~{os.sep}a{os.sep}b"
    outside = Path("/somewhere/else") if os.sep == "/" else Path("D:/elsewhere")
    assert cli._home_relative(outside) == str(outside)


def test_setup_writes_a_config_and_reports_the_watch_folder(
    home: Path, downloads: Path, tmp_path: Path
):
    config = tmp_path / "config.yaml"
    result = _run("--config", str(config), "setup", "--no-service")
    assert config.exists()
    assert "setting up" in result.output
    assert "archive at" in result.output
    # Declining the background watcher has to leave a way to get one later.
    assert "lw start" in result.output


def test_setup_leaves_an_existing_config_alone(home: Path, tmp_path: Path):
    config = _config_watching(tmp_path / "config.yaml", tmp_path)
    result = _run("--config", str(config), "setup", "--no-service")
    assert "using" in result.output
    assert tmp_path.as_posix() in config.read_text(), "an edited config must survive setup"


def test_setup_offers_the_zips_already_sitting_in_the_download_folder(
    home: Path, downloads: Path, tmp_path: Path
):
    _zip(downloads, "order-processor.zip", {"lambda_function.py": PY_V1})
    config = _config_watching(tmp_path / "config.yaml", downloads)
    result = _run("--config", str(config), "setup", "--no-service")
    assert "found 1 zip" in result.output
    # Not a tty and not --yes: it names the command rather than archiving
    # somebody's whole Downloads folder uninvited.
    assert "lw backfill" in result.output
    assert "order-processor" not in _run("--config", str(config), "ls").output


def test_setup_archives_that_history_when_told_to(home: Path, downloads: Path, tmp_path: Path):
    _zip(downloads, "order-processor.zip", {"lambda_function.py": PY_V1})
    config = _config_watching(tmp_path / "config.yaml", downloads)
    _run("--config", str(config), "setup", "--no-service", "--yes")
    assert "order-processor" in _run("--config", str(config), "ls").output


# ----------------------------------------------------------- reports on arrival
def test_a_new_version_arrives_with_its_comparison_already_rendered(archived: Path):
    """Nobody is watching a terminal when a background watcher archives something."""
    reports = archived / "reports" / "order-processor"
    assert (reports / "latest.html").exists()
    assert (reports / "v0001-v0002.html").exists()
    assert "order-processor" in (reports / "latest.html").read_text(encoding="utf-8")


def test_the_first_version_of_a_function_has_nothing_to_compare_against(
    home: Path, downloads: Path
):
    _run("ingest", str(_zip(downloads, "solo.zip", {"lambda_function.py": PY_V1})))
    assert not (home / "reports" / "solo").exists()


def test_the_archive_gets_a_front_page_linking_every_function(archived: Path):
    """Reading a report should not start with knowing which folder it is in."""
    page = (archived / "reports" / "index.html").read_text(encoding="utf-8")
    assert "order-processor" in page
    assert 'href="order-processor/v0001-v0002.html"' in page
    assert "order-processor/index.html" not in page, "nothing has written a history page yet"


def test_a_functions_first_version_is_on_the_front_page_already(home: Path, downloads: Path):
    _run("ingest", str(_zip(downloads, "solo.zip", {"lambda_function.py": PY_V1})))
    page = (home / "reports" / "index.html").read_text(encoding="utf-8")
    assert "solo" in page and "first version" in page


def test_housekeeping_keeps_the_front_page_current(archived: Path):
    front_page = archived / "reports" / "index.html"
    _run("rename", "order-processor", "orders-api")
    page = front_page.read_text(encoding="utf-8")
    assert "orders-api" in page and "order-processor" not in page
    _run("rm", "orders-api", "--yes")
    assert "Nothing is archived yet" in front_page.read_text(encoding="utf-8")


def test_ingest_ls_and_versions(archived: Path):
    assert "order-processor" in _run("ls").output
    output = _run("versions", "order-processor").output
    assert "v0001" in output and "v0002" in output


def test_show_reports_analysis(archived: Path):
    output = _run("show", "order-processor").output
    assert "python" in output
    assert "lambda_function.lambda_handler" in output
    assert "QUEUE_URL" in output


def test_diff_defaults_to_the_last_two_versions(archived: Path):
    output = _run("diff", "order-processor").output
    assert "v0001 → v0002" in output
    assert "boto3" in output
    assert "QUEUE_URL" in output


def test_diff_json(archived: Path):
    import json

    output = _run("diff", "order-processor", "--json").output
    payload = json.loads(output)
    assert payload["from"] == 1 and payload["to"] == 2
    assert payload["counts"]["modified"] >= 1


def test_diff_html_report(archived: Path, tmp_path: Path):
    target = tmp_path / "report.html"
    _run("diff", "order-processor", "--output", str(target))
    assert target.exists()
    assert "<!DOCTYPE html>" in target.read_text()


def test_report_builds_a_browsable_history(archived: Path):
    _run("report", "order-processor")
    index = archived / "reports" / "order-processor" / "index.html"
    assert index.exists()
    assert (archived / "reports" / "order-processor" / "v0001-v0002.html").exists()


@pytest.fixture
def browser(monkeypatch) -> list[Path]:
    """Every page a command tried to show, recorded instead of opening a window."""
    opened: list[Path] = []

    def fake_open(page: Path) -> bool:
        """Record the page and claim a browser took it."""
        opened.append(page)
        return True

    monkeypatch.setattr(cli, "_open_in_browser", fake_open)
    return opened


def test_report_opens_itself_for_someone_at_a_desktop(archived: Path, browser, monkeypatch):
    monkeypatch.setattr(cli, "_desktop_in_front", lambda: True)
    _run("report", "order-processor")
    assert browser == [archived / "reports" / "order-processor" / "index.html"]


def test_report_stays_shut_for_scripts_and_for_no_open(archived: Path, browser, monkeypatch):
    # CliRunner's stdout is not a terminal, which is exactly what a pipe, a cron
    # job or the docs builder looks like — none of them has a screen to open on.
    _run("report", "order-processor")
    monkeypatch.setattr(cli, "_desktop_in_front", lambda: True)
    _run("report", "order-processor", "--no-open")
    assert browser == []


def test_report_open_insists_and_names_the_way_out_when_no_browser_answers(
    archived: Path, monkeypatch
):
    monkeypatch.setattr(cli, "_open_in_browser", lambda page: False)
    result = _run("report", "order-processor", "--open")
    assert "open the file above yourself" in result.output


def test_bare_report_writes_the_page_linking_every_function_and_opens_it(
    archived: Path, browser, monkeypatch
):
    front_page = archived / "reports" / "index.html"
    front_page.unlink()
    monkeypatch.setattr(cli, "_desktop_in_front", lambda: True)
    result = _run("report")
    assert "(1 function)" in result.output
    assert browser == [front_page]


def test_a_functions_history_is_linked_from_the_front_page_once_written(archived: Path):
    _run("report", "order-processor")
    page = (archived / "reports" / "index.html").read_text(encoding="utf-8")
    assert 'href="order-processor/index.html"' in page


def test_bare_report_elsewhere_still_links_back_into_the_archive(archived: Path, tmp_path: Path):
    """Relative links, worked out from where the page landed — not from ``reports/``."""
    import re
    from urllib.parse import unquote

    elsewhere = tmp_path / "shared reports"
    _run("report", "--output", str(elsewhere))
    page = (elsewhere / "index.html").read_text(encoding="utf-8")
    [href] = re.findall(r'href="([^"]*v0001-v0002\.html)"', page)
    assert (elsewhere / unquote(href)).resolve().exists()


def test_bare_report_on_an_empty_archive_says_what_to_do(home: Path, browser, monkeypatch):
    """An empty archive is where everybody starts, not an error."""
    monkeypatch.setattr(cli, "_desktop_in_front", lambda: True)
    result = _run("report")
    assert "lw setup" in result.output
    assert browser == []
    assert not (home / "reports" / "index.html").exists()


@pytest.mark.parametrize(("env", "wsl", "expected"), [
    ({"DISPLAY": ":0"}, False, True),
    # Headless: webbrowser's fallback would start lynx inside this terminal.
    ({}, False, False),
    # WSL needs no DISPLAY: the page goes to the browser on the Windows side.
    ({}, True, True),
    # Over SSH any desktop belongs to a different machine.
    ({"DISPLAY": ":0", "SSH_CONNECTION": "10.0.0.2 50000 10.0.0.1 22"}, False, False),
])
def test_desktop_in_front_on_linux(monkeypatch, env: dict[str, str], wsl: bool, expected: bool):
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER", "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "on_wsl", lambda: wsl)
    assert cli._desktop_in_front() is expected


def test_open_in_browser_hands_a_wsl_page_to_windows_by_its_windows_name(
    tmp_path: Path, monkeypatch
):
    page = tmp_path / "index.html"
    page.write_text("<!DOCTYPE html>", encoding="utf-8")
    windows_name = r"\\wsl.localhost\Ubuntu\home\me\index.html"
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs):
        """Answer ``wslpath -w`` with a Windows name; fail explorer.exe the way it really does."""
        calls.append(argv)
        if argv[0] == "wslpath":
            return subprocess.CompletedProcess(argv, 0, stdout=windows_name + "\n")
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(cli, "on_wsl", lambda: True)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    # A file:///home/... URI is the bug this avoids: a Windows browser reads it as C:\home.
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: pytest.fail(f"webbrowser got {url}"))
    assert cli._open_in_browser(page) is True
    assert calls[-1] == ["explorer.exe", windows_name]


# A real executable, so `open` resolves it the way it resolves `code`; the
# stubbed subprocess.run below means it is never actually started.
EDITOR = Path(sys.executable).as_posix()


def _editor(monkeypatch) -> list[list[str]]:
    """Record the argv `open` would launch instead of launching an editor."""
    launched: list[list[str]] = []

    def fake_run(argv, **kwargs):
        launched.append([str(a) for a in argv])
        return subprocess.CompletedProcess(list(argv), 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return launched


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_open_hands_the_git_mirror_to_the_editor(archived: Path, monkeypatch):
    launched = _editor(monkeypatch)
    result = _run("open", "order-processor", "--editor", EDITOR)

    target = Path(launched[0][-1])
    assert target == archived / "repos" / "order-processor"
    assert (target / ".git").is_dir(), "the whole point is that it is a repo, not a copy"
    # The folder name is what the editor shows as the workspace root, so it has
    # to read as the function, not as "repo".
    assert target.name == "order-processor"
    assert "tagged v0001…v0002" in result.output


def test_open_with_a_version_hands_over_that_versions_files(archived: Path, monkeypatch):
    launched = _editor(monkeypatch)
    _run("open", "order-processor", "1", "--editor", EDITOR)

    target = Path(launched[0][-1])
    assert target.name == "code" and target.parent.name.startswith("0001-")
    assert (target / "lambda_function.py").read_text() == PY_V1


def test_open_without_a_mirror_falls_back_to_the_newest_files(archived: Path, monkeypatch):
    rmtree(archived / "repos" / "order-processor")
    launched = _editor(monkeypatch)
    result = _run("open", "order-processor", "--editor", EDITOR)

    assert Path(launched[0][-1]).parent.name.startswith("0002-")
    assert "no git mirror" in result.output


def test_open_print_names_the_folder_and_launches_nothing(archived: Path, monkeypatch):
    launched = _editor(monkeypatch)
    result = _run("open", "order-processor", "1", "--print")
    assert not launched
    assert result.output.strip().endswith("code")


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable bit to plant a fake editor")
def test_open_keeps_an_editor_path_that_contains_spaces_whole(archived: Path, tmp_path: Path, monkeypatch):
    r"""The natural Windows spelling is `C:\Program Files\...\code.cmd`."""
    spaced = tmp_path / "Program Files" / "My Editor"
    spaced.mkdir(parents=True)
    fake = spaced / "editor"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)

    launched = _editor(monkeypatch)
    _run("open", "order-processor", "1", "--editor", str(fake))
    assert launched[0][0] == str(fake), "the path was split on its spaces"


def test_open_splits_an_editor_command_that_carries_arguments(archived: Path, monkeypatch):
    launched = _editor(monkeypatch)
    _run("open", "order-processor", "1", "--editor", f"{EDITOR} -c pass")
    assert launched[0][:2] == [EDITOR, "-c"]


def test_open_refuses_an_editor_that_is_not_installed(archived: Path):
    result = runner.invoke(app, ["open", "order-processor", "--editor", "not-a-real-editor"])
    assert result.exit_code == 1
    assert "not on PATH" in result.output


@pytest.mark.parametrize("legacy_name", ["git", "repo"])
def test_a_mirror_from_an_older_layout_moves_itself(archived: Path, legacy_name: str):
    """Archives that kept the mirror inside the function directory still work."""
    function = archived / "functions" / "order-processor"
    rmtree(archived / "repos")
    (function / legacy_name / ".git").mkdir(parents=True)

    result = _run("open", "order-processor", "--print")
    assert Path(result.output.strip()) == archived / "repos" / "order-processor"
    assert (archived / "repos" / "order-processor" / ".git").is_dir()
    assert not (function / legacy_name).exists()


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_rename_takes_the_mirror_with_it(archived: Path):
    _run("rename", "order-processor", "OrderProcessorProd")
    assert not (archived / "repos" / "order-processor").exists()
    assert (archived / "repos" / "OrderProcessorProd" / ".git").is_dir()


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_rm_deletes_the_mirror_too(archived: Path):
    assert (archived / "repos" / "order-processor" / ".git").is_dir()
    _run("rm", "order-processor", "--yes")
    assert not (archived / "repos" / "order-processor").exists()


# ------------------------------------------- the two engines reconcile (P8)
# `lw diff` hides vendored files and the mirror keeps them, so the same two
# versions come back with different file counts. Both defaults are deliberate,
# so the fix is not to make one match the other but to make each output say the
# other exists - which is what these tests hold in place.
@pytest.fixture
def vendored(home: Path, downloads: Path):
    """Two versions where a vendored file changed alongside a first-party one.

    The plain ``archived`` fixture has nothing under ``site-packages/``, so it
    cannot show the divergence at all: with no vendored churn the two engines
    agree and there is nothing to reconcile.
    """
    _run("ingest", str(_zip(downloads, "dual.zip", {
        "lambda_function.py": PY_V1, "site-packages/boto3/__init__.py": "VERSION = '1.34.0'\n"})))
    _run("ingest", str(_zip(downloads, "dual-2026-02-01.zip", {
        "lambda_function.py": PY_V2, "site-packages/boto3/__init__.py": "VERSION = '1.35.20'\n"})))
    return home


def test_the_diff_names_the_flag_that_shows_what_it_hid(vendored: Path):
    """A tally saying "1 vendored (hidden)" without saying how to see it is half an answer."""
    output = _run("diff", "dual").output
    assert "1 vendored (hidden)" in output
    assert "lw diff dual --vendor" in output


def test_the_hint_is_absent_when_nothing_was_hidden(archived: Path):
    """It has to mean something when it appears, so it must not always appear."""
    assert "--vendor" not in _run("diff", "order-processor").output


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_the_mirror_says_the_diff_filters_what_it_is_showing(vendored: Path, capfd):
    result = _run("git", "dual", "diff", "--stat", "v0001", "v0002")
    assert "lw diff dual" in result.stderr
    # git writes to the real file descriptor rather than through CliRunner,
    # which is the whole point of the passthrough - it hands the terminal over,
    # so the user's pager and colour behave. That also means only a
    # descriptor-level capture can see it.
    captured = capfd.readouterr()
    assert "site-packages/boto3/__init__.py" in captured.out
    # The note is on stderr and nowhere else: `lw git dual diff > patch` has to
    # still write a patch.
    assert "note:" not in captured.out


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_the_mirror_stays_quiet_when_it_is_not_answering_the_same_question(vendored: Path):
    """`lw git dual log --oneline` is not a diff, so the note would be noise."""
    assert "lw diff" not in _run("git", "dual", "log", "--oneline").stderr


def test_no_note_when_the_two_settings_already_agree(tmp_path: Path):
    """Turn either policy around and there is no discrepancy left to explain."""
    from lambda_watcher.cli import _vendor_policy_note
    from lambda_watcher.config import Config

    config = Config()
    assert _vendor_policy_note(config, "dual", ["diff"])
    config.git_mirror.include_vendor = False
    assert _vendor_policy_note(config, "dual", ["diff"]) is None


def test_a_patch_flag_counts_even_on_a_subcommand_that_usually_is_not_one(tmp_path: Path):
    from lambda_watcher.cli import _vendor_policy_note
    from lambda_watcher.config import Config

    config = Config()
    assert _vendor_policy_note(config, "dual", ["log", "--stat"])
    assert _vendor_policy_note(config, "dual", ["log", "--oneline"]) is None


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_diff_mirror_answers_with_the_files_the_diff_hid(vendored: Path):
    """The reconciliation from the other direction: git's own patch, same command."""
    output = _run("diff", "dual", "--mirror").output
    assert "site-packages/boto3/__init__.py" in output
    assert "1.35.20" in output


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_diff_mirror_resolves_the_same_version_specs_the_diff_does(vendored: Path):
    """`--mirror` earns its place over `lw git` by taking `latest` and `-2`."""
    assert "boto3" in _run("diff", "dual", "--from", "-2", "--to", "latest", "--mirror").output


def test_diff_mirror_will_not_pretend_to_be_a_report(vendored: Path):
    result = runner.invoke(app, ["diff", "dual", "--mirror", "--json"])
    assert result.exit_code == 1
    assert "--json" in result.stderr and "Drop --mirror" in result.stderr


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_diff_mirror_without_a_mirror_names_the_way_out(vendored: Path):
    rmtree(vendored / "repos" / "dual")
    result = runner.invoke(app, ["diff", "dual", "--mirror"])
    assert result.exit_code == 1
    assert "lw diff dual" in result.stderr


def test_export_round_trips_a_version(archived: Path, tmp_path: Path):
    target = tmp_path / "restored.zip"
    _run("export", "order-processor", "1", "-o", str(target))
    with zipfile.ZipFile(target) as zf:
        assert "lambda_function.py" in zf.namelist()
        assert zf.read("lambda_function.py").decode() == PY_V1


def test_rename_moves_the_archive(archived: Path):
    _run("rename", "order-processor", "OrderProcessorProd", "--alias", "order-proc")
    assert "OrderProcessorProd" in _run("ls").output
    assert (archived / "functions" / "OrderProcessorProd").exists()
    # The versions still resolve after the directory moved.
    assert "v0002" in _run("versions", "OrderProcessorProd").output


def test_label_and_search(archived: Path):
    _run("label", "order-processor", "latest", "prod deploy")
    assert "prod deploy" in _run("versions", "order-processor").output
    assert "boto3" in _run("search", "boto3").output


def test_log_lists_activity(archived: Path):
    assert "new-version" in _run("log").output


def test_reindex_rebuilds_from_disk(archived: Path):
    (archived / "index.db").unlink()
    result = _run("reindex", "--yes")
    assert "reindexed" in result.output
    assert "v0002" in _run("versions", "order-processor").output


def test_doctor_runs(home: Path, downloads: Path, tmp_path: Path):
    config = _config_watching(tmp_path / "config.yaml", downloads)
    assert "archive root" in _run("--config", str(config), "doctor").output


def test_doctor_fails_when_a_watch_folder_does_not_exist(home: Path, tmp_path: Path):
    # It used to print MISSING in red and exit 0, so no script or cron job could
    # ever notice the one failure that stops the archive growing.
    config = _config_watching(tmp_path / "config.yaml", tmp_path / "not-there")
    result = runner.invoke(app, ["--config", str(config), "doctor"])
    assert result.exit_code == 1
    assert "MISSING" in result.output
    assert "problem" in result.output


def test_doctor_names_a_config_key_nothing_reads(home: Path, downloads: Path, tmp_path: Path):
    # `dir` for `dirs` loads without complaint and watches the wrong folder, so
    # the typo has to be said out loud somewhere.
    config = tmp_path / "config.yaml"
    config.write_text(
        f'watch:\n  dirs: ["{downloads.as_posix()}"]\n  dir: ["/elsewhere"]\n', encoding="utf-8"
    )
    result = runner.invoke(app, ["--config", str(config), "doctor"])
    assert result.exit_code == 1
    assert "watch.dir" in result.output


def test_doctor_gives_every_problem_a_remedy(home: Path, tmp_path: Path):
    config = _config_watching(tmp_path / "config.yaml", tmp_path / "not-there")
    result = runner.invoke(app, ["--config", str(config), "doctor"])
    assert "what to do" in result.output


def test_a_broken_config_is_explained_rather_than_traced(home: Path, tmp_path: Path):
    """Bare `lw` reads the config, so a hand-edit mistake must not print a traceback."""
    config = tmp_path / "config.yaml"
    config.write_text("watch:\n\tdirs: [nope]\n", encoding="utf-8")   # a tab, which YAML forbids
    result = runner.invoke(app, ["--config", str(config)])
    assert result.exit_code == 1
    assert "could not read" in result.output
    assert str(config) in result.output.replace("\n", "")
    assert "delete it to fall back to defaults" in result.output


def test_unknown_function_is_a_clean_error(archived: Path):
    result = runner.invoke(app, ["diff", "does-not-exist"])
    assert result.exit_code == 1
    assert "no function matching" in result.output


def test_diff_against_the_oldest_version_explains_itself(home: Path, downloads: Path):
    _run("ingest", str(_zip(downloads, "solo-fn.zip", {"lambda_function.py": PY_V1})))
    result = runner.invoke(app, ["diff", "solo-fn"])
    assert result.exit_code == 1
    assert "oldest archived version" in result.output


def test_generated_config_survives_a_windows_style_path(monkeypatch):
    """`init` must write YAML it can read back, backslashes and all.

    A Windows home reaches the template as ``C:\\Users\\you``, and inside a
    double-quoted YAML scalar ``\\U`` starts an escape sequence — so an
    unquoted interpolation made ``init`` emit a config that the very next
    command could not parse. Patching config (not templates) is deliberate:
    reloading templates re-runs its `from .config import ...`.
    """
    import yaml

    from lambda_watcher import config, templates

    home = r"C:\Users\runneradmin\.lambda-watcher"
    downloads = r"C:\Users\runneradmin\Downloads"
    monkeypatch.setattr(config, "DEFAULT_HOME", home)
    monkeypatch.setattr(config, "default_download_dirs", lambda: [downloads])

    try:
        reload(templates)
        parsed = yaml.safe_load(templates.DEFAULT_CONFIG_YAML)
        assert parsed["store"]["root"] == home
        assert parsed["watch"]["dirs"] == [downloads]
    finally:
        monkeypatch.undo()
        reload(templates)


def test_commands_leave_no_open_database_handles(archived: Path):
    """A finished command must not still hold the index open.

    On POSIX a leaked handle is invisible, but Windows refuses to delete an
    open file — so `reindex`, which replaces index.db, failed there whenever
    another command had run first in the same process.
    """
    from lambda_watcher import cli

    for args in (["ls"], ["versions", "order-processor"], ["show", "order-processor"]):
        cli_result = _run(*args)
        assert cli_result.exit_code == 0
        assert not cli._OPEN_DBS, f"`{' '.join(args)}` left the index open"


# ------------------------------------------- an archive that came from another machine
# Everything below is one bug seen from two sides: `versions.dir` used to be stored
# with the platform's own separator, so an index written on Windows named nothing at
# all anywhere else. The diff still printed - the index knows which files changed -
# but with no line in it, and every file labelled as though its bytes were the
# problem. `lw rename` had the same crack: it patched the stored path as a string
# and the `functions/<slug>/` it looked for was never there to be replaced.
def _windowsify(archive: Path) -> list[str]:
    """Rewrite every stored version dir the way an older release on Windows would."""
    import sqlite3

    conn = sqlite3.connect(archive / "index.db")
    with conn:
        for vid, stored in conn.execute("SELECT id, dir FROM versions").fetchall():
            conn.execute("UPDATE versions SET dir = ? WHERE id = ?",
                         (stored.replace("/", "\\"), vid))
        dirs = [row[0] for row in conn.execute("SELECT dir FROM versions")]
    conn.close()
    return dirs


def test_an_index_written_on_windows_still_diffs_here(archived: Path):
    assert all("\\" in d for d in _windowsify(archived))
    output = _run("diff", "order-processor").output
    assert "QUEUE_URL" in output                       # the index half, which never broke
    assert "sqs.send_message" in output                # the line diff, which did
    assert "missing from the archive" not in output


def test_rename_repoints_an_index_written_on_windows(archived: Path):
    _windowsify(archived)
    _run("rename", "order-processor", "OrderProcessorProd")
    assert (archived / "functions" / "OrderProcessorProd").exists()
    output = _run("diff", "OrderProcessorProd").output
    assert "sqs.send_message" in output
    assert "missing from the archive" not in output


def test_a_deleted_version_directory_says_what_to_type(archived: Path):
    """The archive really is incomplete — so say so, and name the command that fixes it."""
    for version_dir in (archived / "functions" / "order-processor" / "versions").glob("0001-*"):
        rmtree(version_dir / "code")
    result = _run("diff", "order-processor")
    assert "missing from the archive" in result.output
    assert "lw reindex" in result.output


def test_status_says_when_a_watch_folder_does_not_exist(home: Path, tmp_path: Path):
    # The failure this tool has to be loudest about: everything downstream looks
    # healthy while nothing can ever arrive.
    config = _config_watching(tmp_path / "config.yaml", tmp_path / "not-there")
    result = _run("--config", str(config))
    assert "does not exist" in result.output
    assert "lw doctor" in result.output


def test_status_still_exits_cleanly_over_a_missing_folder(home: Path, tmp_path: Path):
    # Bare `lw` is a dashboard, and a dashboard reporting a problem is not a failure.
    config = _config_watching(tmp_path / "config.yaml", tmp_path / "not-there")
    assert runner.invoke(app, ["--config", str(config)]).exit_code == 0


def test_logs_shows_the_end_of_the_watcher_log(home: Path, downloads: Path, tmp_path: Path):
    config = _config_watching(tmp_path / "config.yaml", downloads)
    log = home / "logs" / "watcher.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"line {n}\n" for n in range(100)), encoding="utf-8")
    result = _run("--config", str(config), "logs", "-n", "3")
    assert "line 99" in result.output and "line 97" in result.output
    assert "line 96" not in result.output


def test_logs_on_an_empty_log_says_where_to_look_next(home: Path, downloads: Path, tmp_path: Path):
    config = _config_watching(tmp_path / "config.yaml", downloads)
    log = home / "logs" / "watcher.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")
    result = _run("--config", str(config), "logs")
    assert "empty" in result.output and "lw doctor" in result.output


def test_logs_without_a_service_log_names_the_command_that_makes_one(home: Path, tmp_path: Path):
    result = runner.invoke(app, ["logs", "--service"])
    assert result.exit_code == 1
    assert "lw start" in result.output


def test_an_empty_activity_log_points_at_the_log_file(home: Path):
    result = _run("log")
    assert "lw logs" in result.output


def test_a_watcher_started_by_hand_is_seen_from_another_terminal(
    home: Path, downloads: Path, tmp_path: Path
):
    # `lw watch` is invisible to the service manager, so this used to read as
    # "not watching" while the watcher was busy archiving in the next window.
    import os

    from lambda_watcher import heartbeat
    from lambda_watcher.utils import utc_now_iso

    config = _config_watching(tmp_path / "config.yaml", downloads)
    now = utc_now_iso()
    heartbeat.write(home / "state" / "watcher.json", heartbeat.Heartbeat(
        pid=os.getpid(), started_at=now, last_beat_at=now,
        dirs=[str(downloads)], observer="native events", observer_reason="test",
    ))
    result = _run("--config", str(config))
    assert "in a terminal" in result.output
    assert "not watching" not in result.output


def test_a_stale_heartbeat_is_not_mistaken_for_a_watcher(home: Path, downloads: Path, tmp_path: Path):
    import os

    from lambda_watcher import heartbeat

    config = _config_watching(tmp_path / "config.yaml", downloads)
    long_ago = "2026-01-01T00:00:00+00:00"
    heartbeat.write(home / "state" / "watcher.json", heartbeat.Heartbeat(
        pid=os.getpid(), started_at=long_ago, last_beat_at=long_ago,
        dirs=[str(downloads)], observer="native events",
    ))
    result = _run("--config", str(config))
    assert "in a terminal" not in result.output


def test_demo_shows_every_outcome_without_touching_the_real_archive(home: Path):
    result = _run("demo", "--no-open")
    assert "new" in result.output and "unchanged" in result.output
    assert "Dependencies" in result.output and "New findings" in result.output
    assert (home / "demo" / "reports" / "order-processor" / "latest.html").exists()
    # The whole point: a sample function must never show up as one of yours.
    assert "Nothing archived yet" in _run("ls").output


def test_demo_starts_from_nothing_each_time(home: Path):
    _run("demo", "--no-open")
    second = _run("demo", "--no-open")
    assert "order-processor v0001" in second.output
    assert "v0003" not in second.output


def test_demo_clean_removes_it_and_is_calm_when_there_is_nothing(home: Path):
    _run("demo", "--no-open")
    assert "removed" in _run("demo", "--clean").output
    assert not (home / "demo").exists()
    assert "nothing to remove" in _run("demo", "--clean").output


def test_the_dashboard_on_an_empty_archive_offers_the_demo(home: Path):
    assert "lw demo" in _run().output


def test_setup_on_an_empty_archive_ends_with_something_to_look_at(
    home: Path, downloads: Path, tmp_path: Path
):
    config = _config_watching(tmp_path / "config.yaml", downloads)
    result = _run("--config", str(config), "setup", "--no-service")
    assert "lw demo" in result.output
    assert "--install-completion" in result.output


def test_setup_repoints_an_old_config_at_where_downloads_really_land(
    home: Path, downloads: Path, tmp_path: Path, monkeypatch
):
    # A config an older release wrote on WSL names a ~/Downloads that has never
    # existed there. Its owner should be offered the fix, not told to edit YAML.
    from lambda_watcher.templates import render_config

    config = tmp_path / "config.yaml"
    config.write_text(render_config([str(tmp_path / "never-existed")]), encoding="utf-8")
    monkeypatch.setattr(cli, "_best_watch_dirs", lambda: [str(downloads)])

    result = _run("--config", str(config), "setup", "--yes", "--no-service")
    assert "now watching" in result.output
    assert "do not exist" not in result.output
    from lambda_watcher.config import load_config
    assert load_config(config).watch.dirs == [str(downloads)]


def test_setup_will_not_edit_a_config_without_someone_to_ask(
    home: Path, downloads: Path, tmp_path: Path, monkeypatch
):
    config = _config_watching(tmp_path / "config.yaml", tmp_path / "never-existed")
    before = config.read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "_best_watch_dirs", lambda: [str(downloads)])
    result = _run("--config", str(config), "setup", "--no-service")
    assert config.read_text(encoding="utf-8") == before
    assert str(downloads) in result.output          # still named, so it can be typed


def test_rewriting_the_watch_folders_keeps_every_comment(tmp_path: Path):
    from lambda_watcher.templates import render_config

    config = tmp_path / "config.yaml"
    config.write_text(render_config(["/old"]), encoding="utf-8")
    comments = [ln for ln in config.read_text(encoding="utf-8").splitlines() if ln.strip().startswith("#")]
    assert cli._rewrite_watch_dirs(config, ["/new"])
    after = config.read_text(encoding="utf-8")
    assert [ln for ln in after.splitlines() if ln.strip().startswith("#")] == comments
    assert '"/new"' in after and '"/old"' not in after


def test_rewriting_keeps_a_one_line_list_on_one_line(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text('watch:\n  dirs: ["~/Downloads"]\n  stable_seconds: 3\n', encoding="utf-8")
    assert cli._rewrite_watch_dirs(config, ["/a", "/b"])
    assert config.read_text(encoding="utf-8") == 'watch:\n  dirs: ["/a", "/b"]\n  stable_seconds: 3\n'


def test_rewriting_does_not_mangle_a_windows_path(tmp_path: Path):
    # re.sub reads backslashes in a replacement string as escapes, which would
    # turn C:\Users\Sam into something else entirely.
    from lambda_watcher.config import load_config

    config = tmp_path / "config.yaml"
    config.write_text('watch:\n  dirs: ["~/Downloads"]\n', encoding="utf-8")
    windows = "C:\\Users\\Sam\\Downloads"
    assert cli._rewrite_watch_dirs(config, [windows])
    assert load_config(config).watch.dirs == [windows]


def test_rewriting_declines_a_layout_it_does_not_recognise(tmp_path: Path):
    config = tmp_path / "config.yaml"
    odd = "watch:\n    dirs:\n        - ~/Downloads\n"          # four-space indent
    config.write_text(odd, encoding="utf-8")
    assert not cli._rewrite_watch_dirs(config, ["/new"])
    assert config.read_text(encoding="utf-8") == odd
