"""Optional per-function git repository, one commit per archived version.

This is the shortest path to a review workflow you already know: every version
is a commit tagged ``v0007``, so ``git diff v0002 v0010``, ``git log -p``, VS
Code's diff viewer and any git GUI all work on a single function's history
without ten unrelated Lambdas mixed into the same repo. It lives at
``functions/<slug>/repo/``, and ``lambda-watcher open`` hands that folder to an
editor.
"""

from __future__ import annotations

import os
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


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every git call in this tool runs under.

    Scrubbed so the mirror behaves the same on every machine and never blocks:
    ``GIT_CONFIG_NOSYSTEM`` keeps system-wide config and hooks out, and
    ``GIT_TERMINAL_PROMPT=0`` makes git fail instead of waiting forever on a
    credential prompt — which matters because the mirror is written by the
    background service, where there is no terminal to answer one.

    It is a function rather than a constant because every caller needs a fresh
    copy of ``os.environ`` and some add to it: :func:`commit_version` passes the
    author and committer dates through ``extra``.
    """
    env = os.environ.copy()
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    if extra:
        env.update(extra)
    return env


def _run(repo: Path, *args: str, check: bool = True, env_extra: dict[str, str] | None = None) -> str:
    """Run one git command in ``repo`` and return its stdout, stripped.

    The environment comes from :func:`_git_env`, so this and the interactive
    :func:`passthrough` agree about what git they are talking to.
    ``check=False`` returns output for commands whose failure is expected and
    handled.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_git_env(env_extra),
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

    This is what ``lw diff --mirror`` prints — see
    :func:`lambda_watcher.cli._show_mirror_diff`, which resolves the two version
    numbers into tags first. Check the tags with :func:`has_tag` before calling:
    a missing one is an empty diff here, which reads exactly like two identical
    versions.
    """
    args = ["diff", tag_a, tag_b]
    if extra_args:
        args = ["diff", *extra_args, tag_a, tag_b]
    return _run(repo, *args, check=False)


def has_tag(repo: Path, tag: str) -> bool:
    """True when ``tag`` names something the mirror can actually diff.

    :func:`diff` reports a missing tag as an empty diff, which reads exactly like
    two identical versions — so ``lw diff --mirror`` checks here first and says
    which tag is absent instead. The tag can be missing for ordinary reasons:
    the mirror was switched on after that version was archived, or
    ``git_mirror.enabled`` was off at the time.
    """
    if not (repo / ".git").exists():
        return False
    try:
        return bool(_run(repo, "rev-parse", "-q", "--verify", f"{tag}^{{commit}}", check=False))
    except (OSError, subprocess.SubprocessError):
        return False


def passthrough(repo: Path, args: list[str]) -> int:
    """Run one git command in the mirror with the terminal attached; returns its exit code.

    This backs ``lw git my-fn log --oneline``. Output is deliberately *not*
    captured, so the user's pager, colour and terminal width behave exactly as
    they do in any other repository — which is the whole appeal of handing them
    a real git repo.

    It still goes through :func:`_git_env` rather than calling ``git`` directly,
    so one module decides how this tool invokes git. That buys two things a bare
    ``subprocess.run(["git", ...])`` does not: no system-level config or hooks
    leaking into a mirror read, and ``GIT_TERMINAL_PROMPT=0``, so a repo that
    somehow acquired a remote fails instead of hanging on a credential prompt.
    The user's own global config still applies, which is what you want for a
    command they typed themselves.

    There is no timeout, unlike :func:`_run`: a pager holds the process open for
    as long as the reader is reading.
    """
    proc = subprocess.run(["git", *args], cwd=str(repo), env=_git_env())
    return proc.returncode
