"""A selected child must never become an independently approved outcome."""

from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.adapters.projects import github as tracking
from dark_factory.contracts import ContractError, make_request
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store


@pytest.fixture
def controller(tmp_path):
    cfg = cfg_for(tmp_path / "state")
    cfg.windows = []
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    yield ctrl
    store.close()


def selected(number=1, **metadata):
    return {
        "repo": "yeutterg/dark-factory",
        "number": number,
        "title": "Complete outcome",
        "body": "All acceptance criteria",
        **metadata,
    }


def test_remote_parent_cannot_be_overridden_by_caller(monkeypatch):
    def fake(path, *args):
        if path == "graphql":
            return {
                "data": {
                    "repository": {
                        "issue": {
                            "parent": {
                                "number": 29,
                                "repository": {"nameWithOwner": "acme/coordination"},
                            },
                            "subIssues": {"totalCount": 0},
                        }
                    }
                }
            }
        return {
            "title": "Child",
            "body": "Partial work",
            "html_url": "https://github.com/acme/tool/issues/3",
        }

    monkeypatch.setattr(tracking, "api", fake)
    result = tracking.handle(
        make_request(
            "list_selected_issues",
            {
                "issues": [
                    {
                        "repo": "acme/tool",
                        "number": 3,
                        "is_root": True,
                        "parent_id": None,
                        "required_child_count": 0,
                    }
                ]
            },
            "read",
        )
    )
    assert result["items"][0]["is_root"] is False
    assert result["items"][0]["parent_id"] == "github:acme/coordination:issue:29"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"errors": [{"message": "not authorized"}]},
        {"data": {"repository": {"issue": None}}},
        {"data": {"repository": {"issue": {"subIssues": {"totalCount": 0}}}}},
        {
            "data": {
                "repository": {
                    "issue": {"parent": None, "subIssues": {"totalCount": "0"}}
                }
            }
        },
    ],
)
def test_missing_hierarchy_cannot_be_assumed_root(monkeypatch, response):
    monkeypatch.setattr(tracking, "api", lambda *a: response)
    with pytest.raises(ContractError, match="relationships are incomplete"):
        tracking.relationships("acme/tool", 3)


@pytest.mark.parametrize(
    "metadata,reason",
    [
        (
            {"parent_id": "github:acme/coordination:issue:29", "is_root": True},
            "root outcome",
        ),
        ({"is_root": False}, "root outcome"),
        ({"required_child_count": 2}, "required child issues"),
        ({"required_child_count": "0"}, "non-negative"),
    ],
)
def test_intake_preserves_scope_and_rolls_back_batch(controller, metadata, reason):
    with pytest.raises(ControllerError, match=reason):
        controller.intake_issues([selected(1), selected(2, **metadata)])
    assert not controller.store.query("SELECT * FROM work_items")
    assert not controller.store.query("SELECT * FROM jobs")
    assert not (controller.cfg.state_dir / "inputs").exists()


def test_alternate_tracker_must_report_scope(controller, monkeypatch):
    monkeypatch.setattr(
        controller.runner,
        "run_json",
        lambda *a: {
            "ok": True,
            "items": [{"external_id": "alt:child", "repo": "yeutterg/dark-factory"}],
        },
    )
    with pytest.raises(ControllerError, match="root outcome"):
        controller.intake_issues([selected()])
    assert not controller.store.query("SELECT * FROM work_items")


@pytest.mark.parametrize("boundary", ["plan", "approve", "dispatch", "accept"])
def test_stored_child_cannot_progress(controller, boundary):
    wid = controller.intake_issues([selected()])[0]
    controller.start_planning(wid)
    status = {
        "plan": "selected",
        "approve": "awaiting_approval",
        "dispatch": "planning",
        "accept": "awaiting_review",
    }[boundary]
    controller.store.execute(
        "UPDATE work_items SET parent_id='alt:root', status=? WHERE id=?", (status, wid)
    )
    if boundary == "dispatch":
        assert controller.claim_job("worker", ["native", "fake"]) is None
        assert (
            controller.store.one("SELECT status FROM work_items WHERE id=?", (wid,))[0]
            == "failed"
        )
    else:
        with pytest.raises(ControllerError, match="root outcome"):
            if boundary == "plan":
                controller.start_planning(wid)
            elif boundary == "approve":
                controller.approve(wid, "human")
            else:
                controller.accept(wid, "human")
    assert not controller.store.query("SELECT * FROM attempts")
    assert (
        controller.store.one(
            "SELECT approved_digest FROM work_items WHERE id=?", (wid,)
        )[0]
        is None
    )


def test_stored_child_blocks_its_parent_too(controller):
    root, child = controller.intake_issues([selected(1), selected(2)])
    controller.store.execute(
        "UPDATE work_items SET parent_id=?, is_root=0 WHERE id=?", (root, child)
    )
    with pytest.raises(ControllerError, match="required child issues"):
        controller.start_planning(root)
    assert not controller.store.query("SELECT * FROM jobs")
