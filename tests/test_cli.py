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


def test_doctor_runs(home: Path):
    assert "archive root" in _run("doctor").output


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
