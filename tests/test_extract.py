import zipfile
from pathlib import Path

import pytest

from lambda_watcher.extract import ExtractError, extract_zip, peek_top_level, safe_member_path


def test_rejects_path_traversal(tmp_path: Path):
    assert safe_member_path(tmp_path, "../evil") is None
    assert safe_member_path(tmp_path, "/etc/passwd") is None
    assert safe_member_path(tmp_path, "a/../../b") is None
    assert safe_member_path(tmp_path, "a/b.py") == tmp_path / "a" / "b.py"


def test_extract_skips_unsafe_members(tmp_path: Path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("good.py", "x = 1\n")
        zf.writestr("../escape.py", "bad")
        zf.writestr("__MACOSX/._good.py", "junk")
    result = extract_zip(archive, tmp_path / "out")
    assert result.file_count == 1
    assert (tmp_path / "out" / "good.py").exists()
    assert not (tmp_path.parent / "escape.py").exists()
    assert any("escape" in name for name in result.skipped)


def test_rejects_too_many_files(tmp_path: Path):
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i in range(10):
            zf.writestr(f"f{i}.txt", "x")
    with pytest.raises(ExtractError, match="entries"):
        extract_zip(archive, tmp_path / "out", max_files=5)


def test_rejects_oversized_archive(tmp_path: Path):
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.txt", "a" * 100_000)
    with pytest.raises(ExtractError, match="expands to"):
        extract_zip(archive, tmp_path / "out", max_uncompressed_bytes=1000)


def test_rejects_corrupt_archive(tmp_path: Path):
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not a zip at all")
    with pytest.raises(ExtractError):
        extract_zip(archive, tmp_path / "out")


def test_peek_top_level(tmp_path: Path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("my-function/handler.py", "x")
        zf.writestr("my-function/lib/util.py", "y")
    assert peek_top_level(archive) == ["my-function"]
