"""Strict reader for the useful Warp Factories v1alpha1 definition subset.

The adapter deliberately does not reproduce Warp's scheduler.  It extracts the
portable authoring assets Dark Factory can use in its fixed workflow and emits
an explicit compatibility report for everything else.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

FORMAT = "warp-v1alpha1"
MAX_FILES = 500
MAX_FILE_BYTES = 1_000_000

FACTORY_FIELDS = {
    "schemaVersion",
    "name",
    "description",
    "alias",
    "credentialStrategy",
    "restrictFollowupsToStarter",
    "allowServiceAccountFollowups",
    "compactOnCacheTTLExpiry",
    "repositories",
    "codeForges",
    "integrations",
    "secrets",
    "mcpServers",
    "cloudProviders",
    "providers",
    "networkPolicy",
    "hooks",
    "agentDefaults",
    "scorerDefaults",
    "selfImprovement",
    "automations",
    "benchmarkRepoSubstitutions",
    "github",
    "experimental",
}
AGENT_FIELDS = {
    "agentType",
    "computerUseModel",
    "credentialStrategy",
    "description",
    "environmentId",
    "extends",
    "harness",
    "hooks",
    "idleTimeoutMinutes",
    "mcpServers",
    "model",
    "networkPolicy",
    "params",
    "prewarmedPool",
    "required_repos",
    "runner",
    "secrets",
    "spawnableBy",
    "workerHost",
}
SCORER_FIELDS = {
    "agents",
    "description",
    "labels",
    "mcpServers",
    "model",
    "name",
    "output",
    "passingScore",
    "runner",
    "samplingRate",
    "scope_mode",
    "secrets",
    "selfImprovement",
}
RUNNER_FIELDS = {
    "capabilities",
    "description",
    "failureSessionRetentionMinutes",
    "instanceShape",
    "platform",
    "prewarmedPool",
    "setupCommands",
    "workerHost",
}
ROLE_TYPES = {"SPEC": "planner", "IMPLEMENT": "coder", "REVIEW": "reviewer"}
KNOWN_RESOURCE_DIRS = {
    "agents",
    "automations",
    "runners",
    "routers",
    "benchmarks",
    "scorers",
    "skills",
    "webhooks",
}


class WarpDefinitionError(ValueError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
):
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise WarpDefinitionError(f"duplicate YAML key: {key}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _unknown(
    value: dict[str, Any],
    allowed: set[str],
    label: str,
    strict: bool,
    warnings: list[str],
) -> None:
    extra = sorted(set(value) - allowed)
    if not extra:
        return
    message = f"unknown {label} fields: {', '.join(extra)}"
    if strict:
        raise WarpDefinitionError(message)
    warnings.append(message)


def _read(root: Path, path: Path, files: dict[str, str]) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise WarpDefinitionError("definition resource escapes its root") from exc
    if path.is_symlink():
        raise WarpDefinitionError(
            f"definition resources cannot be symlinks: {relative}"
        )
    if not path.is_file():
        raise WarpDefinitionError(f"definition resource is missing: {relative}")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise WarpDefinitionError(f"definition resource is too large: {relative}")
    if len(files) >= MAX_FILES and relative.as_posix() not in files:
        raise WarpDefinitionError("definition contains too many resource files")
    files[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WarpDefinitionError(
            f"definition resource is not UTF-8: {relative}"
        ) from exc


def _yaml(text: str, label: str) -> dict[str, Any]:
    try:
        if any(
            isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None)
            for event in yaml.parse(text)
        ):
            raise WarpDefinitionError(f"YAML aliases are not allowed in {label}")
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, WarpDefinitionError) as exc:
        if isinstance(exc, WarpDefinitionError):
            raise
        raise WarpDefinitionError(f"invalid YAML in {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise WarpDefinitionError(f"{label} must contain a YAML object")
    if not all(isinstance(key, str) for key in value):
        raise WarpDefinitionError(f"{label} keys must be strings")
    return value


def _frontmatter(text: str, label: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise WarpDefinitionError(f"{label} needs YAML frontmatter")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise WarpDefinitionError(f"{label} has unterminated YAML frontmatter") from exc
    frontmatter = _yaml("\n".join(lines[1:end]) or "{}", label)
    body = "\n".join(lines[end + 1 :]).strip()
    return frontmatter, body


def _resource_files(root: Path, directory: Path, files: dict[str, str]) -> None:
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise WarpDefinitionError(
                f"definition resources cannot be symlinks: {path.relative_to(root)}"
            )
        if path.is_file():
            _read(root, path, files)


def _load_skills(
    root: Path,
    directory: Path,
    files: dict[str, str],
    warnings: list[str],
) -> list[str]:
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise WarpDefinitionError(
            f"invalid skills directory: {directory.relative_to(root)}"
        )
    result: list[str] = []
    for child in sorted(directory.iterdir()):
        if child.is_symlink():
            raise WarpDefinitionError(
                f"definition resources cannot be symlinks: {child.relative_to(root)}"
            )
        if child.is_dir() and (child / "SKILL.md").is_file():
            _resource_files(root, child, files)
            result.append(str(child.resolve()))
        elif child.is_file() and child.suffix in {".yaml", ".yml"}:
            _read(root, child, files)
            warnings.append(
                f"external skill reference is recognized but not executed: {child.relative_to(root)}"
            )
        elif child.name not in {"README.md"}:
            warnings.append(f"unrecognized skill resource: {child.relative_to(root)}")
    return result


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def load_definition(path: str | Path, *, strict: bool = True) -> dict[str, Any]:
    """Load a Warp definition tree without granting any of its requested access."""

    requested_path = Path(path)
    if requested_path.is_symlink():
        raise WarpDefinitionError("definition root cannot be a symlink")
    supplied = requested_path.resolve()
    root = supplied.parent if supplied.name == "factory.yaml" else supplied
    factory_path = (
        supplied if supplied.name == "factory.yaml" else root / "factory.yaml"
    )
    if not root.is_dir() or root.is_symlink():
        raise WarpDefinitionError(f"definition root does not exist: {root}")

    files: dict[str, str] = {}
    warnings: list[str] = []
    factory = _yaml(_read(root, factory_path, files), "factory.yaml")
    _unknown(factory, FACTORY_FIELDS, "factory.yaml", strict, warnings)
    if factory.get("schemaVersion") != "v1alpha1":
        raise WarpDefinitionError("factory.yaml schemaVersion must be v1alpha1")
    if not isinstance(factory.get("name"), str) or not factory["name"].strip():
        raise WarpDefinitionError("factory.yaml name is required")

    repositories: list[str] = []
    raw_repositories = factory.get("repositories", [])
    if not isinstance(raw_repositories, list):
        raise WarpDefinitionError("factory.yaml repositories must be a list")
    for item in raw_repositories:
        if not isinstance(item, dict):
            raise WarpDefinitionError("factory repository entries must be objects")
        _unknown(
            item,
            {"owner", "name", "codeForge", "deferred"},
            "repository",
            strict,
            warnings,
        )
        owner, name = item.get("owner"), item.get("name")
        if (
            not isinstance(owner, str)
            or not owner
            or not isinstance(name, str)
            or not name
        ):
            raise WarpDefinitionError("factory repositories require owner and name")
        forge = item.get("codeForge", "GITHUB")
        if forge != "GITHUB":
            warnings.append(
                f"repository {owner}/{name} uses unsupported code forge {forge}"
            )
        repositories.append(f"{owner}/{name}")
    if len(repositories) != len(set(repositories)):
        raise WarpDefinitionError("factory repositories must be unique")

    defaults = factory.get("agentDefaults", {})
    if not isinstance(defaults, dict):
        raise WarpDefinitionError("factory.yaml agentDefaults must be an object")
    _unknown(
        defaults,
        {
            "prewarmedPool",
            "model",
            "harness",
            "runner",
            "environmentId",
            "secrets",
            "mcpServers",
            "workerHost",
            "computerUseModel",
        },
        "agentDefaults",
        strict,
        warnings,
    )
    if bool(defaults.get("model")) == bool(defaults.get("harness")):
        raise WarpDefinitionError(
            "agentDefaults requires exactly one of model or harness"
        )
    default_access = sorted(
        key
        for key in ("secrets", "mcpServers", "workerHost", "environmentId")
        if defaults.get(key)
    )
    if default_access:
        warnings.append(
            "agent defaults request "
            + ", ".join(default_access)
            + "; local policy does not grant them"
        )
    if defaults.get("harness"):
        warnings.append(
            "the requested Warp harness is preserved, but approved local routes remain authoritative"
        )

    agents: list[dict[str, Any]] = []
    role_prompts: dict[str, str] = {}
    role_skills: dict[str, list[str]] = {}
    requested_models: dict[str, str] = {}
    agents_dir = root / "agents"
    if agents_dir.exists():
        if agents_dir.is_symlink() or not agents_dir.is_dir():
            raise WarpDefinitionError("agents must be a directory")
        for child in agents_dir.iterdir():
            if child.is_symlink():
                raise WarpDefinitionError(
                    f"definition resources cannot be symlinks: {child.relative_to(root)}"
                )
        for directory in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
            document = directory / "agent.md"
            if not document.is_file():
                warnings.append(f"agent directory has no agent.md: {directory.name}")
                continue
            frontmatter, body = _frontmatter(
                _read(root, document, files), document.relative_to(root).as_posix()
            )
            _unknown(
                frontmatter, AGENT_FIELDS, f"agent {directory.name}", strict, warnings
            )
            agent_type = str(frontmatter.get("agentType", "CUSTOM")).upper()
            role = ROLE_TYPES.get(agent_type)
            requested_model = frontmatter.get("model", defaults.get("model"))
            if role:
                if role in role_prompts:
                    raise WarpDefinitionError(
                        f"more than one agent maps to role {role}"
                    )
                if not body:
                    raise WarpDefinitionError(
                        f"mapped agent {directory.name} has an empty prompt"
                    )
                role_prompts[role] = str(document.resolve())
                if isinstance(requested_model, str):
                    requested_models[role] = requested_model
                role_skills[role] = _load_skills(
                    root, directory / "skills", files, warnings
                )
            elif agent_type in {"FOREMAN", "MAIN", "TRIAGE", "CUSTOM"}:
                warnings.append(
                    f"agent {directory.name} ({agent_type}) is preserved but not scheduled by the fixed workflow"
                )
            else:
                warnings.append(
                    f"agent {directory.name} has unrecognized type {agent_type}"
                )
            requested_access = sorted(
                key
                for key in (
                    "secrets",
                    "mcpServers",
                    "networkPolicy",
                    "hooks",
                    "harness",
                    "workerHost",
                    "environmentId",
                )
                if frontmatter.get(key)
            )
            if requested_access:
                warnings.append(
                    f"agent {directory.name} requests {', '.join(requested_access)}; local policy does not grant them"
                )
            agents.append(
                {
                    "name": directory.name,
                    "agent_type": agent_type,
                    "role": role,
                    "model": requested_model
                    if isinstance(requested_model, str)
                    else None,
                    "runner": frontmatter.get("runner", defaults.get("runner")),
                    "prompt": document.relative_to(root).as_posix(),
                }
            )

    shared_skills = _load_skills(root, root / "skills", files, warnings)

    scorers: list[dict[str, Any]] = []
    scorers_dir = root / "scorers"
    if scorers_dir.exists():
        if scorers_dir.is_symlink() or not scorers_dir.is_dir():
            raise WarpDefinitionError("scorers must be a directory")
        for child in scorers_dir.iterdir():
            if child.is_symlink():
                raise WarpDefinitionError(
                    f"definition resources cannot be symlinks: {child.relative_to(root)}"
                )
        for directory in sorted(p for p in scorers_dir.iterdir() if p.is_dir()):
            document = directory / "scorer.md"
            if not document.is_file():
                warnings.append(f"scorer directory has no scorer.md: {directory.name}")
                continue
            frontmatter, body = _frontmatter(
                _read(root, document, files), document.relative_to(root).as_posix()
            )
            _unknown(
                frontmatter, SCORER_FIELDS, f"scorer {directory.name}", strict, warnings
            )
            if not body:
                raise WarpDefinitionError(
                    f"scorer {directory.name} has empty instructions"
                )
            scorers.append(
                {
                    "name": frontmatter.get("name", directory.name),
                    "agents": frontmatter.get("agents", []),
                    "self_improvement": bool(frontmatter.get("selfImprovement", False)),
                    "path": document.relative_to(root).as_posix(),
                    "status": "recognized-not-executed",
                }
            )
        if scorers:
            warnings.append(
                "scorers are pinned for compatibility but are not executed as LLM judges"
            )

    runners: list[str] = []
    runners_dir = root / "runners"
    if runners_dir.exists():
        if runners_dir.is_symlink() or not runners_dir.is_dir():
            raise WarpDefinitionError("runners must be a directory")
        for document in sorted(
            [*runners_dir.glob("*.yaml"), *runners_dir.glob("*.yml")]
        ):
            value = _yaml(
                _read(root, document, files), document.relative_to(root).as_posix()
            )
            _unknown(value, RUNNER_FIELDS, f"runner {document.stem}", strict, warnings)
            runners.append(document.stem)
        if runners:
            warnings.append(
                "runner files are validated, but the local execution profile remains authoritative"
            )

    unsupported: dict[str, int] = {}
    for name in ("automations", "routers", "benchmarks", "webhooks"):
        directory = root / name
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise WarpDefinitionError(f"{name} must be a directory")
            _resource_files(root, directory, files)
            count = sum(1 for path in directory.rglob("*") if path.is_file())
            if count:
                unsupported[name] = count
                warnings.append(f"{name} resources are preserved but not executed")

    for child in root.iterdir():
        if (
            child.is_dir()
            and child.name not in KNOWN_RESOURCE_DIRS
            and not child.name.startswith(".")
        ):
            warnings.append(
                f"unrecognized definition directory is ignored: {child.name}"
            )

    unsupported_factory_fields = sorted(
        field
        for field in (
            "integrations",
            "secrets",
            "mcpServers",
            "cloudProviders",
            "providers",
            "networkPolicy",
            "hooks",
            "automations",
        )
        if factory.get(field)
    )
    if unsupported_factory_fields:
        warnings.append(
            "factory requests preserved but not granted: "
            + ", ".join(unsupported_factory_fields)
        )
    if factory.get("selfImprovement"):
        warnings.append(
            "Warp scheduled self-improvement is not executed; Dark Factory exposes deterministic proposal-only analysis"
        )

    return {
        "format": FORMAT,
        "schema_version": "v1alpha1",
        "name": factory["name"],
        "root": str(root),
        "digest": _digest(files),
        "files": files,
        "repositories": repositories,
        "agents": agents,
        "role_prompts": role_prompts,
        "requested_models": requested_models,
        "shared_skills": shared_skills,
        "role_skills": role_skills,
        "scorers": scorers,
        "runners": runners,
        "compatibility": {
            "syntax": "compatible",
            "execution": "fixed-dark-factory-workflow",
            "mapped_roles": sorted(role_prompts),
            "unsupported_resources": unsupported,
            "warnings": warnings,
        },
    }


def public_report(definition: dict[str, Any]) -> dict[str, Any]:
    """Remove local absolute paths while retaining compatibility evidence."""

    return {
        key: value
        for key, value in definition.items()
        if key not in {"root", "role_prompts", "shared_skills", "role_skills"}
    }
