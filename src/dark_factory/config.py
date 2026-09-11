"""Load and validate factory.toml. Relative paths resolve against the config file."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore


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


def _require(d: dict[str, Any], key: str, typ: type) -> Any:
    if key not in d:
        raise ConfigError(f"missing required field {key!r}")
    val = d[key]
    if not isinstance(val, typ):
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


def validate_raw(raw: dict[str, Any], source_path: Path, digest: str) -> FactoryConfig:
    version = _require(raw, "schema_version", int)
    if version != SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema_version {version}; expected {SCHEMA_VERSION}")
    project = _require(raw, "project", dict)
    name = _require(project, "name", str)
    state_dir = Path(_resolve(source_path.parent, _require(project, "state_dir", str)))
    repositories = _require(raw, "repositories", list)
    if not repositories or not all(isinstance(r, str) and r for r in repositories):
        raise ConfigError("repositories must be a non-empty list of strings")

    labels = raw.get("labels", {})
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        raise ConfigError("labels must be a string table")

    routes_raw = _require(raw, "routes", dict)
    routes: dict[str, Route] = {}
    policy = raw.get("geography_policy", {"blocked_regions": ["CN"]})
    if not isinstance(policy, dict):
        raise ConfigError("geography_policy must be a table")
    blocked = {str(x).upper() for x in policy.get("blocked_regions", ["CN"])}
    blocked |= BLOCKED_REGIONS

    for rid, item in routes_raw.items():
        if not isinstance(item, dict):
            raise ConfigError(f"route {rid!r} must be a table")
        region = str(_require(item, "region", str)).strip()
        if not region or region.upper() in {"UNKNOWN", "UNSPECIFIED", ""}:
            raise ConfigError(f"route {rid!r} has unknown processing geography and is blocked")
        if region.upper() in blocked:
            raise ConfigError(f"route {rid!r} region {region!r} is blocked by operator policy")
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
            max_cents=int(item.get("max_cents", 0)),
        )
    if not routes:
        raise ConfigError("at least one route is required")

    windows: list[Window] = []
    for w in raw.get("windows", []):
        if not isinstance(w, dict):
            raise ConfigError("windows entries must be tables")
        windows.append(
            Window(
                days=list(_require(w, "days", list)),
                start=_require(w, "start", str),
                end=_require(w, "end", str),
                timezone=_require(w, "timezone", str),
            )
        )

    exceptions = raw.get("date_exceptions", [])
    if not isinstance(exceptions, list) or not all(isinstance(x, str) for x in exceptions):
        raise ConfigError("date_exceptions must be a list of strings")

    commands: dict[str, IntegrationCommand] = {}
    for cid, item in _require(raw, "commands", dict).items():
        if not isinstance(item, dict):
            raise ConfigError(f"command {cid!r} must be a table")
        argv = _require(item, "argv", list)
        if not argv or not all(isinstance(x, str) for x in argv):
            raise ConfigError(f"command {cid!r} argv must be a list of strings")
        argv = [_resolve(source_path.parent, argv[0])] + list(argv[1:]) if False else list(argv)
        commands[cid] = IntegrationCommand(
            id=cid,
            command=list(argv),
            timeout_seconds=int(item.get("timeout_seconds", 120)),
        )
    for required in ("tracking", "code_host"):
        if required not in commands:
            raise ConfigError(f"commands.{required} is required")

    prompts_raw = raw.get("prompts", {})
    if not isinstance(prompts_raw, dict):
        raise ConfigError("prompts must be a table")
    prompts = {str(k): _resolve(source_path.parent, str(v)) for k, v in prompts_raw.items()}

    skills_raw = raw.get("skills", [])
    if not isinstance(skills_raw, list) or not all(isinstance(x, str) for x in skills_raw):
        raise ConfigError("skills must be a list of paths")
    skills = [_resolve(source_path.parent, s) for s in skills_raw]

    runtime = raw.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigError("runtime must be a table")
    token = str(runtime.get("worker_token", ""))
    if not token:
        raise ConfigError("runtime.worker_token is required")

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
        api_port=int(runtime.get("api_port", 8745)),
        default_timeout_seconds=int(runtime.get("default_timeout_seconds", 3600)),
        max_output_bytes=int(runtime.get("max_output_bytes", 2_000_000)),
        raw=raw,
    )


def snapshot_config(cfg: FactoryConfig) -> str:
    return json.dumps(
        {
            "digest": cfg.digest,
            "project": cfg.project_name,
            "routes": sorted(cfg.routes),
            "commands": {k: v.command for k, v in cfg.commands.items()},
        },
        sort_keys=True,
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
