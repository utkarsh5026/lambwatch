
from lambda_watcher.diffing import compare_versions
from lambda_watcher.diffing.render_html import render_html, render_timeline
from lambda_watcher.ingest import Ingestor
from lambda_watcher.store import Store
from tests.conftest import PY_V1, PY_V2, fake_secret


def _diff(cfg, db, ingestor, function="fn", a=1, b=2, include_vendor=None,
          compute_diffs=True):
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
        compute_diffs=compute_diffs,
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


def _js_module(index: int) -> str:
    """A small CommonJS handler, distinct per index so pairs cannot be swapped."""
    return (
        f"const ID = {index};\n"
        "const AWS = require('aws-sdk');\n\n"
        "exports.handler = async (event) => {\n"
        "  const body = JSON.parse(event.body);\n"
        "  console.log('order', body.id, ID);\n"
        "  return { statusCode: 200 };\n"
        "};\n"
    )


def test_a_js_to_ts_migration_reads_as_a_rename(cfg, db, ingestor: Ingestor, make_zip):
    """The extension changing is the whole point of the rename, not a reason to miss it.

    Rejecting a candidate pair whose language differs is a cheap filter, but
    taken literally it excludes the one rename most worth showing: the migrated
    file. Reported as an add beside a remove, the single line that actually
    changed is buried inside a whole file of each.
    """
    before = _js_module(1)
    after = before.replace("async (event)", "async (event: any)")
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "handler.js": before}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "handler.ts": after}))

    diff = _diff(cfg, db, ingestor)
    renamed = [c for c in diff.files if c.kind == "renamed"]
    assert len(renamed) == 1
    assert (renamed[0].old_path, renamed[0].path) == ("handler.js", "handler.ts")
    assert (renamed[0].added_lines, renamed[0].removed_lines) == (1, 1)
    assert diff.counts()["added"] == 0 and diff.counts()["removed"] == 0


def test_a_whole_migration_costs_one_comparison_per_file(cfg, db, ingestor: Ingestor, make_zip):
    """A real migration is a codebase, not a file, so it must land in the cheap pass.

    Files that kept their name are paired first, one comparison each; a
    migration keeps everything but the extension, so it has to be anchored the
    same way. Left to the quadratic pass, 40 files would be 1,600 candidate
    pairs and the budget here would run out long before the answer arrived.
    """
    cfg.diff.max_rename_pairs = 45           # 40 files, one comparison apiece, and change
    before = {"lambda_function.py": PY_V1}
    before.update({f"src/mod_{i}.js": _js_module(i) for i in range(40)})
    after = {"lambda_function.py": PY_V1}
    after.update({
        f"src/mod_{i}.ts": _js_module(i).replace("async (event)", "async (event: any)")
        for i in range(40)
    })
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    assert diff.counts()["renamed"] == 40
    assert diff.renames_unexamined == 0
    assert {(c.old_path, c.path) for c in diff.files if c.kind == "renamed"} == {
        (f"src/mod_{i}.js", f"src/mod_{i}.ts") for i in range(40)
    }


def test_unrelated_languages_are_still_not_paired(cfg, db, ingestor: Ingestor, make_zip):
    """Compatible is not the same as any: only languages that migrate may pair.

    These two files are almost the same text, which is exactly the case the
    language filter exists to reject - a ``.py`` deleted and a ``.js`` added are
    two files, however alike a line-based matcher finds them.
    """
    body = "".join(f"KEY_{i} = {i}\n" for i in range(20))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "consts.py": body}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "consts.js": body + "X = 1\n"}))

    diff = _diff(cfg, db, ingestor)
    counts = diff.counts()
    assert counts["renamed"] == 0
    assert counts["added"] == 1 and counts["removed"] == 1


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


def test_a_renamed_wrapper_does_not_read_as_a_rewrite(cfg, db, ingestor: Ingestor, make_zip):
    """Two source-archive downloads differ only where the code differs.

    Without wrapper stripping every path changes with the ref, so this diff
    would report one added file and one removed file instead of one edit -
    and on a big enough tree the rename pass runs out of budget and it stays
    that way.
    """
    ingestor.ingest(
        make_zip("myrepo-1.2.3.zip", {"myrepo-1.2.3/app.py": PY_V1}),
        function_override="myrepo",
    )
    ingestor.ingest(
        make_zip("myrepo-1.3.0.zip", {"myrepo-1.3.0/app.py": PY_V2}),
        function_override="myrepo",
    )
    diff = _diff(cfg, db, ingestor, function="myrepo")
    assert diff.counts() == {
        "added": 0, "removed": 0, "modified": 1, "renamed": 0, "mode-changed": 0
    }
    assert [c.path for c in diff.files] == ["app.py"]



def _moved_package(count: int, *, edit: bool = True) -> tuple[dict, dict]:
    """A package moved wholesale, every file lightly edited. Returns (v1, v2)."""
    # The root file keeps strip_wrapper_dir from lifting the lone top-level
    # directory, which would rewrite these moves into plain edits.
    before: dict = {"lambda_function.py": PY_V1}
    after: dict = {"lambda_function.py": PY_V1}
    for i in range(count):
        body = "".join(f"value_{j} = {j}\n" for j in range(20)) + f"NAME = 'mod{i}'\n"
        before[f"src/handlers/mod_{i}.py"] = body
        after[f"src/core/handlers/mod_{i}.py"] = body + ("EXTRA = 1\n" if edit else "")
    return before, after


def test_a_moved_package_is_still_renames_past_the_old_cliff(cfg, db, ingestor: Ingestor, make_zip):
    """Rename detection used to refuse outright past 150 candidates.

    That was the whole point of the pass — a restructure of 160 files is the
    diff nobody can reconstruct by hand — and one file crossing the line turned
    a complete rename map into 320 unrelated adds and deletes.
    """
    before, after = _moved_package(160)
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    counts = diff.counts()
    assert counts["renamed"] == 160
    assert counts["added"] == 0 and counts["removed"] == 0
    assert diff.renames_unexamined == 0


def test_a_lopsided_diff_is_not_refused_for_being_wide(cfg, db, ingestor: Ingestor, make_zip):
    """200 added against 3 removed is 600 comparisons, not 200.

    The budget is on the product because that is what the work costs; a per-side
    cap dropped three real renames here to save a few milliseconds.
    """
    body = "".join(f"line_{j} = {j}\n" for j in range(20))
    before = {"lambda_function.py": PY_V1}
    before.update({f"old/mod_{i}.py": body + f"N = {i}\n" for i in range(3)})
    after = {"lambda_function.py": PY_V1}
    after.update({f"new/mod_{i}.py": body + f"N = {i}\nTRAILER = True\n" for i in range(3)})
    after.update({f"new/fresh_{i}.py": f"FRESH = {i}\n" * 8 for i in range(200)})
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    assert diff.counts()["renamed"] == 3
    assert diff.renames_unexamined == 0


def test_vendored_churn_does_not_starve_first_party_renames(cfg, db, ingestor: Ingestor, make_zip):
    """A dependency bump moves far more files than the reader's own code does.

    Those candidates must not spend the budget first: with ignore_vendor on they
    collapse into a count, so losing four first-party renames to them would be
    paying for output that is never printed.
    """
    cfg.diff.max_rename_pairs = 400          # small enough that the budget runs out
    before, after = {}, {}
    for i in range(60):
        body = "".join(f"v{j} = {j}\n" for j in range(20)) + f"PKG = {i}\n"
        before[f"python/site-packages/pkg/alpha_{i}.py"] = body
        after[f"python/site-packages/pkg/omega_{i}.py"] = body + "BUMPED = 1\n"
    for i in range(4):
        body = "".join(f"a{j} = {j}\n" for j in range(20)) + f"APP = {i}\n"
        before[f"app/handler_{i}.py"] = body
        after[f"app/core/service_{i}.py"] = body + "EDITED = 1\n"

    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    first_party = {
        (c.old_path, c.path) for c in diff.files if c.kind == "renamed" and not c.is_vendor
    }
    assert first_party == {(f"app/handler_{i}.py", f"app/core/service_{i}.py") for i in range(4)}


def test_an_exhausted_rename_budget_says_so(cfg, db, ingestor: Ingestor, make_zip):
    """A partial rename map reads exactly like a complete one unless it is labelled."""
    cfg.diff.max_rename_pairs = 20
    before = {"lambda_function.py": PY_V1}
    after = {"lambda_function.py": PY_V1}
    for i in range(40):
        body = "".join(f"v{j} = {j}\n" for j in range(20)) + f"N = {i}\n"
        before[f"old/alpha_{i}.py"] = body
        after[f"new/omega_{i}.py"] = body + "TAIL = 1\n"
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    assert diff.renames_unexamined > 0
    assert diff.as_dict()["renames_unexamined"] == diff.renames_unexamined


def test_a_generous_budget_leaves_nothing_unexamined(cfg, db, ingestor: Ingestor, make_zip):
    """The counterpart: the same shape resolves whole when there is room for it."""
    before = {"lambda_function.py": PY_V1}
    after = {"lambda_function.py": PY_V1}
    for i in range(40):
        body = "".join(f"v{j} = {j}\n" for j in range(20)) + f"N = {i}\n"
        before[f"old/alpha_{i}.py"] = body
        after[f"new/omega_{i}.py"] = body + "TAIL = 1\n"
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    assert diff.counts()["renamed"] == 40
    assert diff.renames_unexamined == 0


def _rows(diff) -> list[str]:
    """The Files table as plain lines, which is what the reader actually gets."""
    from io import StringIO

    from rich.console import Console

    from lambda_watcher.diffing.render_text import render_files

    buffer = StringIO()
    render_files(Console(file=buffer, width=200, no_color=True), diff, show_diffs=False)
    return [line.rstrip() for line in buffer.getvalue().splitlines() if line.strip()]


def test_a_moved_directory_is_one_row_not_one_per_file(cfg, db, ingestor: Ingestor, make_zip):
    """20 files moving together is one decision, and used to print as 20 rows.

    The rows differed only in the filename at the end, so the reader had to diff
    them against each other to discover they said nothing new.
    """
    before, after = _moved_package(20, edit=False)
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    assert diff.counts()["renamed"] == 20        # detection is unchanged
    assert len(diff.files) == 20                 # and so is the per-file record

    (move,) = diff.moves
    assert (move.old_dir, move.new_dir) == ("src/handlers", "src/core/handlers")
    assert move.moved == 20 and move.edited == 0 and move.is_whole_dir

    body = [r for r in _rows(diff) if "handlers" in r]
    assert len(body) == 1
    assert "src/{handlers → core/handlers}/" in body[0]
    assert "20 files" in body[0]


def test_a_partial_move_does_not_claim_the_whole_directory(cfg, db, ingestor: Ingestor, make_zip):
    """Told only the two names, a reader would conclude the old directory is gone.

    Half of it moving is a different fact from all of it moving, so the row has
    to say ``8 of 20`` rather than imply the source no longer exists.
    """
    before, after = _moved_package(20, edit=False)
    for i in range(8, 20):                       # twelve of them stay put
        after.pop(f"src/core/handlers/mod_{i}.py")
        after[f"src/handlers/mod_{i}.py"] = before[f"src/handlers/mod_{i}.py"]
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    (move,) = diff.moves
    assert move.moved == 8 and move.total_in_old_dir == 20
    assert not move.is_whole_dir
    assert "8 of 20 files" in "".join(_rows(diff))


def test_a_file_edited_on_the_way_keeps_its_own_diff(cfg, db, ingestor: Ingestor, make_zip):
    """The row reports the move; the edit that rode along is a separate fact.

    Folding the group into one row must not swallow the patches — a file that
    moved *and* was rewritten is exactly the one worth opening.
    """
    before, after = _moved_package(20, edit=False)
    for i in (2, 7, 13):
        after[f"src/core/handlers/mod_{i}.py"] += "AUDITED = True\n"
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    (move,) = diff.moves
    assert move.moved == 20 and move.edited == 3
    assert {c.path.rsplit("_", 1)[1] for c in move.edited_members} == {"2.py", "7.py", "13.py"}
    assert "20 files, 3 edited" in "".join(_rows(diff))

    # The three still have patches to print, folded row or not.
    patched = [c for c in diff.files if c.diff_lines]
    assert len(patched) == 3


def test_the_edited_count_survives_no_patch(cfg, db, ingestor: Ingestor, make_zip):
    """``--no-patch`` computes no line counts, so ``edited`` cannot come from them.

    It is read off the content hashes instead, which the index always carries —
    otherwise the one mark distinguishing a rewritten file from a moved one
    disappears in exactly the mode that shows only the summary.
    """
    before, after = _moved_package(20, edit=False)
    for i in (4, 9):
        after[f"src/core/handlers/mod_{i}.py"] += "AUDITED = True\n"
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor, compute_diffs=False)
    assert not diff.diffs_computed
    (move,) = diff.moves
    assert move.added_lines == 0                 # nothing was diffed …
    assert move.edited == 2                      # … and the fact survives anyway
    assert "20 files, 2 edited" in "".join(_rows(diff))


def test_two_packages_moving_stay_two_moves(cfg, db, ingestor: Ingestor, make_zip):
    """Grouping is by the directory *pair*, not by either half.

    Two packages bumped in one release must not merge into a single claim that
    neither of them supports.
    """
    before: dict = {"lambda_function.py": PY_V1}
    after: dict = {"lambda_function.py": PY_V1}
    for pkg in ("boto3", "botocore"):
        for i in range(5):
            body = "".join(f"v{j} = {j}\n" for j in range(20)) + f"N = '{pkg}{i}'\n"
            before[f"site-packages/{pkg}-1.34.0.dist-info/f{i}.py"] = body
            after[f"site-packages/{pkg}-1.35.20.dist-info/f{i}.py"] = body
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor, include_vendor=True)
    assert len(diff.moves) == 2
    assert {m.old_dir.rsplit("/", 1)[1] for m in diff.moves} == {
        "boto3-1.34.0.dist-info", "botocore-1.34.0.dist-info",
    }


def test_a_renamed_file_is_not_folded_into_a_move(cfg, db, ingestor: Ingestor, make_zip):
    """A file that changed *name* is a decision about that file, not about a directory."""
    before, after = _moved_package(20, edit=False)
    moved = after.pop("src/core/handlers/mod_0.py")
    after["src/core/handlers/dispatcher.py"] = moved
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    (move,) = diff.moves
    assert move.moved == 19
    assert "dispatcher.py" not in {c.path for c in move.members}
    # It keeps a row of its own, next to the folded move.
    assert any("dispatcher" in r for r in _rows(diff))


def test_a_small_move_is_left_alone(cfg, db, ingestor: Ingestor, make_zip):
    """Two near-identical rows are not a legibility problem, and folding them costs more."""
    before, after = _moved_package(2, edit=False)
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    assert diff.moves == []
    assert len([r for r in _rows(diff) if "mod_" in r]) == 2


def test_the_machine_readable_diff_keeps_every_file(cfg, db, ingestor: Ingestor, make_zip):
    """The fold is for readers. ``--json`` consumers still get one entry per file."""
    before, after = _moved_package(20, edit=False)
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    payload = _diff(cfg, db, ingestor).as_dict()
    assert len(payload["files"]) == 20
    assert payload["counts"]["renamed"] == 20
    (move,) = payload["moves"]
    assert move["moved"] == 20 and move["whole_dir"] and len(move["paths"]) == 20


def test_the_html_report_folds_the_move_but_keeps_the_edits(cfg, db, ingestor: Ingestor, make_zip):
    """One block for the move, its members listed inside, and a block per real edit."""
    before, after = _moved_package(20, edit=False)
    for i in (3, 11):
        after[f"src/core/handlers/mod_{i}.py"] += "AUDITED = True\n"
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    html = render_html(_diff(cfg, db, ingestor))
    assert html.count('details class="file"') == 3      # the move, plus the two edits
    assert html.count('<li class="mono">') == 20        # nothing hidden that expanding won't show
    assert html.count('<div class="diff">') == 2        # both edits keep their patch
    # The counter stays a count of files, not of blocks.
    assert 'data-files="18"' in html


def test_a_move_out_of_the_archive_root_is_named(cfg, db, ingestor: Ingestor, make_zip):
    """The root has no name, and a blank one renders as ``{ → src}/`` — a gap in the row."""
    before: dict = {"lambda_function.py": PY_V1}
    after: dict = {"lambda_function.py": PY_V1, "keep.py": "KEEP = 1\n"}
    before["keep.py"] = "KEEP = 1\n"
    for i in range(6):
        body = "".join(f"v{j} = {j}\n" for j in range(20)) + f"N = {i}\n"
        before[f"mod_{i}.py"] = body
        after[f"src/mod_{i}.py"] = body
    ingestor.ingest(make_zip("fn.zip", before))
    ingestor.ingest(make_zip("fn.zip", after))

    diff = _diff(cfg, db, ingestor)
    (move,) = diff.moves
    assert move.old_dir == "" and move.display_dirs == (".", "src")
    assert "{. → src}/" in "".join(_rows(diff))


# --- Diffs that carry no information ------------------------------------------

TABBED = "def handler(event, context):\n\tif event:\n\t\treturn compute(event)\n\treturn None\n"
SPACED = "def handler(event, context):\n    if event:\n        return compute(event)\n    return None\n"


def _bundle(constant: int, padding: int = 2000) -> str:
    """A minified bundle: one line, several KB, one digit worth telling apart."""
    return f"!function(e){{var t={constant};{'a=1;' * padding}}}();\n"


def _patch(diff) -> str:
    """The whole terminal rendering, patches included — what `lw diff` prints."""
    from io import StringIO

    from rich.console import Console

    from lambda_watcher.diffing.render_text import render as render_diff

    buffer = StringIO()
    render_diff(Console(file=buffer, width=200, no_color=True), diff)
    return buffer.getvalue()


def test_a_retab_is_labelled_not_reprinted(cfg, db, ingestor: Ingestor, make_zip):
    """Tabs to spaces changes every line and says nothing. The label is the whole story."""
    ingestor.ingest(make_zip("fn.zip", {"handler.py": TABBED}))
    ingestor.ingest(make_zip("fn.zip", {"handler.py": SPACED}))

    diff = _diff(cfg, db, ingestor)
    (change,) = diff.files
    assert change.kind == "modified"          # the file really did change on disk
    assert change.whitespace_only
    assert change.skipped_reason == "whitespace only"
    assert not change.diff_lines
    # +3/-3 was the noise, not the news.
    assert (change.added_lines, change.removed_lines) == (0, 0)
    assert diff.lines_uncounted == 1
    assert "whitespace only" in _patch(diff)
    assert "return compute(event)" not in _patch(diff)


def test_the_whitespace_label_can_be_turned_off(cfg, db, ingestor: Ingestor, make_zip):
    """``lw diff --whitespace`` is the way back when a reindent might be hiding an edit."""
    ingestor.ingest(make_zip("fn.zip", {"handler.py": TABBED}))
    ingestor.ingest(make_zip("fn.zip", {"handler.py": SPACED}))

    cfg.diff.collapse_whitespace_only = False
    (change,) = _diff(cfg, db, ingestor).files
    assert not change.whitespace_only
    assert (change.added_lines, change.removed_lines) == (3, 3)


def test_a_real_edit_beside_a_reindent_is_still_shown(cfg, db, ingestor: Ingestor, make_zip):
    """The test is whole-file: one changed token puts the file back on the line diff."""
    ingestor.ingest(make_zip("fn.zip", {"handler.py": TABBED}))
    ingestor.ingest(make_zip("fn.zip", {"handler.py": SPACED.replace("None", "{}")}))

    (change,) = _diff(cfg, db, ingestor).files
    assert not change.whitespace_only
    assert any(line.startswith("+    return {}") for line in change.diff_lines)


def test_collapsing_whitespace_is_not_deleting_it(cfg, db, ingestor: Ingestor, make_zip):
    """``foo bar`` -> ``foobar`` is a rename, whatever ``git -w`` would call it."""
    ingestor.ingest(make_zip("fn.zip", {"handler.py": "x = foo (1)\n"}))
    ingestor.ingest(make_zip("fn.zip", {"handler.py": "x = foo(1)\n"}))

    (change,) = _diff(cfg, db, ingestor).files
    assert not change.whitespace_only and change.diff_lines


def test_a_minified_bundle_is_diffed_by_word_not_by_line(cfg, db, ingestor: Ingestor, make_zip):
    """One line, 8 KB, one changed digit: the line diff is the file quoted twice."""
    ingestor.ingest(make_zip("fn.zip", {"bundle.min.js": _bundle(1)}))
    ingestor.ingest(make_zip("fn.zip", {"bundle.min.js": _bundle(2)}))

    diff = _diff(cfg, db, ingestor)
    (change,) = diff.files
    assert change.long_lines and not change.diff_lines
    (edit,) = change.word_edits
    assert (edit.before, edit.after) == ("1", "2")
    assert edit.lead.endswith("var t=") and edit.trail.startswith(";a=1;")

    printed = _patch(diff)
    assert "minified — 1 edit" in printed
    assert "1 not counted by line" in printed
    # The 8,000 characters that did not change stay out of the terminal.
    assert "a=1;" * 40 not in printed


def test_a_rebuilt_bundle_reports_its_size_rather_than_quoting_itself(
    cfg, db, ingestor: Ingestor, make_zip
):
    """Past a few KB of difference the two bundles were rebuilt, not patched."""
    ingestor.ingest(make_zip("fn.zip", {"bundle.min.js": _bundle(1)}))
    ingestor.ingest(make_zip("fn.zip", {"bundle.min.js": "!function(e){" + "z=9;" * 3000 + "}();\n"}))

    (change,) = _diff(cfg, db, ingestor).files
    assert change.long_lines and not change.word_edits
    assert change.skipped_reason.startswith("changes span ")
    assert "z=9;" * 40 not in _patch(_diff(cfg, db, ingestor))


def test_an_added_bundle_keeps_the_ordinary_diff(cfg, db, ingestor: Ingestor, make_zip):
    """Word-level needs two sides. A bundle that merely arrived has one."""
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(make_zip("fn.zip", {"lambda_function.py": PY_V1, "bundle.min.js": _bundle(1)}))

    (change,) = [c for c in _diff(cfg, db, ingestor).files if c.path == "bundle.min.js"]
    assert change.kind == "added"
    assert not change.long_lines and change.diff_lines


def test_an_ordinary_module_with_one_long_line_still_diffs_by_line(
    cfg, db, ingestor: Ingestor, make_zip
):
    """The threshold is the mean, so one embedded blob does not disqualify a module."""
    blob = f"DATA = '{'x' * 3000}'\n"
    before = blob + "".join(f"v{i} = {i}\n" for i in range(200))
    ingestor.ingest(make_zip("fn.zip", {"handler.py": before}))
    ingestor.ingest(make_zip("fn.zip", {"handler.py": before + "EXTRA = 1\n"}))

    (change,) = _diff(cfg, db, ingestor).files
    assert not change.long_lines
    assert "+EXTRA = 1" in change.diff_lines


def test_lock_files_are_left_to_the_dependency_layer(cfg, db, ingestor: Ingestor, make_zip):
    """A resolved lock file diffs as thousands of hash lines saying `boto3 moved`."""
    def lock(version: str) -> str:
        """One resolved package, integrity hash and all."""
        return f'{{"packages": {{"a": {{"version": "{version}", "integrity": "sha512-{"q" * 60}"}}}}}}\n'
    ingestor.ingest(make_zip("fn.zip", {"package-lock.json": lock("1.0.0"), "app.js": "var a=1;\n"}))
    ingestor.ingest(make_zip("fn.zip", {"package-lock.json": lock("2.0.0"), "app.js": "var a=2;\n"}))

    diff = _diff(cfg, db, ingestor)
    (locked,) = [c for c in diff.files if c.path == "package-lock.json"]
    assert locked.kind == "modified"                  # still reported as changed
    assert locked.skipped_reason == "ignored by config" and not locked.diff_lines
    (app,) = [c for c in diff.files if c.path == "app.js"]
    assert app.diff_lines                             # first-party code is untouched by this


def test_the_html_report_shows_the_changed_runs(cfg, db, ingestor: Ingestor, make_zip):
    """Both renderers answer the same question; only the markup differs."""
    ingestor.ingest(make_zip("fn.zip", {"bundle.min.js": _bundle(1), "handler.py": TABBED}))
    ingestor.ingest(make_zip("fn.zip", {"bundle.min.js": _bundle(2), "handler.py": SPACED}))

    html = render_html(_diff(cfg, db, ingestor))
    assert '<div class="wordedit">' in html
    assert '<span class="was">1</span> → <span class="now">2</span>' in html
    assert "diffed by word" in html
    assert "lw diff --whitespace" in html          # every note names the way out
    assert '<div class="diff">' not in html        # neither file got a line table
