"""Codex CLI wire/authentication contract without subscription spending."""

import json
import sys

import pytest

from dark_factory.adapters.env.native import process_environment
from dark_factory.adapters.harness.codex import CodexHarness
from dark_factory.config import Route


def run(tmp_path, events, tail=""):
    binary = tmp_path / "codex-fixture"
    binary.write_text(
        f"#!{sys.executable}\n"
        + "import json,sys,os,time\n"
        + "assert '--ignore-user-config' in sys.argv and '--ignore-rules' in sys.argv\n"
        + "assert '--dangerously-bypass-approvals-and-sandbox' not in sys.argv and '--sandbox' not in sys.argv\n"
        + "assert 'forced_login_method=\"chatgpt\"' in sys.argv\n"
        + "assert 'permissions.factory.network.enabled=false' in sys.argv\n"
        + "assert not any(k.endswith('API_KEY') or k.endswith('TOKEN') for k in os.environ)\n"
        + "assert sys.stdin.read() == 'Task brief'\n"
        + f"for event in {events!r}: print(json.dumps(event),flush=True)\n"
        + tail
    )
    binary.chmod(0o700)
    runtime = tmp_path / "runtime"
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "fixture-id",
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
        },
    }
    extra = {
        "runtime": str(runtime),
        "env": {
            **process_environment(runtime),
            "FACTORY_AUTH_JSON": json.dumps(auth),
            "OPENAI_API_KEY": "must-not-inherit",
        },
        "role": "planner",
        "route": {
            "model": "gpt-6.1-sol",
            "provider": "chatgpt-subscription",
            "endpoint": "https://chatgpt.com/backend-api",
            "auth_env": "FACTORY_AUTH_JSON",
            "evidence_kind": "simulation",
        },
        "timeout_seconds": 1,
        "max_output_bytes": 100000,
        "budget_cents": 100,
    }
    harness = CodexHarness(str(binary))
    list(harness.start("Task brief", tmp_path, extra))
    assert not (runtime / "codex-agent" / "auth.json").exists()
    return harness.wait()


def stream():
    return [
        {"type": "thread.started", "thread_id": "session-1"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Task result"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]


def test_native_subscription_output_and_conservative_accounting(tmp_path):
    result = run(tmp_path, stream())
    assert (
        result.ok
        and result.output == "Task result"
        and result.session_id == "session-1"
    )
    assert result.cost_cents == 100 and result.usage["cost_status"] == "unknown"


@pytest.mark.parametrize(
    "events",
    [
        [],
        stream()[:-1],
        stream() + [stream()[-1]],
        [{"type": "error"}],
        [stream()[0], {"type": "turn.completed", "usage": {"input_tokens": -1}}],
    ],
)
def test_incomplete_error_duplicate_or_invalid_usage_fails(tmp_path, events):
    assert not run(tmp_path, events).ok


def test_timeout_kills_cli_and_removes_credentials(tmp_path):
    assert not run(tmp_path, stream()[:1], "time.sleep(20)\n").ok


def test_unqualified_live_route_is_blocked_before_credentials_are_written(tmp_path):
    route = Route(
        id="codex",
        model="gpt-6.1-sol",
        harness="codex",
        harness_version="pinned",
        provider="chatgpt-subscription",
        endpoint="https://chatgpt.com/backend-api",
        billing_account="account",
        region="US",
        role="coder",
        auth_env="AUTH",
        evidence_kind="live",
    )
    with pytest.raises(ValueError, match="unqualified"):
        CodexHarness.validate_route(route)
    with pytest.raises(ValueError, match="unqualified"):
        list(
            CodexHarness().start(
                "brief",
                tmp_path,
                {
                    "route": vars(route),
                    "runtime": str(tmp_path / "runtime"),
                    "prefix": ["sandbox-exec"],
                },
            )
        )
    assert not (tmp_path / "runtime").exists()
