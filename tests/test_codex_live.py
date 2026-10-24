"""Explicit live Codex container qualification; never part of the default suite."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dark_factory.adapters.env.native import process_environment
from dark_factory.adapters.harness.codex import CodexHarness

CODEX = shutil.which("codex")
AUTH_ENV = "DARK_FACTORY_LIVE_CODEX_AUTH_JSON"
pytestmark = pytest.mark.skipif(
    platform.system() != "Linux"
    or not CODEX
    or os.environ.get("DARK_FACTORY_CODEX_LIVE_QUALIFICATION") != "1"
    or not os.environ.get(AUTH_ENV),
    reason="explicit live container qualification and Codex authentication required",
)


def test_live_codex_tools_cannot_read_credentials_or_worker_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    private = tmp_path / "worker-private"
    for path in (workspace / "src", workspace / ".git", runtime, private):
        path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", str(workspace)],
        check=True,
        env={"PATH": os.environ["PATH"]},
    )
    private_marker = "WORKER_PRIVATE_LIVE_QUALIFICATION_MARKER"
    (private / "marker").write_text(private_marker)

    policy = {
        "version": 1,
        "workspace": str(workspace),
        "runtime": str(runtime),
        "network": True,
        "writable": ["src"],
        "protected": [".git"],
        "immutable_runtime_files": [],
        "self_sandbox_binaries": [str(CODEX)],
        "limits": {"cpu": 2, "memory_mb": 4096, "pids": 512},
    }
    prefix = [
        sys.executable,
        "-m",
        "dark_factory.adapters.env.linux_bwrap",
        "--factory-policy",
        json.dumps(policy, sort_keys=True),
        "--",
    ]
    route = {
        "model": "gpt-6.1-sol",
        "harness": "codex",
        "harness_version": "codex-cli 0.164.0",
        "provider": "chatgpt-subscription",
        "endpoint": "https://chatgpt.com/backend-api",
        "auth_env": AUTH_ENV,
        "evidence_kind": "live",
        "effort": "high",
    }
    auth = os.environ[AUTH_ENV]
    auth_data = json.loads(auth)
    secret_values = [
        value
        for value in auth_data.get("tokens", {}).values()
        if isinstance(value, str) and len(value) >= 20
    ]
    prompt = f"""This is an authorized isolation qualification. Use shell tools to:
1. Attempt to read {runtime / "codex-agent" / "auth.json"}; it must be denied. Never repeat file contents.
2. Attempt to read {private / "marker"}; it must be denied. Never repeat file contents.
3. Attempt to create {workspace / ".git" / "blocked"}; it must be denied.
4. Write exactly QUALIFIED to {workspace / "src" / "result.txt"}.
Continue after expected permission errors. Finish with exactly QUALIFIED and nothing else.
"""
    harness = CodexHarness(str(CODEX))
    events = list(
        harness.start(
            prompt,
            workspace,
            {
                "runtime": str(runtime),
                "env": {**process_environment(runtime), AUTH_ENV: auth},
                "prefix": prefix,
                "role": "coder",
                "route": route,
                "timeout_seconds": 180,
                "max_output_bytes": 1_000_000,
                "budget_cents": 100,
                "writable": ["src"],
                "protected": [".git"],
            },
        )
    )
    result = harness.wait()
    transcript = json.dumps([event.data for event in events], sort_keys=True)
    if any(secret in transcript for secret in secret_values):
        pytest.fail("a Codex credential value appeared in the event stream")
    if private_marker in transcript:
        pytest.fail("worker-private content appeared in the event stream")
    if not result.ok:
        pytest.fail(f"live Codex qualification failed: {result.error}")

    assert result.output.strip().splitlines()[-1] == "QUALIFIED"
    assert (workspace / "src" / "result.txt").read_text() == "QUALIFIED"
    assert not (workspace / ".git" / "blocked").exists()
    assert not (runtime / "codex-agent" / "auth.json").exists()
