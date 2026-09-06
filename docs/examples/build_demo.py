#!/usr/bin/env python3
"""Build the demo archive the documentation site is captured from.

Every terminal block on the GitHub Pages site is real output. This script is how
that stays true: it synthesises a plausible ``order-processor`` Lambda, ships it
through the ingest pipeline four times, and prints the captures in page order.

    .venv/bin/python docs/examples/build_demo.py            # print every capture
    .venv/bin/python docs/examples/build_demo.py --keep DIR # and leave the archive behind

The scenario is deliberately ordinary: a function that read DynamoDB gains an SQS
publish, its helper moves into a package, its vendored dependencies drift, and a
config file arrives carrying credentials nobody meant to commit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Credential-shaped fixtures
#
# The secret scanner is only worth demonstrating on strings that look real, which
# also makes them look real to GitHub's push protection. Assembling them from
# fragments at runtime keeps the literal out of the repository — the same trick
# tests/conftest.py uses for the same reason.
# --------------------------------------------------------------------------- #
def fake_secret(kind: str) -> str:
    parts = {
        "aws":    ("AKIA", "IOSFODNN7", "EXAMPLE"),   # AKIA + exactly 16
        "stripe": ("sk_", "live_", "4eC39HqLyjWDarjtT1zdp7dc"),
    }[kind]
    return "".join(parts)


# --------------------------------------------------------------------------- #
# The function, as it looked at v1 and at v2
# --------------------------------------------------------------------------- #
HANDLER_V1 = '''\
"""Validate an incoming order and record it."""

import json
import os

import boto3

from db import put_order

TABLE_NAME = os.environ["TABLE_NAME"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    order = json.loads(event["body"])

    if not order.get("items"):
        return {"statusCode": 400, "body": json.dumps({"error": "order has no items"})}

    put_order(table, order)

    return {"statusCode": 201, "body": json.dumps({"id": order["id"]})}
'''

HANDLER_V2 = '''\
"""Validate an incoming order, record it, and queue it for fulfilment."""

import json
import os

import boto3

from config import MAX_ITEMS
from helpers.db import put_order

TABLE_NAME = os.environ["TABLE_NAME"]
QUEUE_URL = os.environ["QUEUE_URL"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
sqs = boto3.client("sqs")


def lambda_handler(event, context):
    order = json.loads(event["body"])

    if not order.get("items"):
        return {"statusCode": 400, "body": json.dumps({"error": "order has no items"})}

    if len(order["items"]) > MAX_ITEMS:
        return {"statusCode": 422, "body": json.dumps({"error": "too many items"})}

    put_order(table, order)
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(order))

    return {"statusCode": 201, "body": json.dumps({"id": order["id"]})}
'''

DB_V1 = '''\
"""Write an order to DynamoDB."""

from decimal import Decimal


def put_order(table, order):
    table.put_item(
        Item={
            "id": order["id"],
            "items": order["items"],
            "total": Decimal(str(order["total"])),
        }
    )
'''

# Moved to helpers/db.py *and* edited — the case a plain diff reports as an
# unrelated delete plus add.
DB_V2 = '''\
"""Write an order to DynamoDB."""

from decimal import Decimal


def put_order(table, order):
    table.put_item(
        Item={
            "id": order["id"],
            "items": order["items"],
            "total": Decimal(str(order["total"])),
            "status": "PENDING",
        }
    )
'''

CONFIG_V2 = f'''\
"""Runtime configuration.

TODO: move these to Secrets Manager before this goes anywhere near production.
"""

AWS_ACCESS_KEY_ID = "{fake_secret("aws")}"
STRIPE_API_KEY = "{fake_secret("stripe")}"
DEBUG = True

MAX_ITEMS = 50
'''

METADATA = "Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: {summary}\n"

# A real wheel installs five files into its .dist-info, not one. Shipping only
# METADATA made a dependency bump look like a single moved file, which is the
# one shape the collapsed-move row never fires on — so the demo was quietly
# showing an easier problem than the tool actually meets.
WHEEL = "Wheel-Version: 1.0\nGenerator: bdist_wheel (0.43.0)\nRoot-Is-Purelib: true\nTag: py3-none-any\n"

# botocore ships one API model per service, and that is where the bulk of a
# Python Lambda package actually goes. Carrying a realistic slice of it is the
# whole point of the "a plain diff is unreadable" example: the noise has to be
# real noise, not a number asserted in prose.
BOTOCORE_SERVICES = [
    "accessanalyzer", "acm", "apigateway", "appconfig", "athena", "autoscaling",
    "batch", "cloudformation", "cloudfront", "cloudtrail", "cloudwatch", "codebuild",
    "codepipeline", "cognito-idp", "config", "dynamodb", "dynamodbstreams", "ec2",
    "ecr", "ecs", "efs", "eks", "elasticache", "elbv2", "events", "firehose",
    "glue", "iam", "kinesis", "kms", "lambda", "logs", "organizations", "rds",
    "redshift", "route53", "s3", "sagemaker", "secretsmanager", "servicediscovery",
    "ses", "sns", "sqs", "ssm", "stepfunctions", "sts", "wafv2", "xray",
]


def vendored(packages: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Lay out ``site-packages`` the way a built deployment package carries it."""
    tree: dict[str, str] = {}
    for name, (version, summary) in packages.items():
        module = name.replace("-", "_")
        tree[f"site-packages/{module}/__init__.py"] = f'__version__ = "{version}"\n'
        dist_info = f"site-packages/{name}-{version}.dist-info"
        tree[f"{dist_info}/METADATA"] = METADATA.format(
            name=name, version=version, summary=summary
        )
        tree[f"{dist_info}/WHEEL"] = WHEEL
        tree[f"{dist_info}/INSTALLER"] = "pip\n"
        tree[f"{dist_info}/top_level.txt"] = f"{module}\n"
        # RECORD is deliberately left out. A real one is a hash manifest of every
        # installed file, which cannot be faked into anything meaningful here,
        # and a stub of it would pair badly and put a spurious "4 of 5" on a
        # directory that moved whole. The four above are the ones a wheel
        # installs whose contents this demo can honestly reproduce.
        if name == "botocore":
            for service in BOTOCORE_SERVICES:
                # The endpoint list shifts in most botocore releases, so every one
                # of these files differs between the two versions — which is
                # precisely why `diff -rq` is useless here.
                model = {
                    "metadata": {
                        "serviceId": service,
                        "apiVersion": "2012-08-10",
                        "botocoreVersion": version,
                    },
                    "operations": {},
                    "shapes": {},
                }
                tree[f"site-packages/botocore/data/{service}/2012-08-10/service-2.json"] = (
                    json.dumps(model, indent=2) + "\n"
                )
    return tree


V1_FILES = {
    "lambda_function.py": HANDLER_V1,
    "db.py": DB_V1,
    "requirements.txt": "boto3==1.34.0\n",
    **vendored({
        "boto3":    ("1.34.0", "The AWS SDK for Python"),
        "botocore": ("1.34.0", "Low-level, data-driven core of boto 3"),
    }),
}

V2_FILES = {
    "lambda_function.py": HANDLER_V2,
    "helpers/__init__.py": "",
    "helpers/db.py": DB_V2,
    "config.py": CONFIG_V2,
    "requirements.txt": "boto3==1.35.20\npydantic==2.9.0\n",
    **vendored({
        "boto3":    ("1.35.20", "The AWS SDK for Python"),
        "botocore": ("1.35.20", "Low-level, data-driven core of boto 3"),
        "pydantic": ("2.9.0",   "Data validation using Python type hints"),
    }),
}


def write_zip(path: Path, files: dict[str, str], *, built: tuple = (2024, 3, 12, 9, 41, 0)) -> Path:
    """Write a deployment zip, stamped with an explicit build time.

    ``zipfile.writestr`` would otherwise stamp every member with the current
    clock, which makes these captures irreproducible. Pinning it also lets the
    re-download below differ from its original in the one way a real
    re-download does: same files, later build stamp, different archive bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=built)
            info.external_attr = 0o644 << 16
            zf.writestr(info, content)
    return path


# --------------------------------------------------------------------------- #
# Driving the real CLI
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, home: Path, downloads: Path, width: int = 84) -> None:
        self.home = home                       # stands in as the user's home directory
        self.archive = home / ".lambda-watcher"
        self.downloads = downloads
        self.env = {
            **os.environ,
            # Point HOME at the demo directory rather than rewriting paths in
            # the output afterwards: the tool renders `~/...` itself, and Rich's
            # column widths stay the ones it actually computed.
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LAMBDA_WATCHER_HOME": str(home / ".lambda-watcher"),
            "COLUMNS": str(width),    # 84 is the width the documentation page renders at
            "TERM": "dumb",
            "NO_COLOR": "1",
            # Rich draws the diff panel with box characters and renders `→` in
            # renames. Windows defaults a pipe to cp1252, which cannot encode
            # either: Rich silently downgrades the frame to ASCII and the arrow
            # raises outright. Pin both ends to UTF-8 so the captures are the
            # same text on every platform the suite runs on.
            "PYTHONIOENCODING": "utf-8",
        }

    def write_config(self) -> None:
        """Point the config at the demo's download folder.

        `doctor` and `watch` both read it, and a config naming a folder that
        does not exist would make `doctor` report a missing watch directory.
        """
        config = self.archive / "config.yaml"
        text = config.read_text(encoding="utf-8")
        text = re.sub(
            r"^  dirs:\n(?:    - .*\n)+",
            f'  dirs:\n    - "{self.downloads.as_posix()}"\n',
            text, count=1, flags=re.M,
        )
        config.write_text(text, encoding="utf-8")

    def run(self, *args: str) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "lambda_watcher", *args],
            cwd=REPO, env=self.env, capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout + proc.stderr)
            raise SystemExit(f"`lambda-watcher {' '.join(args)}` exited {proc.returncode}")
        return self._as_home(proc.stdout.rstrip("\n"))

    def _as_home(self, text: str) -> str:
        """Write the demo's sandbox paths the way a real machine shows them.

        HOME is redirected so the tool genuinely reads and writes inside the
        demo, but it prints absolute paths, and a temp directory in the middle
        of a capture is just noise. Every path printed sits at the end of its
        line or in free text, so shortening one only ever removes trailing
        padding that ``rstrip`` was going to drop anyway.

        Both spellings of each path are replaced, because the tool echoes the
        config file's forward-slash form as well as the native one, and the
        separators inside what is left are normalised so a capture taken on
        Windows reads the same as one taken anywhere else.
        """
        for real, shown in ((self.downloads, "~/Downloads"),
                            (self.archive, "~/.lambda-watcher"),
                            (self.home, "~")):
            for spelling in {str(real), real.as_posix()}:
                text = re.sub(r"\n(?=" + re.escape(spelling) + ")", "", text)
                text = text.replace(spelling, shown)
        return re.sub(r"~[\\/][^\s]*", lambda m: m.group(0).replace("\\", "/"), text)


def recursive_diff(old: Path, new: Path) -> list[str]:
    """What ``diff -rq`` would report between two trees.

    Reimplemented rather than shelled out because this script runs on the
    Windows CI leg too, where there is no ``diff``.
    """
    def walk(root: Path) -> dict[str, Path]:
        return {str(f.relative_to(root)).replace("\\", "/"): f
                for f in root.rglob("*") if f.is_file()}

    def only_in(these: dict[str, Path], those: dict[str, Path], root: Path, other: Path) -> list[str]:
        # `diff -rq` reports a directory that exists on one side only as a single
        # line, rather than one line per file beneath it.
        seen, out = set(), []
        for name in sorted(these.keys() - those.keys()):
            parts = name.split("/")
            entry = next(
                ("/".join(parts[: i + 1]) for i in range(len(parts))
                 if not (other / "/".join(parts[: i + 1])).exists()),
                name,
            )
            if entry not in seen:
                seen.add(entry)
                out.append(f"Only in {root / entry}".rsplit("/", 1)[0] + f": {entry.rsplit('/', 1)[-1]}")
        return out

    left, right = walk(old), walk(new)
    lines = only_in(left, right, old, new) + only_in(right, left, new, old)
    lines += [
        f"Files {left[name]} and {right[name]} differ"
        for name in sorted(left.keys() & right.keys())
        if left[name].read_bytes() != right[name].read_bytes()
    ]
    return lines


def capture(title: str, command: str, body: str) -> None:
    print(f"\n\n{'=' * 92}\n[{title}]\n$ {command}\n{'-' * 92}\n{body}")


def tree_of(home: Path) -> str:
    """The archive layout, with the two large subtrees folded away."""
    lines = []
    for path in sorted(home.rglob("*")):
        rel = path.relative_to(home)
        if rel.parts[0] == ".staging":
            continue
        if any(part in {"code", "git"} for part in rel.parts[:-1]):
            continue
        depth = len(rel.parts) - 1
        lines.append("  " * depth + rel.name + ("/" if path.is_dir() else ""))
    return "\n".join(lines)


def main() -> int:
    # The captures contain box characters and arrows. Windows defaults stdout to
    # cp1252, which cannot encode them, so printing would fail part-way through
    # a run. The subprocesses are pinned to UTF-8 for the same reason.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", metavar="DIR", help="write the archive here instead of a temp dir")
    parser.add_argument("--publish", action="store_true",
                        help="also refresh docs/examples/report/, the live HTML report")
    args = parser.parse_args()

    tmp = None
    if args.keep:
        base = Path(args.keep).resolve()
        if base.exists():
            shutil.rmtree(base)
    else:
        tmp = tempfile.mkdtemp(prefix="lw-demo-")
        # Resolved because macOS hands out temp directories under /var, which is
        # a symlink to /private/var. The tool reports the resolved path, so an
        # unresolved HOME here would leave every path in the captures unshortened.
        base = Path(tmp).resolve()

    home, downloads = base / "home", base / "home" / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    cli = Runner(home, downloads)
    # Commands that print an archive path get a very wide terminal. The demo's
    # real path is a temp directory — on macOS something like
    # /var/folders/9z/.../T/lw-demo-xxxx — long enough that Rich would fold or
    # truncate it, and shortening it for display afterwards cannot put such a
    # line back together. Everything that varies with width here sits at the end
    # of its line, so the captured text is the same at any generous width.
    wide = Runner(home, downloads, width=240)
    archive = cli.archive

    # The four downloads, in the order they would land in ~/Downloads. The last
    # two are the same bytes, so the run ends with one of each outcome.
    v1 = write_zip(downloads / "order-processor.zip", V1_FILES)
    v2 = write_zip(downloads / "order-processor (1).zip", V2_FILES)
    again = write_zip(downloads / "order-processor (2).zip", V2_FILES,
                      built=(2024, 3, 12, 14, 8, 0))   # same code, packaged again later

    # Space the downloads out in time. The startup scan replays in mtime order,
    # and on a filesystem with coarse timestamps three zips written in one burst
    # share an mtime to the nanosecond - which left the order of the `watch`
    # capture, and so the version numbers in it, up to directory iteration
    # order. Hours apart, well inside the scan's 24-hour window.
    for hours_ago, staged in ((3, v1), (2, v2), (1, again)):
        stamp = time.time() - hours_ago * 3600
        os.utime(staged, (stamp, stamp))

    # ---- intake ------------------------------------------------------------
    capture("init", "lambda-watcher init", wide.run("init"))
    cli.write_config()
    capture("doctor", "lambda-watcher doctor", wide.run("doctor"))

    # What the watcher itself prints, against a throwaway archive of its own so
    # the captures below still start from nothing.
    watch_home = base / "home-watch"
    (watch_home / "Downloads").mkdir(parents=True)
    for zipped in sorted(downloads.iterdir()):
        shutil.copy2(zipped, watch_home / "Downloads" / zipped.name)
    watcher = Runner(watch_home, watch_home / "Downloads", width=240)
    watcher.run("init")
    watcher.write_config()
    capture("watch", "lambda-watcher watch", watcher.run("watch", "--once"))

    capture("ingest", "lambda-watcher ingest ~/Downloads/order-processor*.zip",
            cli.run("ingest", str(v1), str(v2), str(again), str(again)))
    capture("backfill", "lambda-watcher backfill ~/Downloads --dry-run",
            cli.run("backfill", str(downloads), "--dry-run"))

    # ---- the problem, measured rather than asserted -------------------------
    versions = sorted((archive / "functions" / "order-processor" / "versions").iterdir())
    plain = recursive_diff(versions[0] / "code", versions[1] / "code")
    noise = [ln for ln in plain if "site-packages" in ln]
    capture(
        "problem",
        "diff -rq v0001/code v0002/code | wc -l",
        f"{len(plain)}\n\n...of which {len(noise)} lines are site-packages/. "
        f"The {len(plain) - len(noise)} that\nmattered are buried, and two of the real "
        f"changes are not\nfile changes at all.",
    )
    capture("disk", "find ~/.lambda-watcher", tree_of(archive))

    # ---- reading what was archived ------------------------------------------
    capture("ls", "lambda-watcher ls", cli.run("ls"))
    capture("versions", "lambda-watcher versions order-processor",
            cli.run("versions", "order-processor"))
    capture("show", "lambda-watcher show order-processor latest",
            wide.run("show", "order-processor", "latest"))

    # The summary half of a real `diff`, which is what the page quotes: the
    # patch hunks that follow it get their own captures below.
    patch = cli.run("diff", "order-processor")
    capture("diff", "lambda-watcher diff order-processor", patch.split("\n\u2500")[0].rstrip())
    # The rename hunk on its own: one file that moved *and* changed, which a
    # plain diff can only report as an unrelated delete plus add.
    rename = next(h for h in patch.split("\n\n") if "+++ b/helpers/db.py" in h)
    capture("rename-hunk", "lambda-watcher diff order-processor   (one hunk of)", rename)

    capture("search", "lambda-watcher search boto3", cli.run("search", "boto3"))
    capture("log", "lambda-watcher log", cli.run("log"))
    capture("path", "lambda-watcher path order-processor --repo",
            wide.run("path", "order-processor", "--repo"))
    capture("open", "lambda-watcher open order-processor --print",
            wide.run("open", "order-processor", "--print"))
    capture("git", "lambda-watcher git order-processor log --oneline",
            cli.run("git", "order-processor", "log", "--oneline"))

    # ---- getting code back out ----------------------------------------------
    capture("export", "lambda-watcher export order-processor 1 -o rollback.zip",
            wide.run("export", "order-processor", "1", "-o", str(home / "rollback.zip")))
    capture("report", "lambda-watcher report order-processor",
            wide.run("report", "order-processor"))

    # ---- housekeeping, last because these change the archive -----------------
    capture("label", 'lambda-watcher label order-processor 2 "prod deploy 2026-03-01"',
            cli.run("label", "order-processor", "2", "prod deploy 2026-03-01"))
    capture("rename", "lambda-watcher rename order-processor orders-api --alias order-proc",
            cli.run("rename", "order-processor", "orders-api", "--alias", "order-proc"))
    capture("reindex", "lambda-watcher reindex --yes", cli.run("reindex", "--yes"))

    if args.publish:
        publish_report(archive)
    if args.keep:
        print(f"\n\nArchive kept at {archive}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def publish_report(home: Path) -> None:
    """Copy the generated HTML report into docs/, where Pages serves it.

    The report is self-contained — one stylesheet inlined, one relative link
    between its two pages — so it is published as generated, with one exception.
    Its file-diff section quotes ``config.py`` in full, which means the demo's
    credential-shaped fixtures would land in this repository as literals. That
    is the thing `fake_secret` exists to avoid (and GitHub's push protection
    rejects the commit outright), so those two values are masked in the
    published copy. Everything the report computed about them — the findings
    table, the counts, the redacted previews — is untouched, and running the
    builder locally gives you the unmasked report.
    """
    src = home / "reports" / "order-processor"
    dest = REPO / "docs" / "examples" / "report"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    masked = 0
    for page in sorted(dest.glob("*.html")):
        text = original = page.read_text(encoding="utf-8")
        for kind in ("aws", "stripe"):
            secret = fake_secret(kind)
            text = text.replace(secret, secret[:4] + "\u2022" * (len(secret) - 4))
        if text != original:
            page.write_text(text, encoding="utf-8")
            masked += 1

    print(f"\n\nPublished the HTML report to {dest.relative_to(REPO)}/"
          f" ({masked} page(s) with the fixture credentials masked)")


if __name__ == "__main__":
    raise SystemExit(main())
