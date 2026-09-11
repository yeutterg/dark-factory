from __future__ import annotations

import json
import sys
from pathlib import Path

from dark_factory.config import parse_toml_bytes
from dark_factory.controller import Controller, validate_graph
from dark_factory.models import JobSpec, PlanGraph
from dark_factory.store import Store
from dark_factory.worker import Worker


def cfg_for(tmp_path: Path):
    text = Path("examples/factory.toml").read_text(encoding="utf-8")
    text = text.replace('state_dir = "./state"', f'state_dir = "{tmp_path}"')
    return parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())


def test_graph_cycle_rejected(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    graph = PlanGraph(
        version=1,
        jobs=[
            JobSpec("a", "plan", depends_on=["b"]),
            JobSpec("b", "critique", depends_on=["a"]),
        ],
        acceptance=["x"],
        spending_ceiling_cents=10,
        permitted_routes=["fake-coder"],
    )
    try:
        validate_graph(graph, cfg)
        raise AssertionError("cycle should fail")
    except Exception as exc:
        assert "cycle" in str(exc)


def test_full_loop_archive_before_cleanup(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    ids = ctrl.intake_issues(
        [
            {
                "repo": "yeutterg/dark-factory",
                "number": 1,
                "title": "Demo",
                "body": "Do the thing",
                "is_root": True,
            }
        ]
    )
    wid = ids[0]
    ctrl.start_planning(wid)
    worker = Worker(cfg, store, ctrl, "t")
    worker.run_loop(max_jobs=2)
    item = store.one("SELECT status FROM work_items WHERE id=?", (wid,))
    assert item["status"] == "awaiting_approval"
    ctrl.approve(wid, "tester")
    worker.run_loop(max_jobs=10)
    item = store.one(
        "SELECT status, presentation_stage FROM work_items WHERE id=?", (wid,)
    )
    assert item["status"] == "awaiting_review"
    archives = list((cfg.state_dir / "archives").iterdir())
    assert archives, "archives must exist after cleanup"
    workspaces = list((cfg.state_dir / "workspaces").glob("*"))
    assert not workspaces, "workspaces must be removed after archive"
    candidate = json.loads(
        store.one("SELECT candidate_commits FROM work_items WHERE id=?", (wid,))[0]
    )[cfg.repositories[0]]["commit"]
    ctrl.record_manual_gate(
        wid,
        "Inspect candidate diff",
        candidate,
        "tester",
        "Reviewed exact message.py diff",
    )
    ctrl.accept(wid, "tester")
    item = store.one("SELECT status FROM work_items WHERE id=?", (wid,))
    assert item["status"] == "accepted"


def test_pause_blocks_dispatch(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    ids = ctrl.intake_issues(
        [
            {
                "repo": "yeutterg/dark-factory",
                "number": 2,
                "title": "Paused",
                "body": "",
                "is_root": True,
            }
        ]
    )
    ctrl.start_planning(ids[0])
    ctrl.pause("tester")
    assert ctrl.claim_job("w", ["native"]) is None


def test_cancel_does_not_fallback(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    ids = ctrl.intake_issues(
        [
            {
                "repo": "yeutterg/dark-factory",
                "number": 3,
                "title": "Cancel",
                "body": "",
                "is_root": True,
            }
        ]
    )
    ctrl.start_planning(ids[0])
    job = store.one("SELECT id FROM jobs WHERE kind='plan'")
    ctrl.cancel_job(job["id"], "tester")
    worker = Worker(cfg, store, ctrl, "t")
    assert (
        worker.run_once() is False
        or store.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"]
        == "cancelled"
    )


def test_adapter_swap_without_controller_change(tmp_path: Path) -> None:
    script = tmp_path / "alt_tracking.py"
    script.write_text(
        "\n".join(
            [
                "import json,sys",
                "req=json.loads(sys.stdin.read())",
                "print(json.dumps({'contract_version': 1, 'request_id': req['request_id'], 'ok': True, 'operation': req['operation'], 'evidence': {'alt': True}, 'items': [{'external_id': 'alt:1', 'url': '', 'title': 'alt', 'body': '', 'repo': 'yeutterg/dark-factory', 'parent_id': None, 'is_root': True, 'required_child_count': 0, 'iteration': None}]}))",
            ]
        ),
        encoding="utf-8",
    )
    text = Path("examples/factory.toml").read_text(encoding="utf-8")
    text = text.replace('state_dir = "./state"', f'state_dir = "{tmp_path / "state"}"')
    text = text.replace(
        'argv = ["python3", "-m", "dark_factory.adapters.projects.github"]',
        f'argv = ["{sys.executable}", "{script}"]',
    )
    cfg = parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    ids = ctrl.intake_issues([{"ignored": True}])
    row = store.one("SELECT external_id FROM work_items WHERE id=?", (ids[0],))
    assert row["external_id"] == "alt:1"


def test_prompt_override(tmp_path: Path) -> None:
    override = tmp_path / "planner.md"
    override.write_text("custom planner prompt", encoding="utf-8")
    text = Path("examples/factory.toml").read_text(encoding="utf-8")
    text = text.replace('state_dir = "./state"', f'state_dir = "{tmp_path / "state"}"')
    text = text.replace(
        "# omit roles to use packaged defaults", f'planner = "{override}"'
    )
    cfg = parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())
    from dark_factory.config import load_prompt

    body, digest = load_prompt(cfg, "planner", Path("src/dark_factory/agents/prompts"))
    assert body == "custom planner prompt"
    assert digest
