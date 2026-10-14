"""Pinned custom-model rates and conservative unknown usage; no paid inference."""

import json
import sys
from pathlib import Path

import pytest

from dark_factory.adapters.env.native import process_environment
from dark_factory.adapters.harness.pi import PiHarness, validate_pricing
from dark_factory.config import ConfigError, parse_toml_bytes

RATES = {"input": 0.30, "output": 1.20, "cache_read": 0.006, "cache_write": 0.0}


def test_price_change_invalidates_approved_dispatch(tmp_path):
    from test_workflow import setup

    from dark_factory.controller import ControllerError

    cfg, store, ctrl, wid, worker = setup(tmp_path)
    try:
        worker.run_loop(2)
        ctrl.approve(wid, "human-tester")
        cfg.routes["fake-coder"].pricing = RATES.copy()
        with pytest.raises(ControllerError, match="changed"):
            worker.run_once()
        assert len(store.query("SELECT * FROM attempts")) == 2
    finally:
        store.close()


@pytest.mark.parametrize(
    "rates",
    [
        {"input": 0.3},
        {**RATES, "input": -1},
        {**RATES, "output": float("nan")},
        {**RATES, "output": True},
        {**RATES, "output": float("inf")},
        {**RATES, "extra": 1},
    ],
)
def test_bad_pricing_is_rejected(rates):
    with pytest.raises(ValueError, match="pricing"):
        validate_pricing(rates, "fixture", True)


def test_live_custom_route_needs_explicit_nonzero_pricing():
    with pytest.raises(ValueError, match="pinned positive pricing"):
        validate_pricing({}, "", True)
    with pytest.raises(ValueError, match="pinned positive pricing"):
        validate_pricing(dict.fromkeys(RATES, 0), "fixture", True)


@pytest.mark.parametrize(
    "table",
    [
        "pricing = { input = 0.3 }",
        "pricing = { input = nan, output = 1.2, cache_read = 0.006, cache_write = 0.0 }",
        "pricing = { input = true, output = 1.2, cache_read = 0.006, cache_write = 0.0 }",
    ],
)
def test_config_rejects_partial_nonfinite_and_boolean_rates(table):
    text = (
        Path("examples/factory.toml")
        .read_text()
        .replace('model = "fake-local"', 'model = "fake-local"\n' + table)
    )
    with pytest.raises(ConfigError, match="pricing"):
        parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())


def run_fixture(tmp_path, usage, budget=100, *, rates=RATES, tail="", evidence="live"):
    events = [
        {"type": "session", "id": "fixture-session"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "model": "fixture-model",
                "provider": "fixture-provider",
                "content": [{"type": "text", "text": "Fixture answer"}],
                "usage": usage,
            },
        },
    ]
    binary = tmp_path / "pi-fixture"
    invocation = tmp_path / "invocation.json"
    binary.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(invocation)!r}, 'w').write(json.dumps("
        "{'argv': sys.argv, 'stdin': sys.stdin.read()}))\n"
        f"for event in {events!r}: print(json.dumps(event),flush=True)\n" + tail
    )
    binary.chmod(0o700)
    runtime = tmp_path / "runtime"
    opts = {
        "runtime": str(runtime),
        "env": process_environment(runtime),
        "prefix": ["/usr/bin/env"],
        "route": {
            "model": "fixture-model",
            "provider": "fixture-provider",
            "endpoint": "http://127.0.0.1:1",
            "auth_env": "DUMMY_CREDENTIAL",
            "api": "openai-completions",
            "evidence_kind": evidence,
            "pricing": rates,
            "pricing_evidence": "fixture-rates",
        },
        "role": "coder",
        "timeout_seconds": 1,
        "max_output_bytes": 100000,
        "budget_cents": budget,
    }
    harness = PiHarness(str(binary))
    list(harness.start("Fixture brief", tmp_path, opts))
    models = json.loads((runtime / "agent/models.json").read_text())
    return harness.wait(), models["providers"]["fixture-provider"]["models"][0]


def test_pinned_rates_reach_pi_and_zero_native_cost_is_not_free(tmp_path):
    result, model = run_fixture(
        tmp_path,
        {"input": 100000, "output": 10000, "cacheRead": 200000, "cost": {"total": 0}},
    )
    assert model["cost"] == {
        "input": 0.3,
        "output": 1.2,
        "cacheRead": 0.006,
        "cacheWrite": 0,
    }
    assert result.ok and result.cost_cents == 5  # 4.32 cents, conservatively rounded
    assert result.usage["cost_status"] == "client-estimate-not-provider-bill"
    assert result.usage["cost_source"] == "approved-per-token-pricing"


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input": 0, "output": 0},
        {"input": -1, "output": 10},
        {"input": 1, "output": True},
    ],
)
def test_invalid_or_absent_live_usage_charges_reservation_and_fails(tmp_path, usage):
    result, _ = run_fixture(tmp_path, usage)
    assert not result.ok and result.cost_cents == 100


def test_budget_overrun_is_not_undercharged(tmp_path):
    result, _ = run_fixture(tmp_path, {"input": 1000000, "output": 1000000}, budget=100)
    assert not result.ok and result.cost_cents == 150


def test_interrupted_partial_usage_is_unknown_not_discounted(tmp_path):
    result, _ = run_fixture(
        tmp_path, {"input": 100, "output": 10}, tail="import time; time.sleep(20)\n"
    )
    assert not result.ok and result.cost_cents == 100
    assert result.usage["cost_status"] == "unknown-reservation-charged"


def test_explicit_simulation_may_report_zero_cost(tmp_path):
    result, _ = run_fixture(
        tmp_path,
        {"input": 100, "output": 10, "cost": {"total": 0}},
        rates={},
        evidence="simulation",
    )
    assert result.ok and result.cost_cents == 0


def test_explicit_simulation_missing_cost_reserves_allowance(tmp_path):
    result, _ = run_fixture(
        tmp_path, {"input": 100, "output": 10}, rates={}, evidence="simulation"
    )
    assert result.ok and result.cost_cents == 100


def test_live_pricing_and_isolation_guards_precede_runtime_creation(tmp_path):
    opts = {
        "runtime": str(tmp_path / "runtime"),
        "route": {"api": "openai-completions", "evidence_kind": "live"},
    }
    with pytest.raises(ValueError, match="pricing"):
        list(PiHarness().start("brief", tmp_path, opts))
    opts["route"].update(pricing=RATES, pricing_evidence="fixture")
    with pytest.raises(ValueError, match="isolated"):
        list(PiHarness().start("brief", tmp_path, opts))
    assert not (tmp_path / "runtime").exists()


def test_direct_unclassified_pi_dispatch_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="classification"):
        list(
            PiHarness().start(
                "brief", tmp_path, {"route": {}, "runtime": str(tmp_path / "runtime")}
            )
        )
    assert not (tmp_path / "runtime").exists()


def test_brief_travels_on_stdin_not_argv(tmp_path):
    result, _ = run_fixture(tmp_path, {"input": 100, "output": 10})
    invocation = json.loads((tmp_path / "invocation.json").read_text())
    assert result.ok
    assert invocation["stdin"] == "Fixture brief"
    assert "Fixture brief" not in invocation["argv"]
