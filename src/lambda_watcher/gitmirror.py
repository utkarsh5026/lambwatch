"""Optional per-function git repository, one commit per archived version.

This is the shortest path to a review workflow you already know: every version
is a commit tagged ``v0007``, so ``git diff v0002 v0010``, ``git log -p``, VS
Code's diff viewer and any git GUI all work on a single function's history
without ten unrelated Lambdas mixed into the same repo. It lives at
``functions/<slug>/repo/``, and ``lambda-watcher open`` hands that folder to an
editor.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import GitMirrorConfig
from .utils import LOG, matches_any, rmtree


class GitUnavailable(RuntimeError):
    """git is not installed or not usable."""


@dataclass
class MirrorResult:
    """What one mirror commit produced: the repo, the commit and the tag.

    ``commit`` is None when there was nothing to commit and no HEAD to tag —
    an empty first version. ``created_repo`` says whether this call initialised
    the repository, which is worth reporting the first time.
    """

    repo: Path
    commit: str | None
    tag: str | None
    created_repo: bool = False


def git_available() -> bool:
    """True when a usable ``git`` executable is on PATH.

    The mirror is optional, so callers check this and carry on without it rather
    than failing an ingest that has otherwise succeeded.
    """
    return shutil.which("git") is not None


def _run(repo: Path, *args: str, check: bool = True, env_extra: dict[str, str] | None = None) -> str:
    """Run one git command in ``repo`` and return its stdout, stripped.

    The environment is scrubbed so the mirror behaves the same on every machine:
    ``GIT_CONFIG_NOSYSTEM`` keeps system-wide config and hooks out, and
    ``GIT_TERMINAL_PROMPT=0`` makes git fail instead of blocking on a credential
    prompt — which matters because this runs unattended in the background
    service. ``check=False`` returns output for commands whose failure is
    expected and handled.
    """
    import os

    env = os.environ.copy()
    # Keep the mirror hermetic: no user hooks, no global config surprises.
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def ensure_repo(repo: Path, cfg: GitMirrorConfig) -> bool:
    """Create the mirror repo if needed. Returns True when it was created."""
    if not git_available():
        raise GitUnavailable("git executable not found on PATH")
    created = False
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        _run(repo, "init", "-q", "-b", "main")
        _run(repo, "config", "user.name", cfg.author_name)
        _run(repo, "config", "user.email", cfg.author_email)
        _run(repo, "config", "core.autocrlf", "false")
        # Deployment packages contain binaries; keep git from mangling them.
        (repo / ".gitattributes").write_text("* -text\n", encoding="utf-8")
        created = True
    return created


def _clear_worktree(repo: Path) -> None:
    """Delete everything in the repo except ``.git``.

    Each version is committed as the complete tree rather than as a patch, so
    the worktree is emptied and refilled. That is what makes a file deleted
    between two versions show up as a deletion in the commit.
    """
    for entry in repo.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            rmtree(entry)
        else:
            try:
                entry.unlink()
            except OSError:
                pass


def _copy_tree(src: Path, repo: Path, vendor_globs: list[str], include_vendor: bool) -> int:
    """Copy the extracted version into the repo worktree, returning the file count.

    Skips anything under ``.git`` so the copy cannot damage the repository, and
    skips vendored files when ``include_vendor`` is off — which keeps a mirror
    readable when the alternative is ten thousand ``node_modules`` files per
    commit. A file that cannot be copied is logged and stepped over rather than
    failing the mirror.
    """
    copied = 0
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(src).as_posix()
        if rel.startswith(".git/") or rel == ".git":
            continue
        if not include_vendor and matches_any(rel, vendor_globs):
            continue
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, target)
            copied += 1
        except OSError as exc:
            LOG.debug("git mirror skipped %s: %s", rel, exc)
    return copied


def commit_version(
    repo: Path,
    code_dir: Path,
    cfg: GitMirrorConfig,
    seq: int,
    message: str,
    when_iso: str | None = None,
    vendor_globs: list[str] | None = None,
) -> MirrorResult:
    """Replace the worktree with ``code_dir`` and commit it as version ``seq``."""
    created = ensure_repo(repo, cfg)
    _clear_worktree(repo)
    # .gitattributes is part of the repo, not of any version; restore it.
    (repo / ".gitattributes").write_text("* -text\n", encoding="utf-8")
    _copy_tree(code_dir, repo, vendor_globs or [], cfg.include_vendor)

    _run(repo, "add", "-A")
    status = _run(repo, "status", "--porcelain")
    tag = f"{cfg.tag_prefix}{seq:04d}"
    if not status:
        # Identical content: still tag it so `git diff v2 v10` never 404s.
        try:
            head = _run(repo, "rev-parse", "HEAD")
            _run(repo, "tag", "-f", tag, head, check=False)
            return MirrorResult(repo, head, tag, created)
        except RuntimeError:
            return MirrorResult(repo, None, None, created)

    env_extra = {}
    if when_iso:
        env_extra = {"GIT_AUTHOR_DATE": when_iso, "GIT_COMMITTER_DATE": when_iso}
    _run(repo, "-c", f"user.name={cfg.author_name}", "-c", f"user.email={cfg.author_email}",
         "commit", "-q", "-m", message, env_extra=env_extra)
    head = _run(repo, "rev-parse", "HEAD")
    _run(repo, "tag", "-f", tag, head, check=False)
    return MirrorResult(repo, head, tag, created)


def diff(repo: Path, tag_a: str, tag_b: str, extra_args: list[str] | None = None) -> str:
    """Run ``git diff`` between two tags in the mirror and return the raw output.

    ``extra_args`` goes in front of the tags, so ``["--stat"]`` and
    ``["--", "src/"]`` both work. Failure returns whatever git printed instead
    of raising: this feeds a report, and an empty diff is a fine answer.
    """
    args = ["diff", tag_a, tag_b]
    if extra_args:
        args = ["diff", *extra_args, tag_a, tag_b]
    return _run(repo, *args, check=False)
