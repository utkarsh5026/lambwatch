# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The package is not installed globally; work inside a venv.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # pytest, pytest-cov, ruff  (".[lint]" = ruff alone)

.venv/bin/python -m pytest              # full suite (pyproject sets testpaths=tests, -q)
.venv/bin/python -m pytest tests/test_diff.py::test_diff_reports_code_dependency_env_and_service_changes
.venv/bin/python -m pytest -k rename    # by name fragment

ruff check .                            # or .venv/bin/ruff; config lives in pyproject
```

Running the CLI during development: `.venv/bin/lambda-watcher <cmd>` (alias `lw`), or
`python -m lambda_watcher`. Point `LAMBDA_WATCHER_HOME` at a scratch directory so you never
touch the real `~/.lambda-watcher` archive while testing.

## The goal that outranks the others

**Making this effortless for a non-expert to install, start and read is a primary goal, not
polish.** The analysis in here is considerably better than the tool is easy, and every remaining
barrier is in the first five minutes and in the question *"is it even running?"* When a change
trades ergonomics for capability, ergonomics wins unless the capability is the point of the
change.

What that means concretely, and what to preserve:

- **Nothing is required before the tool works.** Every config field has a working default and
  `config.default_download_dirs()` guesses correctly per platform, so `lw setup`, `lw start` and
  every read command must keep working with no config file at all. A new setting that has to be
  set before something functions is a bug, not a feature.
- **Bare `lw` is a status dashboard, not `--help`** (`cli._print_status`, `_next_steps`). It
  answers *is it running* and *what has it caught*, then names the two commands worth typing
  next. It must exit 0 and print something useful on an empty archive — that is a normal state
  to be in, not an error. `no_args_is_help=False` is deliberate.
- **The watcher is a service, not a process the user babysits.** `lw start` installs a real
  per-OS **user** service ([service.py](src/lambda_watcher/service.py)) — never a system daemon,
  never sudo. `service.manager_chain()` is a *chain*, not a choice, because the preferred manager
  can refuse at the moment of use rather than of selection: a Windows account without the right
  to register a scheduled task, a Linux box with no systemd user session. The last entry always
  works, so `install_service()` ends with a watcher running, and the status says which
  arrangement it got and what that arrangement cannot do.
- **A refused service must not fail the command that asked for it.** `lw setup` warns and exits
  0 — the config, the archive and the backfill all still happened. Only `lw start`, whose sole
  job it is, exits nonzero.
- **The answer is written before anyone asks for it.** Each new version renders its own
  comparison during ingest (`Ingestor._render_report`, gated on `report.auto_diff`) into
  `reports/<function>/latest.html`, because nobody is watching a terminal when a background
  service archives something. Rendering must never fail an ingest that already succeeded.
- **Every error names the next command.** `_fail` messages, empty states and `doctor` rows all
  end in something the reader can type. A message that only reports a state is half-written.
- **One name in user-facing text: `lw`.** `lambda-watcher` stays the package and the prose name
  for the tool; every hint, error and doc line the reader is meant to *retype* says `lw`.
- **Commands are grouped, never hidden.** Every command carries a `rich_help_panel`
  (`Everyday` / `Watching` / `Reading the archive` / `Housekeeping`). Typer orders the panels by
  where each panel's first command is registered, so a command's position in `cli.py` decides
  its panel's position in `--help` — that is why `init` sits down beside `reindex`.
- **A new command needs a panel and, if it takes a function name,
  `autocompletion=_complete_function`.** The completer must never raise: it runs inside the
  user's shell on every TAB.

## Write code a stranger can read

**Readability is a correctness property here, not a finishing touch.** Someone arriving at this
codebase should be able to scan a file top to bottom and follow what it does without
reverse-engineering it from the implementation. That is a hard requirement on every change.

- **Every function, method, class and property gets a docstring. No exceptions**, including
  private helpers, nested closures, `__init__`, dunder methods and one-line properties. `src/`
  is at 100% and stays there; a new function without one is an incomplete change. Trivial
  accessors take a single line — the rule is that nothing is undocumented, not that everything
  is an essay.
- **Say what it does in plain language, then why it is that way.** One-line summary, blank line,
  then prose. The summary carries the meaning: `Bytes as something a person can read`, not
  `Format bytes`. Skip the ceremony — no `Args:`/`Returns:` blocks restating the type hints,
  which are already on the signature.
- **Show, don't characterise.** A concrete example beats a description of one: `myrepo-1.2.3` →
  `v1.2.3`, `1,536` → `1.5 KB`, `src/app/handler.py` → `src.app.handler`. Where a function has
  distinct outcomes, name them — see `IngestResult`, which spells out how `unchanged` differs
  from `duplicate-download`.
- **Document the judgement calls, because the code cannot.** Why a threshold is 0.55, why the
  secret scanner under-reports on purpose, why an error is swallowed, what breaks if the
  invariant is dropped. Anything that reads as arbitrary but is not is exactly what belongs in
  a docstring — and a caller who has to read the body to use the function safely is a docstring
  that has not been written yet.
- **The docstring goes directly under the `def`, above any comment.** Explanatory comments a new
  docstring makes redundant get deleted rather than left to drift out of sync. Keep a comment in
  the body only where it explains one specific line.
- **Cross-reference with `:func:` / `:meth:` / `:class:` / `:data:`, and inline code in double
  backticks.** Link the other half of a paired code path from both ends, so it is discoverable
  either way — `Ingestor._index_version` and `reindex._insert` name each other for that reason.
- **Name things so the docstring has less to do.** A docstring rescuing an unclear name is the
  wrong fix; rename it. Match the surrounding file's comment density and idiom.

## Architecture

A zip lands in Downloads → it becomes version *N* of some Lambda function, archived on disk,
indexed in SQLite, and mirrored into a per-function git repo. Everything else is querying that.

**The ingest pipeline** ([ingest.py](src/lambda_watcher/ingest.py), `Ingestor.ingest`) is the spine, and
its step order matters: hash the zip → dedupe against `seen_downloads` → identify the function →
extract safely → analyse → compare tree hash to latest → promote staging to a version directory →
write `manifest.json` → index in SQLite → commit to git. Each step's failure mode differs: an
extraction failure quarantines the file, an unchanged tree hash returns without creating a version.

**Disk is the source of truth; `index.db` is derived.** Every version directory holds a complete
`manifest.json`, and [reindex.py](src/lambda_watcher/reindex.py) rebuilds the whole database from
those manifests. Never store anything in SQLite that isn't recoverable from a manifest.

**Two hashes, two meanings.** `zip_sha256` is the downloaded file (catches literal re-downloads);
`tree_hash` is sha256 over sorted `(path, file sha256)` pairs of the extracted tree, and it is what
decides whether a version is new. Re-downloading the same Lambda produces a different zip but the
same tree hash — that's the `unchanged` outcome, distinct from `duplicate-download` and `new-version`.

**Vendored vs first-party** is the classification everything downstream depends on. `analysis.vendor_globs`
marks `node_modules/`, `site-packages/` etc.; diffs hide those files by default and the dependency layer
explains the churn instead (`boto3 1.34.0 → 1.35.20`). Dependencies are tracked twice — *declared*
(from `requirements.txt`/`package.json`/`go.mod`) and *installed* (from `*.dist-info/METADATA` and
vendored `package.json`) — because only the installed version is what actually ran.

### Module layers

| Layer | Files | Role |
|---|---|---|
| Config | [config.py](src/lambda_watcher/config.py), [templates.py](src/lambda_watcher/templates.py) | Nested dataclasses, every field defaulted; YAML overlay; `LAMBDA_WATCHER_HOME` / `LAMBDA_WATCHER_CONFIG` env overrides |
| Intake | [watcher.py](src/lambda_watcher/watcher.py), [ingest.py](src/lambda_watcher/ingest.py), [identify.py](src/lambda_watcher/identify.py), [extract.py](src/lambda_watcher/extract.py), [service.py](src/lambda_watcher/service.py) | Watch, wait for stability, name, unpack; register the watcher with launchd / systemd / Task Scheduler |
| Analysis | [analysis/](src/lambda_watcher/analysis/) | One module per facet (runtime, handler, deps, envvars, services, secrets, inventory), composed by `analyse()` |
| Persistence | [store.py](src/lambda_watcher/store.py), [db.py](src/lambda_watcher/db.py), [gitmirror.py](src/lambda_watcher/gitmirror.py) | Directory layout, SQLite index, git mirror |
| Presentation | [diffing/](src/lambda_watcher/diffing/), [cli.py](src/lambda_watcher/cli.py) | Compare, render text/HTML, Typer commands. `diffing/build.py` assembles a diff from the index — use it rather than calling `compare_versions` with a dozen lookups again |

### Adding an analysis facet touches seven places

This is the main cross-cutting change in the codebase. A new analyser must be wired through:
`analysis/<name>.py` → `analyse()` and the `Analysis` dataclass → `Analysis.to_manifest()` →
`db.SCHEMA` plus a `<name>_for()` accessor → `Ingestor._index_version()` (write path) →
`reindex._insert()` (rebuild path, must produce identical rows) → `diffing/compare.py` and both
renderers. Skipping `reindex._insert` silently breaks `lambda-watcher reindex`.

### Threading and SQLite

The watchdog observer thread only enqueues paths; one worker thread does all extraction and indexing,
so there is exactly one SQLite writer. The connection is shared (`check_same_thread=False`) with every
statement serialised through a re-entrant lock, and `Database.transaction()` holds that lock for the
whole `BEGIN…COMMIT`. WAL mode lets readers work meanwhile. Preserve this: don't add a second writer.

### Two independent schema versions

`db.SCHEMA_VERSION` (SQLite layout) and `analysis.MANIFEST_SCHEMA` (on-disk manifest format) version
separately. Changing the manifest shape is the breaking one — old manifests must still reindex.

## Conventions and constraints

- **No AWS API calls, ever.** The tool reads files already on disk, never needs credentials, and works
  offline. This is a deliberate design boundary, not an oversight.
- **Extraction is the trust boundary.** [extract.py](src/lambda_watcher/extract.py) refuses path traversal,
  absolute and drive-letter members, symlinks (stored as regular files), zip bombs (checked from the central
  directory *and* enforced while streaming), and encrypted archives. Failures go to `quarantine/` with a
  `.reason.txt` — never silently dropped.
- Version arguments across the CLI accept `7`, `v7`, `latest`, `first`, `-1`, `-2`; resolution lives in
  `cli._resolve_seq`. Use it rather than parsing version specs again.
- Secret findings are stored redacted; the secret value never enters the index.
- All modules use `from __future__ import annotations` and PEP 604 unions.
- Ruff ignores `UP045`/`UP007`/`B008` because Typer resolves default-argument calls at import time —
  don't "fix" `Optional[X] = typer.Option(...)` signatures in [cli.py](src/lambda_watcher/cli.py).

## CI/CD

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on pushes to `main` and PRs: `ruff check`,
then pytest across Python 3.10-3.13 on Linux plus 3.10/3.13 on macOS and Windows, then a build that
installs the wheel into a clean venv outside the source tree and smoke-tests the CLI. The `ci-ok` job
is the aggregate gate to point branch protection at.

- **`ruff format` is deliberately not enforced.** It would reformat 28 of 36 files, collapsing the
  manual alignment this codebase uses on purpose. Only `ruff check` gates CI.
- The cross-platform matrix is the point, not ceremony: this is a filesystem watcher, and watchdog
  behaviour, path handling and file locking genuinely differ per OS. The watcher tests already force
  polling for determinism.
- Test jobs set `git config --global user.name/user.email` because the git-mirror tests commit.
- [release.yml](.github/workflows/release.yml) fires on a `v*` tag: it refuses to proceed unless the tag
  equals `v<pyproject version>`, re-runs the suite on the tagged commit, publishes to PyPI via trusted
  publishing (OIDC, no stored token, environment `pypi`), then cuts a GitHub release.

## Documentation examples

Every terminal block in [README.md](README.md) and on the Pages site
([docs/index.html](docs/index.html)) is captured output, not prose. `docs/examples/build_demo.py`
builds a demo `order-processor` Lambda, runs the real pipeline over it and prints one capture per
command; [tests/test_docs.py](tests/test_docs.py) re-runs it and fails if any documented line is not
one the tool printed. So **changing renderer output means regenerating the docs**, not hand-editing
them: run the builder, copy the block back. Timestamps, git commit ids and the free space `doctor`
reports are the only parts allowed to vary. The capture comparison is POSIX-only — Rich substitutes
box characters on Windows consoles by design — while the structural checks run on every leg.

The demo zips are written with a pinned build stamp so version directories are stable
(`0001-bd9f77c8`, `0002-73d375ad`) and the docs can quote them. `--publish` refreshes
`docs/examples/report/`, the live HTML report the site links to. Credential-shaped fixtures use the
same runtime-assembly trick as `conftest.fake_secret()`.

## Tests

[tests/conftest.py](tests/conftest.py) provides the fixture chain `cfg → db → ingestor`, plus `downloads`
and a `make_zip(name, {member: content})` factory. `cfg` points the store at `tmp_path` and disables the
git mirror and notifications for speed — [test_watcher.py](tests/test_watcher.py) re-enables git explicitly.
Credential-shaped test fixtures are assembled at runtime by `conftest.fake_secret()` so real-looking tokens
never appear as literals in the repo; keep new secret-scanner fixtures on that helper.

The report's syntax highlighter is a table of regexes rather than a parser, so how good it is stays an
empirical question. [tools/measure_highlighting.py](tools/measure_highlighting.py) answers it: point it at
a tree of real source files and it lexes each one both with `diffing/highlight.py` and with Pygments, then
prints per-language agreement on the comment/string/code distinction. Pygments is the oracle and is
deliberately *not* a dependency — `pip install pygments` alongside the dev extras when you want to re-run
it. It also prints what the old line-local path would have scored, so a change that trades accuracy for
something else has to say so out loud. Last run: 97.1% of characters and 94.8% of lines agree with a real
lexer, over 3.5M characters. What remains is mostly taste (Pygments colours INI values and fenced markdown
blocks as strings; we colour the key and the fence instead) plus shell heredocs, which need a backreference
the one-regex-per-family design has no room for.
