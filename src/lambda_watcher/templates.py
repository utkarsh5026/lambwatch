"""The annotated config file written by ``lambda-watcher init``."""

from __future__ import annotations

import json

from .config import DEFAULT_HOME, default_download_dirs


def _yaml_str(value: object) -> str:
    """Quote a value as a YAML scalar.

    Windows paths are the reason this exists: ``C:\\Users\\you`` inside a
    double-quoted YAML scalar makes ``\\U`` an escape sequence, and the config
    ``init`` had just written could not be read back. JSON string syntax is a
    subset of YAML's double-quoted style, so ``json.dumps`` escapes the
    backslashes correctly on every platform.
    """
    return json.dumps(str(value))


_DIRS = "\n".join(f"    - {_yaml_str(d)}" for d in default_download_dirs())

DEFAULT_CONFIG_YAML = f"""# lambda-watcher configuration
# Every setting below is optional; the values shown are the defaults.

watch:
  # Folders to watch. Add more if you download from several places.
  dirs:
{_DIRS}
  extensions: [".zip"]
  # A file must stop changing for this many seconds before it is read, so a
  # half-finished download is never archived.
  stable_seconds: 2.0
  recursive: false
  # Switch on if your Downloads folder is a network share, a VM mount or WSL,
  # where native filesystem events are unreliable.
  force_polling: false
  # Windows reports a file as "modified" when an antivirus scan, the search
  # indexer or OneDrive touches it. Anything whose contents were last written
  # longer ago than this is not treated as a new arrival: the event is ignored,
  # and `on_ingest: move` never deletes it. 0 turns the check off.
  arrival_max_age_seconds: 300
  # Pick up zips that arrived while the watcher was not running.
  scan_on_start: true
  scan_on_start_max_age_hours: 24

store:
  root: {_yaml_str(DEFAULT_HOME)}
  # copy  = leave the download in place (default)
  # move  = take it out of Downloads once archived, keeping that folder clean
  # leave = archive only the extracted tree, never the .zip
  on_ingest: copy
  keep_zip: true
  max_uncompressed_mb: 2048
  max_files: 200000
  # 0 keeps every version forever.
  max_versions_per_function: 0

naming:
  # Explicit filename -> function name rules, tried first. `name` may use \\1 etc.
  # rules:
  #   - pattern: "^prod[-_](.+?)[-_]deploy"
  #     name: "\\\\1"
  #   - pattern: "orders"
  #     name: "order-processor"
  case_insensitive: true
  infer_from_zip: true

analysis:
  scan_secrets: true
  scan_env_vars: true
  scan_aws_services: true
  max_scan_file_kb: 2048
  # Paths treated as third-party rather than your code. Diffs hide these by
  # default and summarise them as dependency changes instead.
  vendor_globs:
    - "node_modules/**"
    - "**/node_modules/**"
    - "**/site-packages/**"
    - "**/*.dist-info/**"
    - "**/*.egg-info/**"
    - "vendor/**"
    - "**/__pycache__/**"

diff:
  # Hide vendored dependency files, and explain the churn as dependency version
  # numbers instead. Note that git_mirror.include_vendor below defaults the
  # other way, on purpose: the mirror is a plain git repo and filtering it would
  # make `lw git` lie about what shipped. So `lw diff` and `lw git ... diff`
  # report different file counts for the same two versions. Each says so and
  # names the other; `lw diff --vendor` and `lw diff --mirror` cross over.
  ignore_vendor: true
  context_lines: 3
  max_diff_file_kb: 512
  max_diff_lines: 2000
  # Never diffed, still tracked as added or removed. The lock files are here
  # because the dependency section above the file list already says `boto3
  # 1.34.0 -> 1.35.20`, and the lock file's own diff is a few thousand lines of
  # resolved hashes saying the same thing less well.
  ignore_globs:
    - "**/*.pyc"
    - "**/*.so"
    - "**/*.map"
    - "**/package-lock.json"
    - "**/yarn.lock"
    - "**/pnpm-lock.yaml"
    - "**/poetry.lock"
    - "**/Pipfile.lock"
    - "**/composer.lock"
    - "**/Cargo.lock"
    - "**/go.sum"
  # A file whose only change is indentation, trailing spaces, line endings or
  # blank lines is reported as `whitespace only` rather than as a hunk with every
  # touched line in it twice. One `black` run over the package is otherwise a
  # total rewrite of every file it touched. `lw diff --whitespace` overrides it.
  collapse_whitespace_only: true
  # Past this average line length a file has no usable lines and is diffed word
  # by word: a minified bundle is one 8,000-character line, and its line diff is
  # the whole file quoted twice to show one changed digit. Mean rather than
  # maximum, so one long data literal in an ordinary module still diffs by line.
  long_line_mean_chars: 400
  # Files that moved *and* changed are matched by comparing candidates pairwise,
  # which is quadratic. Past this many pairs the diff stops and reports how many
  # files it could not check, rather than silently calling a restructure a pile
  # of unrelated adds and deletes. Raise it to trade seconds for completeness.
  max_rename_pairs: 120000
  # A directory rename reaches the diff as one rename per file. Past this many
  # files moving between the same two directories, the move is reported as a
  # single line naming both directories and the count. Raise it a lot to always
  # see every file listed on its own row.
  min_moved_files: 3

git_mirror:
  # Keeps one git repo per function under functions/<name>/repo/, one commit
  # per version, tagged v0001... so `git diff v0002 v0010`, `lw open` and any
  # git GUI just work.
  enabled: true
  author_name: lambda-watcher
  author_email: lambda-watcher@localhost
  # Vendored files are committed too, so the mirror is what actually shipped
  # rather than an edited version of it. This is the counterpart to
  # diff.ignore_vendor above - set it false and the two agree, at the cost of a
  # mirror you cannot deploy from.
  include_vendor: true
  tag_prefix: v

notify:
  enabled: true
  # Only say something when the code actually differs from the last version.
  only_on_change: true
  # Put what changed in the notification - "2 modified, +24/-5 lines, 1 new env
  # var" - rather than just the file count and size.
  summarise_changes: true

report:
  # Render the comparison against the previous version as each one is archived,
  # so the answer to "what changed?" is already written by the time the
  # notification about it appears. Each lands in reports/<function>/, alongside
  # a latest.html that always points at the newest comparison.
  auto_diff: true
  include_vendor: false

# What `lw open` launches on a folder. Left empty, it looks for VS Code and
# friends on PATH. $LAMBDA_WATCHER_EDITOR overrides this.
editor: ""

log_level: INFO
"""
