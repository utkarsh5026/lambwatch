import os
import time
from pathlib import Path

from lambda_watcher.ingest import Ingestor
from tests.conftest import PY_V1, PY_V2


def test_first_download_creates_version_one(ingestor: Ingestor, make_zip, db):
    result = ingestor.ingest(make_zip("order-processor.zip", {"lambda_function.py": PY_V1}))
    assert result.status == "new"
    assert result.function_name == "order-processor"
    assert result.seq == 1
    assert (result.version_dir / "code" / "lambda_function.py").exists()
    assert (result.version_dir / "manifest.json").exists()
    assert (result.version_dir / "package.zip").exists()


def test_identical_file_is_recognised_as_a_duplicate_download(ingestor: Ingestor, make_zip):
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    again = ingestor.ingest(make_zip("fn-copy.zip", {"lambda_function.py": PY_V1}))
    # Same bytes, so it never even gets as far as extracting.
    assert again.status == "duplicate-download"


def test_repacked_identical_code_is_unchanged_not_a_new_version(
    ingestor: Ingestor, make_zip, downloads: Path, monkeypatch
):
    """A zip re-created from the same source has different metadata but the same code."""
    first = ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    assert first.status == "new"

    # Rebuild the archive with a different comment so the file hash differs.
    import zipfile

    path = downloads / "fn-2026-01-02.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.comment = b"re-zipped"
        zf.writestr("lambda_function.py", PY_V1)

    second = ingestor.ingest(path)
    assert second.status == "unchanged"
    assert second.seq == 1


def test_changed_code_creates_a_second_version(ingestor: Ingestor, make_zip, db):
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    second = ingestor.ingest(make_zip("fn-2026-01-02.zip", {"lambda_function.py": PY_V2}))
    assert second.status == "new"
    assert second.seq == 2
    assert second.changed_from == 1

    function = db.get_function("fn")
    assert len(db.list_versions(int(function["id"]))) == 2


def test_corrupt_archive_is_quarantined(ingestor: Ingestor, downloads: Path, cfg):
    broken = downloads / "broken.zip"
    broken.write_bytes(b"definitely not a zip")
    result = ingestor.ingest(broken)
    assert result.status == "failed"
    assert (cfg.quarantine_dir / "broken.zip").exists()
    assert (cfg.quarantine_dir / "broken.zip.reason.txt").exists()


def test_function_override_forces_the_name(ingestor: Ingestor, make_zip):
    result = ingestor.ingest(
        make_zip("mystery-9f8e7d6c.zip", {"lambda_function.py": PY_V1}), function_override="billing"
    )
    assert result.function_name == "billing"


def test_force_reingests_identical_content(ingestor: Ingestor, make_zip):
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    forced = ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}), force=True)
    assert forced.status == "new"
    assert forced.seq == 2


def test_move_mode_clears_the_download(cfg, db, make_zip):
    cfg.store.on_ingest = "move"
    ingestor = Ingestor(cfg, db)
    source = make_zip("fn.zip", {"lambda_function.py": PY_V1})
    result = ingestor.ingest(source)
    assert result.status == "new"
    assert not source.exists()
    assert (result.version_dir / "package.zip").exists()


def test_move_mode_clears_a_duplicate_download(cfg, db, make_zip):
    cfg.store.on_ingest = "move"
    ingestor = Ingestor(cfg, db)
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))

    again = make_zip("fn-copy.zip", {"lambda_function.py": PY_V1})
    assert ingestor.ingest(again).status == "duplicate-download"
    assert not again.exists(), "the same download twice should not pile up in Downloads"


def test_move_mode_keeps_a_zip_that_only_turned_up_in_a_scan(cfg, db, make_zip):
    """A file we found rather than received is not ours to delete.

    A startup scan and a backfill both sweep files that were already sitting
    there, and neither can tell an overnight download from a zip that something
    merely touched. Archiving one is fine; removing it is not.
    """
    cfg.store.on_ingest = "move"
    ingestor = Ingestor(cfg, db)
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))

    found = make_zip("fn-copy.zip", {"lambda_function.py": PY_V1})
    result = ingestor.ingest(found, just_downloaded=False)
    assert result.status == "duplicate-download"
    assert found.exists()


def test_move_mode_keeps_a_zip_nothing_has_written_to(cfg, db, make_zip):
    """The Windows bug this guards: an event arrives, but the file is months old."""
    cfg.store.on_ingest = "move"
    ingestor = Ingestor(cfg, db)
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))

    stale = make_zip("fn-copy.zip", {"lambda_function.py": PY_V1})
    long_ago = time.time() - 30 * 86400
    os.utime(stale, (long_ago, long_ago))

    result = ingestor.ingest(stale)  # an antivirus scan, say, not a download
    assert result.status == "duplicate-download"
    assert stale.exists()


def test_retention_prunes_old_versions(cfg, db, make_zip):
    cfg.store.max_versions_per_function = 2
    ingestor = Ingestor(cfg, db)
    for i in range(4):
        # Same filename each time, as a repeated console download would be.
        ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": f"# v{i}\n" + PY_V1}))
    function = db.get_function("fn")
    versions = db.list_versions(int(function["id"]))
    assert [int(v["seq"]) for v in versions] == [4, 3]


def test_the_same_tree_under_two_refs_is_unchanged(ingestor: Ingestor, make_zip):
    """A GitHub zip renames its wrapper on every download; the code did not change."""
    first = ingestor.ingest(
        make_zip("myrepo-1.2.3.zip", {"myrepo-1.2.3/app.py": PY_V1}),
        function_override="myrepo",
    )
    assert first.status == "new"
    again = ingestor.ingest(
        make_zip("myrepo-main.zip", {"myrepo-main/app.py": PY_V1}),
        function_override="myrepo",
    )
    assert again.status == "unchanged"
    assert again.seq == 1


def test_the_wrapper_ref_becomes_the_version_label(ingestor: Ingestor, make_zip):
    import json

    result = ingestor.ingest(
        make_zip("myrepo-1.2.3.zip", {"myrepo-1.2.3/app.py": PY_V1}),
        function_override="myrepo",
    )
    manifest = json.loads((result.version_dir / "manifest.json").read_text())
    assert manifest["version"]["label"] == "v1.2.3"
    assert manifest["archive"]["wrapper_dir"] == "myrepo-1.2.3"


def test_an_explicit_label_beats_the_wrapper_ref(ingestor: Ingestor, make_zip):
    import json

    result = ingestor.ingest(
        make_zip("myrepo-1.2.3.zip", {"myrepo-1.2.3/app.py": PY_V1}),
        function_override="myrepo",
        label="before the migration",
    )
    manifest = json.loads((result.version_dir / "manifest.json").read_text())
    assert manifest["version"]["label"] == "before the migration"


def test_a_deployment_package_gets_no_label_from_its_layout(ingestor: Ingestor, make_zip):
    import json

    result = ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    manifest = json.loads((result.version_dir / "manifest.json").read_text())
    assert manifest["version"]["label"] is None
    assert manifest["archive"]["wrapper_dir"] is None


def test_downloads_at_different_refs_group_into_one_project(ingestor: Ingestor, make_zip):
    """The payoff: no --as, no config, two refs of one repo land as one project."""
    first = ingestor.ingest(make_zip("myrepo-1.2.3.zip", {"myrepo-1.2.3/app.py": PY_V1}))
    second = ingestor.ingest(make_zip("myrepo-1.3.0.zip", {"myrepo-1.3.0/app.py": PY_V2}))
    assert (first.function_name, first.seq) == ("myrepo", 1)
    assert (second.function_name, second.seq) == ("myrepo", 2)
