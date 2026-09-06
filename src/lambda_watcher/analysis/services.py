"""Detect which AWS services the package talks to."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..utils import read_text
from .inventory import Inventory

_PATTERNS = [
    # boto3.client("dynamodb") / boto3.resource('s3')
    re.compile(r"""boto3\.(?:client|resource)\s*\(\s*['"]([a-z0-9-]+)['"]"""),
    re.compile(r"""session\.(?:client|resource)\s*\(\s*['"]([a-z0-9-]+)['"]"""),
    # @aws-sdk/client-dynamodb
    re.compile(r"""['"]@aws-sdk/client-([a-z0-9-]+)['"]"""),
    # require('aws-sdk').S3 / new AWS.DynamoDB(
    re.compile(r"""new\s+AWS\.([A-Za-z0-9]+)\s*\("""),
    # software.amazon.awssdk.services.s3
    re.compile(r"""software\.amazon\.awssdk\.services\.([a-z0-9]+)"""),
    re.compile(r"""com\.amazonaws\.services\.([a-z0-9]+)"""),
]

_SCANNABLE = {"python", "javascript", "typescript", "java", "ruby", "csharp", "go"}

# Normalise the different spellings onto one service id.
_ALIASES = {
    "dynamodbdocument": "dynamodb",
    "dynamodbstreams": "dynamodb-streams",
    "secretsmanager": "secretsmanager",
    "ssm": "ssm",
    "sfn": "stepfunctions",
    "states": "stepfunctions",
    "cloudwatchlogs": "logs",
}


@dataclass
class ServiceRef:
    """One place in the code where an AWS service is used.

    Recorded per ``(service, file)`` rather than per line — the first mention in
    a file is enough to say the file talks to DynamoDB.
    """

    service: str
    path: str
    line: int

    def as_dict(self) -> dict:
        """This reference as plain JSON-ready data, for the manifest."""
        return {"service": self.service, "path": self.path, "line": self.line}


def detect_services(
    root: Path, inventory: Inventory, max_files: int = 2000
) -> list[ServiceRef]:
    """Find which AWS services the code calls, across SDK dialects.

    Recognises the shapes each SDK uses to name a service: ``boto3.client("s3")``
    in Python, the ``@aws-sdk/client-dynamodb`` import and ``new AWS.S3(`` in
    JavaScript, and the ``com.amazonaws.services.*`` and
    ``software.amazon.awssdk.services.*`` package paths in Java. The captured
    name is lowercased and mapped through ``_ALIASES`` so the spellings converge
    — ``sfn`` and ``states`` both become ``stepfunctions``.

    Only first-party code is scanned, and only the first hit per
    ``(service, file)`` is kept, so vendoring the AWS SDK does not make a
    function look like it calls every service Amazon sells.
    """
    refs: list[ServiceRef] = []
    seen: set[tuple[str, str]] = set()
    scanned = 0

    for entry in inventory.code_files:
        if scanned >= max_files:
            break
        if not entry.is_text or entry.lang not in _SCANNABLE:
            continue
        text = read_text(root / entry.path, max_bytes=1024 * 1024)
        if not text:
            continue
        scanned += 1
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern in _PATTERNS:
                for match in pattern.finditer(line):
                    service = match.group(1).lower()
                    service = _ALIASES.get(service, service)
                    key = (service, entry.path)
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append(ServiceRef(service, entry.path, line_no))
    refs.sort(key=lambda r: (r.service, r.path))
    return refs
