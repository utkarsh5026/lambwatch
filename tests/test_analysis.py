from pathlib import Path

from lambda_watcher.analysis import analyse
from lambda_watcher.analysis.deps import detect_dependencies
from lambda_watcher.analysis.inventory import build_inventory
from lambda_watcher.analysis.secrets import scan
from lambda_watcher.config import AnalysisConfig


def _write(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def test_python_package_is_understood(tmp_path: Path):
    root = _write(
        tmp_path,
        {
            "lambda_function.py": (
                "import os, boto3\n"
                'TABLE = os.environ["TABLE_NAME"]\n'
                'ddb = boto3.client("dynamodb")\n'
                's3 = boto3.resource("s3")\n'
                "def lambda_handler(event, context):\n    return 1\n"
            ),
            "requirements.txt": "boto3==1.34.0\nrequests>=2.31\n# a comment\n-r other.txt\n",
            "python/lib/python3.11/site-packages/requests-2.31.0.dist-info/METADATA": (
                "Name: requests\nVersion: 2.31.0\n\nbody"
            ),
        },
    )
    result = analyse(root, AnalysisConfig())

    assert result.runtime.runtime == "python"
    assert result.runtime.confidence == "high"
    assert result.primary_handler == "lambda_function.lambda_handler"
    assert result.unique_env_vars() == ["TABLE_NAME"]
    assert result.unique_services() == ["dynamodb", "s3"]

    installed = {(d.name, d.version) for d in result.dependencies if not d.is_declared}
    declared = {(d.name, d.version) for d in result.dependencies if d.is_declared}
    assert ("requests", "2.31.0") in installed
    assert ("boto3", "1.34.0") in declared

    # The vendored dist-info must not be counted as first-party code.
    assert result.inventory.code_file_count == 2
    assert result.vendor_file_count == 1


def test_node_package_is_understood(tmp_path: Path):
    root = _write(
        tmp_path,
        {
            "index.mjs": (
                "import { DynamoDBClient } from '@aws-sdk/client-dynamodb';\n"
                "const T = process.env.TABLE_NAME;\n"
                "export const handler = async (event) => ({ statusCode: 200 });\n"
            ),
            "package.json": '{"name":"fn","dependencies":{"@aws-sdk/client-dynamodb":"^3.0.0"}}',
            "node_modules/@aws-sdk/client-dynamodb/package.json":
                '{"name":"@aws-sdk/client-dynamodb","version":"3.540.0"}',
        },
    )
    result = analyse(root, AnalysisConfig())

    assert result.runtime.runtime == "nodejs"
    assert result.primary_handler == "index.handler"
    assert result.unique_env_vars() == ["TABLE_NAME"]
    assert "dynamodb" in result.unique_services()
    installed = {(d.name, d.version) for d in result.dependencies if not d.is_declared}
    assert ("@aws-sdk/client-dynamodb", "3.540.0") in installed


def test_reserved_env_vars_are_marked(tmp_path: Path):
    root = _write(
        tmp_path,
        {"h.py": 'import os\nR = os.environ["AWS_REGION"]\nX = os.environ["MY_VAR"]\n'},
    )
    result = analyse(root, AnalysisConfig())
    assert result.unique_env_vars() == ["MY_VAR"]
    assert "AWS_REGION" in result.unique_env_vars(include_reserved=True)


def test_secret_scanner_skips_placeholders(tmp_path: Path):
    root = _write(
        tmp_path,
        {
            "a.py": (
                'KEY = "AKIAIOSFODNN7EXAMPLE"\n'
                'password = "changeme"\n'
                'password = "s3cr3t-Tr0ub4dor-x9"\n'
                'token = os.environ["TOKEN"]\n'
            )
        },
    )
    inventory = build_inventory(root, AnalysisConfig().vendor_globs)
    findings = scan(root, inventory)
    kinds = {f.kind for f in findings}
    assert "aws-access-key-id" in kinds
    assert sum(1 for f in findings if f.kind == "hardcoded-credential") == 1
    # The redacted detail must never contain the raw value.
    assert all("AKIAIOSFODNN7EXAMPLE" not in f.detail for f in findings)


def test_tree_hash_is_stable_across_identical_trees(tmp_path: Path):
    files = {"a.py": "print(1)\n", "sub/b.py": "print(2)\n"}
    one = build_inventory(_write(tmp_path / "one", files), [])
    two = build_inventory(_write(tmp_path / "two", files), [])
    assert one.tree_hash == two.tree_hash

    three = build_inventory(_write(tmp_path / "three", {**files, "a.py": "print(9)\n"}), [])
    assert three.tree_hash != one.tree_hash


def test_dependency_parsers(tmp_path: Path):
    root = _write(
        tmp_path,
        {
            "go.mod": "module x\n\ngo 1.21\n\nrequire (\n\tgithub.com/aws/aws-lambda-go v1.41.0\n)\n",
            "package-lock.json": (
                '{"lockfileVersion":3,"packages":{"":{"name":"root"},'
                '"node_modules/axios":{"name":"axios","version":"1.6.0"}}}'
            ),
        },
    )
    inventory = build_inventory(root, [])
    deps = {(d.manager, d.name, d.version) for d in detect_dependencies(root, inventory)}
    assert ("go", "github.com/aws/aws-lambda-go", "v1.41.0") in deps
    assert ("npm", "axios", "1.6.0") in deps
