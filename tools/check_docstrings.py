"""Fail the build when something in `src/` has no docstring.

CLAUDE.md asks that every function, method, class and property carry one, and a
rule nobody checks is a rule that decays quietly — the coverage was 38.6% when
it was only ever an intention. This is the enforcement: it walks the AST, finds
every definition without a docstring, and exits nonzero naming each one.

    .venv/bin/python tools/check_docstrings.py                 # checks src/
    .venv/bin/python tools/check_docstrings.py src tests       # or trees you choose
    .venv/bin/python tools/check_docstrings.py --format=github # CI annotations

Stdlib only, on purpose: it runs in the lint job beside `ruff check`, which
installs the linter and nothing else, so this must not need the package
importable to say something useful about it.

Nested functions and closures count. They are where the fiddly reasoning tends
to hide (`_similarity_renames.lines_of` is the cache that stops a quadratic
comparison re-reading every file), and skipping them would exempt exactly the
code most worth explaining.

`@overload` stubs are the one exception. Their body is `...` and the docstring
belongs on the implementation underneath, so requiring one there would be
asking for the wrong thing rather than for more.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: Definition nodes that must carry a docstring.
Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


class Missing:
    """One definition that should have a docstring and does not."""

    def __init__(self, path: Path, qualname: str, lineno: int) -> None:
        """Record where the gap is, for reporting."""
        self.path     = path
        self.qualname = qualname
        self.lineno   = lineno

    def __str__(self) -> str:
        """``src/lambda_watcher/db.py:42: Database.next_seq`` — clickable in most terminals."""
        return f"{self.path}:{self.lineno}: {self.qualname}"

    def as_github(self) -> str:
        """The same thing as a GitHub Actions annotation, so it lands on the diff."""
        return (
            f"::error file={self.path},line={self.lineno}::"
            f"{self.qualname} has no docstring"
        )


def _is_overload(node: Definition) -> bool:
    """True for a `@overload` stub, whose docstring belongs on the implementation.

    Matches both spellings a file might use — a bare ``@overload`` and a
    qualified ``@typing.overload``.
    """
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for decorator in node.decorator_list:
        name = decorator.attr if isinstance(decorator, ast.Attribute) else getattr(decorator, "id", "")
        if name == "overload":
            return True
    return False


def walk_definitions(node: ast.AST, prefix: str = "") -> list[tuple[str, Definition]]:
    """Every definition under ``node``, paired with its dotted qualname.

    Recurses through `if`/`try` bodies as well as through definitions, because a
    function defined under `if sys.platform == "win32":` is still a function.
    Names come out as ``Database.next_seq`` and ``rmtree._onerror``.
    """
    found: list[tuple[str, Definition]] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = f"{prefix}{child.name}"
            found.append((qualname, child))
            found.extend(walk_definitions(child, qualname + "."))
        elif isinstance(child, (ast.If, ast.Try)):
            found.extend(walk_definitions(child, prefix))
    return found


def _relative_to(path: Path, root: Path) -> Path:
    """``path`` written relative to ``root``, or unchanged if it lies outside it.

    Falls back rather than raising, because a caller is free to point this at a
    tree somewhere else entirely.
    """
    try:
        return path.resolve().relative_to(root)
    except ValueError:
        return path


def check_file(path: Path) -> tuple[list[Missing], int]:
    """Check one file, returning its gaps and how many definitions it holds.

    A file that will not parse is reported as one gap against line 1 rather
    than raising: this runs in CI, and "your syntax is broken" is more useful
    from the linter that says so plainly than from a traceback.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [Missing(path, f"could not be parsed: {exc}", 1)], 0

    missing: list[Missing] = []
    definitions = walk_definitions(tree)
    for qualname, node in definitions:
        if _is_overload(node):
            continue
        if ast.get_docstring(node) is None:
            missing.append(Missing(path, qualname, node.lineno))
    return missing, len(definitions)


def check_paths(targets: list[Path]) -> tuple[list[Missing], int]:
    """Check every ``.py`` file under the given files and directories.

    Sorted, so the report reads the same on every machine and a CI failure can
    be compared against a local run line for line.
    """
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.suffix == ".py":
            files.append(target)

    missing: list[Missing] = []
    total = 0
    for path in files:
        gaps, count = check_file(path)
        missing.extend(gaps)
        total += count
    return missing, total


def main(argv: list[str] | None = None) -> int:
    """Run the check and report. Returns the process exit status."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "paths", nargs="*", type=Path, default=None,
        help="files or directories to check (default: src)",
    )
    parser.add_argument(
        "--format", choices=("text", "github"), default="text",
        help="github emits workflow annotations that land on the pull request diff",
    )
    args = parser.parse_args(argv)

    root    = Path(__file__).resolve().parents[1]
    targets = args.paths or [root / "src"]

    missing, total = check_paths(targets)
    # Annotations only attach to the diff when the path is relative to the
    # repository root, and a relative path is what a reader wants to see anyway.
    for gap in missing:
        gap.path = _relative_to(gap.path, root)
    documented = total - len(missing)
    coverage   = (documented / total * 100) if total else 100.0

    for gap in missing:
        print(gap.as_github() if args.format == "github" else gap, file=sys.stderr)

    if missing:
        print(
            f"\n{len(missing)} of {total} definitions have no docstring "
            f"({coverage:.1f}% documented).\n"
            "CLAUDE.md asks for one on every function, method, class and property — "
            "see 'Write code a stranger can read'.",
            file=sys.stderr,
        )
        return 1

    print(f"all {total} definitions documented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
