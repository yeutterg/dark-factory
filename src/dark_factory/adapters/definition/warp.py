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
MAX_FILES = 256
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

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
    "workerRole",
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
AGENT_TYPES = {
    "CUSTOM",
    "FOREMAN",
    "IMPLEMENT",
    "MAIN",
    "REVIEW",
    "SPEC",
    "TRIAGE",
    "VERIFY",
}
# Harness names a Warp agent may request, and the local adapter each corresponds to.
HARNESS_ADAPTERS = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "pi": "pi",
}
HARNESS_TYPES = {"claude", "claude-code", "codex", "gemini", "oz", "pi"}
RUNNER_OS = {"linux", "macos", "windows"}
IMPROVEMENT_DIR = "self-improvement"
KNOWN_RESOURCE_DIRS = {
    "agents",
    "automations",
    "runners",
    "routers",
    "benchmarks",
    "scorers",
    "skills",
    "webhooks",
    IMPROVEMENT_DIR,
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


class _Tree:
    """One read of the whole tree.

    Every file is read, size-checked and hashed exactly once, here. Everything
    the reader later parses comes from these bytes, so what is digested and what
    is used cannot diverge.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.blobs: dict[str, bytes] = {}
        self.files: dict[str, str] = {}
        total = 0
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if ".git" in relative.parts:
                continue
            if path.is_symlink():
                raise WarpDefinitionError(
                    f"definition resources cannot be symlinks: {relative}"
                )
            if not path.is_file():
                continue
            if len(self.blobs) >= MAX_FILES:
                raise WarpDefinitionError(
                    f"definition contains more than {MAX_FILES} files"
                )
            data = path.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise WarpDefinitionError(
                    f"definition resource is larger than {MAX_FILE_BYTES // 1024} KiB: "
                    f"{relative}"
                )
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise WarpDefinitionError(
                    f"definition is larger than {MAX_TOTAL_BYTES // (1024 * 1024)} MiB"
                )
            name = relative.as_posix()
            self.blobs[name] = data
            self.files[name] = hashlib.sha256(data).hexdigest()

    def exists(self, relative: str) -> bool:
        return relative in self.blobs

    def text(self, relative: str) -> str:
        if relative not in self.blobs:
            raise WarpDefinitionError(f"definition resource is missing: {relative}")
        try:
            text = self.blobs[relative].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WarpDefinitionError(
                f"definition resource is not UTF-8: {relative}"
            ) from exc
        return text.removeprefix("﻿")

    def directories(self, parent: str) -> list[str]:
        prefix = parent + "/" if parent else ""
        names = {
            name[len(prefix) :].split("/", 1)[0]
            for name in self.blobs
            if name.startswith(prefix) and "/" in name[len(prefix) :]
        }
        return sorted(names)

    def children(self, parent: str, suffixes: tuple[str, ...]) -> list[str]:
        prefix = parent + "/" if parent else ""
        return sorted(
            name
            for name in self.blobs
            if name.startswith(prefix)
            and "/" not in name[len(prefix) :]
            and name.endswith(suffixes)
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


def _yaml(text: str, label: str) -> dict[str, Any]:
    try:
        if any(
            isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None)
            for event in yaml.parse(text)
        ):
            raise WarpDefinitionError(f"YAML aliases are not allowed in {label}")
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except WarpDefinitionError:
        raise
    except (yaml.YAMLError, TypeError, RecursionError, ValueError) as exc:
        raise WarpDefinitionError(f"invalid YAML in {label}: {exc}") from exc
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise WarpDefinitionError(f"{label} must contain a YAML object")
    bad = [key for key in value if not isinstance(key, str)]
    if bad:
        raise WarpDefinitionError(
            f"{label} keys must be strings (found {bad[0]!r}; quote YAML 1.1 words "
            "such as on, off, yes and no)"
        )
    return value


def _frontmatter(text: str, label: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        raise WarpDefinitionError(f"{label} needs YAML frontmatter")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.rstrip() == "---")
    except StopIteration as exc:
        raise WarpDefinitionError(f"{label} has unterminated YAML frontmatter") from exc
    frontmatter = _yaml("\n".join(lines[1:end]), label)
    body = "\n".join(lines[end + 1 :]).strip()
    return frontmatter, body


def strip_frontmatter(text: str) -> str:
    """The Markdown body of a file that may begin with YAML frontmatter."""
    text = text.removeprefix("﻿")
    lines = text.splitlines()
    if lines and lines[0].rstrip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.rstrip() == "---":
                return "\n".join(lines[i + 1 :]).strip()
    return text.strip()


def _agent_type(frontmatter: dict[str, Any], label: str) -> str:
    if "agentType" in frontmatter and "workerRole" in frontmatter:
        raise WarpDefinitionError(f"{label} sets both agentType and workerRole")
    value = frontmatter.get("agentType", frontmatter.get("workerRole"))
    if value is None:
        return "CUSTOM"  # An omitted or null type resolves to CUSTOM.
    if value not in AGENT_TYPES:
        raise WarpDefinitionError(
            f"{label} agentType must be one of {', '.join(sorted(AGENT_TYPES))} "
            f"(uppercase), not {value!r}"
        )
    return str(value)


def _model_and_harness(
    frontmatter: dict[str, Any], defaults: dict[str, Any], label: str
) -> tuple[str | None, str | None]:
    """(requested model, requested harness type); model and harness are exclusive."""
    model, harness = frontmatter.get("model"), frontmatter.get("harness")
    if model and harness:
        raise WarpDefinitionError(f"{label} sets both model and harness")
    if harness is not None:
        if not isinstance(harness, dict):
            raise WarpDefinitionError(f"{label} harness must be an object")
        kind = harness.get("type")
        if kind is not None and kind not in HARNESS_TYPES:
            raise WarpDefinitionError(f"{label} harness type {kind!r} is not known")
        chosen = harness.get("model")
        return (chosen if isinstance(chosen, str) and chosen else None), kind
    if model:
        return (model if isinstance(model, str) else None), None
    inherited = defaults.get("harness")
    if isinstance(inherited, dict):
        return None, inherited.get("type")
    return (
        defaults.get("model") if isinstance(defaults.get("model"), str) else None
    ), None


def _is_route_alias(model: str, routers: set[str]) -> bool:
    """A model name that means 'use the approved local route', not a model id."""
    return (
        model == "auto"
        or model.startswith("auto-")
        or model.startswith("custom:")
        or model in routers
    )


def _skill_snapshot(tree: _Tree, directory: str) -> dict[str, Any]:
    prefix = directory + "/"
    files = {
        name[len(prefix) :]: digest
        for name, digest in tree.files.items()
        if name.startswith(prefix)
    }
    return {"body": tree.text(directory + "/SKILL.md"), "files": files}


def _load_skills(
    tree: _Tree,
    parent: str,
    snapshots: dict[str, dict[str, Any]],
    warnings: list[str],
) -> list[str]:
    result: list[str] = []
    for name in tree.directories(parent):
        directory = f"{parent}/{name}"
        if tree.exists(directory + "/SKILL.md"):
            key = str((tree.root / directory).resolve())
            snapshots[key] = _skill_snapshot(tree, directory)
            result.append(key)
        else:
            warnings.append(f"unrecognized skill resource: {directory}")
    for name in tree.children(parent, (".yaml", ".yml")):
        warnings.append(
            f"external skill reference is recognized but not executed: {name}"
        )
    return result


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def load_definition(
    path: str | Path,
    *,
    strict: bool = True,
    roles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Load a Warp definition tree without granting any of its requested access.

    ``roles`` names, for each mapped role (planner, coder, reviewer), the agent
    to use when several agents share a type. A single candidate needs no entry.
    """

    requested_path = Path(path)
    if requested_path.is_symlink():
        raise WarpDefinitionError("definition root cannot be a symlink")
    supplied = requested_path.resolve()
    root = supplied.parent if supplied.name == "factory.yaml" else supplied
    if not root.is_dir() or root.is_symlink():
        raise WarpDefinitionError(f"definition root does not exist: {root}")
    tree = _Tree(root)
    warnings: list[str] = []
    resources: list[dict[str, str]] = []
    roles = dict(roles or {})
    if set(roles) - set(ROLE_TYPES.values()):
        raise WarpDefinitionError(
            "definition.roles may only name planner, coder or reviewer"
        )

    if not tree.exists("factory.yaml"):
        raise WarpDefinitionError("factory.yaml is missing")
    factory = _yaml(tree.text("factory.yaml"), "factory.yaml")
    version = factory.get("schemaVersion")
    if version == "v1alpha2" or (root / "workers").is_dir():
        raise WarpDefinitionError(
            "Warp v1alpha2 (blueprints and the workers/ directory) is not supported; "
            "this reader implements v1alpha1"
        )
    if version != "v1alpha1":
        raise WarpDefinitionError("factory.yaml schemaVersion must be v1alpha1")
    _unknown(factory, FACTORY_FIELDS, "factory.yaml", strict, warnings)
    if not isinstance(factory.get("name"), str) or not factory["name"].strip():
        raise WarpDefinitionError("factory.yaml name is required")
    if "repositories" not in factory:
        raise WarpDefinitionError("factory.yaml repositories is required")

    repositories: list[str] = []
    raw_repositories = factory["repositories"]
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

    # Runners and routers first, so references from agents and scorers can resolve.
    runners: list[str] = []
    for name in tree.children("runners", (".yaml", ".yml")):
        stem = Path(name).stem
        value = _yaml(tree.text(name), name)
        _unknown(value, RUNNER_FIELDS, f"runner {stem}", strict, warnings)
        platform = value.get("platform")
        if platform is not None:
            if not isinstance(platform, dict):
                raise WarpDefinitionError(f"{name} platform must be an object")
            if platform.get("os", "linux") not in RUNNER_OS:
                raise WarpDefinitionError(
                    f"{name} platform.os must be one of {', '.join(sorted(RUNNER_OS))}"
                )
        runners.append(stem)
        resources.append(
            {"kind": "runner", "name": stem, "status": "recognized-not-executed"}
        )
    if runners:
        warnings.append(
            "runner files are validated, but the local execution profile remains authoritative"
        )
    routers = {Path(n).stem for n in tree.children("routers", (".yaml", ".yml"))}
    for name in sorted(routers):
        resources.append(
            {"kind": "router", "name": name, "status": "recognized-not-executed"}
        )

    def runner_exists(reference: Any, label: str) -> None:
        if reference is not None and reference not in runners:
            raise WarpDefinitionError(
                f"{label} names runner {reference!r}, which is not declared under runners/"
            )

    runner_exists(defaults.get("runner"), "agentDefaults")

    # Agents.
    agents: list[dict[str, Any]] = []
    candidates: dict[str, list[dict[str, Any]]] = {}
    for name in tree.directories("agents"):
        document = f"agents/{name}/agent.md"
        if not tree.exists(document):
            warnings.append(f"agent directory has no agent.md: {name}")
            continue
        frontmatter, body = _frontmatter(tree.text(document), document)
        _unknown(frontmatter, AGENT_FIELDS, f"agent {name}", strict, warnings)
        agent_type = _agent_type(frontmatter, document)
        model, harness = _model_and_harness(frontmatter, defaults, document)
        runner = frontmatter.get("runner", defaults.get("runner"))
        runner_exists(runner, f"agent {name}")
        role = ROLE_TYPES.get(agent_type)
        record = {
            "name": name,
            "agent_type": agent_type,
            "role": role,
            "model": model,
            "model_is_route_alias": bool(model and _is_route_alias(model, routers)),
            "harness": harness,
            "runner": runner,
            "prompt": document,
            "empty_prompt": not body,
            "_body": body,
        }
        agents.append(record)
        if role:
            candidates.setdefault(role, []).append(record)
        elif agent_type == "VERIFY":
            warnings.append(
                f"agent {name} (VERIFY) is recognized but not scheduled by the fixed workflow"
            )
        else:
            warnings.append(
                f"agent {name} ({agent_type}) is preserved but not scheduled by the fixed workflow"
            )
        requested_access = sorted(
            key
            for key in (
                "secrets",
                "mcpServers",
                "networkPolicy",
                "hooks",
                "workerHost",
                "environmentId",
            )
            if frontmatter.get(key)
        )
        if requested_access:
            warnings.append(
                f"agent {name} requests {', '.join(requested_access)}; local policy does not grant them"
            )
    if agents:
        leaders = [a["name"] for a in agents if a["agent_type"] in {"FOREMAN", "MAIN"}]
        if len(leaders) != 1:
            message = (
                f"exactly one agent must declare FOREMAN or MAIN (found {len(leaders)})"
            )
            if strict:
                raise WarpDefinitionError(message)
            warnings.append(message)

    role_guidance: dict[str, str] = {}
    role_skills: dict[str, list[str]] = {}
    requested_models: dict[str, str] = {}
    requested_harness: dict[str, str] = {}
    skill_snapshots: dict[str, dict[str, Any]] = {}
    selected: dict[str, dict[str, Any]] = {}
    for role, options in sorted(candidates.items()):
        chosen_name = roles.get(role)
        if chosen_name:
            match = [a for a in options if a["name"] == chosen_name]
            if not match:
                raise WarpDefinitionError(
                    f"definition.roles.{role} names {chosen_name!r}, which is not a "
                    f"{role} agent ({', '.join(a['name'] for a in options)})"
                )
            pick = match[0]
        elif len(options) == 1:
            pick = options[0]
        else:
            raise WarpDefinitionError(
                f"more than one agent maps to role {role} "
                f"({', '.join(a['name'] for a in options)}); choose one with "
                f'[definition.roles] {role} = "<agent>"'
            )
        selected[role] = pick
        if pick["_body"]:
            role_guidance[role] = pick["_body"]
        else:
            warnings.append(
                f"agent {pick['name']} has an empty prompt; the packaged {role} guidance is used"
            )
        if pick["model"] and not pick["model_is_route_alias"]:
            requested_models[role] = pick["model"]
        if pick["harness"]:
            requested_harness[role] = pick["harness"]
        role_skills[role] = _load_skills(
            tree, f"agents/{pick['name']}/skills", skill_snapshots, warnings
        )
    for agent in agents:
        agent["selected"] = selected.get(agent["role"] or "") is agent
        agent.pop("_body", None)
        status = (
            "mapped"
            if agent["selected"]
            else "preserved"
            if agent["role"] is None
            else "not-selected"
        )
        resources.append({"kind": "agent", "name": agent["name"], "status": status})

    shared_skills = _load_skills(tree, "skills", skill_snapshots, warnings)
    for key in [*shared_skills, *(p for paths in role_skills.values() for p in paths)]:
        resources.append(
            {"kind": "skill", "name": Path(key).name, "status": "executed"}
        )

    # Scorers.
    scorers: list[dict[str, Any]] = []
    for name in tree.directories("scorers"):
        document = f"scorers/{name}/scorer.md"
        if not tree.exists(document):
            warnings.append(f"scorer directory has no scorer.md: {name}")
            continue
        frontmatter, body = _frontmatter(tree.text(document), document)
        _unknown(frontmatter, SCORER_FIELDS, f"scorer {name}", strict, warnings)
        if not body:
            raise WarpDefinitionError(f"scorer {name} has empty instructions")
        for required in ("labels", "passingScore"):
            if required not in frontmatter:
                raise WarpDefinitionError(f"{document} requires {required}")
        runner_exists(frontmatter.get("runner"), f"scorer {name}")
        scorers.append(
            {
                "name": frontmatter.get("name", name),
                "agents": frontmatter.get("agents", []),
                "self_improvement": bool(frontmatter.get("selfImprovement", False)),
                "path": document,
                "status": "recognized-not-executed",
            }
        )
        resources.append(
            {"kind": "scorer", "name": name, "status": "recognized-not-executed"}
        )
    if scorers:
        warnings.append(
            "scorers are pinned for compatibility but are not executed as LLM judges"
        )

    # Automations and other recognized, unexecuted resources.
    unsupported: dict[str, int] = {}
    for name in tree.directories("automations"):
        document = f"automations/{name}/automation.md"
        if tree.exists(document):
            frontmatter, _ = _frontmatter(tree.text(document), document)
            if "triggers" not in frontmatter:
                raise WarpDefinitionError(f"{document} requires triggers")
    for kind in ("automations", "routers", "benchmarks", "webhooks"):
        count = sum(1 for n in tree.blobs if n.startswith(kind + "/"))
        if count:
            unsupported[kind] = count
            warnings.append(f"{kind} resources are preserved but not executed")
            if kind != "routers":
                resources.append(
                    {"kind": kind, "name": kind, "status": "recognized-not-executed"}
                )

    # Self-improvement: the agent's instructions feed Dark Factory's own loop.
    improvement: dict[str, Any] = {}
    settings = factory.get("selfImprovement") or {}
    if not isinstance(settings, dict):
        raise WarpDefinitionError("factory.yaml selfImprovement must be an object")
    threshold = settings.get("failedRunThreshold")
    if threshold is not None and (
        type(threshold) is not int or not 1 <= threshold <= 50
    ):
        raise WarpDefinitionError(
            "selfImprovement.failedRunThreshold must be an integer from 1 to 50"
        )
    document = f"{IMPROVEMENT_DIR}/agent.md"
    if tree.exists(document):
        frontmatter, body = _frontmatter(tree.text(document), document)
        _unknown(frontmatter, AGENT_FIELDS, "self-improvement agent", strict, warnings)
        improvement["guidance"] = body
        resources.append(
            {"kind": "self-improvement", "name": "agent", "status": "mapped"}
        )
    if threshold is not None:
        improvement["failed_run_threshold"] = threshold
    if settings or improvement:
        warnings.append(
            "Warp's scheduled self-improvement is not run; Dark Factory's own "
            "improvement loop reads this agent's guidance and threshold"
        )

    # Directories nobody recognizes.
    for name in tree.directories(""):
        if name in KNOWN_RESOURCE_DIRS or name.startswith("."):
            continue
        message = f"unrecognized definition directory: {name}"
        if strict:
            raise WarpDefinitionError(message)
        warnings.append(message)

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

    return {
        "format": FORMAT,
        "schema_version": "v1alpha1",
        "name": factory["name"],
        "root": str(root),
        "digest": _digest(tree.files),
        "files": tree.files,
        "repositories": repositories,
        "agents": agents,
        "role_guidance": role_guidance,
        "requested_models": requested_models,
        "requested_harness": requested_harness,
        "shared_skills": shared_skills,
        "role_skills": role_skills,
        "skill_snapshots": skill_snapshots,
        "scorers": scorers,
        "runners": runners,
        "self_improvement": improvement,
        "compatibility": {
            "execution": "fixed-dark-factory-workflow",
            "mapped_roles": sorted(role_guidance),
            "resources": resources,
            "unsupported_resources": unsupported,
            "warnings": warnings,
        },
    }


# Local detail the report omits; the digest and per-resource status remain.
_PRIVATE = {"root", "role_guidance", "shared_skills", "role_skills", "skill_snapshots"}


def public_report(definition: dict[str, Any]) -> dict[str, Any]:
    """Remove local absolute paths and prompt text; keep compatibility evidence."""

    return {key: value for key, value in definition.items() if key not in _PRIVATE}
