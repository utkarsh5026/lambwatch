from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from lambda_watcher.config import Config
from lambda_watcher.db import Database
from lambda_watcher.ingest import Ingestor
from lambda_watcher.store import Store

def fake_secret(kind: str) -> str:
    """Build a credential-shaped string at runtime.

    These fixtures have to *look* like real credentials for the scanner to be
    worth testing, which also makes them look real to GitHub's push
    protection. Assembling them from fragments keeps the literal out of the
    repository while still exercising the same code path.
    """
    parts = {
        "stripe": ("sk_", "live_", "4eC39HqLyjWDarjtT1zdp7dc"),
        "github": ("ghp_", "A" * 20, "b3nT9xQ2Lm5Kd7Vw"),
    }[kind]
    return "".join(parts)


PY_V1 = """import os
import boto3

TABLE = os.environ["TABLE_NAME"]
ddb = boto3.resource("dynamodb")


def lambda_handler(event, context):
    return {"statusCode": 200}
"""

PY_V2 = """import os
import boto3

TABLE = os.environ["TABLE_NAME"]
QUEUE = os.environ["QUEUE_URL"]
ddb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")


def lambda_handler(event, context):
    sqs.send_message(QueueUrl=QUEUE, MessageBody="hi")
    return {"statusCode": 201}
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config()
    config.store.root = str(tmp_path / "store")
    config.watch.dirs = [str(tmp_path / "downloads")]
    config.git_mirror.enabled = False  # exercised separately; keeps tests fast
    config.notify.enabled = False
    (tmp_path / "downloads").mkdir()
    config.ensure_dirs()
    return config


@pytest.fixture
def db(cfg: Config) -> Iterator[Database]:
    database = Database(cfg.db_path)
    yield database
    database.close()


@pytest.fixture
def ingestor(cfg: Config, db: Database) -> Ingestor:
    return Ingestor(cfg, db, Store(cfg))


@pytest.fixture
def downloads(cfg: Config) -> Path:
    return Path(cfg.watch.dirs[0])


@pytest.fixture
def make_zip(downloads: Path):
    """Build a deployment zip, stamped with a fixed build time.

    ``writestr`` would otherwise stamp each member with the current clock, and
    DOS timestamps have two-second granularity — so two zips of identical
    content came out byte-identical most of the time and differed whenever the
    calls straddled an even second. That made
    ``test_identical_file_is_recognised_as_a_duplicate_download`` flaky: the
    second zip is only a `duplicate-download` if its bytes really are the same.
    Tests that need the bytes to differ pass an explicit ``built``.
    """
    def _make(
        name: str,
        files: dict[str, str | bytes],
        built: tuple[int, int, int, int, int, int] = (2026, 1, 1, 0, 0, 0),
    ) -> Path:
        path = downloads / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for member, content in files.items():
                info = zipfile.ZipInfo(member, date_time=built)
                info.external_attr = 0o644 << 16
                zf.writestr(info, content)
        return path

    return _make
