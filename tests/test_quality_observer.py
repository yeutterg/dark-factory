from __future__ import annotations

from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.adapters.decision.quality_observer import handle
from dark_factory.config import IntegrationCommand
from dark_factory.contracts import ContractError, make_request
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store, new_id, now_iso


def quality_controller(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.raw["operators"] = {"humans": ["greg"]}
    cfg.raw["quality_observer"] = {
        "command": "quality-observer",
        "repositories": cfg.repositories,
        "model": "small-observer-v1",
        "provider": "fixture",
        "region": "US",
        "pricing_evidence": "fixture",
        "budget_cents": 5,
        "max_input_tokens": 4000,
        "max_output_tokens": 500,
    }
    cfg.raw["improvement"] = {
        "allowed_paths": ["message.py"],
        "max_files": 1,
        "max_changed_lines": 10,
    }
    cfg.commands["quality-observer"] = IntegrationCommand(
        "quality-observer", cfg.commands["tracking"].command
    )
    store = Store(cfg.state_dir)
    controller = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    root = controller.intake_issues(
        [
            {
                "repo": cfg.repositories[0],
                "number": 71,
                "title": "Observe agent work",
                "body": "fixture",
                "is_root": True,
            }
        ]
    )[0]
    store.execute(
        """INSERT INTO jobs(id,work_item_id,kind,status,spec_json,depends_on)
        VALUES (?,?,?,?,?,?)""",
        ("job_quality", root, "test", "failed", "{}", "[]"),
    )
    return controller, store, root


def add_failed_attempt(store, suffix, job="job_quality"):
    attempt = "att_" + suffix
    store.execute(
        """INSERT INTO attempts
        (id,job_id,status,ended_at,error,result_json)
        VALUES (?,?,?,?,?,?)""",
        (attempt, job, "failed", now_iso(), "assertion failed", "{}"),
    )
    return "attempt:" + attempt


def result(output):
    return {
        "evidence": {
            "model": "small-observer-v1",
            "output": output,
            "usage": {"cost_cents": 1},
        }
    }


def test_observes_then_requires_two_independent_examples_for_pattern(tmp_path):
    controller, store, _ = quality_controller(tmp_path)
    first_evidence = add_failed_attempt(store, "one")
    second_root = controller.intake_issues(
        [
            {
                "repo": controller.cfg.repositories[0],
                "number": 72,
                "title": "Observe another agent change",
                "body": "fixture",
                "is_root": True,
            }
        ]
    )[0]
    store.execute(
        """INSERT INTO jobs(id,work_item_id,kind,status,spec_json,depends_on)
        VALUES (?,?,?,?,?,?)""",
        ("job_quality_two", second_root, "test", "failed", "{}", "[]"),
    )

    def fake_run(command, operation, state, request_id, timeout):
        if operation == "observe_quality":
            evidence_id = state["evidence"][0]["id"]
            return result(
                {
                    "observations": [
                        {
                            "title": "Verification stopped at the first failure",
                            "evidence_ids": [evidence_id],
                            "what_happened": "The agent left a failed verification attempt.",
                            "why_it_matters": "The same gap can escape future changes.",
                            "category": "verification",
                            "similar_observation_ids": [],
                            "confidence": "high",
                        }
                    ],
                    "inaccessible_evidence": [],
                }
            )
        ids = [item["id"] for item in state["observations"]]
        return result(
            {
                "patterns": [
                    {
                        "name": "Verification failures are not recovered",
                        "supporting_observation_ids": ids,
                        "shared_root_cause": "The agent instruction omits recovery guidance.",
                        "why_repeating": "Agents see a failed command but no explicit next action.",
                        "prevention_layer": "agent instruction",
                        "smallest_systemic_change": "Clarify the verification recovery instruction.",
                        "verification": "A fixture responds to a failed check with recovery.",
                        "confidence": "high",
                        "uncertainty": "The failures may have different immediate causes.",
                    }
                ],
                "top_interventions": ["Require passing verification evidence"],
                "isolated_observation_ids": [],
                "additional_evidence_needed": [],
            }
        )

    controller.runner.run_json = fake_run
    first = controller.quality_tick(force=True)
    assert first["runs"][0]["new"] == 1
    assert store.one("SELECT COUNT(*) n FROM quality_patterns")["n"] == 0
    assert (
        first_evidence
        in controller._quality_path(controller.cfg.repositories[0])
        .joinpath("queue.md")
        .read_text()
    )

    add_failed_attempt(store, "two", "job_quality_two")
    second = controller.quality_tick(force=True)
    assert [run["kind"] for run in second["runs"]] == ["observer", "patterns"]
    pattern = store.one("SELECT * FROM quality_patterns")
    assert pattern["prevention_layer"] == "agent instruction"
    report = controller._quality_path(controller.cfg.repositories[0]).joinpath(
        "pattern-report.md"
    )
    assert "Verification failures are not recovered" in report.read_text()
    status = controller.quality_status(controller.cfg.repositories[0])
    assert (
        status[controller.cfg.repositories[0]]["pending_patterns"][0]["id"]
        == pattern["id"]
    )

    approved = controller.quality_pattern_approve(pattern["id"], "greg")
    assert approved["improvement_root"]
    improvement = store.one(
        "SELECT origin FROM work_items WHERE id=?", (approved["improvement_root"],)
    )
    assert improvement["origin"].startswith("improvement:quality-pattern:")
    assert (
        store.one("SELECT approved_by FROM quality_patterns")["approved_by"] == "greg"
    )
    assert not controller.quality_status(controller.cfg.repositories[0])[
        controller.cfg.repositories[0]
    ]["pending_patterns"]
    with pytest.raises(ControllerError, match="configured human"):
        controller.quality_pattern_approve(pattern["id"], "agent")
    store.close()


def test_rejects_observation_without_supplied_evidence(tmp_path):
    controller, store, _ = quality_controller(tmp_path)
    add_failed_attempt(store, new_id("unknown"))

    def fake_run(command, operation, state, request_id, timeout):
        return result(
            {
                "observations": [
                    {
                        "title": "Invented",
                        "evidence_ids": ["attempt:not-supplied"],
                        "what_happened": "Not supported.",
                        "why_it_matters": "Not supported.",
                        "category": "testing",
                        "similar_observation_ids": [],
                        "confidence": "high",
                    }
                ],
                "inaccessible_evidence": [],
            }
        )

    controller.runner.run_json = fake_run
    run = controller.quality_tick(force=True)["runs"][0]
    assert run["new"] == 0
    assert store.one("SELECT COUNT(*) n FROM quality_observations")["n"] == 0
    store.close()


def test_overflow_evidence_remains_eligible_for_the_next_batch(tmp_path):
    controller, store, _ = quality_controller(tmp_path)
    for index in range(3):
        evidence_id = add_failed_attempt(store, f"overflow_{index}")
        store.execute(
            "UPDATE attempts SET error=? WHERE id=?",
            ("failure detail " * 400, evidence_id.removeprefix("attempt:")),
        )

    sent_batches = []

    def fake_run(command, operation, state, request_id, timeout):
        ids = [item["id"] for item in state["evidence"]]
        sent_batches.append(ids)
        return result({"observations": [], "inaccessible_evidence": []})

    controller.runner.run_json = fake_run
    controller._quality_observe(controller.cfg.repositories[0], "test")
    first_seen = {
        row["evidence_id"] for row in store.query("SELECT * FROM quality_evidence_seen")
    }
    assert first_seen == set(sent_batches[0])
    assert len(first_seen) < 3

    controller._quality_observe(controller.cfg.repositories[0], "test")
    assert set(sent_batches[1]).isdisjoint(first_seen)
    assert store.one("SELECT COUNT(*) n FROM quality_evidence_seen")["n"] > len(
        first_seen
    )
    store.close()


def test_failed_provider_call_is_accounted_and_backed_off(tmp_path):
    controller, store, _ = quality_controller(tmp_path)
    add_failed_attempt(store, "provider_failure")

    def wrong_model(command, operation, state, request_id, timeout):
        response = result({"observations": [], "inaccessible_evidence": []})
        response["evidence"]["model"] = "unexpected-model"
        return response

    controller.runner.run_json = wrong_model
    failed = controller.quality_tick(force=True)["runs"][0]
    assert failed["state"] == "failed"
    run = store.one("SELECT * FROM quality_runs")
    assert run["status"] == "failed" and run["cost_cents"] == 1
    assert controller.quality_tick()["runs"] == []
    assert store.one("SELECT COUNT(*) n FROM quality_evidence_seen")["n"] == 0
    store.close()


def test_adapter_uses_strict_schema_and_refuses_an_unsafe_budget():
    request = make_request(
        "observe_quality",
        {
            "evidence": [{"id": "attempt:one", "kind": "failure"}],
            "limits": {
                "max_input_tokens": 4000,
                "max_output_tokens": 500,
                "budget_cents": 5,
            },
        },
        "quality-fixture",
    )

    def send(endpoint, body, key):
        assert body["response_format"]["json_schema"]["strict"] is True
        return {
            "model": "small-observer-v1",
            "choices": [
                {
                    "message": {
                        "content": '{"observations":[],"inaccessible_evidence":[]}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    response = handle(
        request,
        "key",
        "small-observer-v1",
        "https://provider.example/v1/chat/completions",
        0.3,
        1.2,
        send,
    )
    assert response["ok"] is True

    request["input"]["limits"]["budget_cents"] = 1
    with pytest.raises(ContractError, match="token limits exceed"):
        handle(
            request,
            "key",
            "small-observer-v1",
            "https://provider.example/v1/chat/completions",
            100,
            100,
            send,
        )
