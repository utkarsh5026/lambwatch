"""Smoke tests for the command line surface."""

from __future__ import annotations

import shutil
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
    shutil.rmtree(archived / "repos" / "order-processor", ignore_errors=True)
    launched = _editor(monkeypatch)
    result = _run("open", "order-processor", "--editor", EDITOR)

    assert Path(launched[0][-1]).parent.name.startswith("0002-")
    assert "no git mirror" in result.output


def test_open_print_names_the_folder_and_launches_nothing(archived: Path, monkeypatch):
    launched = _editor(monkeypatch)
    result = _run("open", "order-processor", "1", "--print")
    assert not launched
    assert result.output.strip().endswith("code")


def test_open_refuses_an_editor_that_is_not_installed(archived: Path):
    result = runner.invoke(app, ["open", "order-processor", "--editor", "not-a-real-editor"])
    assert result.exit_code == 1
    assert "not on PATH" in result.output


@pytest.mark.parametrize("legacy_name", ["git", "repo"])
def test_a_mirror_from_an_older_layout_moves_itself(archived: Path, legacy_name: str):
    """Archives that kept the mirror inside the function directory still work."""
    function = archived / "functions" / "order-processor"
    shutil.rmtree(archived / "repos", ignore_errors=True)
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
