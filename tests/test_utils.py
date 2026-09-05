from pathlib import Path

import pytest

from lambda_watcher.utils import (
    count_lines,
    human_size,
    is_probably_text,
    language_for,
    matches_any,
    ref_from_dirname,
    rename_label,
    slugify,
    tree_hash,
)


def test_slugify_makes_safe_names():
    assert slugify("order-processor") == "order-processor"
    assert slugify("My Función/Handler") == "My-Funcion-Handler"
    assert slugify("") == "unnamed"
    assert slugify("CON").startswith("_")  # reserved on Windows


def test_matches_any_handles_globstar():
    assert matches_any("node_modules/foo/a.js", ["node_modules/**"])
    assert matches_any("python/lib/site-packages/x.py", ["**/site-packages/**"])
    assert not matches_any("app/handler.py", ["node_modules/**"])


def test_tree_hash_ignores_order():
    a = tree_hash([("a.py", "1"), ("b.py", "2")])
    b = tree_hash([("b.py", "2"), ("a.py", "1")])
    assert a == b
    assert a != tree_hash([("a.py", "1"), ("b.py", "3")])


def test_count_lines_counts_trailing_partial(tmp_path: Path):
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc")
    assert count_lines(path) == 3
    path.write_text("a\nb\n")
    assert count_lines(path) == 2
    path.write_text("")
    assert count_lines(path) == 0


def test_is_probably_text(tmp_path: Path):
    text = tmp_path / "a.py"
    text.write_text("print('hi')\n")
    binary = tmp_path / "a.bin"
    binary.write_bytes(b"\x00\x01\x02")
    assert is_probably_text(text)
    assert not is_probably_text(binary)


def test_language_and_size_formatting():
    assert language_for("app/handler.py") == "python"
    assert language_for("index.mjs") == "javascript"
    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.5 KB"


# ------------------------------------------------------------ rename labels
@pytest.mark.parametrize(
    "old, new, expected",
    [
        # The version moved; the 90 characters around it did not.
        ("site-packages/boto3-1.34.0.dist-info/METADATA",
         "site-packages/boto3-1.35.20.dist-info/METADATA",
         ("site-packages/boto3-1.", "34.0", "35.20", ".dist-info/METADATA")),
        # A file moved into a package, keeping its name and extension.
        ("db.py", "helpers/db.py", ("", "db", "helpers/db", ".py")),
        ("a/b/old.py", "a/b/new.py", ("a/b/", "old", "new", ".py")),
        # Nothing in common: both paths survive whole rather than being cut up.
        ("totally.py", "different.js", ("", "totally.py", "different.js", "")),
        # A pure move, where only the directory differs.
        ("src/handler.py", "app/handler.py", ("", "src", "app", "/handler.py")),
    ],
)
def test_a_rename_is_split_on_separators(old: str, new: str, expected: tuple) -> None:
    assert rename_label(old, new) == expected


@pytest.mark.parametrize(
    "old, new",
    [
        ("site-packages/boto3-1.34.0.dist-info/METADATA",
         "site-packages/boto3-1.35.20.dist-info/METADATA"),
        ("db.py", "helpers/db.py"),
        ("same.py", "same.py"),
        ("", "new.py"),
        ("a", "ab"),
    ],
)
def test_the_pieces_of_a_rename_still_spell_both_paths(old: str, new: str) -> None:
    """The label is a rewriting of the two paths, so it has to contain them."""
    head, was, now, tail = rename_label(old, new)
    assert head + was + tail == old
    assert head + now + tail == new


def test_ref_from_dirname_reads_the_ref_a_source_archive_was_cut_from():
    assert ref_from_dirname("myrepo-1.2.3") == "v1.2.3"
    assert ref_from_dirname("myrepo-v1.2.3") == "v1.2.3"
    assert ref_from_dirname("myrepo-1.2.3-rc1") == "v1.2.3-rc1"
    assert ref_from_dirname("myrepo-main") == "main"
    assert ref_from_dirname("myrepo-master") == "master"
    assert ref_from_dirname("myrepo-a1b2c3d") == "a1b2c3d"


def test_ref_from_dirname_leaves_ordinary_names_alone():
    assert ref_from_dirname("order-processor") is None
    assert ref_from_dirname("myrepo") is None
    # A bare `-v2` is part of the name far more often than it is a tag, which
    # is the same call NamingConfig.strip_patterns makes.
    assert ref_from_dirname("payments-v2") is None
    assert ref_from_dirname("report-2024") is None
