"""Pi JSON events with an explicit route, fresh context and supervised lifecycle."""

from __future__ import annotations

import json
import math
import queue
import shutil
import threading
from pathlib import Path

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult
from dark_factory.runner import CommandRunner


class PiHarness:
    name = "pi"

    def __init__(self, binary: str = "pi") -> None:
        self.binary = shutil.which(binary) or binary
        self._cancel = threading.Event()
        self._result = HarnessResult(False, "", {}, None, error="not started")

    @classmethod
    def available(cls, binary: str = "pi") -> bool:
        return shutil.which(binary) is not None

    def start(self, prompt: str, workspace: Path, extra=None):
        extra = extra or {}
        route = extra["route"]
        runtime = Path(extra["runtime"])
        agent = runtime / "agent"
        agent.mkdir(parents=True)
        # Use only harness-native provider connections. No host global resources,
        # subscription files, extensions or project settings are discovered.
        settings = {
            "retry": {"enabled": False, "maxRetries": 0},
            "compaction": {"enabled": False},
            "defaultProjectTrust": "never",
        }
        (agent / "settings.json").write_text(json.dumps(settings))
        provider = {"baseUrl": route["endpoint"], "apiKey": "$" + route["auth_env"]}
        if route.get("api"):
            provider["api"] = route["api"]
            provider["models"] = [
                {
                    "id": route["model"],
                    "name": route["model"],
                    "reasoning": False,
                    "input": ["text"],
                    "contextWindow": 128000,
                    "maxTokens": 8192,
                }
            ]
        (agent / "models.json").write_text(
            json.dumps({"providers": {route["provider"]: provider}})
        )
        env = {**extra["env"], "PI_CODING_AGENT_DIR": str(agent)}
        # Explicit tool allowlist: code writes are OS-scoped; no shell, extensions,
        # implicit skills or arbitrary privileged scripts run on model instruction.
        tools = (
            "read,write,edit,grep,find,ls"
            if extra["role"] == "coder"
            else "read,grep,find,ls"
        )
        argv = [
            *extra.get("prefix", []),
            self.binary,
            "--mode",
            "json",
            "--print",
            "--provider",
            route["provider"],
            "--model",
            route["model"],
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
            "--offline",
            "--tools",
            tools,
            "--session-dir",
            str(agent / "sessions"),
            prompt,
        ]
        events = queue.Queue()
        pending = bytearray()
        messages = []
        usage = {"input": 0, "output": 0, "cost": 0.0, "reported_cost": False}
        session_id = None
        parse_error = None
        self._cancel.clear()

        def output(name, data):
            nonlocal session_id, parse_error
            if name != "stdout":
                return
            pending.extend(data)
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending[:] = remainder
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    parse_error = "invalid Pi JSON event"
                    self.cancel()
                    continue
                if item.get("type") == "session":
                    session_id = item.get("id")
                message = item.get("message", {})
                if (
                    item.get("type") == "message_end"
                    and message.get("role") == "assistant"
                ):
                    if (
                        message.get("model") != route["model"]
                        or message.get("provider") != route["provider"]
                    ):
                        parse_error = "Pi output route differs from approved route"
                        self.cancel()
                    text = "".join(
                        part.get("text", "")
                        for part in message.get("content", [])
                        if part.get("type") == "text"
                    )
                    if text:
                        messages.append(text)
                    if message.get("stopReason") in {"error", "aborted"}:
                        parse_error = message.get(
                            "errorMessage", "Pi stopped without completion"
                        )
                    u = message.get("usage", {})
                    usage["input"] += u.get("input", 0)
                    usage["output"] += u.get("output", 0)
                    if "total" in u.get("cost", {}):
                        usage["reported_cost"] = True
                    usage["cost"] += u.get("cost", {}).get("total", 0)
                    if math.ceil(usage["cost"] * 100) > extra["budget_cents"]:
                        parse_error = "attempt budget reached"
                        self.cancel()
                events.put(HarnessEvent(item.get("type", "event"), data=item))

        def execute():
            try:
                runner = CommandRunner(workspace, extra["max_output_bytes"])
                code, stdout, stderr = runner._run(
                    argv,
                    b"",
                    extra["timeout_seconds"],
                    workspace,
                    env,
                    should_stop=self._cancel.is_set,
                    on_output=output,
                    on_start=extra.get("on_start"),
                )
                if pending.strip():
                    output("stdout", b"\n")
                error = parse_error or (
                    stderr.decode(errors="replace")[-2000:] if code else None
                )
                self._result = HarnessResult(
                    code == 0 and not error and bool(messages),
                    messages[-1] if messages else "",
                    usage,
                    session_id,
                    cost_cents=math.ceil(usage["cost"] * 100)
                    if usage["reported_cost"]
                    else extra["budget_cents"],
                    error=error,
                )
            except Exception as exc:
                self._result = HarnessResult(
                    False,
                    messages[-1] if messages else "",
                    usage,
                    session_id,
                    cost_cents=math.ceil(usage["cost"] * 100)
                    if usage["reported_cost"]
                    else extra["budget_cents"],
                    error=parse_error or str(exc),
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

    def attach_command(self, session_id: str) -> list[str]:
        return [self.binary, "--session", session_id]
