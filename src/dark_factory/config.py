"""Load and validate factory.toml. Relative paths resolve against the config file."""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
import zoneinfo
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
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
    geography_policy: list[str]
    worker_token: str
    api_host: str
    api_port: int
    default_timeout_seconds: int
    max_output_bytes: int
    raw: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    operator_token: str = ""


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


def _fields(table: dict[str, Any], allowed: set[str], name: str) -> None:
    extra = set(table) - allowed
    if extra:
        raise ConfigError(f"unknown {name} fields: {', '.join(sorted(extra))}")


def _integer(table: dict[str, Any], key: str, default: int, minimum: int = 0) -> int:
    value = table.get(key, default)
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


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
        },
        "profile",
    )
    for key, allowed in {
        "project": {"name", "state_dir"},
        "runtime": {
            "worker_token",
            "worker_token_env",
            "operator_token",
            "operator_token_env",
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
    _integer(raw.get("budgets", {}), "root_cents", 1000)
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

    labels = raw.get("labels", {})
    if not isinstance(labels, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in labels.items()
    ):
        raise ConfigError("labels must be a string table")

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
        )
    if not routes:
        raise ConfigError("at least one route is required")

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
        if "/" in argv[0]:
            argv[0] = _resolve(source_path.parent, argv[0])
        commands[cid] = IntegrationCommand(
            id=cid,
            command=list(argv),
            timeout_seconds=_integer(item, "timeout_seconds", 120, 1),
        )
    for required in ("tracking", "code_host"):
        if required not in commands:
            raise ConfigError(f"commands.{required} is required")

    prompts_raw = raw.get("prompts", {})
    if not isinstance(prompts_raw, dict):
        raise ConfigError("prompts must be a table")
    _fields(prompts_raw, {"planner", "critic", "coder", "reviewer"}, "prompts")
    if not all(isinstance(v, str) and v for v in prompts_raw.values()):
        raise ConfigError("prompt paths must be non-empty strings")
    prompts = {
        str(k): _resolve(source_path.parent, str(v)) for k, v in prompts_raw.items()
    }

    skills_raw = raw.get("skills", [])
    if not isinstance(skills_raw, list) or not all(
        isinstance(x, str) for x in skills_raw
    ):
        raise ConfigError("skills must be a list of paths")
    skills = []
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
    for path in prompts.values():
        if not Path(path).is_file():
            raise ConfigError(f"selected prompt is missing: {path}")

    runtime = raw.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigError("runtime must be a table")
    for key in (
        "worker_token",
        "worker_token_env",
        "operator_token",
        "operator_token_env",
        "api_host",
    ):
        if key in runtime:
            _require(runtime, key, str)
    token = (
        os.environ.get(runtime["worker_token_env"], "")
        if "worker_token_env" in runtime
        else str(runtime.get("worker_token", ""))
    )
    operator_token = (
        os.environ.get(runtime["operator_token_env"], "")
        if "operator_token_env" in runtime
        else str(runtime.get("operator_token", ""))
    )
    if operator_token and operator_token == token:
        raise ConfigError("worker and operator credentials must differ")
    if not token:
        raise ConfigError("runtime.worker_token is required")

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
                "require_live_evidence",
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
        for key in ("checks", "allowed_paths", "protected_paths", "manual_acceptance"):
            values = source.get(key, [])
            if not isinstance(values, list) or not all(
                isinstance(v, str) and v for v in values
            ):
                raise ConfigError(f"source {key} must be a list of strings")
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
    execution = raw.get("execution", {})
    if not isinstance(execution, dict):
        raise ConfigError("execution must be a table")
    _fields(execution, {"profile", "pi_binary", "max_seconds"}, "execution")
    _integer(execution, "max_seconds", 7200, 1)
    profile = execution.get("profile", "native")
    if profile not in {"native", "macos-sandbox"}:
        raise ConfigError("unsupported execution profile")
    for route in routes.values():
        if route.harness not in {"fake", "pi"}:
            raise ConfigError("unsupported harness")
        if route.harness == "pi":
            if profile != "macos-sandbox" or not normalized_sources:
                raise ConfigError(
                    "live Pi requires sources and macos-sandbox execution"
                )
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
    for key, default in (("planning_cents", 100), ("account_cents", 10000)):
        _integer(raw.get("budgets", {}), key, default, 1)

    return FactoryConfig(
        schema_version=version,
        project_name=name,
        state_dir=state_dir,
        source_path=source_path,
        digest=digest,
        repositories=list(repositories),
        labels={str(k): str(v) for k, v in labels.items()},
        routes=routes,
        windows=windows,
        date_exceptions=list(exceptions),
        commands=commands,
        prompts=prompts,
        skills=skills,
        geography_policy=sorted(blocked),
        worker_token=token,
        api_host=str(runtime.get("api_host", "127.0.0.1")),
        api_port=_integer(runtime, "api_port", 8745, 1),
        default_timeout_seconds=_integer(runtime, "default_timeout_seconds", 3600, 1),
        max_output_bytes=_integer(runtime, "max_output_bytes", 2000000, 1),
        raw=raw,
        sources=normalized_sources,
        execution=dict(execution),
        operator_token=operator_token,
    )


def load_prompt(cfg: FactoryConfig, role: str, packaged_dir: Path) -> tuple[str, str]:
    override = cfg.prompts.get(role)
    if override:
        path = Path(override)
    else:
        path = packaged_dir / f"{role}.md"
    if not path.is_file():
        raise ConfigError(f"prompt for role {role!r} not found: {path}")
    text = path.read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode()).hexdigest()
