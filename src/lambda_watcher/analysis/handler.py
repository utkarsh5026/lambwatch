"""Find the likely Lambda entry point(s) in an extracted package."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..utils import read_text
from .inventory import Inventory

# def handler(event, context)  /  async def handler(event, context)
_PY_HANDLER = re.compile(
    r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(\s*[A-Za-z_]\w*\s*(?::[^,)]+)?\s*,"
    r"\s*[A-Za-z_]\w*",
    re.MULTILINE,
)
_JS_HANDLER = re.compile(
    r"(?:^|\n)\s*(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=|"
    r"(?:^|\n)\s*export\s+(?:async\s+)?(?:const|function)\s+([A-Za-z_$][\w$]*)",
)
_JAVA_HANDLER = re.compile(r"implements\s+RequestHandler|public\s+\w+\s+handleRequest\s*\(")

# Filenames AWS itself defaults to, in preference order.
_PREFERRED = (
    "lambda_function.py", "index.mjs", "index.js", "index.py", "app.py",
    "main.py", "handler.py", "lambda_handler.py", "app.js", "handler.js",
    "index.cjs", "main.go", "bootstrap",
)


@dataclass
class HandlerCandidate:
    """One possible Lambda entry point, with a score saying how likely it is.

    ``handler`` is the string you would actually paste into the AWS console —
    ``lambda_function.lambda_handler`` — assembled from the file's module path
    and the function's name.
    """

    path: str
    symbol: str
    handler: str  # "module.function", the value you paste into the console
    score: int

    def as_dict(self) -> dict:
        """This candidate as plain JSON-ready data, for the manifest."""
        return {"path": self.path, "symbol": self.symbol, "handler": self.handler, "score": self.score}


def _module_name(path: str) -> str:
    """Turn a file path into the dotted module path AWS expects.

    ``src/app/handler.py`` -> ``src.app.handler``. The extension is dropped and
    every directory separator becomes a dot, which is the form the handler
    setting takes.
    """
    pure = PurePosixPath(path)
    stem = pure.stem
    parts = list(pure.parts[:-1]) + [stem]
    return ".".join(parts)


def detect_handlers(root: Path, inventory: Inventory, max_files: int = 400) -> list[HandlerCandidate]:
    """Return handler candidates, best guess first."""
    candidates: list[HandlerCandidate] = []
    scanned = 0

    entries = [
        f
        for f in inventory.code_files
        if f.is_text and f.lang in {"python", "javascript", "typescript", "java"}
    ]
    # Shallow files first: the real handler is rarely six directories deep.
    entries.sort(key=lambda f: (f.path.count("/"), len(f.path)))

    for entry in entries:
        if scanned >= max_files:
            break
        scanned += 1
        text = read_text(root / entry.path, max_bytes=512 * 1024)
        if not text:
            continue

        name = PurePosixPath(entry.path).name
        base_score = 0
        if name in _PREFERRED:
            base_score += 50 - _PREFERRED.index(name)
        if entry.path.count("/") == 0:
            base_score += 20

        if entry.lang == "python":
            for match in _PY_HANDLER.finditer(text):
                symbol = match.group(1)
                score = base_score + (30 if symbol in {"lambda_handler", "handler"} else 0)
                if symbol.startswith("_"):
                    score -= 15
                candidates.append(
                    HandlerCandidate(entry.path, symbol, f"{_module_name(entry.path)}.{symbol}", score)
                )
        elif entry.lang in {"javascript", "typescript"}:
            for match in _JS_HANDLER.finditer(text):
                symbol = match.group(1) or match.group(2)
                if not symbol:
                    continue
                score = base_score + (30 if symbol == "handler" else 0)
                candidates.append(
                    HandlerCandidate(entry.path, symbol, f"{_module_name(entry.path)}.{symbol}", score)
                )
        elif entry.lang == "java":
            if _JAVA_HANDLER.search(text):
                candidates.append(
                    HandlerCandidate(entry.path, "handleRequest", _module_name(entry.path), base_score + 20)
                )

    candidates.sort(key=lambda c: -c.score)
    # Deduplicate on the handler string while preserving order.
    seen: set[str] = set()
    unique: list[HandlerCandidate] = []
    for candidate in candidates:
        if candidate.handler in seen:
            continue
        seen.add(candidate.handler)
        unique.append(candidate)
    return unique[:10]
