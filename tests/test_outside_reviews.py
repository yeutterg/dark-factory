"""GitHub Projects intake and outside PR reviews as candidate-bound evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.adapters.git import github as code_host
from dark_factory.adapters.projects import github as tracking
from dark_factory.contracts import ContractError, make_request
from dark_factory.control import planning
from dark_factory.control.review import assess_external
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker

SHA = "a" * 40


def issue_node(number, repo="acme/tool", state="OPEN", parent=None, children=0):
    return {
        "number": number,
        "title": f"Issue {number}",
        "url": f"https://github.com/{repo}/issues/{number}",
        "state": state,
        "repository": {"nameWithOwner": repo},
        "parent": parent,
        "subIssues": {"totalCount": children},
    }


def test_project_discovery_merges_boards_with_labeled_issues(monkeypatch):
    calls = []

    def fake(path, method="GET", payload=None):
        calls.append(payload["variables"])
        if "number" in payload["variables"] and "owner" in payload["variables"]:
            if "theme" in payload["variables"]:
                return {
                    "data": {
                        "repositoryOwner": {
                            "projectV2": {
                                "title": "Roadmap",
                                "url": "https://github.com/orgs/acme/projects/3",
                                "items": {
                                    "nodes": [
                                        {
                                            "status": {"name": "Todo"},
                                            "theme": {"name": "Reliability"},
                                            "content": issue_node(7),
                                        },
                                        {
                                            "status": None,
                                            "theme": None,
                                            "content": issue_node(8, state="CLOSED"),
                                        },
                                        {
                                            "status": None,
                                            "theme": None,
                                            "content": issue_node(9, repo="other/x"),
                                        },
                                        {"status": None, "theme": None, "content": {}},
                                    ],
                                    "pageInfo": {"hasNextPage": False},
                                },
                            }
                        }
                    }
                }
        return {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [issue_node(7), issue_node(11)],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        }

    monkeypatch.setattr(tracking, "api", fake)
    result = tracking.handle(
        make_request(
            "list_candidate_issues",
            {"repositories": ["acme/tool"], "label": "factory", "limit": 10},
            "discover",
        ),
        ["acme/3"],
        "Theme",
    )
    assert [i["number"] for i in result["items"]] == [7, 11]
    assert result["items"][0]["project"] == {
        "id": "acme/3",
        "title": "Roadmap",
        "url": "https://github.com/orgs/acme/projects/3",
        "status": "Todo",
        "theme": "Reliability",
    }
    assert result["items"][1]["project"] is None
    assert result["evidence"]["projects"] == ["acme/3"]
    assert calls[0] == {"owner": "acme", "number": 3, "theme": "Theme"}


def test_unreadable_project_fails_closed(monkeypatch):
    monkeypatch.setattr(
        tracking,
        "api",
        lambda *a, **k: {"data": {"repositoryOwner": {"projectV2": None}}},
    )
    with pytest.raises(ContractError, match="read:project"):
        tracking.project_issues("acme/3", "Theme", ["acme/tool"])
    with pytest.raises(ContractError):
        tracking.project_reference("acme/zero")


def test_inspect_pr_reads_reviews_and_comments(monkeypatch):
    responses = {
        "repos/acme/tool/pulls/5": {
            "head": {"sha": SHA},
            "base": {"sha": "b" * 40, "ref": "main"},
            "html_url": "https://github.com/acme/tool/pull/5",
            "state": "open",
            "merged": False,
            "mergeable": True,
        },
        f"repos/acme/tool/commits/{SHA}/check-runs?per_page=100": {
            "total_count": 1,
            "check_runs": [{"name": "Cursor Bugbot", "conclusion": "success"}],
        },
        f"repos/acme/tool/commits/{SHA}/status?per_page=100": {
            "total_count": 0,
            "statuses": [],
        },
        "repos/acme/tool/pulls/5/reviews?per_page=100": [
            {
                "user": {"login": "devin-ai-integration[bot]"},
                "state": "APPROVED",
                "commit_id": SHA,
                "submitted_at": "2026-10-15T08:00:00Z",
                "body": "Looks good",
                "html_url": "https://github.com/acme/tool/pull/5#r1",
            }
        ],
        "repos/acme/tool/pulls/5/comments?per_page=100": [
            {
                "user": {"login": "cursor[bot]"},
                "path": "app.py",
                "line": 12,
                "commit_id": SHA,
                "body": "Possible None dereference",
            }
        ],
    }
    monkeypatch.setattr(code_host, "api", lambda path, *a: responses[path])
    evidence = code_host.handle(
        make_request("inspect_pr", {"repo": "acme/tool", "number": 5}, "read")
    )["evidence"]
    assert evidence["reviews"][0]["author"] == "devin-ai-integration[bot]"
    assert evidence["reviews"][0]["state"] == "APPROVED"
    assert evidence["review_comments"][0] == {
        "author": "cursor[bot]",
        "path": "app.py",
        "line": 12,
        "commit_id": SHA,
        "created_at": "",
        "body": "Possible None dereference",
        "url": "",
    }


def test_outside_evidence_only_counts_on_the_exact_candidate():
    evidence = {
        "head_sha": SHA,
        "checks": [
            {
                "name": "Cursor Bugbot",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2026-10-15T07:00:00Z",
            },
            {
                "name": "Cursor Bugbot",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-10-15T08:00:00Z",
            },
        ],
        "statuses": [{"context": "ci/build", "state": "failure"}],
        "reviews": [
            {"author": "Devin", "state": "APPROVED", "commit_id": "c" * 40},
            {"author": "devin", "state": "COMMENTED", "commit_id": SHA},
            {"author": "reviewer", "state": "APPROVED", "commit_id": SHA},
        ],
    }
    results = {
        r["name"]: r["passed"]
        for r in assess_external(
            ["Cursor Bugbot", "ci/build", "Devin", "reviewer", "missing"],
            evidence,
            SHA,
        )
    }
    assert results == {
        "Cursor Bugbot": True,
        "ci/build": False,
        "Devin": False,  # Its approval was for another commit; a comment never passes.
        "reviewer": True,
        "missing": False,
    }
    assert not assess_external(["Cursor Bugbot"], evidence, "d" * 40)[0]["passed"]


def test_acceptance_waits_for_required_outside_reviews(tmp_path, monkeypatch):
    original = planning.fixture
    monkeypatch.setattr(
        planning,
        "fixture",
        lambda root: {**original(root), "external_acceptance": ["Cursor Bugbot"]},
    )
    cfg = cfg_for(tmp_path / "state")
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    try:
        wid = ctrl.intake_issues(
            [
                {
                    "repo": "yeutterg/dark-factory",
                    "number": 1,
                    "title": "Demo",
                    "body": "Do the thing",
                    "is_root": True,
                }
            ]
        )[0]
        ctrl.start_planning(wid)
        worker = Worker(cfg, store, ctrl, "t")
        worker.run_loop(max_jobs=2)
        ctrl.approve(wid, "tester")
        worker.run_loop(max_jobs=10)
        row = store.one("SELECT * FROM work_items WHERE id=?", (wid,))
        assert row["status"] == "awaiting_review"
        candidate = json.loads(row["candidate_commits"])[row["repo"]]["commit"]
        ctrl.record_manual_gate(
            wid, "Inspect candidate diff", candidate, "tester", "Read the diff"
        )
        packet = ctrl.review_packet(wid)
        assert packet["external_remaining"] == ["Cursor Bugbot"]
        assert "external:Cursor Bugbot" in packet["missing_evidence"]
        with pytest.raises(ControllerError, match="outside review missing"):
            ctrl.accept(wid, "tester")
        with pytest.raises(ControllerError, match="name the pull request"):
            ctrl.read_pr_evidence(wid, None, "tester")

        head = {"sha": "f" * 40}
        requests = []

        def inspect(command, operation, payload, request_id, timeout):
            requests.append((operation, payload))
            return {
                "ok": True,
                "evidence": {
                    "head_sha": head["sha"],
                    "url": "https://github.com/yeutterg/dark-factory/pull/42",
                    "checks": [
                        {
                            "name": "Cursor Bugbot",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ],
                    "statuses": [],
                    "reviews": [],
                    "review_comments": [
                        {"author": "cursor[bot]", "path": "message.py", "body": "ok"}
                    ],
                },
            }

        monkeypatch.setattr(ctrl.runner, "run_json", inspect)
        with pytest.raises(ControllerError, match="not the reviewed candidate"):
            ctrl.read_pr_evidence(wid, 42, "tester")
        head["sha"] = candidate
        result = ctrl.read_pr_evidence(wid, 42, "tester")
        assert result["remaining"] == []
        assert requests[-1] == (
            "inspect_pr",
            {"repo": "yeutterg/dark-factory", "number": 42},
        )
        packet = ctrl.review_packet(wid)
        assert packet["external_reviews"]["pr"]["number"] == 42
        assert packet["external_reviews"]["comments"][0]["author"] == "cursor[bot]"
        ctrl.accept(wid, "tester")
        assert (
            store.one("SELECT status FROM work_items WHERE id=?", (wid,))["status"]
            == "accepted"
        )
    finally:
        store.close()
