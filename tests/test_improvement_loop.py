"""The improvement loop: on by default, ordinary pipeline, narrow scope, draft PR."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker

PROMPTS = Path("src/dark_factory/agents/prompts")


def build(tmp_path, **improvement):
    cfg = cfg_for(tmp_path / "state")
    cfg.windows = []
    cfg.raw["improvement"] = {
        "allowed_paths": ["message.py"],
        "max_files": 1,
        "max_changed_lines": 10,
        **improvement,
    }
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    return cfg, store, ctrl


def seed_rejections(
    store, count=3, signal_reason="The plan omitted a requirement", start=0
):
    for index in range(start, start + count):
        root_id = f"wi_seed{index}"
        store.execute(
            """INSERT INTO work_items
            (id,external_id,url,title,body,repo,status,is_root,created_at)
            VALUES (?,?,?,?,?,?,?,1,?)""",
            (
                root_id,
                f"seed:{index}",
                "",
                f"Outcome {index}",
                "",
                "yeutterg/dark-factory",
                "rejected",
                f"2026-11-19T00:00:0{index}+00:00",
            ),
        )
        store.audit(
            "human", "reject", {"work_item_id": root_id, "reason": signal_reason}
        )


def open_roots(store):
    return store.query("SELECT * FROM work_items WHERE origin LIKE 'improvement:%'")


def run_to_review(ctrl, cfg, store):
    worker = Worker(cfg, store, ctrl, "improver")
    worker.run_loop(2)  # plan and critique
    ctrl.improvement_tick()  # the standing policy approves
    worker.run_loop()  # implement, check, review
    return worker


def test_three_failing_roots_produce_exactly_one_scoped_improvement_root(tmp_path):
    cfg, store, ctrl = build(tmp_path)
    try:
        seed_rejections(store)
        assert ctrl.improvement_tick()["created"]
        roots = open_roots(store)
        assert len(roots) == 1 and roots[0]["origin"] == "improvement:human-rejection"
        run_to_review(ctrl, cfg, store)
        root = store.one("SELECT * FROM work_items WHERE origin LIKE 'improvement:%'")
        assert root["status"] == "awaiting_review"
        assert root["approved_by"] == "policy:improvement"
        snapshot = json.loads(root["snapshot_json"])
        assert snapshot["source"]["allowed_paths"] == ["message.py"]
        assert snapshot["source"]["diff_limits"]["max_files"] == 1
        # The ordinary stages ran: plan, critique, implement, check and review.
        kinds = {
            r["kind"]
            for r in store.query("SELECT kind FROM jobs WHERE status='succeeded'")
        }
        assert {"plan", "critique", "implement", "check", "review"} <= kinds
        # A simulation route runs the same path and never publishes.
        assert ctrl.improvement_tick()["advanced"] == [
            {"root": root["id"], "published": "skipped: simulation"}
        ]
        audit = store.query(
            "SELECT action FROM audit WHERE action LIKE 'improvement.%' OR action LIKE 'publish.%'"
        )
        assert "publish.skipped" in {a["action"] for a in audit}
        # The standing policy never accepts: that stays a person's decision.
        with pytest.raises(ControllerError):
            ctrl.accept(root["id"], "policy:improvement")
    finally:
        store.close()


def test_a_second_trigger_creates_nothing_while_one_is_open_or_cooling(tmp_path):
    cfg, store, ctrl = build(tmp_path)
    try:
        seed_rejections(store)
        ctrl.improvement_tick()
        assert len(open_roots(store)) == 1
        # More evidence arrives while the first root is open.
        seed_rejections(store, 3, "another reason", start=3)
        assert ctrl.improvement_tick()["created"] is None
        assert len(open_roots(store)) == 1
        # Closed, but three fresh roots failed since it started: new evidence ends
        # the cooldown, so one new root may begin.
        store.execute(
            "UPDATE work_items SET status='rejected' WHERE origin LIKE 'improvement:%'"
        )
        assert ctrl.improvement_tick()["created"]
        assert len(open_roots(store)) == 2
        # Closed again with no new evidence since: the cooldown holds the signal back.
        store.execute(
            "UPDATE work_items SET status='rejected' WHERE origin LIKE 'improvement:%'"
        )
        assert ctrl.improvement_tick()["created"] is None
        assert len(open_roots(store)) == 2
    finally:
        store.close()


def test_the_loop_is_off_when_disabled_and_needs_a_threshold(tmp_path):
    cfg, store, ctrl = build(tmp_path, enabled=False)
    try:
        seed_rejections(store)
        assert ctrl.improvement_tick() == {"state": "disabled"}
        assert not open_roots(store)
    finally:
        store.close()
    cfg, store, ctrl = build(tmp_path / "b")
    try:
        seed_rejections(store, 2)  # below the default of three distinct roots
        assert ctrl.improvement_tick()["created"] is None
    finally:
        store.close()


def test_an_out_of_scope_edit_is_refused(tmp_path):
    cfg, store, ctrl = build(tmp_path, allowed_paths=["docs"])
    try:
        seed_rejections(store)
        ctrl.improvement_tick()
        run_to_review(ctrl, cfg, store)
        root = store.one("SELECT * FROM work_items WHERE origin LIKE 'improvement:%'")
        assert root["status"] == "failed"
        attempt = store.one(
            "SELECT error FROM attempts WHERE status='failed' ORDER BY started_at DESC"
        )
        assert "out-of-scope" in attempt["error"]
    finally:
        store.close()


def test_an_oversized_diff_is_a_scope_violation_with_no_repair(tmp_path):
    cfg, store, ctrl = build(tmp_path, max_changed_lines=1)
    try:
        seed_rejections(store)
        ctrl.improvement_tick()
        run_to_review(ctrl, cfg, store)
        root = store.one("SELECT * FROM work_items WHERE origin LIKE 'improvement:%'")
        assert root["status"] == "failed"
        error = store.one(
            "SELECT error FROM attempts WHERE status='failed' ORDER BY started_at DESC"
        )["error"]
        assert "diff-size" in error
        assert not store.query(
            "SELECT 1 FROM jobs WHERE spec_json LIKE '%\"repair\": true%' AND status='succeeded'"
        )
    finally:
        store.close()


def test_the_loops_own_roots_are_never_evidence(tmp_path):
    cfg, store, ctrl = build(tmp_path)
    try:
        seed_rejections(store)
        ctrl.improvement_tick()
        wid = open_roots(store)[0]["id"]
        for index in range(3):
            store.audit("human", "reject", {"work_item_id": wid, "reason": f"r{index}"})
        before = store.one("SELECT COUNT(*) FROM improvement_signals")[0]
        store.set_op("improvement:seen", "", "test")
        ctrl.refresh_improvement_signals()
        assert store.one("SELECT COUNT(*) FROM improvement_signals")[0] == before
    finally:
        store.close()


def test_standing_approval_covers_only_improvement_roots(tmp_path):
    cfg, store, ctrl = build(tmp_path)
    try:
        wid = ctrl.intake_issues(
            [{"repo": cfg.repositories[0], "number": 1, "title": "Normal", "body": ""}]
        )[0]
        ctrl.start_planning(wid)
        Worker(cfg, store, ctrl, "w").run_loop(2)
        with pytest.raises(ControllerError, match="improvement roots only"):
            ctrl.approve_standing(wid)
    finally:
        store.close()


def test_target_cannot_be_the_running_installation_or_hold_the_state_dir(tmp_path):
    import tomllib

    from dark_factory.config import ConfigError, validate_raw

    path = Path("examples/factory.toml").resolve()
    raw = tomllib.loads(path.read_text())
    raw["commands"]["verify"] = {"argv": ["true"], "timeout_seconds": 5}
    repo = raw["repositories"][0]
    installation = Path(__file__).resolve().parents[1]
    raw["sources"] = {
        repo: {
            "path": str(installation),
            "revision": "HEAD",
            "checks": ["verify"],
            "allowed_paths": ["src"],
            "protected_paths": ["tests"],
        }
    }
    raw["improvement"] = {"repository": repo}
    raw["project"]["state_dir"] = str(tmp_path / "state")
    with pytest.raises(ConfigError, match="running installation"):
        validate_raw(raw, path, "digest")
    separate = tmp_path / "checkout"
    separate.mkdir()
    raw["sources"][repo]["path"] = str(separate)
    raw["project"]["state_dir"] = str(separate / "state")
    with pytest.raises(ConfigError, match="state directory"):
        validate_raw(raw, path, "digest")
    raw["project"]["state_dir"] = str(tmp_path / "elsewhere")
    validate_raw(raw, path, "digest")
