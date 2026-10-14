"""Installed sandbox probes only; no account login or external network access."""

import base64
import json
import platform
import shutil
from pathlib import Path

import pytest

from dark_factory.adapters.env.native import NativeEnvironment, process_environment
from dark_factory.adapters.git.local import git
from dark_factory.adapters.harness.codex import CodexHarness, inline_table
from dark_factory.runner import CommandRunner


def native_binary():
    path = shutil.which("codex")
    if not path:
        return None
    path = Path(path).resolve()
    if path.suffix != ".js":
        return path
    # Test discovery only. Production profiles explicitly name an executable.
    matches = list(
        path.parent.parent.glob(
            "node_modules/@openai/codex-darwin-*/vendor/*/bin/codex"
        )
    )
    return matches[0] if len(matches) == 1 else None


BINARY = native_binary()
pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin" or not BINARY,
    reason="macOS and native Codex required",
)


def test_installed_codex_command_sandbox_denies_dummy_credentials(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    env = process_environment(runtime)
    env["CODEX_HOME"] = str(runtime / "codex-agent")
    agent = Path(env["CODEX_HOME"])
    agent.mkdir()
    credential = agent / "auth.json"
    credential.write_text("DUMMY_CREDENTIAL_FIXTURE")
    (workspace / "public.txt").write_text("PUBLIC_FIXTURE")
    filesystem = {
        ":root": "deny",
        ":minimal": "read",
        ":workspace_roots": "write",
        str(runtime / "tmp"): "write",
        str(agent): "deny",
    }
    argv = [
        str(BINARY),
        "sandbox",
        "--cd",
        str(workspace),
        "-P",
        "factory",
        "-c",
        "permissions.factory.filesystem=" + inline_table(filesystem),
        "-c",
        "permissions.factory.network.enabled=false",
    ]
    runner = CommandRunner(workspace)
    code, out, _ = runner.supervise(
        argv + ["/bin/cat", str(workspace / "public.txt")], b"", 10, workspace, env
    )
    assert code == 0 and out == b"PUBLIC_FIXTURE"
    code, out, _ = runner.supervise(
        argv + ["/bin/cat", str(credential)], b"", 10, workspace, env
    )
    assert code != 0 and b"DUMMY_CREDENTIAL_FIXTURE" not in out
    # This proves the command sandbox only, not every built-in model tool.


def test_installed_codex_nested_sandbox_fails_closed(tmp_path, monkeypatch):
    environment = NativeEnvironment(tmp_path / "workspaces")
    prepared = environment.prepare("codex-fixture")
    git(prepared.workspace, "init")
    runtime = tmp_path / "runtime"
    env = process_environment(runtime)
    # Startup proof needs no external traffic, even with dummy authentication.
    prefix = environment.prefix(
        prepared,
        runtime,
        network=False,
        writable=["message.py"],
        protected=[],
        readable_files=[BINARY],
    )
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "fixture-id",
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
        },
    }
    claims = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "fixture-account",
            "chatgpt_plan_type": "plus",
        },
        "exp": 4102444800,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    auth["tokens"]["id_token"] = "e30." + encoded + ".fixture"
    auth["tokens"]["account_id"] = "fixture-account"
    auth["last_refresh"] = "2026-10-13T00:00:00Z"
    diagnostics = []
    original = CommandRunner.supervise

    def observed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        diagnostics.append(result)
        return result

    monkeypatch.setattr(CommandRunner, "supervise", observed)
    harness = CodexHarness(str(BINARY))
    list(
        harness.start(
            "Dummy fixture only",
            prepared.workspace,
            {
                "runtime": str(runtime),
                "env": {**env, "FACTORY_AUTH_JSON": json.dumps(auth)},
                "prefix": prefix,
                "role": "coder",
                "route": {
                    "provider": "chatgpt-subscription",
                    "endpoint": "https://chatgpt.com/backend-api",
                    "model": "gpt-6.1-sol",
                    "evidence_kind": "simulation",
                    "auth_env": "FACTORY_AUTH_JSON",
                },
                "timeout_seconds": 15,
                "budget_cents": 100,
                "max_output_bytes": 100000,
            },
        )
    )
    assert not harness.wait().ok
    assert any(
        b"sandbox_apply: Operation not permitted" in err for _, _, err in diagnostics
    ), diagnostics
    assert all(b"reserved built" not in err for _, _, err in diagnostics)
    assert not (runtime / "codex-agent" / "auth.json").exists()
    assert not (prepared.workspace / "message.py").exists()
