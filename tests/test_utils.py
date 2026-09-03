from pathlib import Path

from lambda_watcher.utils import (
    count_lines,
    human_size,
    is_probably_text,
    language_for,
    matches_any,
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
