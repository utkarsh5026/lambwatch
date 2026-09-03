# Design notes

Why the pieces work the way they do. Useful if you want to change something.

## Content hashing, not file hashing

Downloading the same unchanged Lambda twice produces two different `.zip`
files: the archive carries timestamps and ordering that vary per download. If
versions were keyed on the file hash, every backup would look like a new
version and the history would be noise.

So each extracted tree gets a **tree hash**: `sha256` over the sorted list of
`(relative path, file sha256)` pairs. It ignores zip metadata, member order and
mtimes, and it is the identity used to decide whether something is new.

There are three distinct outcomes when a file is ingested, and the log
distinguishes them:

| Outcome | Meaning |
|---|---|
| `duplicate-download` | Byte-identical file already ingested. Caught before extraction. |
| `unchanged` | Different file, same code. No new version. |
| `new-version` | The code actually changed. |

## Vendored code is not your code

A Python Lambda with dependencies contains thousands of files under
`site-packages/`; a Node one contains `node_modules/`. Diffing those files is
worse than useless — it hides the three lines that matter.

Every file is classified as first-party or vendored using
`analysis.vendor_globs`. Diffs hide vendored files by default and report
`N vendored files changed` instead, while the dependency layer explains *why*
they changed (`boto3 1.34.0 → 1.35.20`). `--vendor` shows them when you really
do need to look.

## Dependencies: declared vs installed

Two different questions:

- **Declared** — what `requirements.txt`, `package.json` or `go.mod` asks for.
  Often a range (`requests>=2.31`).
- **Installed** — what is actually vendored in the zip, read from
  `*.dist-info/METADATA` and `node_modules/*/package.json`. An exact version,
  and the one that really ran.

Both are recorded and diffed. When the two tell the same story, the report
collapses them into one row and keeps the installed version, because that is
the ground truth of what shipped.

## Configuration impact

The most expensive deploy failures are not code bugs — they are a new
`os.environ["QUEUE_URL"]` with no matching environment variable, or a new
`boto3.client("sqs")` with no matching IAM permission. Neither shows up as
anything alarming in a file diff.

So the analysers extract, per version:

- environment variables the code reads (AWS's own reserved ones are filtered
  out as noise),
- AWS services the code constructs clients for,

and the diff surfaces the added ones as their own section with a plain-language
note about what they imply.

## Rename detection

Two passes:

1. **Identical content** — a removed file and an added file with the same
   `sha256` are the same file, moved.
2. **Similar content** — remaining pairs are compared with
   `difflib.SequenceMatcher` (same language, size within 5×, similarity ≥ 0.55,
   with a bonus for a matching basename), then matched greedily strongest-first.

The second pass is what turns "deleted `validators.py`, added
`order_validators.py`" into one rename with a readable diff. It is capped at
150 candidates per side so it cannot become quadratic on a large package.

## Safety at extraction

Deployment packages come from your own account, but they are still archives,
and extraction is the one place where a malformed one could do damage. The
extractor refuses:

- absolute paths, `..` traversal, and Windows drive-letter members;
- symlinks — the link text is stored as a regular file, so the tree stays
  self-contained and cannot point outside the store;
- archives that expand beyond `store.max_uncompressed_mb` or
  `store.max_files` (zip bombs), checked from the central directory *and*
  enforced again while streaming;
- password-protected archives.

Anything that fails is copied to `quarantine/` with a `.reason.txt` next to it,
never silently dropped.

## Disk is the source of truth

`index.db` is derived state. Every version directory holds a complete
`manifest.json`, and `lambda-watcher reindex` rebuilds the entire database from
those manifests. This means the store survives a corrupted database, can be
copied between machines, and can be inspected with `cat` and `jq` if this tool
ever stops being useful to you.

## Threading

The watchdog observer thread only enqueues paths; a single worker thread does
all extraction and indexing. A slow ingest therefore cannot cause a missed
event, and there is exactly one writer to SQLite.

The connection is shared across those threads (`check_same_thread=False`) and
every statement is serialised through one re-entrant lock, with
`transaction()` holding it for the whole `BEGIN…COMMIT` block. WAL mode lets
readers proceed against a consistent snapshot meanwhile.

## Waiting for downloads to finish

A file appearing in Downloads does not mean it is complete. Chrome and Edge
write `foo.zip.crdownload` and rename at the end (so the useful event is a
*move*, not a *create*); other tools write in place.

Every candidate is therefore polled until its size and mtime hold steady for
`watch.stable_seconds`, followed by a final `open()` to confirm nothing still
holds it — which matters on Windows, where an in-progress download keeps an
exclusive handle. Files with a partial-download suffix are never considered
directly.

## What is deliberately not here

- **No AWS API calls.** The tool never needs credentials and never talks to
  AWS. It reads files that are already on your disk. That keeps it safe to run
  continuously and useful offline.
- **No daemon of its own.** `watch` is a plain foreground process; your
  platform's service manager is better at supervision than a hand-rolled
  daemoniser would be. See [autostart.md](autostart.md).
- **No database server.** SQLite and a directory tree, so the whole archive is
  a folder you can copy, back up, or delete.
