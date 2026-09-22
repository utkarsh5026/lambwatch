"""`lw reindex` rebuilds the index from disk, so every edit has to reach the disk.

`rename`, `label`, `--alias` and `merge` used to change only ``index.db``, and a
rebuild quietly undid all four. Nothing raised: the function simply came back
under its old name, or a label was gone. So each test here makes an edit,
rebuilds, and asserts on what the archive says afterwards.

The back-compat tests build the shape an older release left behind — the index
edited, the manifests not — because that is what is sitting on disk for anyone
who upgrades, and it has to heal on the first rebuild rather than lose the edits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lambda_watcher.cli import app
from lambda_watcher.db import Database
from lambda_watcher.demo import V2_FILES, stage_downloads, write_zip

runner = CliRunner()


@pytest.fixture
def lw(tmp_path: Path, monkeypatch):
    """Run `lw` against a scratch archive with the git mirror off, asserting it succeeded."""
    home = tmp_path / "store"
    monkeypatch.setenv("LAMBDA_WATCHER_HOME", str(home))
    monkeypatch.setenv("COLUMNS", "200")
    config = tmp_path / "config.yaml"
    config.write_text(
        f'watch:\n  dirs: ["{(tmp_path / "dl").as_posix()}"]\ngit_mirror:\n  enabled: false\n',
        encoding="utf-8",
    )

    def run(*args: str):
        result = runner.invoke(app, ["--config", str(config), *args])
        assert result.exit_code == 0, f"`lw {' '.join(args)}` failed:\n{result.output}\n{result.exception}"
        return result

    run.home = home
    return run


@pytest.fixture
def staged(lw, tmp_path: Path) -> list[Path]:
    """Two versions of the sample function, archived; returns the three sample zips."""
    zips = stage_downloads(tmp_path / "dl")
    lw("ingest", str(zips[0]), str(zips[1]))
    return zips


def _names(home: Path) -> list[str]:
    """Every function name the index holds, sorted."""
    db = Database(home / "index.db")
    try:
        return sorted(row["name"] for row in db.list_functions())
    finally:
        db.close()


def _versions(home: Path, name: str) -> list[tuple[int, str, str | None]]:
    """``(seq, tree_hash, label)`` for each version of one function, oldest first."""
    db = Database(home / "index.db")
    try:
        row = db.get_function_by_name(name)
        assert row is not None, f"{name} is not in the index"
        return sorted((int(v["seq"]), v["tree_hash"], v["label"]) for v in db.list_versions(int(row["id"])))
    finally:
        db.close()


def _manifests(home: Path, slug: str) -> list[dict]:
    """Every manifest under one function directory, in directory order."""
    versions = home / "functions" / slug / "versions"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(versions.glob("*/manifest.json"))]


def _drop_index(home: Path) -> None:
    """Delete the index outright, the way copying an archive without it does."""
    for suffix in ("", "-wal", "-shm"):
        Path(str(home / "index.db") + suffix).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Edits made now
#
# Each rebuild below starts with the index deleted. The recovery path for older
# archives would otherwise restore a rename or label from the index being
# replaced, and these would pass without the edit ever reaching a manifest —
# which is the exact bug. With no index, the disk is all a rebuild can read.
# --------------------------------------------------------------------------- #
def _rebuild_from_disk_alone(lw) -> None:
    """Throw the index away, then rebuild it from what is on disk."""
    _drop_index(lw.home)
    lw("reindex", "--yes")


@pytest.mark.usefixtures("staged")
def test_a_rename_survives_a_rebuild(lw):
    lw("rename", "order-processor", "orders-api")
    _rebuild_from_disk_alone(lw)
    assert _names(lw.home) == ["orders-api"]
    assert "Dependencies" in lw("diff", "orders-api").output     # and its versions still resolve


@pytest.mark.usefixtures("staged")
def test_a_rename_is_on_disk_before_any_rebuild(lw):
    lw("rename", "order-processor", "orders-api")
    assert {m["function"]["name"] for m in _manifests(lw.home, "orders-api")} == {"orders-api"}


@pytest.mark.usefixtures("staged")
def test_a_label_survives_a_rebuild(lw):
    lw("label", "order-processor", "2", "prod deploy 2026-03-01")
    _rebuild_from_disk_alone(lw)
    assert _versions(lw.home, "order-processor")[1][2] == "prod deploy 2026-03-01"


@pytest.mark.usefixtures("staged")
def test_clearing_a_label_survives_a_rebuild(lw):
    # Set, then cleared: both have to reach the disk, or the rebuild resurrects
    # a label the user deliberately removed.
    lw("label", "order-processor", "2", "short-lived")
    _rebuild_from_disk_alone(lw)
    assert _versions(lw.home, "order-processor")[1][2] == "short-lived"
    lw("label", "order-processor", "2", "")
    _rebuild_from_disk_alone(lw)
    assert _versions(lw.home, "order-processor")[1][2] is None


@pytest.mark.usefixtures("staged")
def test_an_alias_survives_a_rebuild(lw, tmp_path: Path):
    lw("rename", "order-processor", "orders-api", "--alias", "mystery-pkg")
    _rebuild_from_disk_alone(lw)
    # A later download named nothing like the function still lands on it. New
    # archive bytes (a later build stamp), so it is identified rather than
    # recognised as a download already seen.
    later = write_zip(tmp_path / "dl" / "mystery-pkg-build.zip", V2_FILES, built=(2024, 4, 1, 10, 0, 0))
    assert "orders-api" in lw("ingest", str(later)).output


def test_a_merge_survives_a_rebuild(lw, staged):
    # A second entry for the same Lambda, the way a misidentified download makes one.
    lw("ingest", str(staged[1]), "--as", "order-processor-old", "--force")
    lw("merge", "order-processor-old", "order-processor")
    merged = _versions(lw.home, "order-processor")
    assert [seq for seq, _, _ in merged] == [1, 2, 3]
    lw("reindex", "--yes")
    assert _versions(lw.home, "order-processor") == merged
    _rebuild_from_disk_alone(lw)
    assert _names(lw.home) == ["order-processor"]
    assert _versions(lw.home, "order-processor") == merged


def test_a_merge_names_every_directory_after_its_new_number(lw, staged):
    lw("ingest", str(staged[1]), "--as", "order-processor-old", "--force")
    lw("merge", "order-processor-old", "order-processor")
    versions = lw.home / "functions" / "order-processor" / "versions"
    assert [p.name[:4] for p in sorted(versions.iterdir())] == ["0001", "0002", "0003"]
    assert not (lw.home / "functions" / "order-processor-old").exists()


def test_merging_in_a_copy_of_a_version_already_there_keeps_both(lw, staged):
    # The source's v1 has the same number and content as the target's v1, so
    # both directories are called 0001-7fc98e0e. Merge used to skip moving that
    # one, then delete the source directory with it still inside.
    lw("ingest", str(staged[0]), "--as", "order-processor-old", "--force")
    lw("merge", "order-processor-old", "order-processor")
    versions = lw.home / "functions" / "order-processor" / "versions"
    assert len([p for p in versions.iterdir() if (p / "manifest.json").exists()]) == 3
    _rebuild_from_disk_alone(lw)
    assert len(_versions(lw.home, "order-processor")) == 3


@pytest.mark.usefixtures("staged")
def test_a_rebuild_with_nothing_to_heal_rewrites_nothing(lw):
    manifests = sorted((lw.home / "functions").glob("*/versions/*/manifest.json"))
    before = [p.stat().st_mtime_ns for p in manifests]
    lw("reindex", "--yes")
    assert [p.stat().st_mtime_ns for p in manifests] == before


# --------------------------------------------------------------------------- #
# back-compat: archives an older release edited in the index alone
# --------------------------------------------------------------------------- #
def _rename_as_an_older_release_did(home: Path, old: str, new: str) -> None:
    """Move the directory and edit the index, leaving every manifest saying ``old``."""
    (home / "functions" / old).rename(home / "functions" / new)
    db = Database(home / "index.db")
    try:
        row = db.get_function_by_name(old)
        assert row is not None
        with db.transaction():
            for version in db.list_versions(int(row["id"])):
                db.conn.execute(
                    "UPDATE versions SET dir = ? WHERE id = ?",
                    (version["dir"].replace(f"functions/{old}/", f"functions/{new}/"), version["id"]),
                )
            db.rename_function(int(row["id"]), new, new)
    finally:
        db.close()


@pytest.mark.usefixtures("staged")
def test_an_older_releases_rename_is_kept_and_written_to_disk(lw):
    _rename_as_an_older_release_did(lw.home, "order-processor", "orders-api")
    assert {m["function"]["name"] for m in _manifests(lw.home, "orders-api")} == {"order-processor"}

    lw("reindex", "--yes")
    assert _names(lw.home) == ["orders-api"]
    # Healed on read: the manifests say it too now, so the next rebuild needs no memory.
    assert {m["function"]["name"] for m in _manifests(lw.home, "orders-api")} == {"orders-api"}


@pytest.mark.usefixtures("staged")
def test_an_older_rename_with_its_index_gone_goes_by_the_directory(lw):
    # With nothing left to remember it by, the directory is still the rename:
    # it is the one thing the old `rename` did move.
    _rename_as_an_older_release_did(lw.home, "order-processor", "orders-api")
    _drop_index(lw.home)
    lw("reindex", "--yes")
    assert _names(lw.home) == ["orders-api"]
    assert "Dependencies" in lw("diff", "orders-api").output


@pytest.mark.usefixtures("staged")
def test_labels_and_aliases_only_the_old_index_knew_are_written_to_disk(lw):
    db = Database(lw.home / "index.db")
    try:
        row = db.get_function_by_name("order-processor")
        assert row is not None
        newest = db.latest_version(int(row["id"]))
        assert newest is not None
        with db.transaction():
            db.set_version_label(int(newest["id"]), "prod")
            db.add_alias(int(row["id"]), "mystery-pkg")
    finally:
        db.close()
    aliases_file = lw.home / "functions" / "order-processor" / "aliases.json"
    assert not aliases_file.exists()

    lw("reindex", "--yes")
    assert _versions(lw.home, "order-processor")[1][2] == "prod"
    assert _manifests(lw.home, "order-processor")[1]["version"]["label"] == "prod"
    assert json.loads(aliases_file.read_text(encoding="utf-8")) == [
        {"pattern": "mystery-pkg", "is_regex": False}
    ]


@pytest.mark.usefixtures("staged")
def test_versions_an_older_merge_left_on_one_number_are_renumbered_by_archive_time(lw):
    # An old merge moved directories by their old names and never told the
    # manifests, so two of them can claim the same number. The index's UNIQUE
    # constraint would drop one; archive time decides instead.
    newest = sorted((lw.home / "functions" / "order-processor" / "versions").glob("*/manifest.json"))[1]
    manifest = json.loads(newest.read_text(encoding="utf-8"))
    manifest["version"]["seq"] = 1
    newest.write_text(json.dumps(manifest), encoding="utf-8")
    _drop_index(lw.home)

    lw("reindex", "--yes")
    assert [seq for seq, _, _ in _versions(lw.home, "order-processor")] == [1, 2]
