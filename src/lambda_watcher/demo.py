"""A sample Lambda, for seeing the tool work before a real deploy comes along.

``order-processor`` at two releases, packaged the way a real deployment zip is:
a handler that read DynamoDB gains an SQS publish, its helper moves into a
package *and* changes, its vendored boto3 drifts a minor version and picks up
pydantic, and a config file arrives carrying credentials nobody meant to commit.
Every layer the diff has — dependencies, configuration impact, findings, a
rename that survived an edit, fifty-odd vendored files reduced to three version
numbers — has something to show.

Two callers, one source. ``lw demo`` runs it through the real pipeline on a
scratch archive, and ``docs/examples/build_demo.py`` runs the same zips through
the CLI to produce every capture the documentation quotes. Keeping the fixtures
here rather than in the docs harness is what lets a ``pipx`` install, which has
no checkout, show the same thing the website does.

The zips are stamped with a pinned build time, so the version directories they
produce are stable (``0001-7fc98e0e``, ``0002-7f887035``) and the documentation
can quote them.
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

#: The function every sample download is a version of.
DEMO_FUNCTION = "order-processor"


# --------------------------------------------------------------------------- #
# Credential-shaped fixtures
#
# The secret scanner is only worth demonstrating on strings that look real, which
# also makes them look real to GitHub's push protection. Assembling them from
# fragments at runtime keeps the literal out of the repository — the same trick
# tests/conftest.py uses for the same reason.
# --------------------------------------------------------------------------- #
def fake_secret(kind: str) -> str:
    """A credential-shaped string, assembled here so it is never a literal in the repo."""
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


def write_zip(
    path: Path, files: dict[str, str], *, built: tuple[int, int, int, int, int, int] = (2024, 3, 12, 9, 41, 0)
) -> Path:
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


def stage_downloads(downloads: Path) -> list[Path]:
    """Drop the three sample downloads into ``downloads``, oldest first.

    ``order-processor.zip`` is v1; ``order-processor (1).zip`` is v2; and
    ``order-processor (2).zip`` is v2 packaged again later — the same files under
    a newer build stamp, so different archive bytes and the same tree. Ingested in
    order they produce one of each outcome: a new version, a changed version, and
    ``unchanged``.

    The files are also spaced out in time. A watcher's start-up scan replays in
    mtime order, and on a filesystem with coarse timestamps three zips written in
    one burst share an mtime to the nanosecond, which left their order — and so
    the version numbers — to directory iteration. Hours apart keeps them well
    inside the scan's 24-hour window.
    """
    v1 = write_zip(downloads / f"{DEMO_FUNCTION}.zip", V1_FILES)
    v2 = write_zip(downloads / f"{DEMO_FUNCTION} (1).zip", V2_FILES)
    again = write_zip(downloads / f"{DEMO_FUNCTION} (2).zip", V2_FILES,
                      built=(2024, 3, 12, 14, 8, 0))    # same code, packaged again later
    for hours_ago, staged in ((3, v1), (2, v2), (1, again)):
        stamp = time.time() - hours_ago * 3600
        os.utime(staged, (stamp, stamp))
    return [v1, v2, again]
