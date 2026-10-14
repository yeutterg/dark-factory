"""Regressions for the controller efficiency and dispatch review."""

import json
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.controller import Controller, ControllerError
from dark_factory.execution.policy import selected_route
from dark_factory.store import Store
from dark_factory.worker import Worker

PROMPTS = Path("src/dark_factory/agents/prompts")


def issue(number):
    return {
        "repo": "yeutterg/dark-factory",
        "number": number,
        "title": f"Root {number}",
    }


def test_stale_root_does_not_block_other_roots(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    try:
        stale = ctrl.intake_issues([issue(1)])[0]
        ctrl.start_planning(stale)
        Worker(cfg, store, ctrl).run_loop(2)
        ctrl.approve(stale, "human")
        store.execute("UPDATE work_items SET plan_json='{}' WHERE id=?", (stale,))
        fresh = ctrl.intake_issues([issue(2)])[0]
        ctrl.start_planning(fresh)
        claim = ctrl.claim_job("w", ["native", "fake"])
        assert claim["work_item"]["id"] == fresh
        assert ctrl.continuation(stale)["next_action"]["kind"] == "blocked"
    finally:
        store.close()


def test_stale_root_alone_reports_reason_after_committing_scan(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    try:
        stale = ctrl.intake_issues([issue(1)])[0]
        ctrl.start_planning(stale)
        Worker(cfg, store, ctrl).run_loop(2)
        ctrl.approve(stale, "human")
        store.execute("UPDATE work_items SET plan_json='{}' WHERE id=?", (stale,))
        expired = ctrl.intake_issues([issue(2)])[0]
        ctrl.start_planning(expired)
        store.execute(
            "UPDATE work_items SET snapshot_json=json_set(snapshot_json,'$.deadline','2000-01-01T00:00:00+00:00') WHERE id=?",
            (expired,),
        )
        with pytest.raises(ControllerError, match="approved inputs changed"):
            ctrl.claim_job("w", ["native", "fake"])
        status = store.one("SELECT status FROM work_items WHERE id=?", (expired,))
        assert status[0] == "failed"
    finally:
        store.close()


def test_live_routes_reject_caller_supplied_issue_fixtures(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    try:
        for route in cfg.routes.values():
            route.evidence_kind = "live"
        with pytest.raises(ControllerError, match="provided issue fixtures"):
            ctrl.intake_issues([issue(1)])
        assert store.one("SELECT COUNT(*) FROM work_items")[0] == 0
    finally:
        store.close()


def test_live_route_for_one_role_never_serves_another(tmp_path):
    cfg = cfg_for(tmp_path)
    assert selected_route(cfg, "planner") is not None
    for route in cfg.routes.values():
        route.evidence_kind = "live"
    assert selected_route(cfg, "planner") is None
    assert selected_route(cfg, "coder") is not None


def test_legacy_jobs_are_backfilled_and_ordered_numerically(tmp_path):
    store = Store(tmp_path)
    store.execute(
        "INSERT INTO work_items(id,external_id,url,title,body,repo,status,is_root,plan_version) VALUES ('wi','x','','t','','r','planning',1,1)"
    )
    for position in (10, 2):
        store.execute(
            "INSERT INTO jobs(id,work_item_id,kind,status,spec_json,depends_on) VALUES (?,?,?,?,?,?)",
            (
                f"{position}_job_wi_v1",
                "wi",
                "plan",
                "ready",
                f'{{"logical_id": "job{position}", "version": 1}}',
                "[]",
            ),
        )
    store.execute("UPDATE jobs SET plan_version=NULL,logical_id=NULL,position=NULL")
    store.close()
    store = Store(tmp_path)
    try:
        rows = store.query("SELECT logical_id,plan_version FROM jobs ORDER BY position")
        assert [tuple(r) for r in rows] == [("job2", 1), ("job10", 1)]
    finally:
        store.close()


def test_unchanged_read_only_attempts_archive_only_their_commit(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    try:
        wid = ctrl.intake_issues([issue(1)])[0]
        ctrl.start_planning(wid)
        worker = Worker(cfg, store, ctrl)
        worker.run_loop(2)
        ctrl.approve(wid, "human")
        worker.run_loop(10)
        rows = store.query(
            "SELECT j.kind,a.archive_path FROM attempts a JOIN jobs j ON a.job_id=j.id"
        )
        archives = {row["kind"]: Path(row["archive_path"]) for row in rows}
        for kind in ("plan", "critique", "review"):
            manifest = json.loads((archives[kind] / "manifest.json").read_text())
            assert not (archives[kind] / "workspace").exists()
            assert len(manifest["workspace_commit"]) == 40
        assert (archives["implement"] / "workspace" / "message.py").is_file()
        assert ctrl.continuation(wid)["next_action"]["kind"] == "human_review"
    finally:
        store.close()


def test_policy_snapshot_is_never_shared_across_threads(tmp_path):
    import threading

    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    inside, release = threading.Event(), threading.Event()

    def hold_decision():
        with ctrl.decision():
            ctrl._policy()
            inside.set()
            release.wait(10)

    holder = threading.Thread(target=hold_decision)
    holder.start()
    try:
        assert inside.wait(10)
        before = ctrl._policy()
        cfg.routes["fake-coder"].region = "CA"
        assert ctrl._policy() != before
        with ctrl.decision():
            assert ctrl._policy()["routes"]["fake-coder"]["region"] == "CA"
    finally:
        release.set()
        holder.join()
        store.close()


@pytest.mark.parametrize("change", [None, "note", "branch", "untracked"])
def test_read_only_archive_keeps_any_git_only_work(tmp_path, change):
    from dark_factory import artifacts
    from dark_factory.adapters.git.local import git

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git(workspace, "init", "-b", "main")
    (workspace / "a.txt").write_text("a\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "base")
    commit = git(workspace, "rev-parse", "HEAD")
    baseline = (commit, artifacts.git_fingerprint(workspace))
    if change == "note":
        git(workspace, "notes", "add", "-m", "review evidence", "HEAD")
    elif change == "branch":
        git(workspace, "branch", "kept")
    elif change == "untracked":
        (workspace / "b.txt").write_text("b\n")
    destination = tmp_path / "archive"
    artifacts.archive(workspace, destination, {}, {}, unchanged=baseline)
    assert (destination / "workspace").exists() == (change is not None)
