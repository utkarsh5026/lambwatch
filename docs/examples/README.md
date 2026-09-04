# Where the examples on the site come from

Every terminal capture on the [documentation site](https://utkarsh5026.github.io/lambwatch/)
is real output. [`build_demo.py`](build_demo.py) is how that claim stays true.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/python docs/examples/build_demo.py                 # print every capture
.venv/bin/python docs/examples/build_demo.py --keep ./demo   # and keep the archive to poke at
.venv/bin/python docs/examples/build_demo.py --publish       # also refresh report/
```

The script synthesises a plausible `order-processor` Lambda, ships it through the real
ingest pipeline, and prints one capture per command — 21 of them, in the order the page
uses them. Nothing is typed by hand, and nothing touches the network or AWS.

`--publish` additionally copies the generated HTML report into [`report/`](report/), which
GitHub Pages serves as the live example linked from the site. The report is self-contained
(one inlined stylesheet, one relative link between its two pages), so it is published as
generated, with one exception: its file-diff section quotes `config.py` in full, so the two
credential-shaped fixtures are masked in the published copy. Committing them as literals is
exactly what `fake_secret` exists to prevent, and GitHub's push protection rejects it
outright. Everything the report computed about those values — the findings table, the
counts, the redacted previews — is untouched, and running the builder locally gives you the
unmasked report.

`tests/test_docs.py` runs this script and fails if any terminal block on the site or in the
top-level README is not a line the tool actually printed. That is what stops the
documentation drifting back into plausible-looking fiction — the usual failure mode for
example output that was written rather than captured.

That comparison runs on POSIX only. Rich renders the same output differently on a Windows
console: it substitutes the rounded corners the diff panel is drawn with (`╭` becomes `┌`,
while `│` is left alone) and sizes some columns differently. There is no way to turn that
off from the environment, and it is a property of the terminal rather than a defect in the
documentation. The checks that do not depend on rendering — every command the page lists
exists in the CLI, and every one of them is shown with its output — run everywhere.

## Why HOME is redirected

The runner points `HOME` at the demo directory rather than rewriting paths afterwards, so
the tool genuinely reads and writes inside the sandbox and Rich computes its column widths
against the paths it is really printing. Only the leading temp path is shortened for
display, to `~/.lambda-watcher` and `~/Downloads`; because every path printed sits at the
end of its line or in free text, that only ever removes trailing padding.

Commands that print an archive path run at a wider terminal for the same reason: the demo's
real path is long enough that Rich would fold it mid-string, and a line broken inside a path
cannot be put back together after the fact.

## The scenario

Two builds of one function, plus the same build downloaded twice more:

| Download | Outcome | Why |
|---|---|---|
| `order-processor.zip` | `new-version` → v0001 | reads DynamoDB, `boto3 1.34.0` vendored |
| `order-processor (1).zip` | `new-version` → v0002 | adds an SQS publish, moves `db.py`, gains a `config.py` |
| `order-processor (2).zip` | `unchanged` | v0002's code, packaged again — different bytes, identical tree |
| `order-processor (2).zip` | `duplicate-download` | the same file a second time |

The vendored tree carries a realistic slice of `botocore`'s per-service API models, because
the "a plain diff is unreadable" example only means anything if the noise is real noise: a
`diff -rq` between the two versions reports 61 changed files, 56 of them `site-packages/`.

Between v0001 and v0002 the function picks up everything the diff layer exists to show:
a dependency bump and a new dependency, a file that moved *and* changed, a new environment
variable, a new AWS service, and three secret-scanner findings.

## Reproducibility

The zips are written with a pinned build stamp, so the version directories come out as
`0001-bd9f77c8` and `0002-73d375ad` on any machine — the same identifiers the page quotes.
Only the archive timestamps and the temp path differ between runs.

That pinning is load-bearing for one capture in particular. Left to itself `zipfile`
stamps members with the current clock, so the re-download really does produce different
archive bytes for identical code — which is the `zip_sha256` vs `tree_hash` distinction the
page is built around, demonstrated rather than asserted.

## Credential-shaped fixtures

`config.py` in the demo carries an AWS key, a Stripe key and a debug flag, because a secret
scanner is only worth showing on strings that look real. They are assembled from fragments at
runtime so the literals never appear in this repository — the same approach
[`tests/conftest.py`](../../tests/conftest.py) takes, for the same reason. The values are the
published documentation examples, and the tool stores every finding redacted regardless.
