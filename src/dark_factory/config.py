"""Load and validate factory.toml. Relative paths resolve against the config file."""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
import tomllib
import zoneinfo
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from dark_factory.secrets import secret

SCHEMA_VERSION = 1
# Every optional setting has exactly one default, shared by validation and use.
DEFAULT_DISK = {
    "min_free_gb": 2,
    "max_state_gb": 0,
    "keep_finished_days": 7,
    "keep_audit_days": 90,
}
DEFAULT_IMPROVEMENT = {
    "max_files": 3,
    "max_changed_lines": 80,
    "max_open": 1,
    "cooldown_days": 7,
    "budget_cents": 500,
    "threshold": 3,
}
BUDGET_DEFAULTS = {"root_cents": 1000, "planning_cents": 100, "account_cents": 10000}
DEFAULT_MAX_REPAIRS = 0
DEFAULT_MAX_RESUMES = 1
MAX_RESUMES_LIMIT = 3
DEFAULT_DECIDER_CONFIDENCE = 0.8
DEFAULT_TASK_SUPERVISOR_REQUESTS = 20
DEFAULT_TASK_SUPERVISOR_INPUT_TOKENS = 4000
DEFAULT_TASK_SUPERVISOR_OUTPUT_TOKENS = 800
DEFAULT_MAX_SECONDS = 7200
DEFAULT_LEASE_SECONDS = 30
BLOCKED_REGIONS = {"CN", "CHINA", "PRC"}


class ConfigError(ValueError):
    pass


@dataclass
class Route:
    id: str
    model: str
    harness: str
    harness_version: str
    provider: str
    endpoint: str
    billing_account: str
    region: str
    role: str
    max_cents: int = 0
    auth_env: str = ""
    geography_evidence: str = ""
    api: str = ""
    evidence_kind: str = "simulation"
    effort: str = "high"
    reasoning: bool = False
    context_window: int = 128000
    max_tokens: int = 8192
    pricing: dict[str, float] = field(default_factory=dict)
    pricing_evidence: str = ""
    project_id: str = ""
    account_kind: str = ""
    environment: str = ""


@dataclass
class IntegrationCommand:
    id: str
    command: list[str]
    timeout_seconds: int = 120


@dataclass
class Window:
    days: list[str]
    start: str
    end: str
    timezone: str


@dataclass
class FactoryConfig:
    schema_version: int
    project_name: str
    state_dir: Path
    source_path: Path
    digest: str
    repositories: list[str]
    labels: dict[str, str]
    routes: dict[str, Route]
    windows: list[Window]
    date_exceptions: list[str]
    commands: dict[str, IntegrationCommand]
    prompts: dict[str, str]
    skills: list[str]
    role_skills: dict[str, list[str]]
    definition: dict[str, Any]
    geography_policy: list[str]
    api_host: str
    api_port: int
    default_timeout_seconds: int
    max_output_bytes: int
    raw: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    operator_token: str = ""
    project_id: str = ""

    @classmethod
    def replica(cls, state_dir, source_path, execution, project_id, max_output_bytes):
        """Settings for executing one assignment in a private attempt replica."""
        return cls(
            schema_version=SCHEMA_VERSION,
            project_name="",
            state_dir=Path(state_dir),
            source_path=Path(source_path),
            digest="",
            repositories=[],
            labels={},
            routes={},
            windows=[],
            date_exceptions=[],
            commands={},
            prompts={},
            skills=[],
            role_skills={},
            definition={},
            geography_policy=[],
            api_host="",
            api_port=0,
            default_timeout_seconds=0,
            max_output_bytes=max_output_bytes,
            execution=dict(execution),
            project_id=project_id,
        )

    # Typed views over validated tables. They read raw on access so an operator
    # reload (or a test) that replaces a table is reflected without re-parsing.
    def _table(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    @property
    def budgets(self) -> dict[str, int]:
        return {**BUDGET_DEFAULTS, **self._table("budgets")}

    @property
    def max_repairs(self) -> int:
        return self._table("continuation").get("max_repairs", DEFAULT_MAX_REPAIRS)

    @property
    def max_resumes(self) -> int:
        return self._table("continuation").get("max_resumes", DEFAULT_MAX_RESUMES)

    @property
    def decider(self) -> dict[str, Any]:
        """The optional decision agent: command name, region and confidence floor."""
        table = self._table("decider")
        if not table:
            return {}
        return {
            "command": table.get("command", "decider"),
            "region": table["region"],
            "min_confidence": table.get("min_confidence", DEFAULT_DECIDER_CONFIDENCE),
        }

    @property
    def runners(self) -> dict[str, dict[str, Any]]:
        """Runners the profile grants; `local` always exists, with no secrets."""
        from dark_factory.runners import runner_table

        return runner_table(self.raw)

    @property
    def gate(self) -> dict[str, Any]:
        """The required pull-request status; off until a repository adopts it."""
        table = self._table("gate")
        return {
            "enabled": table.get("enabled", False),
            "repositories": list(table.get("repositories", [])),
            "interval_seconds": table.get("interval_seconds", 60),
        }

    @property
    def contributor_token(self) -> str:
        """The credential an outside coding agent uses; it cannot approve or accept."""
        reference = self._table("runtime").get("contributor_token_env")
        return secret(reference) if reference else ""

    @property
    def disk(self) -> dict[str, int]:
        """The disk the factory may use. New work is refused below the floor."""
        table = self._table("disk")
        return {
            "min_free_gb": table.get("min_free_gb", DEFAULT_DISK["min_free_gb"]),
            "max_state_gb": table.get("max_state_gb", DEFAULT_DISK["max_state_gb"]),
            "keep_finished_days": table.get(
                "keep_finished_days", DEFAULT_DISK["keep_finished_days"]
            ),
            "keep_audit_days": table.get(
                "keep_audit_days", DEFAULT_DISK["keep_audit_days"]
            ),
        }

    @property
    def improvement(self) -> dict[str, Any]:
        """Self-improvement: on unless disabled, always inside code-enforced limits."""
        table = self._table("improvement")
        threshold = table.get("threshold") or self.definition.get(
            "self_improvement", {}
        ).get("failed_run_threshold")
        return {
            "enabled": table.get("enabled", True),
            "repository": table.get("repository")
            or (self.repositories[0] if len(self.repositories) == 1 else ""),
            "base_branch": table.get("base_branch", "main"),
            "allowed_paths": list(table.get("allowed_paths", [])),
            "protected_paths": list(table.get("protected_paths", [])),
            "max_files": table.get("max_files", DEFAULT_IMPROVEMENT["max_files"]),
            "max_changed_lines": table.get(
                "max_changed_lines", DEFAULT_IMPROVEMENT["max_changed_lines"]
            ),
            "max_open": table.get("max_open", DEFAULT_IMPROVEMENT["max_open"]),
            "cooldown_days": table.get(
                "cooldown_days", DEFAULT_IMPROVEMENT["cooldown_days"]
            ),
            "budget_cents": table.get(
                "budget_cents", DEFAULT_IMPROVEMENT["budget_cents"]
            ),
            "threshold": threshold or DEFAULT_IMPROVEMENT["threshold"],
        }

    @property
    def task_supervisor(self) -> dict[str, Any]:
        """Optional bounded structured-output task reconciler."""
        table = self._table("task_supervisor")
        if not table:
            return {}
        return {
            "command": table.get("command", "task-supervisor"),
            "region": table["region"],
            "model": table["model"],
            "provider": table["provider"],
            "pricing_evidence": table["pricing_evidence"],
            "max_requests_per_root": table.get(
                "max_requests_per_root", DEFAULT_TASK_SUPERVISOR_REQUESTS
            ),
            "budget_cents": table.get("budget_cents", 0),
            "max_input_tokens": table.get(
                "max_input_tokens", DEFAULT_TASK_SUPERVISOR_INPUT_TOKENS
            ),
            "max_output_tokens": table.get(
                "max_output_tokens", DEFAULT_TASK_SUPERVISOR_OUTPUT_TOKENS
            ),
        }

    @property
    def max_seconds(self) -> int:
        return self.execution.get("max_seconds", DEFAULT_MAX_SECONDS)

    @property
    def humans(self) -> list[str]:
        return self._table("operators").get("humans", [])

    @property
    def role_routes(self) -> dict[str, str]:
        return self._table("role_routes")

    @property
    def environments(self) -> dict[str, dict[str, Any]]:
        return self._table("environments")

    @property
    def workers(self) -> dict[str, dict[str, Any]]:
        return self._table("workers")

    def lease_seconds(self, worker_id: str) -> int:
        return self.workers.get(worker_id, {}).get(
            "lease_seconds", DEFAULT_LEASE_SECONDS
        )


def _require(d: dict[str, Any], key: str, typ: type) -> Any:
    if key not in d:
        raise ConfigError(f"missing required field {key!r}")
    val = d[key]
    if type(val) is not typ:
        raise ConfigError(f"{key!r} must be {typ.__name__}, got {type(val).__name__}")
    return val


def _resolve(base: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((base / path).resolve())


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_toml_bytes(data: bytes, source_path: Path) -> FactoryConfig:
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    return validate_raw(raw, source_path, _hash_bytes(data))


def load_config(path: str | os.PathLike[str]) -> FactoryConfig:
    source = Path(path).resolve()
    if not source.is_file():
        raise ConfigError(f"configuration file not found: {source}")
    return parse_toml_bytes(source.read_bytes(), source)


def load_operator_token(path: str | os.PathLike[str]) -> str:
    """Read only the operator credential needed by a remote CLI or TUI.

    A remote operator is a client of the running controller. It must not need
    the controller's worker credential, source checkout, route credentials or
    installed execution dependencies merely to connect.
    """

    source = Path(path).resolve()
    if not source.is_file():
        raise ConfigError(f"configuration file not found: {source}")
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    runtime = raw.get("runtime")
    if not isinstance(runtime, dict):
        raise ConfigError("runtime must be a table")
    reference = runtime.get("operator_token_env")
    if reference is not None:
        if not isinstance(reference, str) or not reference:
            raise ConfigError("runtime.operator_token_env must be a non-empty string")
        token = secret(reference)
    else:
        token = runtime.get("operator_token", "")
        if not isinstance(token, str):
            raise ConfigError("runtime.operator_token must be a string")
    if not token:
        raise ConfigError("runtime.operator_token is required for remote operators")
    return token


def load_operator_endpoint(path: str | os.PathLike[str]) -> str:
    """Build the loopback operator URL without loading controller settings."""

    source = Path(path).resolve()
    if not source.is_file():
        raise ConfigError(f"configuration file not found: {source}")
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    runtime = raw.get("runtime")
    if not isinstance(runtime, dict):
        raise ConfigError("runtime must be a table")
    host = runtime.get("api_host", "127.0.0.1")
    port = runtime.get("api_port", 8745)
    if not isinstance(host, str) or not host:
        raise ConfigError("runtime.api_host must be a non-empty string")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("runtime.api_port must be an integer from 1 to 65535")
    # Bind-all addresses are server settings, not client destinations.
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    client_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{client_host}:{port}"


def _fields(table: dict[str, Any], allowed: set[str], name: str) -> None:
    extra = set(table) - allowed
    if extra:
        raise ConfigError(f"unknown {name} fields: {', '.join(sorted(extra))}")


def _integer(table: dict[str, Any], key: str, default: int, minimum: int = 0) -> int:
    value = table.get(key, default)
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


def _parse_routes(
    raw: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Route], set[str]]:
    """Routes plus the geography checks that cover them and the optional decision agents.

    Returns (raw routes table, routes, blocked regions)."""
    routes_raw = _require(raw, "routes", dict)
    routes: dict[str, Route] = {}
    policy = raw.get("geography_policy", {"blocked_regions": ["CN"]})
    if not isinstance(policy, dict):
        raise ConfigError("geography_policy must be a table")
    regions = policy.get("blocked_regions", ["CN"])
    if not isinstance(regions, list) or not all(isinstance(x, str) for x in regions):
        raise ConfigError("blocked_regions must be a list of strings")
    blocked = {x.upper() for x in regions}
    blocked |= BLOCKED_REGIONS
    decider = raw.get("decider", {})
    if not isinstance(decider, dict):
        raise ConfigError("decider must be a table")
    _fields(decider, {"command", "region", "min_confidence"}, "decider")
    if decider:
        # A decision agent is inference: its processing region is declared and checked.
        region = decider.get("region")
        if not isinstance(region, str) or not region.strip():
            raise ConfigError("decider.region is required; unknown geography blocks it")
        if region.upper() in blocked:
            raise ConfigError("decider region is blocked by geography policy")
        confidence = decider.get("min_confidence", DEFAULT_DECIDER_CONFIDENCE)
        if type(confidence) not in {int, float} or not 0.5 <= confidence <= 1:
            raise ConfigError("decider.min_confidence must be between 0.5 and 1")
    task_supervisor = raw.get("task_supervisor", {})
    if not isinstance(task_supervisor, dict):
        raise ConfigError("task_supervisor must be a table")
    _fields(
        task_supervisor,
        {
            "command",
            "region",
            "max_requests_per_root",
            "budget_cents",
            "max_input_tokens",
            "max_output_tokens",
            "model",
            "provider",
            "pricing_evidence",
        },
        "task_supervisor",
    )
    if task_supervisor:
        region = task_supervisor.get("region")
        if not isinstance(region, str) or not region.strip():
            raise ConfigError(
                "task_supervisor.region is required; unknown geography blocks it"
            )
        if region.upper() in blocked:
            raise ConfigError("task supervisor region is blocked by geography policy")
        for key in ("model", "provider", "pricing_evidence"):
            if (
                not isinstance(task_supervisor.get(key), str)
                or not task_supervisor[key].strip()
            ):
                raise ConfigError(f"task_supervisor.{key} is required")
        for key, default, minimum in (
            ("max_requests_per_root", DEFAULT_TASK_SUPERVISOR_REQUESTS, 1),
            ("budget_cents", 0, 0),
            ("max_input_tokens", DEFAULT_TASK_SUPERVISOR_INPUT_TOKENS, 1),
            ("max_output_tokens", DEFAULT_TASK_SUPERVISOR_OUTPUT_TOKENS, 1),
        ):
            _integer(task_supervisor, key, default, minimum)

    for rid, item in routes_raw.items():
        if not isinstance(item, dict):
            raise ConfigError(f"route {rid!r} must be a table")
        _fields(
            item,
            {
                "model",
                "harness",
                "harness_version",
                "provider",
                "endpoint",
                "billing_account",
                "region",
                "role",
                "max_cents",
                "auth_env",
                "geography_evidence",
                "api",
                "evidence_kind",
                "effort",
                "reasoning",
                "context_window",
                "max_tokens",
                "pricing",
                "pricing_evidence",
                "project_id",
                "account_kind",
                "environment",
            },
            "route",
        )
        for key in (
            "harness_version",
            "endpoint",
            "billing_account",
            "role",
            "auth_env",
            "geography_evidence",
            "api",
            "effort",
            "pricing_evidence",
        ):
            if key in item:
                _require(item, key, str)
        region = str(_require(item, "region", str)).strip()
        if not region or region.upper() in {"UNKNOWN", "UNSPECIFIED", ""}:
            raise ConfigError(
                f"route {rid!r} has unknown processing geography and is blocked"
            )
        if region.upper() in blocked:
            raise ConfigError(
                f"route {rid!r} region {region!r} is blocked by operator policy"
            )
        evidence_kind = item.get(
            "evidence_kind", "simulation" if item.get("harness") == "fake" else "live"
        )
        if (
            not isinstance(evidence_kind, str)
            or evidence_kind not in {"simulation", "live"}
            or (item.get("harness") == "fake" and evidence_kind != "simulation")
        ):
            raise ConfigError(
                "invalid route evidence_kind; fake routes are simulation only"
            )
        pricing = item.get("pricing", {})
        if not isinstance(pricing, dict):
            raise ConfigError("route pricing must be a table")
        if pricing and (
            set(pricing) != {"input", "output", "cache_read", "cache_write"}
            or any(
                type(v) not in {int, float} or not math.isfinite(v) or v < 0
                for v in pricing.values()
            )
        ):
            raise ConfigError(
                "pricing needs four finite non-negative per-million-token rates"
            )
        routes[rid] = Route(
            id=rid,
            model=_require(item, "model", str),
            harness=_require(item, "harness", str),
            harness_version=str(item.get("harness_version", "unspecified")),
            provider=_require(item, "provider", str),
            endpoint=str(item.get("endpoint", "")),
            billing_account=str(item.get("billing_account", "")),
            region=region,
            role=str(item.get("role", "coder")),
            max_cents=_integer(item, "max_cents", 0),
            auth_env=str(item.get("auth_env", "")),
            geography_evidence=str(item.get("geography_evidence", "")),
            api=str(item.get("api", "")),
            evidence_kind=evidence_kind,
            effort=str(item.get("effort", "high")),
            reasoning=bool(item.get("reasoning", False)),
            context_window=_integer(item, "context_window", 128000, 1),
            max_tokens=_integer(item, "max_tokens", 8192, 1),
            pricing=dict(pricing),
            pricing_evidence=str(item.get("pricing_evidence", "")),
            project_id=str(item.get("project_id", "")),
            account_kind=str(item.get("account_kind", "")),
            environment=str(item.get("environment", "")),
        )
    if not routes:
        raise ConfigError("at least one route is required")
    return routes_raw, routes, blocked


def _load_definition(raw: dict[str, Any], source_path: Path) -> dict[str, Any]:
    """The Warp definition the profile points at, or an empty one."""
    definition_table = raw.get("definition", {})
    if not isinstance(definition_table, dict):
        raise ConfigError("definition must be a table")
    _fields(definition_table, {"format", "path", "strict", "roles"}, "definition")
    definition: dict[str, Any] = {}
    if definition_table:
        if definition_table.get("format") != "warp-v1alpha1":
            raise ConfigError("definition.format must be warp-v1alpha1")
        definition_path = definition_table.get("path")
        if not isinstance(definition_path, str) or not definition_path.strip():
            raise ConfigError("definition.path is required")
        strict = definition_table.get("strict", True)
        if type(strict) is not bool:
            raise ConfigError("definition.strict must be a boolean")
        from dark_factory.adapters.definition.warp import (
            WarpDefinitionError,
            load_definition,
        )

        role_choices = definition_table.get("roles", {})
        if not isinstance(role_choices, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in role_choices.items()
        ):
            raise ConfigError("definition.roles must map a role to an agent name")
        try:
            definition = load_definition(
                _resolve(source_path.parent, definition_path),
                strict=strict,
                roles=role_choices,
            )
        except WarpDefinitionError as exc:
            raise ConfigError(f"invalid Warp definition: {exc}") from exc
    return definition


def _normalize_sources(
    raw: dict[str, Any],
    repositories: list[str],
    commands: dict[str, IntegrationCommand],
    source_path: Path,
) -> dict[str, dict[str, Any]]:
    """Validate each repository source: pinned revision, checks, and a literal scope."""
    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        raise ConfigError("sources must be a table")
    normalized_sources = {}
    for repo, source in sources.items():
        if repo not in repositories or not isinstance(source, dict):
            raise ConfigError("sources must name configured repositories")
        _fields(
            source,
            {
                "path",
                "revision",
                "checks",
                "allowed_paths",
                "protected_paths",
                "manual_acceptance",
                "external_acceptance",
                "require_live_evidence",
                "ephemeral_paths",
                "pipeline_ref",
            },
            "source",
        )
        if "require_live_evidence" in source:
            _require(source, "require_live_evidence", bool)
        path = Path(_resolve(source_path.parent, _require(source, "path", str)))
        if not path.is_dir():
            raise ConfigError(f"source repository does not exist: {path}")
        revision = _require(source, "revision", str)
        if not revision or revision.startswith("-"):
            raise ConfigError("source revision is required")
        for key in (
            "checks",
            "allowed_paths",
            "protected_paths",
            "manual_acceptance",
            "external_acceptance",
            "ephemeral_paths",
        ):
            values = source.get(key, [])
            if not isinstance(values, list) or not all(
                isinstance(v, str) and v for v in values
            ):
                raise ConfigError(f"source {key} must be a list of strings")
        for requirement in source.get("external_acceptance", []):
            kind, _, ident = requirement.partition(":")
            if kind not in {"check", "status", "review"} or not ident:
                raise ConfigError(
                    "external_acceptance entries must be check:<name>, "
                    f"status:<context> or review:<login>, not {requirement!r}"
                )
        if not source.get("protected_paths"):
            raise ConfigError(
                "sources must explicitly pin protected verification definitions"
            )
        if not source.get("checks") or not source.get("allowed_paths"):
            raise ConfigError(
                "source needs canonical checks and explicit allowed_paths"
            )
        for cid in source["checks"]:
            if cid not in commands:
                raise ConfigError(f"unknown verification command {cid}")
        for path_name in source["allowed_paths"] + source.get("protected_paths", []):
            if (
                Path(path_name).is_absolute()
                or ".." in Path(path_name).parts
                or path_name in {".", ".git"}
                or any(c in path_name for c in "*?[")
            ):
                raise ConfigError(
                    "scope paths must be literal repository-relative files or directories"
                )
        normalized_sources[repo] = {**source, "path": str(path), "revision": revision}
        for ephemeral in source.get("ephemeral_paths", []):
            if (
                Path(ephemeral).is_absolute()
                or ".." in Path(ephemeral).parts
                or ephemeral in {".", ".git", ""}
                or any(c in ephemeral for c in "*?[\n")
            ):
                raise ConfigError(
                    "ephemeral paths must be literal owned output directories"
                )
            for required_path in (
                source["allowed_paths"] + source["protected_paths"] + [".git"]
            ):
                if (
                    ephemeral == required_path
                    or ephemeral.startswith(required_path + "/")
                    or required_path.startswith(ephemeral + "/")
                ):
                    raise ConfigError(
                        "ephemeral outputs cannot overlap source or protected inputs"
                    )
    return normalized_sources


def _validate_improvement(
    raw: dict[str, Any],
    repositories: list[str],
    sources: dict[str, dict[str, Any]],
    state_dir: Path,
) -> None:
    table = raw.get("improvement", {})
    if not isinstance(table, dict):
        raise ConfigError("improvement must be a table")
    _fields(
        table,
        {
            "enabled",
            "repository",
            "base_branch",
            "allowed_paths",
            "protected_paths",
            "max_files",
            "max_changed_lines",
            "max_open",
            "cooldown_days",
            "budget_cents",
            "threshold",
        },
        "improvement",
    )
    if "enabled" in table:
        _require(table, "enabled", bool)
    for key in ("max_files", "max_changed_lines", "max_open", "cooldown_days"):
        _integer(table, key, 1, 1)
    _integer(table, "budget_cents", 1, 1)
    if "threshold" in table:
        _integer(table, "threshold", 3, 2)
    for key in ("allowed_paths", "protected_paths"):
        values = table.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(v, str) and v for v in values
        ):
            raise ConfigError(f"improvement.{key} must be a list of strings")
        for name in values:
            if (
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or name in {".", ".git"}
                or any(c in name for c in "*?[")
            ):
                raise ConfigError(
                    "improvement scope paths must be literal repository-relative paths"
                )
    repository = table.get("repository")
    if repository is not None and repository not in repositories:
        raise ConfigError("improvement.repository must be a configured repository")
    target = sources.get(repository or "")
    if target:
        # The loop may change a separate checkout, never the installation that is
        # running it or the directory that holds its state.
        path = Path(target["path"]).resolve()
        installation = Path(__file__).resolve().parents[2]
        if path == installation or installation.is_relative_to(path):
            raise ConfigError(
                "improvement.repository cannot be the running installation; use a separate checkout"
            )
        if state_dir.resolve().is_relative_to(path) or path.is_relative_to(
            state_dir.resolve()
        ):
            raise ConfigError(
                "improvement.repository cannot contain the controller state directory"
            )


def validate_raw(raw: dict[str, Any], source_path: Path, digest: str) -> FactoryConfig:
    _fields(
        raw,
        {
            "schema_version",
            "project",
            "repositories",
            "labels",
            "routes",
            "windows",
            "date_exceptions",
            "commands",
            "prompts",
            "skills",
            "geography_policy",
            "runtime",
            "budgets",
            "sources",
            "execution",
            "operators",
            "continuation",
            "role_routes",
            "environments",
            "workers",
            "decider",
            "task_supervisor",
            "definition",
            "improvement",
            "disk",
            "gate",
            "runners",
            "secret_classes",
        },
        "profile",
    )
    for key, allowed in {
        "project": {"name", "state_dir", "id"},
        "runtime": {
            "operator_token",
            "operator_token_env",
            "contributor_token_env",
            "api_host",
            "api_port",
            "default_timeout_seconds",
            "max_output_bytes",
        },
        "budgets": {"root_cents", "planning_cents", "account_cents"},
        "geography_policy": {"blocked_regions"},
    }.items():
        table = raw.get(key, {})
        if not isinstance(table, dict):
            raise ConfigError(f"{key} must be a table")
        _fields(table, allowed, key)
    definition = _load_definition(raw, source_path)
    _integer(raw.get("budgets", {}), "root_cents", BUDGET_DEFAULTS["root_cents"])
    continuation = raw.get("continuation", {})
    if not isinstance(continuation, dict):
        raise ConfigError("continuation must be a table")
    _fields(continuation, {"max_repairs", "max_resumes"}, "continuation")
    if _integer(continuation, "max_repairs", DEFAULT_MAX_REPAIRS) > 1:
        raise ConfigError("at most one targeted correctness repair is supported")
    if _integer(continuation, "max_resumes", DEFAULT_MAX_RESUMES) > MAX_RESUMES_LIMIT:
        raise ConfigError(
            f"at most {MAX_RESUMES_LIMIT} automatic resumes per step are supported"
        )
    operators = raw.get("operators", {})
    if not isinstance(operators, dict):
        raise ConfigError("operators must be a table")
    _fields(operators, {"humans"}, "operators")
    humans = operators.get("humans", [])
    if (
        not isinstance(humans, list)
        or not all(isinstance(x, str) and x.strip() for x in humans)
        or len(set(humans)) != len(humans)
    ):
        raise ConfigError("operators.humans must contain unique named identities")
    if _integer(raw.get("runtime", {}), "api_port", 8745, 1) > 65535:
        raise ConfigError("api_port must be <= 65535")
    version = _require(raw, "schema_version", int)
    if version != SCHEMA_VERSION:
        raise ConfigError(
            f"unsupported schema_version {version}; expected {SCHEMA_VERSION}"
        )
    project = _require(raw, "project", dict)
    name = _require(project, "name", str)
    state_dir = Path(_resolve(source_path.parent, _require(project, "state_dir", str)))
    repositories = _require(raw, "repositories", list)
    if not repositories or not all(isinstance(r, str) and r for r in repositories):
        raise ConfigError("repositories must be a non-empty list of strings")
    if definition and definition["repositories"] != repositories:
        raise ConfigError(
            "Warp definition repositories must exactly match the operator profile"
        )

    labels = raw.get("labels", {})
    if not isinstance(labels, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
    ):
        raise ConfigError("labels must be a string table")

    routes_raw, routes, blocked = _parse_routes(raw)

    if definition:
        from dark_factory.adapters.harness.registry import adapter_for

        for role, requested in definition["requested_models"].items():
            eligible = [
                route
                for route in routes.values()
                if role in adapter_for(route.harness).roles
            ]
            exact = [route for route in eligible if route.role == role]
            shared = [
                route
                for route in eligible
                if route.harness == "fake" and route.evidence_kind == "simulation"
            ]
            choices = exact or shared
            selected = choices[0] if choices else None
            if selected is None:
                raise ConfigError(f"Warp agent role {role} has no approved local route")
            if requested != "auto" and selected.model != requested:
                raise ConfigError(
                    f"Warp agent role {role} requests model {requested!r}, but the "
                    f"approved local route pins {selected.model!r}"
                )
        from dark_factory.adapters.definition.warp import HARNESS_ADAPTERS

        for role, kind in definition.get("requested_harness", {}).items():
            local = [
                r.harness
                for r in routes.values()
                if r.role == role or r.harness == "fake"
            ]
            wanted = HARNESS_ADAPTERS.get(kind)
            note = (
                f"Warp agent for {role} requests harness {kind!r}, which has no local "
                "adapter; the approved local route is used"
                if wanted is None
                else f"Warp agent for {role} requests harness {kind!r}, but the "
                f"approved local route uses {local[0] if local else 'none'}"
                if wanted not in local
                else ""
            )
            if note:
                definition["compatibility"]["warnings"].append(note)

    windows: list[Window] = []
    if not isinstance(raw.get("windows", []), list):
        raise ConfigError("windows must be a list")
    for w in raw.get("windows", []):
        if not isinstance(w, dict):
            raise ConfigError("windows entries must be tables")
        _fields(w, {"days", "start", "end", "timezone"}, "window")
        days = _require(w, "days", list)
        if not days or not all(
            isinstance(d, str)
            and d in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
            for d in days
        ):
            raise ConfigError("window days must contain weekday abbreviations")
        for key in ("start", "end"):
            if not re.fullmatch(
                r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", _require(w, key, str)
            ):
                raise ConfigError("window time must be HH:MM")
        if w["start"] == w["end"]:
            raise ConfigError("window start and end must differ")
        try:
            zoneinfo.ZoneInfo(_require(w, "timezone", str))
        except (ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
            raise ConfigError("invalid window timezone") from exc
        windows.append(
            Window(
                days=list(_require(w, "days", list)),
                start=_require(w, "start", str),
                end=_require(w, "end", str),
                timezone=_require(w, "timezone", str),
            )
        )

    exceptions = raw.get("date_exceptions", [])
    if not isinstance(exceptions, list) or not all(
        isinstance(x, str) for x in exceptions
    ):
        raise ConfigError("date_exceptions must be a list of strings")

    try:
        for value in exceptions:
            date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("invalid date exception") from exc

    commands: dict[str, IntegrationCommand] = {}
    for cid, item in _require(raw, "commands", dict).items():
        if not isinstance(item, dict):
            raise ConfigError(f"command {cid!r} must be a table")
        _fields(item, {"argv", "timeout_seconds"}, "command")
        argv = _require(item, "argv", list)
        if not argv or not all(isinstance(x, str) for x in argv):
            raise ConfigError(f"command {cid!r} argv must be a list of strings")
        argv = list(argv)
        if argv[0] == "dark-factory":
            installed = Path(sys.executable).with_name("dark-factory")
            if not installed.is_file():
                raise ConfigError(
                    "dark-factory command is not installed beside the active Python"
                )
            argv[0] = str(installed.resolve())
        elif "/" in argv[0]:
            argv[0] = _resolve(source_path.parent, argv[0])
        commands[cid] = IntegrationCommand(
            id=cid,
            command=list(argv),
            timeout_seconds=_integer(item, "timeout_seconds", 120, 1),
        )
    for required in ("tracking", "code_host"):
        if required not in commands:
            raise ConfigError(f"commands.{required} is required")
    decider_table = raw.get("decider", {})
    if decider_table and decider_table.get("command", "decider") not in commands:
        raise ConfigError("decider.command must name a configured command")
    supervisor_table = raw.get("task_supervisor", {})
    if (
        supervisor_table
        and supervisor_table.get("command", "task-supervisor") not in commands
    ):
        raise ConfigError("task_supervisor.command must name a configured command")

    prompts_raw = raw.get("prompts", {})
    if not isinstance(prompts_raw, dict):
        raise ConfigError("prompts must be a table")
    _fields(prompts_raw, {"planner", "critic", "coder", "reviewer"}, "prompts")
    if not all(isinstance(v, str) and v for v in prompts_raw.values()):
        raise ConfigError("prompt paths must be non-empty strings")
    # Files named here replace a role's guidance. The factory's output contract is
    # appended by load_prompt either way, so no prompt can remove it.
    prompts = {
        str(k): _resolve(source_path.parent, str(v)) for k, v in prompts_raw.items()
    }

    skills_raw = raw.get("skills", [])
    if not isinstance(skills_raw, list) or not all(
        isinstance(x, str) for x in skills_raw
    ):
        raise ConfigError("skills must be a list of paths")
    skills = list(definition.get("shared_skills", []))
    for value in skills_raw:
        if value.startswith("@"):
            if not re.fullmatch(r"@[a-z0-9-]+", value):
                raise ConfigError("invalid packaged skill name")
            path = Path(__file__).parent / "agents" / "skills" / value[1:]
        else:
            path = Path(_resolve(source_path.parent, value))
        if not (path / "SKILL.md").is_file():
            raise ConfigError(f"selected skill is missing: {value}")
        skills.append(str(path.resolve()))
    skills = list(dict.fromkeys(skills))
    role_skills = {
        role: list(dict.fromkeys(paths))
        for role, paths in definition.get("role_skills", {}).items()
    }
    for path in prompts.values():
        if not Path(path).is_file():
            raise ConfigError(f"selected prompt is missing: {path}")

    runtime = raw.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigError("runtime must be a table")
    for key in ("operator_token", "operator_token_env", "api_host"):
        if key in runtime:
            _require(runtime, key, str)
    operator_token = (
        secret(runtime["operator_token_env"])
        if "operator_token_env" in runtime
        else str(runtime.get("operator_token", ""))
    )
    if "contributor_token_env" in runtime:
        contributor = secret(_require(runtime, "contributor_token_env", str))
        if contributor and (len(contributor) < 32 or contributor == operator_token):
            raise ConfigError(
                "the contributor credential must be at least 32 characters and differ from the operator's"
            )
    if operator_token and len(operator_token) < 32:
        raise ConfigError("the operator credential must be at least 32 characters")

    normalized_sources = _normalize_sources(raw, repositories, commands, source_path)
    from dark_factory.runners import validate_runner_table

    validate_runner_table(raw)
    gate = raw.get("gate", {})
    if not isinstance(gate, dict):
        raise ConfigError("gate must be a table")
    _fields(gate, {"enabled", "repositories", "interval_seconds"}, "gate")
    _integer(gate, "interval_seconds", 60, 10)
    if "enabled" in gate:
        _require(gate, "enabled", bool)
    for name in gate.get("repositories", []):
        if name not in repositories:
            raise ConfigError("gate.repositories must be configured repositories")
    disk = raw.get("disk", {})
    if not isinstance(disk, dict):
        raise ConfigError("disk must be a table")
    _fields(
        disk,
        {"min_free_gb", "max_state_gb", "keep_finished_days", "keep_audit_days"},
        "disk",
    )
    _integer(disk, "min_free_gb", DEFAULT_DISK["min_free_gb"], 0)
    _integer(disk, "max_state_gb", DEFAULT_DISK["max_state_gb"], 0)
    _integer(disk, "keep_finished_days", DEFAULT_DISK["keep_finished_days"], 1)
    _integer(disk, "keep_audit_days", DEFAULT_DISK["keep_audit_days"], 1)
    _validate_improvement(raw, repositories, normalized_sources, state_dir)
    execution = raw.get("execution", {})
    if not isinstance(execution, dict):
        raise ConfigError("execution must be a table")
    _fields(
        execution,
        {"max_seconds"},
        "execution",
    )
    _integer(execution, "max_seconds", DEFAULT_MAX_SECONDS, 1)
    for route in routes.values():
        from dark_factory.adapters.harness.registry import adapter_for

        try:
            adapter = adapter_for(route.harness)
        except RuntimeError as exc:
            raise ConfigError("unsupported harness") from exc
        if not adapter.supports_effort and "effort" in routes_raw[route.id]:
            raise ConfigError(
                "explicit effort is supported only by native Claude Code and Codex adapters"
            )
        if route.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ConfigError("invalid route effort")
        validator = getattr(adapter.factory, "validate_route", None)
        if validator:
            try:
                validator(route)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        if adapter.binary_key:
            if not normalized_sources:
                raise ConfigError("a real harness requires a configured source")
            if route.region.upper() not in {
                "US",
                "CA",
                "GB",
                "EU",
                "DE",
                "FR",
                "AU",
                "JP",
                "SG",
                "KR",
                "CH",
                "LOCAL",
            }:
                raise ConfigError("unverified processing geography")
            if (
                not all(
                    (
                        route.geography_evidence,
                        route.auth_env,
                        route.endpoint,
                        route.billing_account,
                    )
                )
                or route.harness_version == "unspecified"
            ):
                raise ConfigError(
                    "live route requires pinned version, endpoint, account, credential reference and geography evidence"
                )
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", route.auth_env):
                raise ConfigError("auth_env must name an environment variable")
    for key in ("planning_cents", "account_cents"):
        _integer(raw.get("budgets", {}), key, BUDGET_DEFAULTS[key], 1)

    from dark_factory.execution.policy import validate_deployment

    project_id = validate_deployment(raw, routes)
    config_digest = digest
    if definition:
        config_digest = _hash_bytes(f"{digest}:{definition['digest']}".encode())
    return FactoryConfig(
        schema_version=version,
        project_name=name,
        state_dir=state_dir,
        source_path=source_path,
        digest=config_digest,
        repositories=list(repositories),
        labels={str(k): str(v) for k, v in labels.items()},
        routes=routes,
        windows=windows,
        date_exceptions=list(exceptions),
        commands=commands,
        prompts=prompts,
        skills=skills,
        role_skills=role_skills,
        definition=definition,
        geography_policy=sorted(blocked),
        api_host=str(runtime.get("api_host", "127.0.0.1")),
        api_port=_integer(runtime, "api_port", 8745, 1),
        default_timeout_seconds=_integer(runtime, "default_timeout_seconds", 3600, 1),
        max_output_bytes=_integer(runtime, "max_output_bytes", 2000000, 1),
        raw=raw,
        sources=normalized_sources,
        execution=dict(execution),
        operator_token=operator_token,
        project_id=project_id,
    )


def load_prompt(cfg: FactoryConfig, role: str, packaged_dir: Path) -> tuple[str, str]:
    """A role's prompt: authored guidance, then the factory's output contract.

    Guidance comes from an operator override file, else the Warp agent's body
    (snapshotted when the definition loaded), else the packaged text. The
    contract is always the packaged one and always last.
    """
    from dark_factory.adapters.definition.warp import strip_frontmatter

    override = cfg.prompts.get(role)
    if override:
        path = Path(override)
        if not path.is_file():
            raise ConfigError(f"prompt for role {role!r} not found: {path}")
        guidance = strip_frontmatter(path.read_text(encoding="utf-8"))
    elif cfg.definition.get("role_guidance", {}).get(role):
        guidance = cfg.definition["role_guidance"][role]
    else:
        path = packaged_dir / f"{role}.md"
        if not path.is_file():
            raise ConfigError(f"prompt for role {role!r} not found: {path}")
        guidance = path.read_text(encoding="utf-8").strip()
    contract_path = packaged_dir / f"{role}.contract.md"
    if not contract_path.is_file():
        raise ConfigError(f"output contract for role {role!r} not found")
    text = (
        guidance.strip()
        + "\n\n"
        + contract_path.read_text(encoding="utf-8").strip()
        + "\n"
    )
    return text, hashlib.sha256(text.encode()).hexdigest()
