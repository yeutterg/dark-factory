"""Read-only deployment diagnostics, separate from run authority and qualification."""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import asdict
from pathlib import Path

from dark_factory.adapters.git.local import pin
from dark_factory.adapters.harness.registry import adapter_for
from dark_factory.workflow import runtime_for


def inspect_configuration(cfg):
    checks = []

    def check(name, action):
        try:
            details = action()
        except (RuntimeError, ValueError, OSError) as exc:
            checks.append({"name": name, "passed": False, "reason": str(exc)})
        else:
            checks.append({"name": name, "passed": True, "details": details})

    # No Store/Controller: do not create, lock, migrate, recover or read live state.
    check("workflow", lambda: runtime_for(cfg, None).validate())
    for repo, source in cfg.sources.items():
        check(
            "source:" + repo,
            lambda s=source: {"commit": pin(Path(s["path"]), s["revision"])},
        )
    for name, command in cfg.commands.items():

        def executable(command=command):
            if not shutil.which(command.command[0]):
                raise RuntimeError("configured command executable is unavailable")
            return {"available": True, "executed": False}

        check("command:" + name, executable)
    roles = set()
    for name, route in cfg.routes.items():
        adapter = adapter_for(route.harness)

        def harness(adapter=adapter, route=route):
            if not cfg.workers:
                adapter.open(cfg, asdict(route))
            return {
                "harness": adapter.name,
                "version": route.harness_version,
                "evidence_kind": route.evidence_kind,
                "placement": "remote-worker" if cfg.workers else "local",
            }

        check("route:" + name, harness)
        if checks[-1]["passed"]:
            roles.update(adapter.roles)
        if route.harness != "fake":
            checks.append(
                {
                    "name": "credential-source:" + name,
                    "passed": bool(os.environ.get(route.auth_env))
                    or bool(cfg.workers)
                    or (
                        route.harness == "pi"
                        and not cfg.project_id
                        and route.provider == "together"
                        and route.auth_env == "TOGETHER_API_KEY"
                    ),
                    "requirement": "explicit environment credential or configured harness-native login source; validity unverified",
                }
            )
    missing_roles = sorted({"planner", "critic", "coder", "reviewer"} - roles)
    checks.append(
        {"name": "roles", "passed": not missing_roles, "missing": missing_roles}
    )
    live = any(route.evidence_kind == "live" for route in cfg.routes.values())
    if live:
        checks.append(
            {
                "name": "operator-channel",
                "passed": len(cfg.operator_token) >= 32
                and len(cfg.worker_token) >= 32
                and cfg.operator_token != cfg.worker_token,
                "requirement": "distinct private random worker/operator credentials, at least 32 characters",
            }
        )
        checks.append(
            {
                "name": "native-isolation",
                "passed": bool(cfg.workers)
                or platform.system() == "Darwin"
                and Path("/usr/bin/sandbox-exec").is_file()
                and cfg.execution.get("profile") == "macos-sandbox",
            }
        )
        checks.append(
            {
                "name": "human-identities",
                "passed": bool(cfg.humans),
            }
        )
        checks.append(
            {
                "name": "live-evidence-policy",
                "passed": bool(cfg.sources)
                and all(s.get("require_live_evidence") for s in cfg.sources.values()),
            }
        )
    # Presence is not authentication validity. Never extract logins or print values.
    credential_references = [
        {
            "route": name,
            "environment_present": bool(os.environ.get(route.auth_env)),
            "native_login_possible": route.harness == "pi"
            and not cfg.project_id
            and route.provider == "together"
            and route.auth_env == "TOGETHER_API_KEY",
        }
        for name, route in cfg.routes.items()
        if route.auth_env
    ]
    return {
        "preflight_passed": all(c["passed"] for c in checks),
        "live_routes_configured": live,
        "checks": checks,
        "credential_references": credential_references,
        "qualification": "not established by preflight; requires an approved live end-to-end outcome",
        "authority": "no inference, spending, plan approval, acceptance, merge or installation authorized",
        "capability_limits": [
            "single-repository roots only; required-child graphs unsupported",
            "hosted-CI continuation and automated publishing unsupported",
            "Claude Code is read-only; live Codex admission remains blocked",
        ],
    }
