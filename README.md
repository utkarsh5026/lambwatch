# lambda-watcher

Watch your Downloads folder for AWS Lambda deployment zips, archive every
version automatically, and review what actually changed between any two of
them.

If your deploy ritual is *"download the current function zip as a backup, then
push the new one"*, you end up with a folder of near-identical archives and no
practical way to answer **"what changed between the 2nd one and the 10th?"**.
This tool turns that pile into a queryable, diffable history — with no change
to how you work. You keep downloading zips; it does the rest.

**[How it works, step by step →](https://utkarsh5026.github.io/lambwatch/)**

```
$ lambda-watcher watch
lambda-watcher 0.1.1 — archiving into ~/.lambda-watcher
watching ~/Downloads. Press Ctrl-C to stop.
               new  order-processor v0001  order-processor.zip — archived a new version
               new  order-processor v0002  order-processor (1).zip — 2 added, 2 modified, 9 renamed, 55 vendored
                    +24/-5 lines · new: 1 env var, 1 AWS service, 3 secrets
                    report: ~/.lambda-watcher/reports/order-processor/v0001-v0002.html
         unchanged  order-processor v0002  order-processor (2).zip — identical to version 2
done
```

That third line is the one that matters: the same code, downloaded again, is
recorded as `unchanged` rather than becoming a bogus version 3.

---

## What it does

Every time a `.zip` lands in your Downloads folder, lambda-watcher:

1. **waits** until the download has actually finished (no half-written files);
2. **works out which Lambda it is** from the filename, a sidecar
   `get-function` JSON, or the archive's contents — `order-processor.zip`,
   `order-processor (1).zip` and `order-processor-2026-03-01.zip` all land
   under the same function;
3. **extracts** it safely (path traversal, zip bombs and encrypted archives are
   refused, not trusted);
4. **hashes the content, not the file** — re-downloading unchanged code is
   recorded as `unchanged` instead of creating a bogus new version;
5. **analyses** it: runtime, handler, dependencies (declared *and* the versions
   actually vendored in the zip), environment variables the code reads, AWS
   services it calls, and hardcoded credentials;
6. **archives** it as version *N* with a `manifest.json`, indexes it in SQLite,
   and commits it to a per-function git repo tagged `v0004`.

Then you review:

```bash
lw diff order-processor                    # last two versions, in the terminal
lw diff order-processor --from 2 --to 10   # any two versions
lw diff order-processor --html --open      # a shareable HTML report
lw report order-processor                  # the whole history, browsable
lw open order-processor                    # the whole archive, in your editor
lw git order-processor log -p              # or just use git
```

## Why the diffs are actually readable

A raw `diff -r` between two Lambda zips is unusable: thousands of vendored
dependency files drown three lines of real change. lambda-watcher fixes that by
answering the questions you actually have, in order:

| | |
|---|---|
| **Your code, separated from theirs** | `node_modules/`, `site-packages/` and friends are classified as vendored and hidden by default. Three changed files, not 3,000. |
| **Dependencies as versions, not files** | A `boto3` upgrade shows as `boto3 1.34.0 → 1.35.20`, parsed from the `dist-info` actually shipped in the zip — not 400 changed files. |
| **Config impact, called out** | A new `os.environ["QUEUE_URL"]` is flagged as *"this must exist in the function's environment before you deploy"*. A new `boto3.client("sqs")` is flagged as *"the execution role may need new IAM permissions"*. These are the changes that break a deploy and never show up in a file diff. |
| **Renames survive edits** | A file that moved *and* changed is shown as one rename with a diff, not an unrelated add plus delete. |
| **Secrets are diffed too** | An AWS key or Stripe token that appears between v7 and v8 gets its own section. Values are stored redacted; the secret itself never enters the index. |

Concretely — the two versions above, where a `diff -rq` reports 61 changed
files and 56 of them are `site-packages/`:

```
$ lambda-watcher diff order-processor
╭──────────────────────────────────────────────────────────────────────╮
│ order-processor   v0001 → v0002                                      │
│ 2 added  2 modified  9 renamed  55 vendored (hidden)  +24 / -5 lines │
╰──────────────────────────────────────────────────────────────────────╯
  size     8.3 KB → 9.2 KB (+886 B)

Dependencies                                      
   manager  package   from    to       origin     
+  pip      pydantic  —       2.9.0    installed  
~  pip      boto3     1.34.0  1.35.20  installed  
~  pip      botocore  1.34.0  1.35.20  installed  

  Env vars added: QUEUE_URL
  AWS services added: sqs
  ↑ these need to exist in the function's environment configuration

New findings                                              
high  aws-access-key-id  config.py:6  AKIA…LE (20 chars)  
high  stripe-key         config.py:7  sk_l…dc (32 chars)  
 low  debug-flag         config.py:8  DEBUG = True        

Files                                                                               
   path                                                                 +  −  size  
~  lambda_function.py                                                   9  2  +322  
~  requirements.txt                                                     2  1   +17  
+  config.py                                                           10     +235  
+  helpers/__init__.py                                                              
→  {db → helpers/db}.py                                                 1      +33  
→  site-packages/boto3-1.{34.0 → 35.20}.dist-info/ · 4 files, 1         1  1    +1  
   edited                                                                           
→  site-packages/botocore-1.{34.0 → 35.20}.dist-info/ · 4 files, 1      1  1    +1  
   edited
```

The 55 vendored files became three version numbers, `db.py` moving into a
package is one rename rather than a delete plus an add, each bumped package's
`dist-info` is one line rather than four, and the new environment variable, the
new AWS service and the three secret findings are changes that a file diff
cannot express at all.

That capture is not illustrative — it is the output of
[`docs/examples/build_demo.py`](docs/examples/build_demo.py), which builds the
two zips and runs the real pipeline over them. Run it yourself:

```bash
.venv/bin/python docs/examples/build_demo.py
```

The same comparison as a shareable HTML page — `--html --open` on any diff, or
`report` for the whole history — is published from this repository:
**[see the generated report](https://utkarsh5026.github.io/lambwatch/examples/report/v0001-v0002.html)**.

## Install

Python 3.10+.

```bash
uv tool install lambda-watcher      # or: pipx install lambda-watcher
```

Either one puts `lambda-watcher` — and the shorter alias `lw`, used throughout
this README — on your `PATH` in its own environment. Plain `pip install
lambda-watcher` works too if you would rather manage the environment yourself.

<details>
<summary>From a checkout instead</summary>

```bash
git clone https://github.com/utkarsh5026/lambwatch.git
cd lambwatch
python3 -m venv .venv
.venv/bin/pip install -e .
```

</details>

## Quick start

```bash
lw setup
```

That is the whole thing. `setup` writes a config you can edit, finds your
downloads folder, offers to import any zips already sitting in it, and starts
the watcher in the background — as a launchd agent on macOS, a systemd user
service on Linux, a scheduled task on Windows — so it comes back after a
reboot without you thinking about it.

Then just keep downloading zips. When you want to know what happened:

```bash
lw                      # is it running, and what has it caught?
lw diff order-processor # what changed in the last version
```

Every new version also writes its own comparison to
`~/.lambda-watcher/reports/<function>/latest.html` as it is archived, so the
answer is a bookmark rather than a command.

<details>
<summary>Running it by hand, or setting it up piece by piece</summary>

```bash
lw watch      # run in the foreground instead; Ctrl-C stops it
lw start      # install and start the background watcher
lw stop       # stop it (--remove also unregisters it)
lw restart    # after editing the config
lw doctor     # check the config, watch folders, store, git and disk space
```

Already have a folder of old backups? Import them oldest-first so the version
numbers match real history:

```bash
lw backfill ~/Downloads/lambda-backups --dry-run   # check the names first
lw backfill ~/Downloads/lambda-backups
```

If your platform's service manager is unavailable — WSL without a systemd user
session, say — `lw start` falls back to a plain background process and tells
you it will not survive a reboot. [docs/autostart.md](docs/autostart.md) has
the manual recipes.

</details>

## Commands

| Command | What it does |
|---|---|
| `setup` | Config, background watcher and any history already on disk, in one go. `--no-service` skips the background watcher, `--yes` takes every default. |
| `status` | Is it running, and what has it archived? Also what bare `lw` prints. |
| `start` / `stop` | Register the background watcher with the OS, or stop it. `stop --remove` unregisters it too. |
| `restart` | Stop and start it — use after editing the config. |
| `watch` | Watch the download folders in the foreground. `--once` processes what is already there and exits. |
| `ingest FILE...` | Archive specific zips by hand. `--as NAME` overrides the detected function, `--label` annotates the version. |
| `backfill DIR` | Import a folder of old downloads, oldest first. `--dry-run` shows the names it would assign. |
| `ls` | Every function archived so far. |
| `versions FN` | Every archived version of one function. |
| `show FN [V]` | Runtime, handler, dependencies, env vars, services and findings for one version. `--files`, `--json`. |
| `diff FN` | Compare two versions. Defaults to the last two. `--from`/`--to`, `--html`, `--open`, `--vendor`, `--no-patch`, `--json`. |
| `report FN` | Build a browsable HTML history: an index plus a diff for every step. |
| `export FN [V]` | Get a version back out as a deployable zip (`--zip`) or a plain folder (`--tree`). |
| `open FN [V]` | Open the function's mirror in your editor — every version in one folder, with history. Name a version to open just its files. |
| `git FN ...` | Run git inside that function's mirror repo: `lw git order-processor log --oneline`. |
| `rename OLD NEW` | Fix a misidentified name. `--alias FRAGMENT` remembers the mapping for next time. |
| `merge SRC DST` | Combine two entries that are really the same Lambda, renumbering by archive time. |
| `label FN V TEXT` | Annotate a version, e.g. `label order-processor 7 "prod deploy 2026-03-01"`. |
| `search TERM` | Search filenames and dependencies across everything archived. |
| `log` | Recent activity, including downloads that were skipped and why. |
| `path FN [V]` | Print a path, for `cd "$(lw path order-processor 7)"`. |
| `rm FN` | Delete a function and everything archived for it. |
| `reindex` | Rebuild the SQLite index from the manifests on disk. |
| `doctor` | Check the config, watch folders, store, git and disk space. |

Version arguments accept `7`, `v7`, `latest`, `first`, or `-1` / `-2` counting
back from the newest.

## Where things are kept

```
~/.lambda-watcher/
├── config.yaml
├── index.db                        # rebuildable index (see `reindex`)
├── logs/watcher.log
├── reports/                        # generated HTML
├── quarantine/                     # archives that failed, with a reason file
├── repos/
│   └── order-processor/            # git mirror: one commit per version, tagged v0001…
└── functions/
    └── order-processor/
        └── versions/
            ├── 0001-7fc98e0e/
            │   ├── code/           # the extracted tree
            │   ├── manifest.json   # the full analysis
            │   └── package.zip     # the original download
            └── 0002-7f887035/
```

The directories are the source of truth. `index.db` is a cache you can delete
and rebuild with `lw reindex`, and the whole store is portable —
copy it to another machine and reindex.

### The git mirror

Each function gets its own git repository at `repos/<name>/`, whose working tree
is the latest version, with one commit per archived version tagged `v0001`,
`v0002`, … The folder is named after the function on purpose: it is what an
editor shows as the workspace root, so an open window says `order-processor`
rather than something generic. Every tool you already know works on it:

```bash
lw open order-processor  # VS Code, on the whole repo

cd "$(lw path order-processor --repo)"
git diff v0002 v0010                 # the diff you originally wanted
git log --oneline --stat
```

`open` finds VS Code, Cursor, Windsurf, VSCodium, Zed or Sublime on your `PATH`
— set `editor:` in the config (or `LAMBDA_WATCHER_EDITOR`, or `--editor`) to
name a different one. What you get is a folder of real files, not a diff: the
sidebar reads `order-processor`, and the editor's own file tree, search, Source
Control panel and timeline all work, with every earlier version a tag away. Name
a version — `lw open order-processor 3` — to open that version's
files on their own instead.

One repo per function is the point: your 2nd and 10th version of *one* Lambda
sit next to each other, with no other function's history in the way.

## Configuration

`lw init` writes an annotated `~/.lambda-watcher/config.yaml`.
Everything is optional. The settings worth knowing:

```yaml
watch:
  dirs: ["~/Downloads"]          # add more if you download from several places
  stable_seconds: 2.0            # how long a file must stop changing before it is read
  force_polling: false           # turn on for network shares, VM mounts, WSL
  arrival_max_age_seconds: 300   # ignore "modified" events for files older than this

store:
  on_ingest: copy            # copy | move | leave
                             # `move` takes the zip out of Downloads once archived
  strip_wrapper_dir: true    # lift a lone `myrepo-1.2.3/` wrapper to the root, so a
                             # source archive's ref does not read as a full rewrite
  max_versions_per_function: 0   # 0 keeps everything

naming:
  rules:                     # explicit filename → function name mappings
    - pattern: "^prod[-_](.+?)[-_]deploy"
      name: '\1'

diff:
  ignore_vendor: true        # hide vendored dependency files in diffs
  context_lines: 3
```

Set `LAMBDA_WATCHER_HOME` to relocate the whole archive, or
`LAMBDA_WATCHER_CONFIG` to point at a different config file — useful for
keeping work and personal archives separate.

### When it guesses the wrong name

The filename is a guess, and `lw log` records which strategy was
used and how confident it was. Two commands fix any mistake:

```bash
lw rename unknown-a1b2c3d4 order-processor --alias "a1b2c3d4"
lw merge order-processor-old order-processor
```

`--alias` teaches it permanently: any future download whose filename contains
that fragment maps straight to the right function.

## Notes and limits

- **Only the code is archived.** A deployment package does not contain the
  function's configuration — memory, timeout, environment variable *values*,
  IAM role, layers or triggers. lambda-watcher infers what it can from the code
  (which env vars are read, which services are called) and flags it, but if you
  want the real configuration archived too, save
  `aws lambda get-function --function-name X > X.json` next to the zip: it is
  picked up as a naming hint, and it is a genuinely useful thing to keep.
- **Secret scanning is a tripwire, not a security tool.** It catches the
  obvious cases — an AWS key, a private key block, a live Stripe token — and it
  skips placeholders. Treat a finding as a prompt to look, not a verdict.
- **Layers are separate functions in AWS**, and they are downloaded separately;
  they will be archived as their own entries.
- **Source archives work too.** A zip from GitHub (or npm, or `git archive`)
  wraps everything in a directory named after the ref — `myrepo-1.2.3/` — and
  names the file the same way. Both are handled: the wrapper is lifted to the
  root so a re-download diffs as an edit rather than a total rewrite, the ref
  becomes the version's label, and the ref is stripped from the name so
  `myrepo-1.2.3.zip`, `myrepo-main.zip` and `myrepo-a1b2c3d.zip` all land as
  versions of one `myrepo`. A trailing `-v2` is still left alone: it is part of
  a name far more often than it is a tag.
- **A filesystem event is not proof that a file was written.** Windows reports
  a zip as modified when an antivirus scan, the search indexer or OneDrive so
  much as touches it — watchdog asks the OS for attribute and last-access
  changes too — so a background sweep re-announces every zip in the folder at
  once. Events claiming a write to a file nothing has written to are ignored
  (`watch.arrival_max_age_seconds`), and `store.on_ingest: move` only clears
  out a download the watcher saw arrive: a zip that a startup scan or a
  `backfill` merely found is archived where it lies, never deleted.
- Large vendored packages make for large archives. `store.on_ingest: leave`
  and `store.keep_zip: false` trade the original zips for disk space, and
  `store.max_versions_per_function` caps history.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The design decisions behind the analysis and diff layers are written up in
[docs/design.md](docs/design.md).

## Licence

Apache 2.0. See [LICENSE](LICENSE).
