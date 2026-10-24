"""Native Codex CLI assignments with explicit subscription credentials and isolation."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from dark_factory.adapters.harness.base import HarnessEvent, HarnessResult
from dark_factory.adapters.harness.credential_file import credential_file
from dark_factory.adapters.harness.streaming import JsonLines, StreamingCliHarness
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


def redacted_cli_error(code: int, stderr: bytes, auth: dict) -> str:
    """Return bounded CLI diagnostics without allowing credential disclosure."""
    detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
    secrets = [
        value
        for value in auth.get("tokens", {}).values()
        if isinstance(value, str) and value
    ]
    api_key = auth.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key:
        secrets.append(api_key)
    for secret in secrets:
        detail = detail.replace(secret, "[REDACTED]")
    return f"Codex CLI exited {code}" + (f": {detail}" if detail else "")


def validate_codex_route(route: dict) -> None:
    """Shared by profile validation and dispatch; route is the dict form."""
    if (
        route.get("provider") != "chatgpt-subscription"
        or route.get("endpoint") != "https://chatgpt.com/backend-api"
        or route.get("api")
    ):
        raise ValueError("Codex requires its native first-party subscription route")
    auth_env = route.get("auth_env")
    if not auth_env or auth_env in {"OPENAI_API_KEY", "CODEX_API_KEY"}:
        raise ValueError("Codex requires an explicit subscription credential reference")
    if not re.fullmatch(r"gpt-[0-9][a-z0-9.-]+", route.get("model") or ""):
        raise ValueError("Codex requires an explicit GPT model ID")
    if route.get("effort", "high") not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("unsupported Codex effort")


class CodexHarness(StreamingCliHarness):
    name = "codex"

    @staticmethod
    def validate_route(route):
        validate_codex_route(vars(route))

    def __init__(self, binary="codex"):
        super().__init__(shutil.which(binary) or binary)

    def start(self, prompt, workspace, extra=None):
        extra = extra or {}
        route = extra["route"]
        validate_codex_route(route)
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
            yield from self._authenticated_start(prompt, workspace, extra, agent, auth)

    def _authenticated_start(self, prompt, workspace, extra, agent, auth):
        runtime = Path(extra["runtime"])
        route = extra["route"]
        env = subscription_environment(extra["env"], agent)
        filesystem = {
            ":root": "deny",
            ":minimal": "read",
            ":workspace_roots": "read",
            str(runtime / "tmp"): "write",
            # Linux Codex creates a short-lived argv[0] helper under CODEX_HOME/tmp
            # for its own Bubblewrap sandbox. Grant only that directory; the exact
            # authentication file remains denied to every model tool process.
            str(agent / "tmp"): "read",
            str(agent / "auth.json"): "deny",
        }
        for value in extra.get("writable", []) if extra["role"] == "coder" else []:
            path = (workspace / value).resolve()
            if not path.is_relative_to(workspace.resolve()):
                raise ValueError("Codex writable path escapes workspace")
            filesystem[str(path)] = "write"
        for value in [
            *extra.get("protected", []),
            ".git",
            ".pi",
            ".claude",
            ".mcp.json",
            ".agents",
            ".github",
        ]:
            path = (workspace / value).resolve()
            if not path.is_relative_to(workspace.resolve()):
                raise ValueError("Codex protected path escapes workspace")
            if path.exists():
                filesystem[str(path)] = "read"
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
            # Built-in providers cannot be overridden in current Codex. Native
            # transport retries remain bounded by the supervised attempt deadline;
            # they do not authorize another factory attempt/provider/account.
        }
        argv = [
            *extra.get("prefix", []),
            self.binary,
            "exec",
            "--strict-config",
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

        def run(emit):
            nonlocal error

            def handle(line):
                nonlocal session_id, terminal, error
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
                        emit(HarnessEvent("session", data={"session_id": sid}))
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
                    emit(HarnessEvent("event", data=event))
                except (ValueError, KeyError, TypeError) as exc:
                    error = str(exc)
                    self._cancel.set()

            lines = JsonLines(handle)
            try:
                code, _, stderr = CommandRunner(
                    workspace, extra["max_output_bytes"]
                ).supervise(
                    argv,
                    prompt.encode(),
                    extra["timeout_seconds"],
                    workspace,
                    env,
                    should_stop=self._cancel.is_set,
                    on_output=lines,
                    on_start=extra.get("on_start"),
                )
                if code:
                    error = error or redacted_cli_error(code, stderr, auth)
                elif lines.incomplete or not terminal or not answer:
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

        # Leaving the generator early stops Codex before credentials are erased.
        yield from self._stream(run, stop_on_exit=True)

    def attach_command(self, session_id):
        return [self.binary, "resume", session_id]
