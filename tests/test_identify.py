import zipfile
from pathlib import Path

import pytest

from lambda_watcher.config import NamingConfig
from lambda_watcher.identify import clean_stem, identify


@pytest.fixture
def zip_factory(tmp_path: Path):
    def _make(name: str, members: dict[str, str] | None = None) -> Path:
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as zf:
            for member, content in (members or {"lambda_function.py": "x"}).items():
                zf.writestr(member, content)
        return path

    return _make


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("order-processor.zip", "order-processor"),
        ("order-processor (1).zip", "order-processor"),
        ("order-processor (12).zip", "order-processor"),
        ("order-processor-2026-01-15.zip", "order-processor"),
        ("order-processor_20260115-1030.zip", "order-processor"),
        ("checkout-deadbeefdeadbeefcafe.zip", "checkout"),
        ("notify-1737024000000.zip", "notify"),
        ("MyLambda-backup.zip", "MyLambda"),
        ("my-fn-a1b2c3d4-1111-2222-3333-444455556666.zip", "my-fn"),
        # Source archives, named after the ref they were cut from.
        ("myrepo-1.2.3.zip", "myrepo"),
        ("myrepo-v1.2.3.zip", "myrepo"),
        ("myrepo-2.0.0-rc1.zip", "myrepo"),
        ("myrepo-main.zip", "myrepo"),
        ("myrepo_master.zip", "myrepo"),
        ("myrepo-a1b2c3d.zip", "myrepo"),
    ],
)
def test_filename_heuristics(zip_factory, filename, expected):
    assert identify(zip_factory(filename), NamingConfig()).name == expected


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("payments_api_v3.zip", "payments_api_v3"),   # -v3 is part of the name
        ("report-2024.zip", "report-2024"),           # a year, not a version
        ("etl-main-street.zip", "etl-main-street"),   # "main" is not the suffix
        ("checkout-defaced.zip", "checkout-defaced"),  # all-hex word, but no digit
    ],
)
def test_ref_stripping_leaves_ordinary_names_alone(zip_factory, filename, expected):
    assert identify(zip_factory(filename), NamingConfig()).name == expected


def test_version_suffix_is_kept_as_part_of_the_name(zip_factory):
    # "payments_api_v3" is a different function from "payments_api_v2".
    assert identify(zip_factory("payments_api_v3.zip"), NamingConfig()).name == "payments_api_v3"


def test_meaningless_name_falls_back_stably(zip_factory):
    first = identify(zip_factory("a1b2c3d4e5f6a7b8.zip"), NamingConfig())
    assert first.strategy == "fallback"
    assert first.confidence == "low"
    assert first.name.startswith("unknown-")


def test_config_rule_wins(zip_factory):
    naming = NamingConfig(rules=[{"pattern": r"^prod[-_](.+?)[-_]deploy", "name": r"\1"}])
    assert identify(zip_factory("prod-orders-deploy.zip"), naming).name == "orders"


def test_sidecar_json_supplies_the_exact_name(tmp_path: Path, zip_factory):
    archive = zip_factory("random-8f3a2b1c.zip")
    archive.with_suffix(".json").write_text(
        '{"Configuration": {"FunctionName": "OrderProcessorProd"}}'
    )
    result = identify(archive, NamingConfig())
    assert result.name == "OrderProcessorProd"
    assert result.strategy == "sidecar-json"


def test_single_top_level_directory_is_used(zip_factory):
    archive = zip_factory(
        "0f1e2d3c4b5a6978.zip", {"invoice-renderer/handler.py": "x", "invoice-renderer/a.py": "y"}
    )
    result = identify(archive, NamingConfig())
    assert result.name == "invoice-renderer"
    assert result.strategy == "zip-top-level"


def test_known_function_matching_is_case_insensitive(zip_factory, db):
    db.upsert_function("OrderProcessor", "OrderProcessor", "2026-01-01T00:00:00+00:00")
    result = identify(zip_factory("orderprocessor.zip"), NamingConfig(), db)
    assert result.name == "OrderProcessor"
    assert result.strategy == "known-function"


def test_alias_maps_an_awkward_filename(zip_factory, db):
    function_id = db.upsert_function("billing", "billing", "2026-01-01T00:00:00+00:00")
    db.add_alias(function_id, "acct-svc")
    result = identify(zip_factory("acct-svc-export.zip"), NamingConfig(), db)
    assert result.name == "billing"
    assert result.strategy == "alias"


def test_explicit_override_beats_everything(zip_factory):
    result = identify(zip_factory("whatever.zip"), NamingConfig(), None, override="Chosen")
    assert (result.name, result.strategy) == ("Chosen", "explicit")


def test_clean_stem_is_idempotent():
    naming = NamingConfig()
    once = clean_stem("order-processor-2026-01-15 (1)", naming.strip_patterns)
    assert clean_stem(once, naming.strip_patterns) == once == "order-processor"
