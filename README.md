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
lambda-watcher 0.1.0 — archiving into ~/.lambda-watcher
watching ~/Downloads. Press Ctrl-C to stop.
               new  order-processor v0001  order-processor.zip — archived a new version
               new  order-processor v0002  order-processor (1).zip — archived a new version
                    review: lambda-watcher diff "order-processor" --html --open
         unchanged  order-processor v0002  order-processor (2).zip — identical to version 2
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
lambda-watcher diff order-processor                    # last two versions, in the terminal
lambda-watcher diff order-processor --from 2 --to 10   # any two versions
lambda-watcher diff order-processor --html --open      # a shareable HTML report
lambda-watcher report order-processor                  # the whole history, browsable
lambda-watcher git order-processor log -p              # or just use git
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
│ 2 added  2 modified  3 renamed  52 vendored (hidden)  +24 / -5 lines │
╰──────────────────────────────────────────────────────────────────────╯
  size     8.1 KB → 8.9 KB (+782 B)

Dependencies                                      
   manager  package   from    to       origin     
+  pip      pydantic  —       2.9.0    installed  
~  pip      boto3     1.34.0  1.35.20  installed  
~  pip      botocore  1.34.0  1.35.20  installed  

  Env vars added: QUEUE_URL
  AWS services added: sqs
  ↑ these need to exist in the function's environment configuration

New findings
    high aws-access-key-id  config.py:6  AKIA…LE (20 chars)
    high stripe-key  config.py:7  sk_l…dc (32 chars)
     low debug-flag  config.py:8  DEBUG = True

Files                                                                               
   path                                                                 +  −  size  
~  lambda_function.py                                                   9  2  +322  
~  requirements.txt                                                     2  1   +17  
+  config.py                                                           10     +235  
+  helpers/__init__.py                                                              
→  db.py → helpers/db.py                                                1      +33  
→  site-packages/boto3-1.34.0.dist-info/METADATA →                      1  1    +1  
   site-packages/boto3-1.35.20.dist-info/METADATA                                   
→  site-packages/botocore-1.34.0.dist-info/METADATA →                   1  1    +1  
   site-packages/botocore-1.35.20.dist-info/METADATA
```

The 52 vendored files became three version numbers, `db.py` moving into a
package is one rename rather than a delete plus an add, and the new environment
variable, the new AWS service and the three secret findings are changes that a
file diff cannot express at all.

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
git clone https://github.com/utkarsh5026/lambwatch.git
cd lambwatch
python3 -m venv .venv
.venv/bin/pip install -e .
```

That gives you `lambda-watcher` (and the shorter alias `lw`). To use it from
anywhere, either add `.venv/bin` to your `PATH` or install with
[pipx](https://pipx.pypa.io/): `pipx install -e .`

## Quick start

```bash
lambda-watcher init      # write ~/.lambda-watcher/config.yaml (optional)
lambda-watcher doctor    # confirm the watch folder and store look right
lambda-watcher watch     # leave it running; download zips as you normally do
```

Already have a folder of old backups? Import them oldest-first so the version
numbers match real history:

```bash
lambda-watcher backfill ~/Downloads/lambda-backups --dry-run   # check the names first
lambda-watcher backfill ~/Downloads/lambda-backups
```

To keep it running in the background permanently, see
[docs/autostart.md](docs/autostart.md) (launchd, systemd and Task Scheduler
recipes).

## Commands

| Command | What it does |
|---|---|
| `watch` | Watch the download folders and archive everything that lands. `--once` processes what is already there and exits. |
| `ingest FILE...` | Archive specific zips by hand. `--as NAME` overrides the detected function, `--label` annotates the version. |
| `backfill DIR` | Import a folder of old downloads, oldest first. `--dry-run` shows the names it would assign. |
| `ls` | Every function archived so far. |
| `versions FN` | Every archived version of one function. |
| `show FN [V]` | Runtime, handler, dependencies, env vars, services and findings for one version. `--files`, `--json`. |
| `diff FN` | Compare two versions. Defaults to the last two. `--from`/`--to`, `--html`, `--open`, `--vendor`, `--no-patch`, `--json`. |
| `report FN` | Build a browsable HTML history: an index plus a diff for every step. |
| `export FN [V]` | Get a version back out as a deployable zip (`--zip`) or a plain folder (`--tree`). |
| `git FN ...` | Run git inside that function's mirror repo: `lw git order-processor log --oneline`. |
| `rename OLD NEW` | Fix a misidentified name. `--alias FRAGMENT` remembers the mapping for next time. |
| `merge SRC DST` | Combine two entries that are really the same Lambda, renumbering by archive time. |
| `label FN V TEXT` | Annotate a version, e.g. `label order-processor 7 "prod deploy 2026-03-01"`. |
| `search TERM` | Search filenames and dependencies across everything archived. |
| `log` | Recent activity, including downloads that were skipped and why. |
| `path FN [V]` | Print a path, for `cd "$(lambda-watcher path order-processor 7)"`. |
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
└── functions/
    └── order-processor/
        ├── versions/
        │   ├── 0001-bd9f77c8/
        │   │   ├── code/           # the extracted tree
        │   │   ├── manifest.json   # the full analysis
        │   │   └── package.zip     # the original download
        │   └── 0002-73d375ad/
        └── git/                    # one commit per version, tagged v0001…
```

The directories are the source of truth. `index.db` is a cache you can delete
and rebuild with `lambda-watcher reindex`, and the whole store is portable —
copy it to another machine and reindex.

### The git mirror

Each function gets its own git repository whose working tree is the latest
version, with one commit per archived version tagged `v0001`, `v0002`, … That
means every tool you already know works:

```bash
cd "$(lambda-watcher path order-processor --git)"
git diff v0002 v0010                # the diff you originally wanted
git log --oneline --stat
code .                              # or open it in any git GUI
```

One repo per function is the point: your 2nd and 10th version of *one* Lambda
sit next to each other, with no other function's history in the way.

## Configuration

`lambda-watcher init` writes an annotated `~/.lambda-watcher/config.yaml`.
Everything is optional. The settings worth knowing:

```yaml
watch:
  dirs: ["~/Downloads"]      # add more if you download from several places
  stable_seconds: 2.0        # how long a file must stop changing before it is read
  force_polling: false       # turn on for network shares, VM mounts, WSL

store:
  on_ingest: copy            # copy | move | leave
                             # `move` takes the zip out of Downloads once archived
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

The filename is a guess, and `lambda-watcher log` records which strategy was
used and how confident it was. Two commands fix any mistake:

```bash
lambda-watcher rename unknown-a1b2c3d4 order-processor --alias "a1b2c3d4"
lambda-watcher merge order-processor-old order-processor
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
