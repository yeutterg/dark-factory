"""Validated deployment bindings; inference identity never chooses an executor."""

from __future__ import annotations

import re


def validate_deployment(raw, routes):
    from dark_factory.config import ConfigError

    project = raw.get("project", {}).get("id", "")
    enabled = bool(
        project or any(k in raw for k in ("workers", "environments", "role_routes"))
    )
    if not enabled:
        return ""
    if not isinstance(project, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{2,63}", project
    ):
        raise ConfigError("distributed deployment requires stable project.id")
    envs = raw.get("environments", {})
    if not isinstance(envs, dict) or not envs:
        raise ConfigError("project-scoped deployment requires environments")
    for name, recipe in envs.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or not isinstance(recipe, dict):
            raise ConfigError("invalid environment identity")
        allowed = {
            "kind",
            "image",
            "binary",
            "cpu",
            "memory_mb",
            "pids",
            "qualification",
            "argv",
            "region",
            "geography_evidence",
            "driver_files",
            "harness_binaries",
            "command_binaries",
            "network",
            "daemon_host",
        }
        if set(recipe) - allowed or recipe.get("kind") not in {
            "native",
            "macos-sandbox",
            "oci",
            "command",
        }:
            raise ConfigError("unsupported environment recipe")
        if recipe["kind"] == "oci" and not re.fullmatch(
            r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[a-f0-9]{64}", str(recipe.get("image", ""))
        ):
            raise ConfigError("OCI images must be digest pinned")
        if "network" in recipe and (
            not isinstance(recipe["network"], str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]+", recipe["network"])
        ):
            raise ConfigError(
                "network must name an explicitly configured project gateway"
            )
        if "daemon_host" in recipe and (
            not isinstance(recipe["daemon_host"], str)
            or not recipe["daemon_host"].startswith("unix:///")
        ):
            raise ConfigError(
                "OCI broker daemon must use an explicitly pinned local Unix socket"
            )
        for field, default in (("cpu", 1), ("memory_mb", 512), ("pids", 128)):
            if (
                type(recipe.get(field, default)) is not int
                or recipe.get(field, default) < 1
            ):
                raise ConfigError(
                    "environment resource limits must be positive integers"
                )
        if recipe["kind"] == "command":
            argv = recipe.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(v, str) and v for v in argv)
            ):
                raise ConfigError("external sandbox requires a named argv contract")
            files = recipe.get("driver_files", {})
            if (
                not isinstance(files, dict)
                or not files
                or any(
                    not isinstance(k, str)
                    or not k.startswith("/")
                    or not isinstance(v, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", v)
                    for k, v in files.items()
                )
            ):
                raise ConfigError(
                    "external sandbox drivers require pinned file digests"
                )
        for key in ("harness_binaries", "command_binaries"):
            values = recipe.get(key, {})
            if not isinstance(values, dict) or any(
                not isinstance(v, str) or not v.startswith("/") for v in values.values()
            ):
                raise ConfigError("environment binary overrides must be absolute paths")
        if recipe.get("command_binaries") and recipe["kind"] != "oci":
            raise ConfigError(
                "portable command binaries require a digest-pinned OCI image"
            )
    roles = raw.get("role_routes", {})
    if not isinstance(roles, dict) or set(roles) != {
        "planner",
        "critic",
        "coder",
        "reviewer",
    }:
        raise ConfigError("pin all four role_routes explicitly")
    from dark_factory.adapters.harness.registry import adapter_for

    bindings = {}
    for route in routes.values():
        if route.project_id != project or route.environment not in envs:
            raise ConfigError("route project/environment binding mismatch")
        if route.account_kind not in {"api", "subscription", "simulation"}:
            raise ConfigError("route account_kind must be explicit")
        if route.harness == "fake" and route.account_kind != "simulation":
            raise ConfigError("fake accounts are simulation only")
        if route.harness != "fake" and not route.auth_env:
            raise ConfigError("project routes require explicit external credentials")
        if route.harness != "fake" and not route.auth_env.startswith(
            project.upper().replace("-", "_") + "_"
        ):
            raise ConfigError("credential references must be project namespaced")
        if route.harness == "claude-code" and route.account_kind != "subscription":
            raise ConfigError("Claude Code requires a subscription account")
        if route.harness == "codex" and route.account_kind != "subscription":
            raise ConfigError("Codex requires a subscription account")
        if route.harness == "pi" and route.account_kind != "api":
            raise ConfigError(
                "this Pi adapter requires an API account; subscriptions use their native CLI adapter"
            )
        if route.auth_env:
            identity = (route.provider, route.billing_account, route.account_kind)
            if route.auth_env in bindings and bindings[route.auth_env] != identity:
                raise ConfigError(
                    "one credential reference cannot name different accounts"
                )
            bindings[route.auth_env] = identity
        recipe = envs[route.environment]
        if route.harness != "fake" and recipe["kind"] == "native":
            raise ConfigError(
                "real harnesses always require isolation, even with simulation evidence"
            )
        if route.evidence_kind == "live":
            if recipe["kind"] == "oci" and not recipe.get("network"):
                raise ConfigError(
                    "live OCI inference requires a project-owned internal gateway network"
                )
            if recipe["kind"] == "native" or not recipe.get("qualification"):
                raise ConfigError(
                    "live routes require an explicitly qualified isolated environment"
                )
            if (
                not recipe.get("region")
                or recipe["region"].upper() in {"UNKNOWN", "CN", "CHINA", "PRC"}
                or not recipe.get("geography_evidence")
            ):
                raise ConfigError(
                    "execution geography must be pinned independently of inference"
                )
    for role, rid in roles.items():
        if rid not in routes or role not in adapter_for(routes[rid].harness).roles:
            raise ConfigError("role route lacks harness capability")
    workers = raw.get("workers", {})
    if not isinstance(workers, dict) or not workers:
        raise ConfigError(
            "project-scoped deployments require separately registered workers"
        )
    refs = set()
    for name, item in workers.items():
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]+", name)
            or not isinstance(item, dict)
            or not {"token_env", "environments"} <= set(item)
            or set(item) - {"token_env", "environments", "lease_seconds"}
        ):
            raise ConfigError("worker requires token_env and environments")
        ref = item["token_env"]
        if (
            not isinstance(ref, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", ref)
            or ref in refs
        ):
            raise ConfigError("workers require distinct external credentials")
        refs.add(ref)
        if (
            type(item.get("lease_seconds", 30)) is not int
            or not 30 <= item.get("lease_seconds", 30) <= 300
        ):
            raise ConfigError("worker lease_seconds must be between 30 and 300")
        choices = item["environments"]
        if (
            not isinstance(choices, list)
            or not choices
            or any(e not in envs for e in choices)
        ):
            raise ConfigError("worker environment is not configured")
    reserved = refs | {
        raw.get("runtime", {}).get(k)
        for k in ("worker_token_env", "operator_token_env")
    }
    if any(r.auth_env and r.auth_env in reserved for r in routes.values()):
        raise ConfigError(
            "inference credentials cannot use worker/operator credential references"
        )
    return project


def selected_route(cfg, role):
    if cfg.project_id:
        return cfg.routes[cfg.role_routes[role]]
    from dark_factory.adapters.harness.registry import adapter_for

    eligible = [r for r in cfg.routes.values() if role in adapter_for(r.harness).roles]
    exact = [r for r in eligible if r.role == role]
    # A route approved for one role never silently serves another with live
    # authority; simulation fixtures may share one route across roles.
    shared = [r for r in eligible if r.evidence_kind == "simulation"]
    return (exact or shared or [None])[0]


def recipe_for(cfg, route):
    if cfg.project_id:
        return cfg.environments[
            route["environment"]
            if route
            else cfg.routes[cfg.role_routes["coder"]].environment
        ]
    return {"kind": cfg.execution.get("profile", "native")}
