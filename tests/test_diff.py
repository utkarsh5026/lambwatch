
from lambda_watcher.diffing import compare_versions
from lambda_watcher.diffing.render_html import render_html, render_timeline
from lambda_watcher.ingest import Ingestor
from lambda_watcher.store import Store
from tests.conftest import PY_V1, PY_V2, fake_secret


def _diff(cfg, db, ingestor, function="fn", a=1, b=2, include_vendor=None):
    store = Store(cfg)
    row = db.get_function(function)
    va = db.get_version(int(row["id"]), a)
    vb = db.get_version(int(row["id"]), b)
    return compare_versions(
        row["name"], a, b,
        db.files_for(int(va["id"])), db.files_for(int(vb["id"])),
        store.resolve_version_dir(va["dir"]) / "code",
        store.resolve_version_dir(vb["dir"]) / "code",
        cfg.diff,
        a_deps=db.deps_for(int(va["id"])), b_deps=db.deps_for(int(vb["id"])),
        a_env=db.env_for(int(va["id"])), b_env=db.env_for(int(vb["id"])),
        a_services=db.services_for(int(va["id"])), b_services=db.services_for(int(vb["id"])),
        a_findings=db.findings_for(int(va["id"])), b_findings=db.findings_for(int(vb["id"])),
        a_meta=dict(va), b_meta=dict(vb),
        include_vendor=include_vendor,
    )


def test_diff_reports_code_dependency_env_and_service_changes(cfg, db, ingestor: Ingestor, make_zip):
    ingestor.ingest(make_zip("fn.zip", {
        "lambda_function.py": PY_V1,
        "requirements.txt": "boto3==1.34.0\n",
    }))
    ingestor.ingest(make_zip("fn.zip", {
        "lambda_function.py": PY_V2,
        "requirements.txt": "boto3==1.35.20\nrequests==2.32.3\n",
        "helpers.py": "def fmt(x):\n    return str(x)\n",
    }))

    diff = _diff(cfg, db, ingestor)
    counts = diff.counts()
    assert counts["modified"] == 2      # handler + requirements
    assert counts["added"] == 1         # helpers.py
    assert diff.total_added_lines > 0

    changed = {(d.kind, d.name) for d in diff.deps}
    assert ("changed", "boto3") in changed
    assert ("added", "requests") in changed

    assert diff.env_added == ["QUEUE_URL"]
    assert diff.services_added == ["sqs"]
    assert not diff.is_empty


def test_identical_rename_is_detected(cfg, db, ingestor: Ingestor, make_zip):
    body = "def helper():\n    return 42\n"
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "util.py": body}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "helpers/util.py": body}))

    diff = _diff(cfg, db, ingestor)
    renamed = [c for c in diff.files if c.kind == "renamed"]
    assert len(renamed) == 1
    assert renamed[0].old_path == "util.py"
    assert renamed[0].path == "helpers/util.py"
    assert diff.counts()["added"] == 0
    assert diff.counts()["removed"] == 0


def test_rename_with_an_edit_is_detected(cfg, db, ingestor: Ingestor, make_zip):
    before = "REQUIRED = ('a', 'b')\n\n\ndef validate(event):\n    return True\n"
    after = before + "\n\ndef validate_more(event):\n    return False\n"
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "validators.py": before}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "order_validators.py": after}))

    diff = _diff(cfg, db, ingestor)
    renamed = [c for c in diff.files if c.kind == "renamed"]
    assert len(renamed) == 1
    assert (renamed[0].old_path, renamed[0].path) == ("validators.py", "order_validators.py")
    assert renamed[0].added_lines > 0


def test_vendored_files_are_summarised_not_listed(cfg, db, ingestor: Ingestor, make_zip):
    vendor = "python/lib/python3.11/site-packages/pkg/mod.py"
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, vendor: "x = 1\n"}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, vendor: "x = 2\n"}))

    hidden = _diff(cfg, db, ingestor)
    assert hidden.files == []
    assert hidden.vendor_files_changed == 1

    shown = _diff(cfg, db, ingestor, include_vendor=True)
    assert [c.path for c in shown.files] == [vendor]


def test_new_secret_shows_up_as_a_new_finding(cfg, db, ingestor: Ingestor, make_zip):
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    leaked = PY_V1 + f'\nSTRIPE = "{fake_secret("stripe")}"\n'
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": leaked}))

    diff = _diff(cfg, db, ingestor)
    assert any(f["kind"] == "stripe-key" for f in diff.findings_new)


def test_html_report_is_self_contained(cfg, db, ingestor: Ingestor, make_zip):
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V2}))
    page = render_html(_diff(cfg, db, ingestor))

    assert page.startswith("<!DOCTYPE html>")
    assert "<style>" in page and "<script>" in page
    # No external requests of any kind.
    assert "http://" not in page and "https://" not in page
    assert "lambda_function.py" in page


def test_html_escapes_content(cfg, db, ingestor: Ingestor, make_zip):
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(make_zip("fn.zip", {
        "lambda_function.py": PY_V1 + '\nBAD = "<script>alert(1)</script>"\n'
    }))
    page = render_html(_diff(cfg, db, ingestor))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_timeline_renders():
    page = render_timeline(
        "fn",
        [
            {"seq": 2, "ingested_at": "2026-01-02T00:00:00+00:00", "runtime": "python",
             "handler": "h.handler", "file_count": 3, "total_size": 100,
             "diff_href": "v0001-v0002.html", "diff_summary": "1 modified"},
            {"seq": 1, "ingested_at": "2026-01-01T00:00:00+00:00", "runtime": "python",
             "handler": "h.handler", "file_count": 2, "total_size": 90},
        ],
    )
    assert "v0002" in page and "v0001-v0002.html" in page and "first version" in page
