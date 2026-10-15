"""Pi JSON events with an explicit route, fresh context and supervised lifecycle."""

from __future__ import annotations

import json
import math
import shutil
from decimal import Decimal
from pathlib import Path

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult
from dark_factory.adapters.harness.streaming import JsonLines, StreamingCliHarness
from dark_factory.runner import CommandRunner


def validate_pricing(pricing, evidence, required=False):
    if pricing and (
        not isinstance(pricing, dict)
        or set(pricing) != {"input", "output", "cache_read", "cache_write"}
        or any(
            type(v) not in {int, float} or not math.isfinite(v) or v < 0
            for v in pricing.values()
        )
    ):
        raise ValueError(
            "Pi pricing needs four finite non-negative per-million-token rates"
        )
    if required and (
        not pricing
        or not evidence.strip()
        or not (pricing["input"] > 0 or pricing["output"] > 0)
    ):
        raise ValueError(
            "live custom Pi models require pinned positive pricing and pricing_evidence; absent prices default to zero"
        )


def validate_pi_route(route: dict) -> None:
    if route.get("evidence_kind") not in {"simulation", "live"}:
        raise ValueError("Pi requires explicit live or simulation classification")
    validate_pricing(
        route.get("pricing", {}),
        route.get("pricing_evidence", ""),
        route.get("evidence_kind") == "live" and bool(route.get("api")),
    )


class PiHarness(StreamingCliHarness):
    name = "pi"

    @staticmethod
    def validate_route(route):
        validate_pi_route(vars(route))

    def __init__(self, binary: str = "pi") -> None:
        super().__init__(shutil.which(binary) or binary)

    def start(self, prompt: str, workspace: Path, extra=None):
        extra = extra or {}
        route = extra["route"]
        validate_pi_route(route)
        pricing = route.get("pricing", {})
        if route.get("evidence_kind") == "live" and not extra.get("prefix"):
            raise ValueError("Pi requires an isolated execution environment")
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
        api = route.get("api") or ""
        if api:
            provider["api"] = api
            provider["models"] = [
                {
                    "id": route["model"],
                    "name": route["model"],
                    "reasoning": bool(route.get("reasoning")),
                    "input": ["text"],
                    "contextWindow": int(route.get("context_window") or 128000),
                    "maxTokens": int(route.get("max_tokens") or 8192),
                    **(
                        {
                            "cost": {
                                "input": pricing["input"],
                                "output": pricing["output"],
                                "cacheRead": pricing["cache_read"],
                                "cacheWrite": pricing["cache_write"],
                            }
                        }
                        if pricing
                        else {}
                    ),
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
            # --tools keeps MCP tools, so MCP is disabled outright.
            "--no-mcp",
            "--tools",
            tools,
            "--session-dir",
            str(agent / "sessions"),
        ]
        if extra.get("resume_session"):
            # Continue the interrupted conversation in a new session; the archived
            # original is never modified.
            # Outside the agent directory: Pi moves stray session files found there.
            resumed = runtime / "resume" / "session.jsonl"
            resumed.parent.mkdir()
            resumed.write_text(extra["resume_session"])
            argv += ["--fork", str(resumed)]
        messages = []
        usage = {"input": 0, "output": 0, "cost": 0.0, "reported_cost": False}
        cost_known = False
        estimated_cost = Decimal(0)
        session_id = None
        parse_error = None
        self._cancel.clear()

        def run(emit):
            def handle(line):
                nonlocal session_id, parse_error, cost_known, estimated_cost
                try:
                    item = json.loads(line)
                except ValueError:
                    parse_error = "invalid Pi JSON event"
                    self.cancel()
                    return
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
                    if (
                        not isinstance(u, dict)
                        or any(
                            type(u.get(k)) is not int or u[k] < 0
                            for k in ("input", "output")
                        )
                        or (
                            route.get("evidence_kind") == "live"
                            and not any(
                                u.get(k, 0)
                                for k in ("input", "output", "cacheRead", "cacheWrite")
                            )
                        )
                        or any(
                            type(u.get(k, 0)) is not int or u.get(k, 0) < 0
                            for k in ("cacheRead", "cacheWrite")
                        )
                    ):
                        parse_error = "invalid Pi usage; cost is unknown"
                        cost_known = False
                        self.cancel()
                        return
                    usage["input"] += u.get("input", 0)
                    usage["output"] += u.get("output", 0)
                    if pricing:
                        estimated_cost += (
                            sum(
                                Decimal(str(pricing[rate])) * u.get(counter, 0)
                                for rate, counter in (
                                    ("input", "input"),
                                    ("output", "output"),
                                    ("cache_read", "cacheRead"),
                                    ("cache_write", "cacheWrite"),
                                )
                            )
                            / 1000000
                        )
                        usage["cost"] = float(estimated_cost)
                        usage["cost_source"] = "approved-per-token-pricing"
                        usage["cost_status"] = "client-estimate-not-provider-bill"
                        cost_known = True
                    else:
                        total = (
                            u.get("cost", {}).get("total")
                            if isinstance(u.get("cost"), dict)
                            else None
                        )
                        if (
                            type(total) in {int, float}
                            and math.isfinite(total)
                            and total >= 0
                            and (
                                total > 0 or route.get("evidence_kind") == "simulation"
                            )
                        ):
                            usage["reported_cost"] = True
                            usage["cost"] += total
                            cost_known = True
                        elif route.get("evidence_kind") == "simulation":
                            cost_known = False  # A fixture may omit cost; reserve conservatively.
                        else:
                            cost_known = False
                            parse_error = "Pi live pricing/usage is unqualified; zero is not a billing receipt"
                            self.cancel()
                    if math.ceil(usage["cost"] * 100) > extra["budget_cents"]:
                        parse_error = "attempt budget reached"
                        self.cancel()
                emit(HarnessEvent(item.get("type", "event"), data=item))

            lines = JsonLines(handle)
            try:
                runner = CommandRunner(workspace, extra["max_output_bytes"])
                # The brief travels on stdin: it can exceed argv limits and must
                # not be visible in the process table.
                code, stdout, stderr = runner.supervise(
                    argv,
                    prompt.encode(),
                    extra["timeout_seconds"],
                    workspace,
                    env,
                    should_stop=self._cancel.is_set,
                    on_output=lines,
                    on_start=extra.get("on_start"),
                )
                lines.flush()
                error = parse_error or (
                    stderr.decode(errors="replace")[-2000:] if code else None
                )
                if error or not cost_known:
                    usage["partial_estimate_usd"] = usage["cost"]
                    usage["cost_status"] = "unknown-reservation-charged"
                self._result = HarnessResult(
                    code == 0 and not error and bool(messages),
                    messages[-1] if messages else "",
                    usage,
                    session_id,
                    cost_cents=math.ceil(usage["cost"] * 100)
                    if cost_known and not error
                    else max(extra["budget_cents"], math.ceil(usage["cost"] * 100)),
                    error=error,
                )
            except Exception as exc:
                usage["partial_estimate_usd"] = usage["cost"]
                usage["cost_status"] = "unknown-reservation-charged"
                self._result = HarnessResult(
                    False,
                    messages[-1] if messages else "",
                    usage,
                    session_id,
                    cost_cents=max(
                        extra["budget_cents"], math.ceil(usage["cost"] * 100)
                    ),
                    error=parse_error or str(exc),
                )

        yield from self._stream(run)

    def attach_command(self, session_id: str) -> list[str]:
        return [self.binary, "--session", session_id]


def resolve_credential(route: dict, binary: str = "pi") -> str:
    """Only Pi routes may discover Pi's native provider authentication."""
    import os
    import subprocess

    name = route.get("auth_env") or ""
    if not name:
        return ""
    secret = os.environ.get(name, "")
    if secret:
        return secret
    if route.get("project_id"):
        return ""
    if route.get("provider") == "together" and name == "TOGETHER_API_KEY":
        proc = subprocess.run(
            [
                shutil.which(binary) or binary,
                "auth",
                "print-api-key",
                "--provider",
                "together",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    return ""
