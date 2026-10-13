"""Native Codex CLI assignments with explicit subscription credentials and isolation."""

from __future__ import annotations

import json
import queue
import shutil
import threading
from pathlib import Path

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult
from dark_factory.adapters.harness.credential_file import credential_file
from dark_factory.runner import CommandRunner


def subscription_environment(base, agent):
    # No API key, proxy, host config, Node hook or another harness's login is inherited.
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
    }
    return {**{k: v for k, v in base.items() if k in allowed}, "CODEX_HOME": str(agent)}


def inline_table(values):
    return (
        "{"
        + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in values.items())
        + "}"
    )


class CodexHarness:
    name = "codex"

    @staticmethod
    def validate_route(route):
        import re

        if (
            route.provider != "chatgpt-subscription"
            or route.endpoint != "https://chatgpt.com/backend-api"
            or route.api
        ):
            raise ValueError("Codex requires its native first-party subscription route")
        if not route.auth_env or route.auth_env in {"OPENAI_API_KEY", "CODEX_API_KEY"}:
            raise ValueError(
                "Codex requires an explicit subscription credential reference"
            )
        if not re.fullmatch(r"gpt-[0-9][a-z0-9.-]+", route.model):
            raise ValueError("Codex requires an explicit GPT model ID")
        if route.effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("unsupported Codex effort")

    def __init__(self, binary="codex"):
        self.binary = shutil.which(binary) or binary
        self._cancel = threading.Event()
        self._result = HarnessResult(False, "", {}, None, error="not started")

    @classmethod
    def available(cls, binary="codex"):
        return shutil.which(binary) is not None

    def start(self, prompt, workspace, extra=None):
        extra = extra or {}
        route = extra["route"]
        if (
            route["provider"] != "chatgpt-subscription"
            or route["endpoint"] != "https://chatgpt.com/backend-api"
        ):
            raise ValueError("Codex requires its native first-party subscription route")
        if not extra.get("prefix") and route.get("evidence_kind") != "simulation":
            raise ValueError("Codex requires an isolated execution environment")
        try:
            auth = json.loads(extra["env"][route["auth_env"]])
            if (
                auth.get("auth_mode") != "chatgpt"
                or auth.get("OPENAI_API_KEY")
                or not all(
                    auth["tokens"].get(k)
                    for k in ("access_token", "refresh_token", "id_token")
                )
            ):
                raise ValueError("not subscription authentication")
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "explicit Codex ChatGPT authentication JSON is required; no API fallback"
            ) from None
        runtime = Path(extra["runtime"])
        agent = runtime / "codex-agent"
        with credential_file(
            runtime,
            "codex-agent/auth.json",
            json.dumps(auth),
            extra["timeout_seconds"] + 5,
        ):
            yield from self._authenticated_start(prompt, workspace, extra, agent)

    def _authenticated_start(self, prompt, workspace, extra, agent):
        runtime = Path(extra["runtime"])
        route = extra["route"]
        env = subscription_environment(extra["env"], agent)
        filesystem = {
            ":root": "deny",
            ":minimal": "read",
            ":workspace_roots": "write" if extra["role"] == "coder" else "read",
            str(runtime / "tmp"): "write",
            str(agent): "deny",
        }
        # Native tool permissions protect the credential directory; the outer OS profile
        # independently scopes writes and excludes host/controller data.
        settings = {
            "forced_login_method": '"chatgpt"',
            "cli_auth_credentials_store": '"file"',
            "approval_policy": '"never"',
            "default_permissions": '"factory"',
            "permissions.factory.filesystem": inline_table(filesystem),
            "permissions.factory.network.enabled": "false",
            "shell_environment_policy.inherit": '"none"',
            "shell_environment_policy.set": inline_table(
                {k: env[k] for k in ("PATH", "HOME", "TMPDIR") if k in env}
            ),
            "web_search": '"disabled"',
            "mcp_servers": "{}",
            "hooks": "{}",
            "model_provider": '"openai"',
            "model_reasoning_effort": json.dumps(route.get("effort", "high")),
            "chatgpt_base_url": json.dumps(route["endpoint"] + "/"),
            "model_providers.openai.request_max_retries": "0",
            "model_providers.openai.stream_max_retries": "0",
        }
        argv = [
            *extra.get("prefix", []),
            self.binary,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--json",
            "--model",
            route["model"],
            "--cd",
            str(workspace),
        ]
        for feature in (
            "hooks",
            "apps",
            "browser_use",
            "computer_use",
            "multi_agent",
            "shell_snapshot",
            "daemon_auto_start",
        ):
            argv += ["--disable", feature]
        for key, value in settings.items():
            argv += ["-c", key + "=" + value]
        argv.append("-")
        events = queue.Queue()
        pending = bytearray()
        session_id = None
        terminal = None
        answer = []
        error = None
        usage = {
            "billing_method": "chatgpt-subscription",
            "cost_status": "unknown",
            "reported_cost": False,
            "model_binding": "pinned-cli-invocation",
        }
        self._cancel.clear()

        def output(name, data):
            nonlocal session_id, terminal, error
            if name != "stdout":
                return
            pending.extend(data)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    kind = event["type"]
                    if event.get("model") and event["model"] != route["model"]:
                        raise ValueError("Codex model differs from approved invocation")
                    if kind == "thread.started":
                        sid = event.get("thread_id")
                        if session_id or not isinstance(sid, str) or not sid:
                            raise ValueError("invalid Codex session identity")
                        session_id = sid
                        events.put(HarnessEvent("session", data={"session_id": sid}))
                    elif (
                        kind == "item.completed"
                        and event.get("item", {}).get("type") == "agent_message"
                    ):
                        if terminal:
                            raise ValueError("Codex output after terminal result")
                        answer.append(event["item"]["text"])
                    elif kind == "turn.completed":
                        if terminal or not session_id:
                            raise ValueError("invalid Codex terminal result")
                        terminal = kind
                        tokens = event.get("usage", {})
                        if not isinstance(tokens, dict) or any(
                            type(v) is not int or v < 0 for v in tokens.values()
                        ):
                            raise ValueError("invalid Codex usage")
                        usage["tokens"] = tokens
                    elif kind in {"turn.failed", "error"}:
                        raise ValueError("Codex assignment failed; no paid fallback")
                    events.put(HarnessEvent("event", data=event))
                except (ValueError, KeyError, TypeError) as exc:
                    error = str(exc)
                    self._cancel.set()

        def run():
            nonlocal error
            try:
                code, _, _ = CommandRunner(workspace, extra["max_output_bytes"])._run(
                    argv,
                    prompt.encode(),
                    extra["timeout_seconds"],
                    workspace,
                    env,
                    should_stop=self._cancel.is_set,
                    on_output=output,
                    on_start=extra.get("on_start"),
                )
                if code or pending.strip() or not terminal or not answer:
                    error = error or "Codex ended without a complete answer"
            except Exception as exc:
                error = error or str(exc)
            finally:
                self._result = HarnessResult(
                    not error,
                    "\n".join(answer),
                    usage,
                    session_id,
                    extra["budget_cents"],
                    error,
                )
                events.put(None)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            while True:
                try:
                    event = events.get(timeout=0.1)
                except queue.Empty:
                    yield HarnessEvent("heartbeat")
                    continue
                if event is None:
                    break
                yield event
        finally:
            self._cancel.set()
            thread.join(timeout=5)

    def wait(self):
        return self._result

    def cancel(self):
        self._cancel.set()

    def attach_command(self, session_id):
        return [self.binary, "resume", session_id]
