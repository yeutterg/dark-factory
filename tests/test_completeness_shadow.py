"""Criterion completeness advice is bounded, deduplicated and non-blocking."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.config import ConfigError, IntegrationCommand, validate_raw
from dark_factory.controller import Controller
from dark_factory.execution.packages import identity
from dark_factory.store import Store
from dark_factory.worker import Worker


def configure_shadow(cfg, script):
    cfg.raw["decider"] = {
        "command": "decider",
        "region": "US",
        "completeness_shadow": True,
        "completeness_budget_cents": 5,
        "completeness_model": "jev-test-2026-11-01",
    }
    cfg.commands["decider"] = IntegrationCommand(
        id="decider", command=[sys.executable, str(script)], timeout_seconds=10
    )


def script_for(tmp_path, available=True):
    path = tmp_path / "completeness.py"
    path.write_text(
        "import json, sys\n"
        "req=json.load(sys.stdin)\n"
        + ("sys.exit(2)\n" if not available else "")
        + "answers={k:{'choice':'supported'} for k in req['input']['questions']}\n"
        "print(json.dumps({'contract_version':1,'request_id':req['request_id'],"
        "'ok':True,'operation':'decide','evidence':{'agent':'jev-shadow',"
        "'model':'jev-test-2026-11-01','usage':{'cost_cents':2},'answers':answers}}))\n"
    )
    return path


@pytest.mark.parametrize(
    "available,expected", [(True, "recorded"), (False, "unavailable")]
)
def test_shadow_completeness_never_blocks_independent_review(
    tmp_path, available, expected
):
    cfg = cfg_for(tmp_path / "state")
    cfg.windows = []
    configure_shadow(cfg, script_for(tmp_path, available))
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        root_id = ctrl.intake_issues(
            [{"repo": cfg.repositories[0], "number": 1, "title": "Fixture", "body": ""}]
        )[0]
        ctrl.start_planning(root_id)
        worker = Worker(cfg, store, ctrl, "shadow-worker")
        worker.run_loop(2)
        ctrl.approve(root_id, "human")
        worker.run_loop(2)  # implementation and canonical check

        review = store.one(
            "SELECT * FROM jobs WHERE work_item_id=? AND kind='review' AND status='ready'",
            (root_id,),
        )
        assert review
        result = ctrl.completeness_tick()
        assert result["status"] == expected
        assert ctrl.completeness_tick() is None  # evidence-bound retry deduplication
        assert (
            store.one("SELECT status FROM jobs WHERE id=?", (review["id"],))[0]
            == "ready"
        )

        assessment = store.one("SELECT * FROM completeness_assessments")
        assert assessment["model"] == ("jev-test-2026-11-01" if available else "")
        expected_cost = 2 if available else 5
        assert assessment["cost_cents"] == expected_cost
        assert (
            store.one(
                "SELECT amount_cents FROM costs WHERE source='completeness-shadow'"
            )[0]
            == expected_cost
        )
        assert worker.run_once()  # independent reviewer still owns the gate
        assert (
            store.one("SELECT status FROM work_items WHERE id=?", (root_id,))[0]
            == "awaiting_review"
        )
        report = ctrl.completeness_report()
        assert report["runs"] == 1 and report["cost_cents"] == expected_cost
    finally:
        store.close()


def test_shadow_mode_requires_an_explicit_pinned_model(tmp_path):
    cfg = cfg_for(tmp_path / "state")
    raw = copy.deepcopy(cfg.raw)
    raw["commands"]["decider"] = {"argv": ["true"], "timeout_seconds": 10}
    raw["decider"] = {
        "command": "decider",
        "region": "US",
        "completeness_shadow": True,
        "completeness_budget_cents": 5,
    }
    with pytest.raises(ConfigError, match="completeness_model"):
        validate_raw(raw, cfg.source_path, identity(raw))
