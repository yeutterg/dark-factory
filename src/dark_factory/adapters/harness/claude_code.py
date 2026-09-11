"""Read-only Claude Code assignments using native Claude.ai subscription login."""

from __future__ import annotations

import json
import math
import queue
import shutil
import threading
import time
from pathlib import Path

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult
from dark_factory.runner import CommandRunner

READ_TOOLS = {"Read", "Grep", "Glob"}
SETTINGS = {
    "forceLoginMethod": "claudeai",
    "apiKeyHelper": "",
    "disableAllHooks": True,
    "disableClaudeAiConnectors": True,
    "fallbackModel": [],
    "switchModelsOnFlag": False,
    "autoMemoryEnabled": False,
}


def subscription_environment(base, runtime, token):
    # Construct from an allowlist: inherited API keys, OAuth overrides, gateways,
    # provider switches, proxy settings and API-key helpers must not change billing.
    env = {
        k: v
        for k, v in base.items()
        if k
        in {
            "PATH",
            "LANG",
            "TMPDIR",
            "USER",
            "LOGNAME",
            "PYTHONDONTWRITEBYTECODE",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
        }
    }
    env.update(
        {
            "HOME": base["HOME"],
            "CLAUDE_CODE_TMPDIR": str(runtime / "tmp"),
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            "CLAUDE_CODE_SAFE_MODE": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "DISABLE_UPDATES": "1",
            "DISABLE_EXTRA_USAGE_COMMAND": "1",
            "DISABLE_COMPACT": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "FALLBACK_FOR_ALL_PRIMARY_MODELS": "1",
        }
    )
    return env


class ClaudeCodeHarness:
    name = "claude-code"

    def __init__(self, binary="claude"):
        self.binary = str(Path(shutil.which(binary) or binary).resolve())
        self._cancel = threading.Event()
        self._result = HarnessResult(False, "", {}, None, error="not started")

    @classmethod
    def available(cls, binary="claude"):
        return shutil.which(binary) is not None

    def _base_argv(self, prefix, model=None):
        settings = {**SETTINGS, **({"availableModels": [model]} if model else {})}
        return [
            *prefix,
            self.binary,
            "--safe-mode",
            "--restricted",
            "--setting-sources",
            "",
            "--settings",
            json.dumps(settings),
        ]

    def start(self, prompt, workspace, extra=None):
        extra = extra or {}
        route = extra["route"]
        if extra["role"] not in {"planner", "critic", "reviewer"}:
            raise ValueError("Claude Code subscription adapter is read-only")
        if (
            route["provider"] != "claude-subscription"
            or route["endpoint"] != "https://api.anthropic.com"
        ):
            raise ValueError(
                "Claude Code requires the native first-party subscription route"
            )
        if not route.get("auth_env") or route.get("api"):
            raise ValueError(
                "a subscription OAuth token reference is required; API credentials are not permitted"
            )
        if not extra.get("prefix") and route.get("evidence_kind") != "simulation":
            raise ValueError("Claude Code requires an isolated execution environment")
        runtime = Path(extra["runtime"])
        (runtime / "tmp").mkdir(parents=True, exist_ok=True)
        token = extra["env"].get(route["auth_env"])
        if not token or not token.startswith("sk-ant-oat01-"):
            raise ValueError(
                "Claude subscription OAuth token is unavailable; run claude setup-token"
            )
        env = subscription_environment(extra["env"], runtime, token)
        base = self._base_argv(extra.get("prefix", []), route["model"])
        tools = ",".join(sorted(READ_TOOLS))
        argv = [
            *base,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            tools,
            "--allowedTools",
            tools,
            "--permission-mode",
            "dontAsk",
            "--model",
            route["model"],
            "--effort",
            route.get("effort", "high"),
            "--max-budget-usd",
            f"{extra['budget_cents'] / 100:.2f}",
        ]
        events = queue.Queue()
        pending = bytearray()
        terminal = None
        session_id = None
        initialized = False
        error = None
        self._cancel.clear()
        usage = {
            "billing_method": "claude-subscription",
            "cost_status": "unknown",
            "reported_cost": False,
        }

        def output(name, data):
            nonlocal terminal, session_id, initialized, error
            if name != "stdout":
                return
            pending.extend(data)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError("event must be an object")
                    sid = item.get("session_id")
                    if sid and session_id and sid != session_id:
                        raise ValueError("Claude session identity changed")
                    if sid:
                        session_id = sid
                    kind = item.get("type")
                    if kind == "system" and item.get("subtype") == "init":
                        if initialized or item.get("model") != route["model"]:
                            raise ValueError(
                                "Claude initialization differs from approved model"
                            )
                        if not set(item.get("tools", [])) <= READ_TOOLS:
                            raise ValueError("Claude exposed unapproved tools")
                        initialized = True
                    if kind == "assistant":
                        msg = item.get("message", {})
                        if terminal is not None or msg.get("model") != route["model"]:
                            raise ValueError(
                                "Claude output differs from approved model"
                            )
                        for part in msg.get("content", []):
                            if (
                                part.get("type") == "tool_use"
                                and part.get("name") not in READ_TOOLS
                            ):
                                raise ValueError("Claude requested an unapproved tool")
                    if kind == "result":
                        if terminal is not None:
                            raise ValueError("duplicate Claude result")
                        terminal = item
                        models = item.get("modelUsage", {})
                        if not isinstance(models, dict) or set(models) - {
                            route["model"]
                        }:
                            raise ValueError(
                                "Claude reported usage on an unapproved model"
                            )
                        cost = item.get("total_cost_usd")
                        if cost is not None:
                            if (
                                type(cost) not in {int, float}
                                or not math.isfinite(cost)
                                or cost < 0
                            ):
                                raise ValueError("invalid Claude cost estimate")
                            usage["api_equivalent_cost_usd"] = cost
                            if math.ceil(cost * 100) > extra["budget_cents"]:
                                raise ValueError("Claude attempt budget reached")
                        usage["tokens"] = item.get("usage", {})
                        usage["model_usage"] = models
                    events.put(HarnessEvent(kind or "event", data=item))
                except (ValueError, TypeError, AttributeError) as exc:
                    error = str(exc)
                    self.cancel()

        def execute():
            nonlocal error
            try:
                started = time.monotonic()
                runner = CommandRunner(workspace, extra["max_output_bytes"])
                code, out, _ = runner._run(
                    [*base, "auth", "status"],
                    b"",
                    min(15, extra["timeout_seconds"]),
                    workspace,
                    env,
                    should_stop=self._cancel.is_set,
                )
                auth = json.loads(out)
                if (
                    code
                    or auth.get("loggedIn") is not True
                    or auth.get("authMethod") != "oauth_token"
                    or auth.get("apiProvider") != "firstParty"
                ):
                    raise ValueError(
                        "Claude.ai subscription login unavailable; refusing API fallback"
                    )
                usage["subscription_type"] = auth.get("subscriptionType")
                usage["auth_method"] = "oauth_token"
                remaining = extra["timeout_seconds"] - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Claude attempt timed out during authentication")
                code, _, stderr = runner._run(
                    argv,
                    prompt.encode(),
                    remaining,
                    workspace,
                    env,
                    should_stop=self._cancel.is_set,
                    on_output=output,
                    on_start=extra.get("on_start"),
                )
                if pending.strip():
                    output("stdout", b"\n")
                if not initialized or not session_id or terminal is None:
                    error = error or "incomplete Claude event stream"
                elif (
                    terminal.get("is_error") is not False
                    or terminal.get("subtype") != "success"
                ):
                    error = error or "Claude did not complete successfully"
                if code or self._cancel.is_set():
                    error = (
                        error
                        or "Claude process stopped: "
                        + stderr.decode(errors="replace")[-1000:]
                    )
                answer = terminal.get("result", "") if terminal else ""
                if not isinstance(answer, str) or not answer.strip():
                    error = error or "Claude returned no final output"
                    answer = ""
                self._result = HarnessResult(
                    not error,
                    answer,
                    usage,
                    session_id,
                    cost_cents=extra["budget_cents"],
                    error=error,
                )
            except Exception as exc:
                self._result = HarnessResult(
                    False,
                    "",
                    usage,
                    session_id,
                    cost_cents=extra["budget_cents"],
                    error=error or str(exc),
                )
            finally:
                events.put(None)

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        while True:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                yield HarnessEvent("heartbeat")
                continue
            if event is None:
                break
            yield event
        thread.join()

    def wait(self):
        return self._result

    def cancel(self):
        self._cancel.set()

    def attach_command(self, session_id):
        raise NotImplementedError(
            "read-only Claude assignments do not retain resumable sessions"
        )
