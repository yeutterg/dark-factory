"""Subscription routing and bounded Claude CLI lifecycle, without paid inference."""

import os
import sys
import tomllib
from pathlib import Path

import pytest

from dark_factory.adapters.env.native import process_environment
from dark_factory.adapters.harness.claude_code import (
    ClaudeCodeHarness,
    subscription_environment,
)
from dark_factory.config import ConfigError, validate_raw

MODEL = "claude-opus-4-6"


def extra_for(tmp_path):
    runtime = tmp_path / "runtime"
    return {
        "route": {
            "provider": "claude-subscription",
            "endpoint": "https://api.anthropic.com",
            "model": MODEL,
            "auth_env": "FACTORY_CLAUDE_TOKEN",
            "evidence_kind": "simulation",
            "effort": "high",
        },
        "role": "planner",
        "runtime": str(runtime),
        "env": {
            **process_environment(runtime),
            "FACTORY_CLAUDE_TOKEN": "sk-ant-oat01-fixture",
        },
        "budget_cents": 100,
        "timeout_seconds": 3,
        "max_output_bytes": 100000,
    }


def stream():
    return [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "session-1",
            "model": MODEL,
            "tools": ["Read", "Grep", "Glob"],
        },
        {
            "type": "assistant",
            "session_id": "session-1",
            "message": {"model": MODEL, "content": [{"type": "text", "text": "draft"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "session-1",
            "result": "Complete root plan",
            "total_cost_usd": 0.25,
            "usage": {"input_tokens": 20},
            "modelUsage": {MODEL: {"inputTokens": 20}},
        },
    ]


def binary(tmp_path, events, auth=None, tail=""):
    auth = auth or {
        "loggedIn": True,
        "authMethod": "oauth_token",
        "apiProvider": "firstParty",
    }
    path = tmp_path / "claude-fixture"
    path.write_text(
        f"#!{sys.executable}\n"
        + "import json,sys,os,time\n"
        + f"auth={auth!r}\nevents={events!r}\n"
        + "assert not any(k.startswith('ANTHROPIC_') for k in os.environ)\n"
        + "if sys.argv[-2:] == ['auth', 'status']:\n print(json.dumps(auth));sys.exit(0)\n"
        + "assert '--bare' not in sys.argv and '--safe-mode' in sys.argv and '--restricted' in sys.argv\n"
        + "assert '--no-session-persistence' in sys.argv and '--fallback-model' not in sys.argv\n"
        + "assert os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == 'sk-ant-oat01-fixture'\n"
        + "assert sys.stdin.read() == 'Plan the complete root'\n"
        + "for event in events: print(json.dumps(event), flush=True)\n"
        + tail
    )
    path.chmod(0o700)
    return str(path)


def run(tmp_path, events, auth=None, tail="", timeout=3):
    opts = extra_for(tmp_path)
    opts["timeout_seconds"] = timeout
    h = ClaudeCodeHarness(binary(tmp_path, events, auth, tail))
    list(h.start("Plan the complete root", tmp_path, opts))
    return h.wait()


def test_subscription_environment_never_inherits_api_billing(tmp_path):
    base = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "ANTHROPIC_API_KEY": "wrong",
        "ANTHROPIC_AUTH_TOKEN": "wrong",
        "ANTHROPIC_BASE_URL": "https://wrong",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "wrong",
        "HTTPS_PROXY": "https://wrong",
        "CLAUDE_CONFIG_DIR": "/private/user-config",
    }
    env = subscription_environment(base, tmp_path, "sk-ant-oat01-fixture")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-fixture"
    assert not any(k.startswith("ANTHROPIC_") for k in env)
    assert "CLAUDE_CODE_USE_BEDROCK" not in env and "HTTPS_PROXY" not in env
    assert "CLAUDE_CONFIG_DIR" not in env


def test_success_records_subscription_and_estimate_separately(tmp_path):
    result = run(tmp_path, stream())
    assert result.ok and result.output == "Complete root plan"
    assert result.session_id == "session-1"
    assert result.usage["billing_method"] == "claude-subscription"
    assert result.usage["api_equivalent_cost_usd"] == 0.25
    assert result.usage["cost_status"] == "unknown"
    assert result.cost_cents == 100  # conservative reservation, not an API bill


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda es: es[0].update(model="other-model"), "approved model"),
        (lambda es: es[1]["message"].update(model="other-model"), "approved model"),
        (lambda es: es[-1].update(modelUsage={"other-model": {}}), "unapproved model"),
        (lambda es: es[-1].update(session_id="other-session"), "identity changed"),
        (
            lambda es: es[-1].update(is_error=True, subtype="error_max_budget_usd"),
            "complete successfully",
        ),
        (lambda es: es[-1].update(total_cost_usd=1.01), "budget reached"),
        (lambda es: es[0].update(tools=["Read", "Bash"]), "unapproved tools"),
        (
            lambda es: es[1]["message"].update(
                content=[{"type": "tool_use", "name": "Bash"}]
            ),
            "unapproved tool",
        ),
    ],
)
def test_failed_contract_cannot_report_success(tmp_path, change, reason):
    events = stream()
    change(events)
    result = run(tmp_path, events)
    assert not result.ok and reason in result.error


@pytest.mark.parametrize("events", [[], stream()[:-1], stream() + [stream()[-1]]])
def test_missing_or_duplicate_terminal_result_fails(tmp_path, events):
    assert not run(tmp_path, events).ok


def test_api_authentication_does_not_start_assignment(tmp_path):
    result = run(
        tmp_path,
        stream(),
        {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"},
    )
    assert not result.ok and "refusing API fallback" in result.error
    assert result.session_id is None


def test_timeout_and_cancellation_stop_process(tmp_path):
    result = run(tmp_path, stream()[:1], tail="time.sleep(30)\n", timeout=0.5)
    assert not result.ok and "timed out" in result.error
    opts = extra_for(tmp_path)
    h = ClaudeCodeHarness(binary(tmp_path, stream()[:1], tail="time.sleep(30)\n"))
    for event in h.start("Plan the complete root", tmp_path, opts):
        if event.type == "system":
            h.cancel()
    assert not h.wait().ok


def config():
    raw = tomllib.loads(Path("examples/factory-live.toml").read_text())
    raw["runtime"] = {"worker_token": "test-worker", "operator_token": "test-operator"}
    raw["sources"]["owner/repository"]["path"] = str(Path.cwd())
    raw["routes"]["primary"].update(
        model=MODEL,
        harness="claude-code",
        harness_version="2.1.268 (Claude Code)",
        provider="claude-subscription",
        endpoint="https://api.anthropic.com",
        region="US",
        geography_evidence="fixture operator evidence",
        auth_env="FACTORY_CLAUDE_TOKEN",
        role="planner",
        effort="xhigh",
    )
    return raw


def test_claude_config_requires_subscription_and_read_only_role(tmp_path):
    raw = config()
    cfg = validate_raw(raw, tmp_path / "factory.toml", "fixture")
    assert cfg.routes["primary"].effort == "xhigh"
    for field, value in [
        ("role", "coder"),
        ("auth_env", "ANTHROPIC_API_KEY"),
        ("provider", "anthropic"),
        ("endpoint", "https://gateway.invalid"),
        ("model", "opus"),
        ("model", "claude-opus-latest"),
        ("effort", "invalid"),
        ("region", "UNKNOWN"),
    ]:
        raw = config()
        raw["routes"]["primary"][field] = value
        with pytest.raises(ConfigError):
            validate_raw(raw, tmp_path / "factory.toml", "fixture")


def test_api_key_cannot_masquerade_as_subscription_token(tmp_path):
    opts = extra_for(tmp_path)
    opts["env"]["FACTORY_CLAUDE_TOKEN"] = "sk-ant-api01-not-oauth"
    h = ClaudeCodeHarness(binary(tmp_path, stream()))
    with pytest.raises(ValueError, match="subscription OAuth token"):
        list(h.start("Plan the complete root", tmp_path, opts))


def test_read_only_adapter_refuses_coder_role(tmp_path):
    opts = extra_for(tmp_path)
    opts["role"] = "coder"
    with pytest.raises(ValueError, match="read-only"):
        list(ClaudeCodeHarness().start("code", tmp_path, opts))


def test_invalid_json_does_not_pass_after_valid_result(tmp_path):
    result = run(tmp_path, stream(), tail="print('not-json',flush=True)\n")
    assert not result.ok


def test_worker_pins_claude_version(tmp_path):
    from types import SimpleNamespace

    from dark_factory.worker import Worker

    fixture = tmp_path / "claude-version"
    fixture.write_text(f"#!{sys.executable}\nprint('2.1.268 (Claude Code)')\n")
    fixture.chmod(0o700)
    cfg = SimpleNamespace(
        execution={"claude_binary": str(fixture)},
        source_path=tmp_path / "factory.toml",
        state_dir=tmp_path / "state",
    )
    worker = Worker(cfg, None, None)
    assert isinstance(
        worker._harness(
            {"harness": "claude-code", "harness_version": "2.1.268 (Claude Code)"}
        ),
        ClaudeCodeHarness,
    )
    with pytest.raises(RuntimeError, match="version differs"):
        worker._harness({"harness": "claude-code", "harness_version": "wrong"})
