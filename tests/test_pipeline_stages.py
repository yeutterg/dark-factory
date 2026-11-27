"""A repository's build and test stages run in the one check path, bound to the commit."""

from __future__ import annotations

import json
import subprocess

import pytest
from test_workflow import setup

from dark_factory.control import planning
from dark_factory.controller import ControllerError
from dark_factory.pipeline import PIPELINE_PATH

PASS = """schemaVersion: 1
change:
  build: {steps: [["python", "-c", "import os; open(os.environ['FACTORY_OUT'] + '/built', 'w').write('x')"]]}
  test: {steps: [["python", "-c", "import os; assert os.environ['FACTORY_STAGE'] == 'test'"]]}
"""
FAIL_BUILD = PASS.replace(
    "open(os.environ['FACTORY_OUT'] + '/built', 'w').write('x')", "raise SystemExit(3)"
)
NA = """schemaVersion: 1
change:
  build: {not_applicable: "nothing to compile"}
  test: {steps: [["python", "-c", "pass"]]}
"""


def with_pipeline(monkeypatch, text):
    import os as _os

    # Capacity checks read the machine's load; keep these tests independent of it.
    monkeypatch.setattr(_os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    original = planning.fixture

    def fixture(root):
        source = original(root)
        repo = source["path"]
        git = lambda *a: subprocess.check_output(["git", "-C", repo, *a], text=True)  # noqa: E731
        import os

        if not os.path.exists(os.path.join(repo, PIPELINE_PATH)):
            os.makedirs(os.path.join(repo, "factory"), exist_ok=True)
            with open(os.path.join(repo, PIPELINE_PATH), "w") as handle:
                handle.write(text)
            git("add", ".")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "pipeline")
        return source

    monkeypatch.setattr(planning, "fixture", fixture)


def run_to_review(ctrl, worker, wid):
    worker.run_loop(2)
    ctrl.approve(wid, "human-tester")
    worker.run_loop()


def check_result(store):
    row = store.one(
        "SELECT a.result_json FROM attempts a JOIN jobs j ON a.job_id=j.id "
        "WHERE j.kind='check' ORDER BY a.started_at DESC"
    )
    return json.loads(row[0])


def test_pipeline_stages_run_before_the_canonical_checks_and_bind_the_commit(
    tmp_path, monkeypatch
):
    with_pipeline(monkeypatch, PASS)
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    snapshot = json.loads(store.one("SELECT snapshot_json FROM work_items")[0])
    assert snapshot["pipeline"]["change"]["build"]["steps"]
    assert PIPELINE_PATH in snapshot["source"]["protected_paths"]
    run_to_review(ctrl, worker, wid)
    assert store.one("SELECT status FROM work_items")[0] == "awaiting_review"
    result = check_result(store)
    stages = [(s["stage"], s["status"]) for s in result["stages"]]
    assert stages == [("build", "passed"), ("test", "passed")]
    assert {s["candidate"] for s in result["stages"]} == {
        result["checks"][0]["candidate"]
    }
    assert {s["pipeline_digest"] for s in result["stages"]} == {
        snapshot["pipeline"]["digest"]
    }
    assert result["checks"], "the host's canonical checks still run after the stages"


def test_a_failing_stage_fails_the_check_and_is_named(tmp_path, monkeypatch):
    with_pipeline(monkeypatch, FAIL_BUILD)
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    run_to_review(ctrl, worker, wid)
    result = check_result(store)
    assert result["failure_kind"] == "pipeline-stage-failure"
    assert result["stages"][0]["exit_code"] == 3 and not result["checks"]
    assert "build stage failed" in result["error"]
    assert store.one("SELECT status FROM work_items")[0] == "failed"


def test_a_stage_that_does_not_apply_is_recorded_with_its_reason(tmp_path, monkeypatch):
    with_pipeline(monkeypatch, NA)
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    run_to_review(ctrl, worker, wid)
    first = check_result(store)["stages"][0]
    assert first == {
        "stage": "build",
        "status": "not_applicable",
        "reason": "nothing to compile",
    }


def test_repository_scripts_named_by_the_pipeline_must_be_protected(
    tmp_path, monkeypatch
):
    with_pipeline(
        monkeypatch,
        PASS.replace('["python", "-c", "pass"]', '["./scripts/x"]').replace(
            '[["python", "-c", "import os; assert os.environ[\'FACTORY_STAGE\'] == \'test\'"]]',
            '[["./scripts/x"]]',
        ),
    )
    with pytest.raises(ControllerError, match="not protected paths"):
        setup(tmp_path)


def test_a_repository_without_a_pipeline_is_unchanged(tmp_path):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    assert (
        json.loads(store.one("SELECT snapshot_json FROM work_items")[0])["pipeline"]
        is None
    )
    run_to_review(ctrl, worker, wid)
    assert check_result(store)["stages"] == []


def test_change_stages_appear_in_the_same_metrics_report(tmp_path, monkeypatch):
    from dark_factory.metrics import stage_metrics

    with_pipeline(monkeypatch, PASS)
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    run_to_review(ctrl, worker, wid)
    rows = {(r["loop"], r["stage"]): r for r in stage_metrics(store)["stages"]}
    assert rows[("change", "build")]["passed"] == 1
    assert rows[("change", "test")]["runner"] == "local"
    assert ("change", "implement") in rows and ("change", "review") in rows
