"""Extract dependencies from a deployment package.

Two kinds of dependency are collected and kept apart:

``declared``
    What a manifest says the function wants (``requirements.txt``,
    ``package.json``, ``go.mod`` ...).
``installed``
    What is actually vendored inside the zip (``*.dist-info/METADATA``,
    ``node_modules/*/package.json``). These are the versions that really ran,
    and they are usually the ones worth diffing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..utils import LOG, read_text
from .inventory import Inventory

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Dependency:
    """One dependency, either declared in a manifest or installed in the zip.

    ``is_declared`` is the important flag. A declared entry came from
    ``requirements.txt`` and may be a range (``boto3>=1.34``); an installed
    entry came from ``site-packages`` and is an exact version that really
    shipped (``boto3 1.34.0``). Frozen so it can go in a set.
    """

    manager: str      # pip | npm | go | maven | gem
    name: str
    version: str | None
    source: str       # the file it came from
    is_declared: bool  # False => vendored/installed

    def key(self) -> tuple[str, str]:
        """Identity across versions: ``(manager, lowercased name)``.

        Deliberately excludes the version, because this is what a diff groups on to
        notice that ``boto3`` went from 1.34.0 to 1.35.20 rather than reporting one
        package removed and a different one added.
        """
        return (self.manager, self.name.lower())

    def as_dict(self) -> dict:
        """This dependency as plain JSON-ready data, for the manifest."""
        return {
            "manager": self.manager,
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "is_declared": self.is_declared,
        }


_REQ_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*"
    r"(?P<op>==|>=|<=|~=|!=|>|<|===)?\s*(?P<version>[^\s;#]+)?"
)


def _parse_requirements(text: str, source: str) -> list[Dependency]:
    """Parse a ``requirements.txt`` into declared pip dependencies.

    Handles the ordinary ``boto3==1.34.0`` form plus extras (``requests[security]``),
    direct URLs and ``git+`` references (the trailing path segment becomes the
    name), and PEP 508 ``name @ url`` entries. Comments and the flag lines that
    start with ``-`` (``-r base.txt``, ``-e .``, ``--index-url``) are skipped.

    A bare ``boto3`` with no comparison operator records a None version — the
    file asked for the package but not for any particular release.
    """
    deps: list[Dependency] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue  # -r includes, -e editables, --index-url
        if line.startswith(("git+", "http://", "https://")):
            name = re.sub(r"[#?].*$", "", line).rstrip("/").split("/")[-1]
            deps.append(Dependency("pip", name or line, None, source, True))
            continue
        if " @ " in line:  # PEP 508 direct reference
            name = line.split(" @ ", 1)[0].strip()
            deps.append(Dependency("pip", name, line.split(" @ ", 1)[1].strip(), source, True))
            continue
        match = _REQ_LINE.match(line)
        if not match or not match.group("name"):
            continue
        version = match.group("version") if match.group("op") else None
        deps.append(Dependency("pip", match.group("name"), version, source, True))
    return deps


def _parse_pyproject(text: str, source: str) -> list[Dependency]:
    """Parse a ``pyproject.toml`` into declared pip dependencies.

    Reads both the standard ``[project] dependencies`` list and Poetry's
    ``[tool.poetry.dependencies]`` table, whose values may be a bare version
    string or a table with a ``version`` key. Poetry's ``python`` entry is
    dropped: it constrains the interpreter, not the package set.

    Returns nothing if TOML cannot be parsed — on Python 3.10 ``tomli`` may be
    absent, and a malformed file is not worth failing an ingest over.
    """
    if tomllib is None:
        return []
    try:
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001 - malformed manifests are not fatal
        return []
    deps: list[Dependency] = []
    for spec in data.get("project", {}).get("dependencies", []) or []:
        if not isinstance(spec, str):
            continue
        match = _REQ_LINE.match(spec)
        if match and match.group("name"):
            deps.append(
                Dependency(
                    "pip", match.group("name"),
                    match.group("version") if match.group("op") else None, source, True,
                )
            )
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        version = spec if isinstance(spec, str) else (spec or {}).get("version")
        deps.append(Dependency("pip", name, version, source, True))
    return deps


def _parse_package_json(text: str, source: str, declared: bool = True) -> list[Dependency]:
    """Parse a ``package.json``, either as a manifest or as an installed package.

    The same filename means two different things depending on where it sits.
    At the package root it is a manifest, and ``declared=True`` reads the
    ``dependencies``/``devDependencies``/``optionalDependencies`` tables, whose
    values are ranges like ``^4.17.21``. Inside ``node_modules/<pkg>/`` it
    describes one installed package, and ``declared=False`` takes the file's own
    ``name`` and ``version`` — the exact release that shipped.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    deps: list[Dependency] = []
    if not declared:
        # A vendored node_modules/<pkg>/package.json: the resolved version.
        name = data.get("name")
        version = data.get("version")
        if isinstance(name, str):
            return [Dependency("npm", name, version if isinstance(version, str) else None, source, False)]
        return []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, version in (data.get(section) or {}).items():
            deps.append(
                Dependency("npm", name, version if isinstance(version, str) else None, source, True)
            )
    return deps


def _parse_package_lock(text: str, source: str) -> list[Dependency]:
    """Parse a ``package-lock.json`` into installed npm dependencies.

    Supports both lockfile layouts: v2/v3 keep a flat ``packages`` map keyed by
    path, where the name has to be recovered from the key when the entry omits
    it, while v1 keeps a ``dependencies`` map keyed by name. Both give resolved
    versions, so entries are recorded as installed rather than declared.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    deps: list[Dependency] = []
    packages = data.get("packages")
    if isinstance(packages, dict):  # lockfile v2 / v3
        for path, meta in packages.items():
            if not path or not isinstance(meta, dict):
                continue
            name = meta.get("name") or path.split("node_modules/")[-1]
            version = meta.get("version")
            if name:
                deps.append(Dependency("npm", name, version, source, False))
    elif isinstance(data.get("dependencies"), dict):  # lockfile v1
        for name, meta in data["dependencies"].items():
            version = meta.get("version") if isinstance(meta, dict) else None
            deps.append(Dependency("npm", name, version, source, False))
    return deps


_YARN_ENTRY = re.compile(r'^"?([^@\s"][^@\s"]*)@[^\n:]*:\s*$\n(?:.*\n)*?\s+version\s+"([^"]+)"', re.MULTILINE)


def _parse_yarn_lock(text: str, source: str) -> list[Dependency]:
    """Parse a ``yarn.lock`` into installed npm dependencies.

    Yarn's format is not JSON, so this matches each ``name@range:`` header
    against the indented ``version "1.2.3"`` line that follows it and takes the
    resolved version.
    """
    deps: list[Dependency] = []
    for match in _YARN_ENTRY.finditer(text):
        deps.append(Dependency("npm", match.group(1), match.group(2), source, False))
    return deps


_GO_REQUIRE_BLOCK = re.compile(r"require\s*\(([^)]*)\)", re.DOTALL)
_GO_REQUIRE_LINE = re.compile(r"^\s*([^\s/]+\S*)\s+(v\S+)", re.MULTILINE)


def _parse_go_mod(text: str, source: str) -> list[Dependency]:
    """Parse a ``go.mod`` into declared Go dependencies.

    Covers both spellings: the grouped ``require ( ... )`` block and the
    single-line ``require example.com/mod v1.2.3`` form.
    """
    deps: list[Dependency] = []
    for block in _GO_REQUIRE_BLOCK.findall(text):
        for name, version in _GO_REQUIRE_LINE.findall(block):
            deps.append(Dependency("go", name, version, source, True))
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("require ") and "(" not in line:
            parts = line.split()
            if len(parts) >= 3:
                deps.append(Dependency("go", parts[1], parts[2], source, True))
    return deps


def _parse_pom(text: str, source: str) -> list[Dependency]:
    """Parse a Maven ``pom.xml`` into declared Java dependencies.

    Each ``<dependency>`` becomes one entry named ``groupId:artifactId``, the
    coordinate Maven itself uses. A ``<version>`` that is a property reference
    (``${aws.sdk.version}``) is recorded verbatim, since resolving it would mean
    evaluating the build.

    Regex rather than an XML parser because this only needs the common shape and
    must not fail an ingest over an unusual document.
    """
    deps: list[Dependency] = []
    for block in re.findall(r"<dependency>(.*?)</dependency>", text, re.DOTALL):
        group = re.search(r"<groupId>(.*?)</groupId>", block, re.DOTALL)
        artifact = re.search(r"<artifactId>(.*?)</artifactId>", block, re.DOTALL)
        version = re.search(r"<version>(.*?)</version>", block, re.DOTALL)
        if artifact:
            artifact_id = artifact.group(1).strip()
            name = f"{group.group(1).strip()}:{artifact_id}" if group else artifact_id
            resolved = version.group(1).strip() if version else None
            deps.append(Dependency("maven", name, resolved, source, True))
    return deps


_GEMFILE_LOCK = re.compile(r"^\s{4}([a-zA-Z0-9_-]+)\s+\(([^)]+)\)", re.MULTILINE)


def _parse_gemfile_lock(text: str, source: str) -> list[Dependency]:
    """Parse a ``Gemfile.lock`` into installed Ruby gems.

    The four-space-indented ``name (1.2.3)`` lines under ``specs:`` are the
    resolved versions, which is why these are recorded as installed.
    """
    return [
        Dependency("gem", name, version, source, False)
        for name, version in _GEMFILE_LOCK.findall(text)
    ]


def _parse_metadata(text: str, source: str) -> list[Dependency]:
    """``*.dist-info/METADATA`` or ``*.egg-info/PKG-INFO``: an installed package."""
    name = version = None
    for line in text.splitlines():
        if line.startswith("Name:") and name is None:
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Version:") and version is None:
            version = line.split(":", 1)[1].strip()
        if name and version:
            break
        if not line.strip():  # headers end at the first blank line
            break
    if name:
        return [Dependency("pip", name, version, source, False)]
    return []


# filename (lowercase) -> parser
_MANIFEST_PARSERS = {
    "requirements.txt": _parse_requirements,
    "requirements-prod.txt": _parse_requirements,
    "requirements_prod.txt": _parse_requirements,
    "pyproject.toml": _parse_pyproject,
    "package.json": _parse_package_json,
    "package-lock.json": _parse_package_lock,
    "yarn.lock": _parse_yarn_lock,
    "go.mod": _parse_go_mod,
    "pom.xml": _parse_pom,
    "gemfile.lock": _parse_gemfile_lock,
}


def detect_dependencies(root: Path, inventory: Inventory) -> list[Dependency]:
    """Collect declared and installed dependencies from an extracted package."""
    found: list[Dependency] = []

    for entry in inventory.files:
        pure = PurePosixPath(entry.path)
        name = pure.name.lower()
        parent = pure.parent.name.lower()

        try:
            # Installed Python distributions.
            if name == "metadata" and parent.endswith(".dist-info"):
                text = read_text(root / entry.path, max_bytes=64 * 1024)
                if text:
                    found.extend(_parse_metadata(text, entry.path))
                continue
            if name == "pkg-info" and parent.endswith(".egg-info"):
                text = read_text(root / entry.path, max_bytes=64 * 1024)
                if text:
                    found.extend(_parse_metadata(text, entry.path))
                continue

            # Installed npm packages: node_modules/<pkg>/package.json, or
            # node_modules/@scope/<pkg>/package.json for scoped ones.
            if name == "package.json" and "node_modules/" in entry.path:
                tail = entry.path.rsplit("node_modules/", 1)[1]
                max_depth = 2 if tail.startswith("@") else 1
                if tail.count("/") <= max_depth:  # skip foo/dist/package.json and friends
                    text = read_text(root / entry.path, max_bytes=256 * 1024)
                    if text:
                        found.extend(_parse_package_json(text, entry.path, declared=False))
                continue

            if entry.is_vendor:
                continue

            parser = _MANIFEST_PARSERS.get(name)
            if parser is None:
                continue
            if entry.size > 8 * 1024 * 1024:
                continue
            text = read_text(root / entry.path, max_bytes=8 * 1024 * 1024)
            if text:
                found.extend(parser(text, entry.path))
        except Exception as exc:  # noqa: BLE001 - one bad manifest must not fail ingest
            LOG.debug("dependency parse failed for %s: %s", entry.path, exc)

    # Deduplicate, preferring installed versions over declared ranges.
    best: dict[tuple[str, str, bool], Dependency] = {}
    for dep in found:
        key = (dep.manager, dep.name.lower(), dep.is_declared)
        current = best.get(key)
        if current is None or (current.version is None and dep.version is not None):
            best[key] = dep
    return sorted(best.values(), key=lambda d: (d.manager, d.is_declared, d.name.lower()))
