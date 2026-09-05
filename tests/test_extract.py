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


def test_a_lone_wrapper_directory_is_lifted_away(tmp_path: Path):
    archive = tmp_path / "repo.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("myrepo-1.2.3/README.md", "hello\n")
        zf.writestr("myrepo-1.2.3/src/app.py", "x = 1\n")
    result = extract_zip(archive, tmp_path / "out")
    assert result.wrapper_dir == "myrepo-1.2.3"
    assert (tmp_path / "out" / "README.md").read_text() == "hello\n"
    assert (tmp_path / "out" / "src" / "app.py").exists()
    # top_level describes the tree after unwrapping, which is what gets hashed.
    assert result.top_level == ["README.md", "src"]


def test_a_wrapper_is_kept_when_the_root_holds_anything_else(tmp_path: Path):
    archive = tmp_path / "fn.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("lambda_function.py", "x = 1\n")
        zf.writestr("helpers/util.py", "y = 2\n")
    result = extract_zip(archive, tmp_path / "out")
    assert result.wrapper_dir is None
    assert (tmp_path / "out" / "helpers" / "util.py").exists()


def test_only_one_level_of_wrapping_is_removed(tmp_path: Path):
    archive = tmp_path / "nested.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("myrepo-main/src/app.py", "x = 1\n")
    result = extract_zip(archive, tmp_path / "out")
    assert result.wrapper_dir == "myrepo-main"
    # `src/` is the project's own layout, not a second wrapper to collapse.
    assert (tmp_path / "out" / "src" / "app.py").exists()


def test_unwrapping_survives_a_child_sharing_the_wrapper_name(tmp_path: Path):
    """`pkg/pkg/__init__.py` would collide if children were moved one by one."""
    archive = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("pkg/pkg/__init__.py", "")
        zf.writestr("pkg/setup.py", "x = 1\n")
    result = extract_zip(archive, tmp_path / "out")
    assert result.wrapper_dir == "pkg"
    assert (tmp_path / "out" / "pkg" / "__init__.py").exists()
    assert (tmp_path / "out" / "setup.py").exists()


def test_a_lone_file_at_the_root_is_not_a_wrapper(tmp_path: Path):
    archive = tmp_path / "one.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("lambda_function.py", "x = 1\n")
    result = extract_zip(archive, tmp_path / "out")
    assert result.wrapper_dir is None
    assert (tmp_path / "out" / "lambda_function.py").exists()


def test_wrapper_stripping_can_be_turned_off(tmp_path: Path):
    archive = tmp_path / "repo.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("myrepo-main/app.py", "x = 1\n")
    result = extract_zip(archive, tmp_path / "out", strip_wrapper=False)
    assert result.wrapper_dir is None
    assert (tmp_path / "out" / "myrepo-main" / "app.py").exists()
