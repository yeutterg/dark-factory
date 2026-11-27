"""Read-only deployment diagnostics, separate from run authority and qualification."""

from __future__ import annotations

import platform
import shutil
from dataclasses import asdict
from pathlib import Path

from dark_factory.adapters.git.local import pin
from dark_factory.adapters.harness.registry import adapter_for
from dark_factory.secrets import secret


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
    # The code-host credential should be a fine-grained token scoped to the
    # one repository, not a classic token that reaches every repository.
    token = secret("GH_TOKEN") or secret("GITHUB_TOKEN")
    if token:
        checks.append(
            {
                "name": "github-token-scope",
                "passed": not token.startswith(("ghp_", "gho_")),
                "requirement": "a fine-grained token limited to the configured repositories "
                "(github_pat_… or an app installation token), not a classic token",
            }
        )
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
                    "passed": bool(secret(route.auth_env)) or bool(cfg.workers),
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
                "passed": len(cfg.operator_token) >= 32,
                "requirement": "private random operator credential, at least 32 characters",
            }
        )
        checks.append(
            {
                "name": "native-isolation",
                "passed": all(
                    cfg.environments[r.environment]["kind"] != "native"
                    and (
                        cfg.environments[r.environment]["kind"] != "macos-sandbox"
                        or platform.system() == "Darwin"
                        and Path("/usr/bin/sandbox-exec").is_file()
                    )
                    for r in cfg.routes.values()
                    if r.evidence_kind == "live"
                ),
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
            "environment_present": bool(secret(route.auth_env)),
            "native_login_possible": False,
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
            "Claude Code is read-only; Codex qualification is route/environment-specific",
        ],
    }
