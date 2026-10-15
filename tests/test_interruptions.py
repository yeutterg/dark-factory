"""Interrupted steps resume from saved work when trivial and ask a person otherwise."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory.adapters.decision import jev
from dark_factory.adapters.env.native import process_environment
from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult
from dark_factory.adapters.harness.fake import FakeHarness
from dark_factory.adapters.harness.pi import PiHarness
from dark_factory.config import ConfigError, IntegrationCommand, parse_toml_bytes
from dark_factory.contracts import ContractError, make_request
from dark_factory.controller import Controller, ControllerError
from dark_factory.interruptions import categorize, decide, is_interruption
from dark_factory.store import Store
from dark_factory.worker import Worker


@pytest.mark.parametrize(
    "error,timed_out,category",
    [
        ("worker interrupted; workspace retained", True, "worker_lost"),
        ("durable dispatch failed: storage", False, "engine"),
        ("provider returned HTTP 429: rate limited", False, "provider"),
        ("upstream 503 Service Unavailable", False, "provider"),
        ("invalid role result: missing criteria", False, "invalid_output"),
        ("route credential unavailable: KEY", False, "credentials"),
        ("", True, "time_limit"),
        ("something odd", False, "unknown"),
    ],
)
def test_causes_are_categorized(error, timed_out, category):
    assert categorize(error, timed_out) == category


@pytest.mark.parametrize(
    "error",
    [
        "approved inputs changed during attempt",
        "protected or out-of-scope changes: ['x']",
        "trusted command binding changed",
        "durable dispatch failed: inputs_changed",
    ],
)
def test_policy_failures_are_never_interruptions(error):
    assert not is_interruption(error, False, "execution")


def test_verdict_combines_rules_advice_and_hard_limits():
    assert decide("provider", 0, 1, []).action == "resume"
    assert decide("time_limit", 0, 1, []).action == "ask"
    # Confident advice can move either way; unconfident advice is ignored.
    assert (
        decide("time_limit", 0, 1, [], {"decision": "resume", "confidence": 0.9}).action
        == "resume"
    )
    asked = decide("provider", 0, 1, [], {"decision": "stop", "confidence": 0.95})
    assert (asked.action, asked.recommendation) == ("ask", "stop")
    assert (
        decide("provider", 0, 1, [], {"decision": "ask", "confidence": 0.6}).action
        == "resume"
    )
    # Code limits always win.
    assert decide("provider", 1, 1, []).action == "ask"
    blocked = decide(
        "provider", 0, 1, ["no budget"], {"decision": "resume", "confidence": 1.0}
    )
    assert (blocked.action, blocked.blockers) == ("ask", ["no budget"])


class FlakyCoder(FakeHarness):
    """Writes partial work, then fails like a provider outage; succeeds when resumed."""

    calls: list[str] = []

    def start(self, prompt, workspace, extra=None):
        role = (extra or {}).get("role")
        if role != "coder":
            yield from super().start(prompt, workspace, extra)
            return
        FlakyCoder.calls.append(prompt)
        if len(FlakyCoder.calls) == 1:
            (Path(workspace) / "message.py").write_text("# partial progress\n")
            yield HarnessEvent("text", "working")
            self._last = HarnessResult(
                False, "", {}, "s1", cost_cents=1, error="HTTP 503 overloaded"
            )
            return
        assert (Path(workspace) / "message.py").read_text() == "# partial progress\n"
        yield from super().start(prompt, workspace, extra)


@pytest.fixture
def loop(tmp_path, monkeypatch, request):
    cfg = cfg_for(tmp_path / "state")
    cfg.windows = []
    marker = request.node.get_closest_marker("decider")
    if marker:
        # The decider is approved policy: it is configured before planning.
        use_decider(cfg, write_decider(tmp_path, marker.args[0]))
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    FlakyCoder.calls = []
    monkeypatch.setattr(Worker, "_harness", lambda self, route: FlakyCoder())
    wid = ctrl.intake_issues(
        [{"repo": cfg.repositories[0], "number": 1, "title": "Fixture", "body": ""}]
    )[0]
    ctrl.start_planning(wid)
    worker = Worker(cfg, store, ctrl)
    worker.run_loop(2)
    ctrl.approve(wid, "human")
    yield cfg, store, ctrl, wid, worker
    store.close()


def test_provider_outage_resumes_from_saved_work(loop):
    cfg, store, ctrl, wid, worker = loop
    worker.run_loop()
    assert store.one("SELECT status FROM work_items")[0] == "awaiting_review"
    row = store.one("SELECT * FROM interruptions")
    assert (row["category"], row["outcome"]) == ("provider", "auto_resumed")
    resumed = store.one(
        "SELECT inputs_json FROM attempts WHERE id=?", (row["resumed_by_attempt"],)
    )
    assert json.loads(resumed[0])["resume"]["attempt"] == row["attempt_id"]
    assert "resumed_after_interruption" in FlakyCoder.calls[1]
    assert (
        cfg.state_dir / "archives" / row["resumed_by_attempt"] / "result.json"
    ).exists()


def write_decider(tmp_path, body):
    script = tmp_path / "decider.py"
    script.write_text(
        "import json, sys\n"
        "req = json.load(sys.stdin)\n"
        f"body = {body!r}\n"
        "if body is None: sys.exit(1)\n"
        "print(json.dumps({'contract_version': 1, 'request_id': req['request_id'],"
        " 'ok': True, 'operation': 'decide', 'evidence': {'agent': 'test-decider',"
        " 'answers': {'next': body}}}))\n"
    )
    return script


def use_decider(cfg, script):
    cfg.raw["decider"] = {"region": "US", "min_confidence": 0.8}
    cfg.commands["decider"] = IntegrationCommand(
        id="decider", command=[sys.executable, str(script)], timeout_seconds=10
    )


@pytest.mark.decider({"choice": "ask", "confidence": 0.93})
def test_decider_can_hold_a_trivial_interruption_for_a_person(loop):
    cfg, store, ctrl, wid, worker = loop
    worker.run_loop()
    assert store.one("SELECT status FROM work_items")[0] == "interrupted"
    verdict = json.loads(store.one("SELECT verdict_json FROM interruptions")[0])
    assert verdict["reasons"][-1] == "test-decider: ask (0.93)"
    assert ctrl.continuation(wid)["next_action"]["kind"] == "human_resume"
    with pytest.raises(ControllerError):
        ctrl.continue_work(wid, "")
    ctrl.continue_work(wid, "human")
    worker.run_loop()
    assert store.one("SELECT status FROM work_items")[0] == "awaiting_review"
    assert store.one("SELECT outcome FROM interruptions")[0] == "resumed"


@pytest.mark.decider(None)
def test_failed_decider_falls_back_to_rules(loop):
    cfg, store, ctrl, wid, worker = loop
    worker.run_loop()
    assert store.one("SELECT status FROM work_items")[0] == "awaiting_review"
    assert store.one("SELECT 1 FROM audit WHERE action='decider.unavailable'")


@pytest.mark.decider({"choice": "stop", "confidence": 0.99})
def test_person_can_stop_and_limits_cannot_be_resumed_past(loop):
    cfg, store, ctrl, wid, worker = loop
    worker.run_loop()
    assert store.one("SELECT status FROM work_items")[0] == "interrupted"
    store.execute(
        "UPDATE work_items SET spending_ceiling_cents=spent_cents WHERE id=?", (wid,)
    )
    with pytest.raises(ControllerError, match="budget"):
        ctrl.continue_work(wid, "human")
    ctrl.stop_work(wid, "human", "provider keeps failing")
    assert store.one("SELECT status FROM work_items")[0] == "failed"
    assert store.one("SELECT outcome FROM interruptions")[0] == "stopped"


def test_jev_adapter_asks_a_typed_choice_and_checks_the_answer():
    sent = {}

    def send(body, key):
        sent.update(body=body, key=key)
        return {
            "model": "jev-1",
            "answers": {
                "next": {
                    "type": "choice",
                    "choice": "resume",
                    "confidence": 0.91,
                    "probabilities": {"resume": 0.91, "ask": 0.06, "stop": 0.03},
                }
            },
        }

    request = make_request(
        "decide",
        {
            "state": {"cause": "provider"},
            "questions": {
                "next": {
                    "type": "choice",
                    "instructions": "What next?",
                    "options": {"resume": "a", "ask": "b", "stop": "c"},
                }
            },
        },
        "r1",
    )
    result = jev.handle(request, "key", send=send)
    assert sent["key"] == "key"
    assert sent["body"]["questions"]["next"]["criteria"] == {
        "resume": "a",
        "ask": "b",
        "stop": "c",
    }
    assert result["evidence"]["answers"]["next"]["choice"] == "resume"
    assert result["evidence"]["agent"] == "jev"
    with pytest.raises(ContractError):
        jev.handle(request, "key", send=lambda body, key: {"answers": {}})


@pytest.mark.parametrize(
    "extra,message",
    [
        ("\n[decider]\nmin_confidence = 0.9\n", "region"),
        ('\n[decider]\nregion = "CN"\n', "blocked"),
        ('\n[decider]\nregion = "US"\n', "command"),
        ("\n[continuation]\nmax_resumes = 4\n", "resumes"),
    ],
)
def test_decider_and_resume_limits_are_validated(extra, message):
    text = Path("examples/factory.toml").read_text() + extra
    with pytest.raises(ConfigError, match=message):
        parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())


def test_pi_continues_the_interrupted_session_as_a_fork(tmp_path):
    binary = tmp_path / "pi-fixture"
    invocation = tmp_path / "invocation.json"
    binary.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(invocation)!r}, 'w').write(json.dumps(sys.argv))\n"
    )
    binary.chmod(0o700)
    runtime = tmp_path / "runtime"
    harness = PiHarness(str(binary))
    list(
        harness.start(
            "brief",
            tmp_path,
            {
                "runtime": str(runtime),
                "env": process_environment(runtime),
                "prefix": ["/usr/bin/env"],
                "route": {
                    "model": "m",
                    "provider": "p",
                    "endpoint": "http://127.0.0.1:1",
                    "auth_env": "DUMMY",
                    "evidence_kind": "simulation",
                },
                "role": "coder",
                "timeout_seconds": 5,
                "max_output_bytes": 100000,
                "budget_cents": 10,
                "resume_session": '{"type": "session", "id": "s1"}\n',
            },
        )
    )
    argv = json.loads(invocation.read_text())
    forked = Path(argv[argv.index("--fork") + 1])
    assert forked.read_text() == '{"type": "session", "id": "s1"}\n'
    assert "--no-mcp" in argv
