from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory import artifacts
from dark_factory.adapters.decision.task_supervisor import handle
from dark_factory.api import serve
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.ui.projection import controller_projection
from dark_factory.worker import Worker


def planned(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    wid = ctrl.intake_issues(
        [
            {
                "repo": cfg.repositories[0],
                "number": 71,
                "title": "Task supervisor outcome",
                "body": "Build it and expose progress.",
                "is_root": True,
            }
        ]
    )[0]
    ctrl.start_planning(wid)
    Worker(cfg, store, ctrl, "planner").run_loop(max_jobs=2)
    return cfg, store, ctrl, wid


def test_plan_seeds_revisioned_tasks_and_markdown(tmp_path):
    cfg, store, ctrl, wid = planned(tmp_path)
    try:
        view = ctrl.tasks_view(wid)
        assert view["revision"] == 1
        assert any(task["owner_kind"] == "agent" for task in view["tasks"])
        assert any(task["owner_kind"] == "controller" for task in view["tasks"])
        assert any(task["manual_gate"] for task in view["tasks"])
        path = ctrl.tasks_path(wid)
        assert path.is_file()
        body = path.read_text()
        assert f"root_id: {wid}" in body
        assert "## Current state" in body
        detail = controller_projection(ctrl, wid)["items"][0]
        assert detail["task_counts"]["total"] == len(view["tasks"])
        assert detail["tasks"]
    finally:
        store.close()


def test_user_task_revision_and_structured_admission(tmp_path):
    _, store, ctrl, wid = planned(tmp_path)
    try:
        added = ctrl.add_task(
            wid,
            "Cover the reconnect edge case",
            "agent",
            "greg",
            1,
            idempotency_key="request-1",
        )
        duplicate = ctrl.add_task(
            wid,
            "Cover the reconnect edge case",
            "agent",
            "greg",
            1,
            idempotency_key="request-1",
        )
        assert duplicate["task_id"] == added["task_id"]
        assert ctrl.tasks_view(wid)["inbox_count"] == 1
        with pytest.raises(ControllerError, match="task list changed"):
            ctrl.add_task(wid, "stale", "human", "greg", 1)
        criterion = next(iter(ctrl._criteria(ctrl._task_root(wid))))
        event_id = store.one(
            "SELECT id FROM task_events WHERE task_id=?", (added["task_id"],)
        )["id"]
        result = ctrl.apply_supervisor_response(
            wid,
            {
                "schema_version": 1,
                "root_id": wid,
                "base_revision": 2,
                "processed_event_ids": [event_id],
                "operations": [
                    {
                        "op": "admit_task",
                        "task_id": added["task_id"],
                        "criterion_ids": [criterion],
                        "source_event_ids": [event_id],
                    }
                ],
                "current_summary": "The request is queued.",
                "suggested_next_task_id": added["task_id"],
                "needs_decision": [],
            },
        )
        assert result["revision"] == 3
        task = next(
            t for t in ctrl.tasks_view(wid)["tasks"] if t["id"] == added["task_id"]
        )
        assert task["state"] == "queued"
        assert task["delivery_state"] == "queued"
    finally:
        store.close()


def test_human_completion_and_markdown_conflict_preservation(tmp_path):
    _, store, ctrl, wid = planned(tmp_path)
    try:
        result = ctrl.add_task(wid, "Confirm the wording", "human", "greg", 1)
        completed = ctrl.complete_task(
            wid,
            result["task_id"],
            "greg",
            result["revision"],
            "Confirmed in the draft.",
        )
        assert completed["state"] == "done"
        path = ctrl.tasks_path(wid)
        path.write_text(path.read_text() + "\n- [ ] Keep this edit\n")
        exported = ctrl.export_tasks(wid)
        assert exported["written"] is False
        assert (
            Path(exported["pending_path"])
            .read_text()
            .endswith("- [ ] Keep this edit\n")
        )
    finally:
        store.close()


def test_markdown_import_adds_agent_work_and_checks_human_tasks(tmp_path):
    _, store, ctrl, wid = planned(tmp_path)
    try:
        added = ctrl.add_task(wid, "Confirm the task view", "human", "greg", 1)
        path = ctrl.tasks_path(wid)
        body = path.read_text().replace(
            f"- [ ] Confirm the task view <!-- task:{added['task_id']} owner:human -->",
            f"- [x] Confirm the task view <!-- task:{added['task_id']} owner:human -->",
        )
        path.write_text(body + "\n- [ ] Handle the reconnect edge case\n")
        imported = ctrl.import_tasks(wid, "greg", added["revision"])
        assert imported["completed"] == [added["task_id"]]
        assert len(imported["added"]) == 1
        imported_task = next(
            task
            for task in ctrl.tasks_view(wid)["tasks"]
            if task["id"] == imported["added"][0]
        )
        assert imported_task["owner_kind"] == "agent"
        assert imported_task["state"] == "inbox"
    finally:
        store.close()


def test_backup_and_restore_include_task_documents(tmp_path):
    _, store, ctrl, wid = planned(tmp_path / "state")
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    original_body = ctrl.tasks_path(wid).read_text()
    try:
        artifacts.backup(store, backup)
    finally:
        store.close()
    artifacts.restore(backup, restored)
    restored_store = Store(restored)
    try:
        assert (restored / "work-items" / wid / "tasks.md").read_text() == original_body
        assert restored_store.one("SELECT COUNT(*) n FROM tasks")["n"] > 0
    finally:
        restored_store.close()


def test_agent_progress_is_bound_to_active_attempt(tmp_path):
    _, store, ctrl, wid = planned(tmp_path)
    try:
        ctrl.approve(wid, "greg")
        claim = ctrl.claim_job("worker-a", ["native", "fake"])
        assert claim["job"]["kind"] == "implement"
        task = next(
            task
            for task in ctrl.tasks_view(wid)["tasks"]
            if task["owner_kind"] == "agent" and task["job_id"] == claim["job"]["id"]
        )
        result = ctrl.record_task_progress(
            claim["attempt_id"],
            task["id"],
            "task_started",
            {"summary": "Writing the change"},
            "worker-a",
            "event-1",
        )
        assert result["accepted"] is True
        updated = next(
            t for t in ctrl.tasks_view(wid)["tasks"] if t["id"] == task["id"]
        )
        assert updated["state"] == "active"
        with pytest.raises(ControllerError, match="not bound"):
            ctrl.record_task_progress(
                claim["attempt_id"],
                next(
                    t["id"]
                    for t in ctrl.tasks_view(wid)["tasks"]
                    if t["owner_kind"] == "human"
                ),
                "task_started",
                {},
                "worker-a",
                "event-2",
            )
    finally:
        store.close()


def test_late_required_instruction_cannot_be_lost_at_acceptance(tmp_path):
    cfg, store, ctrl, wid = planned(tmp_path)
    try:
        ctrl.approve(wid, "greg")
        Worker(cfg, store, ctrl, "worker-a").run_loop(max_jobs=10)
        root = ctrl._task_root(wid)
        candidate = json.loads(root["candidate_commits"])[root["repo"]]["commit"]
        for gate in json.loads(root["snapshot_json"])["manual_acceptance"]:
            ctrl.record_manual_gate(
                wid, gate, candidate, "greg", "Verified exact candidate"
            )
        revision = ctrl.task_revision(wid)
        added = ctrl.add_task(
            wid,
            "Also cover the late reconnect request",
            "agent",
            "greg",
            revision,
            idempotency_key="late-request",
        )
        event_id = store.one(
            "SELECT id FROM task_events WHERE task_id=?", (added["task_id"],)
        )["id"]
        criterion = next(iter(ctrl._criteria(root)))
        ctrl.apply_supervisor_response(
            wid,
            {
                "schema_version": 1,
                "root_id": wid,
                "base_revision": added["revision"],
                "processed_event_ids": [event_id],
                "operations": [
                    {
                        "op": "admit_task",
                        "task_id": added["task_id"],
                        "criterion_ids": [criterion],
                        "source_event_ids": [event_id],
                    }
                ],
                "current_summary": "A late required request is queued.",
                "suggested_next_task_id": added["task_id"],
                "needs_decision": [],
            },
        )
        with pytest.raises(ControllerError, match="required tasks remain incomplete"):
            ctrl.accept(wid, "greg")
        instruction = store.one(
            "SELECT state,attempt_id FROM task_instructions WHERE task_id=?",
            (added["task_id"],),
        )
        assert instruction["state"] == "queued"
        assert instruction["attempt_id"] is None
    finally:
        store.close()


def test_browser_session_requires_csrf_for_mutation(tmp_path):
    cfg, store, ctrl, wid = planned(tmp_path)
    server = serve(ctrl, "127.0.0.1", 0, cfg.worker_token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(
            "POST",
            "/operator/session",
            json.dumps({"token": cfg.operator_token, "actor": "greg"}),
            {"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        assert response.status == 200
        csrf = json.loads(response.read())["csrf"]
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        conn.request("GET", "/operator/dashboard", headers={"Cookie": cookie})
        dashboard = conn.getresponse()
        assert dashboard.status == 200
        assert b"task supervisor" in dashboard.read()
        payload = json.dumps(
            {
                "cmd": "task-add",
                "work_item_id": wid,
                "title": "Browser task",
                "owner_kind": "human",
                "expected_revision": 1,
            }
        )
        conn.request(
            "POST",
            "/operator",
            payload,
            {"Content-Type": "application/json", "Cookie": cookie},
        )
        assert conn.getresponse().status == 403
        conn.request(
            "POST",
            "/operator",
            payload,
            {
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
            },
        )
        accepted = conn.getresponse()
        assert accepted.status == 200
        assert json.loads(accepted.read())["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_structured_supervisor_adapter_requests_schema_and_accounts_for_usage():
    observed = {}

    def send(endpoint, body, key):
        observed.update(endpoint=endpoint, body=body, key=key)
        return {
            "model": "small-flash",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "schema_version": 1,
                                "root_id": "root_1",
                                "base_revision": 3,
                                "processed_event_ids": ["event_1"],
                                "operations": [],
                                "current_summary": "Waiting for a human decision.",
                                "suggested_next_task_id": None,
                                "needs_decision": [
                                    "The request does not match approved scope."
                                ],
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
        }

    result = handle(
        {
            "contract_version": 1,
            "request_id": "request_1",
            "operation": "reconcile_tasks",
            "input": {
                "root_id": "root_1",
                "base_revision": 3,
                "limits": {"max_output_tokens": 800},
            },
        },
        "secret",
        "small-flash",
        "https://provider.example/v1/chat/completions",
        0.30,
        1.20,
        send,
    )
    assert observed["body"]["response_format"]["type"] == "json_schema"
    assert observed["body"]["temperature"] == 0
    assert result["proposal"]["root_id"] == "root_1"
    assert result["evidence"]["usage"]["cost_cents"] == 1
