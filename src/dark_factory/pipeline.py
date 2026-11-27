"""A repository's pipeline: the steps behind each stage of the one fixed path.

Core owns the stage names and their order. A repository only says what each stage
runs. Every configurable stage is either bound to steps or declared not applicable
with a reason, so a stage can never be skipped silently. The file lives in the
repository, is read from its main branch by commit, and can request a runner or a
secret by name; only the host profile grants one.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

PIPELINE_PATH = "factory/x-dark-factory.yaml"
SCHEMA_VERSION = 1
# Stages a repository configures. The others (intake, plan, critique, approve,
# review, accept; assemble, approve, close) are fixed and owned by core.
CHANGE_STAGES = ("build", "test")
RELEASE_STAGES = ("build", "test", "package", "stage", "verify", "promote")
STAGE_ORDER_CHANGE = (
    "intake",
    "plan",
    "critique",
    "approve",
    "implement",
    "build",
    "test",
    "review",
    "accept",
)
STAGE_ORDER_RELEASE = (
    "assemble",
    "build",
    "test",
    "package",
    "approve",
    "stage",
    "verify",
    "promote",
    "close",
)
STAGE_FIELDS = {
    "steps",
    "runner",
    "labels",
    "environment",
    "network",
    "timeout_seconds",
    "artifacts",
    "caches",
    "secrets",
    "manual",
    "not_applicable",
}
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")


class PipelineError(ValueError):
    pass


@dataclass
class Stage:
    name: str
    steps: list[list[str]] = field(default_factory=list)
    runner: str = "local"
    labels: list[str] = field(default_factory=list)
    environment: str = ""
    network: bool = False
    timeout_seconds: int = 1800
    artifacts: list[str] = field(default_factory=list)
    caches: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    not_applicable: str = ""

    @property
    def applicable(self) -> bool:
        return not self.not_applicable


@dataclass
class Release:
    kind: str
    branch: str = "main"
    keep: int = 2
    stages: dict[str, Stage] = field(default_factory=dict)


@dataclass
class Pipeline:
    change: dict[str, Stage] = field(default_factory=dict)
    releases: dict[str, Release] = field(default_factory=dict)
    digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "change": {k: asdict(v) for k, v in self.change.items()},
            "releases": {k: asdict(v) for k, v in self.releases.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pipeline:
        return cls(
            change={k: Stage(**v) for k, v in data["change"].items()},
            releases={
                k: Release(
                    kind=v["kind"],
                    branch=v["branch"],
                    keep=v["keep"],
                    stages={n: Stage(**s) for n, s in v["stages"].items()},
                )
                for k, v in data["releases"].items()
            },
            digest=data["digest"],
        )


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise PipelineError(f"{label} must be a list of non-empty strings")
    return list(value)


def _stage(name: str, raw: Any, label: str) -> Stage:
    if not isinstance(raw, dict):
        raise PipelineError(f"{label} must be an object")
    extra = sorted(set(raw) - STAGE_FIELDS)
    if extra:
        raise PipelineError(f"unknown {label} fields: {', '.join(extra)}")
    if raw.get("not_applicable"):
        if raw.get("steps"):
            raise PipelineError(f"{label} is both bound to steps and not applicable")
        reason = raw["not_applicable"]
        if not isinstance(reason, str) or not reason.strip():
            raise PipelineError(f"{label} not_applicable needs a reason")
        return Stage(name=name, not_applicable=reason.strip())
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PipelineError(
            f"{label} needs steps, or not_applicable with a reason; a stage cannot be skipped silently"
        )
    argvs = [_strings(step, f"{label} step") for step in steps]
    for argv in argvs:
        head = argv[0]
        path = PurePosixPath(head)
        if ".." in path.parts or head.startswith("~"):
            raise PipelineError(f"{label} step {head!r} may not leave the repository")
    timeout = raw.get("timeout_seconds", 1800)
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise PipelineError(f"{label} timeout_seconds must be 1 to 86400")
    if "network" in raw and type(raw["network"]) is not bool:
        raise PipelineError(f"{label} network must be a boolean")
    secrets = _strings(raw.get("secrets", []), f"{label} secrets")
    caches = _strings(raw.get("caches", []), f"{label} caches")
    for item in [*secrets, *caches]:
        if not NAME.fullmatch(item):
            raise PipelineError(f"{label} has an invalid name {item!r}")
    return Stage(
        name=name,
        steps=argvs,
        runner=str(raw.get("runner", "local")),
        labels=_strings(raw.get("labels", []), f"{label} labels"),
        environment=str(raw.get("environment", "")),
        network=bool(raw.get("network", False)),
        timeout_seconds=timeout,
        artifacts=_strings(raw.get("artifacts", []), f"{label} artifacts"),
        caches=caches,
        secrets=secrets,
        manual=_strings(raw.get("manual", []), f"{label} manual"),
    )


def parse_pipeline(text: str) -> Pipeline:
    try:
        raw = yaml.safe_load(text.removeprefix("﻿"))
    except yaml.YAMLError as exc:
        raise PipelineError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PipelineError("the pipeline must be a YAML object")
    if raw.get("schemaVersion") != SCHEMA_VERSION:
        raise PipelineError(f"schemaVersion must be {SCHEMA_VERSION}")
    extra = sorted(set(raw) - {"schemaVersion", "change", "release"})
    if extra:
        raise PipelineError(f"unknown pipeline fields: {', '.join(extra)}")
    change_raw = raw.get("change")
    if not isinstance(change_raw, dict):
        raise PipelineError("change is required: bind or exclude build and test")
    if set(change_raw) != set(CHANGE_STAGES):
        raise PipelineError(
            f"change must configure exactly: {', '.join(CHANGE_STAGES)}"
        )
    change = {n: _stage(n, change_raw[n], f"change.{n}") for n in CHANGE_STAGES}
    releases: dict[str, Release] = {}
    release_raw = raw.get("release", {})
    if not isinstance(release_raw, dict):
        raise PipelineError("release must be an object of release kinds")
    for kind, body in release_raw.items():
        if not NAME.fullmatch(str(kind)) or not isinstance(body, dict):
            raise PipelineError(f"release kind {kind!r} is invalid")
        extra = sorted(set(body) - {"branch", "keep", *RELEASE_STAGES})
        if extra:
            raise PipelineError(f"unknown release.{kind} fields: {', '.join(extra)}")
        missing = [s for s in RELEASE_STAGES if s not in body]
        if missing:
            raise PipelineError(
                f"release.{kind} must bind or exclude: {', '.join(missing)}"
            )
        keep = body.get("keep", 2)
        if type(keep) is not int or not 1 <= keep <= 20:
            raise PipelineError(f"release.{kind}.keep must be 1 to 20")
        branch = body.get("branch", "main")
        if not isinstance(branch, str) or not branch or branch.startswith("-"):
            raise PipelineError(f"release.{kind}.branch is invalid")
        stages = {n: _stage(n, body[n], f"release.{kind}.{n}") for n in RELEASE_STAGES}
        package = stages["package"]
        if package.applicable and not package.artifacts:
            raise PipelineError(
                f"release.{kind}.package must name the artifacts it produces"
            )
        releases[kind] = Release(kind=kind, branch=branch, keep=keep, stages=stages)
    digest = hashlib.sha256(text.encode()).hexdigest()
    return Pipeline(change=change, releases=releases, digest=digest)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    if result.returncode:
        raise PipelineError(result.stderr.strip()[:300] or "git failed")
    return result.stdout


def read_pipeline(
    repo: Path, ref: str = "main", path: str = PIPELINE_PATH
) -> tuple[Pipeline | None, str]:
    """The pipeline at a branch head, read by commit with no checkout.

    Returns (pipeline or None when the file is absent, the commit it was read at).
    """
    if ref.startswith("-"):
        raise PipelineError("invalid ref")
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    listing = _git(repo, "ls-tree", "--name-only", commit, "--", path).strip()
    if not listing:
        return None, commit
    return parse_pipeline(_git(repo, "show", f"{commit}:{path}")), commit


# Tools that run files from the repository without naming them on the command line.
IMPLICIT_INPUTS: dict[str, tuple[str, ...]] = {
    "make": ("Makefile", "makefile", "GNUmakefile"),
    "pytest": ("conftest.py", "pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"),
    "tox": ("tox.ini", "pyproject.toml"),
    "nox": ("noxfile.py",),
    "npm": ("package.json",),
    "yarn": ("package.json",),
    "pnpm": ("package.json",),
    "cargo": ("Cargo.toml", "build.rs"),
    "go": ("go.mod",),
    "mvn": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    "just": ("justfile",),
    "rake": ("Rakefile",),
}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
INLINE_FLAGS = {"-c", "-e", "-p", "--eval", "-m"}
SCRIPT_SUFFIXES = (
    ".py",
    ".sh",
    ".bash",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".rb",
    ".pl",
    ".ps1",
)
PATH_TOKEN = re.compile(
    r"(?:\./|[\w.-]+/)[\w./-]+|[\w.-]+\.(?:py|sh|bash|js|mjs|cjs|ts|rb|pl|ps1)"
)


def _covered(path: str, protected: list[str]) -> bool:
    clean = PurePosixPath(path).as_posix().removeprefix("./")
    return any(clean == p or clean.startswith(p.rstrip("/") + "/") for p in protected)


def unprotected_steps(
    pipeline_stages: dict[str, Stage],
    protected: list[str],
    tracked: set[str] | None = None,
) -> list[str]:
    """Repository files a step would run that an agent could change.

    A stage is only trustworthy if everything it executes from the repository is a
    protected path, so the change being judged cannot rewrite what judges it. That
    covers the executable, every script-like argument, the configuration files of
    tools that read them implicitly (a Makefile, conftest.py, package.json), and any
    path named inside a shell command. `tracked` is the repository's file list at the
    base commit, used to find implicit inputs that actually exist.
    """
    problems: set[str] = set()

    def need(path: str) -> None:
        if not _covered(path, protected):
            problems.add(PurePosixPath(path).as_posix().removeprefix("./"))

    for stage in pipeline_stages.values():
        for argv in stage.steps:
            head = PurePosixPath(argv[0]).name
            if "/" in argv[0] and not argv[0].startswith("/"):
                need(argv[0])
            if head in SHELLS and "-c" in argv:
                for token in PATH_TOKEN.findall(argv[argv.index("-c") + 1]):
                    if not token.startswith("/"):
                        need(token)
            for before, element in zip(argv, argv[1:], strict=False):
                if before in INLINE_FLAGS:
                    continue  # inline code or a module name, not a file; the shell case is scanned above
                if element.startswith(("-", "/")) or "://" in element:
                    continue
                if "/" in element or element.endswith(SCRIPT_SUFFIXES):
                    need(element)
            tool = head
            if head.startswith("python") and len(argv) > 2 and argv[1] == "-m":
                tool = argv[2]
            for name in IMPLICIT_INPUTS.get(tool, ()):
                for path in sorted(tracked or ()):
                    if path == name or (
                        name == "conftest.py" and path.endswith("/conftest.py")
                    ):
                        need(path)
    return sorted(problems)


def dumps(pipeline: Pipeline) -> str:
    return json.dumps(pipeline.to_dict(), sort_keys=True)


SKELETON = """schemaVersion: 1
# Core fixes the stage order. Bind each stage to steps, or say why it does not apply.
change:
  build:
    steps:
      - ["./scripts/factory/build"]
  test:
    steps:
      - ["./scripts/factory/test"]
# release:
#   production:
#     branch: main
#     keep: 2
#     build:   {steps: [["./scripts/factory/build"]]}
#     test:    {steps: [["./scripts/factory/test"]]}
#     package: {steps: [["./scripts/factory/package"]], artifacts: ["*.tar.gz"], runner: gha, secrets: [signing-key]}
#     stage:   {steps: [["./scripts/factory/deploy", "--target", "staging"]]}
#     verify:  {steps: [["./scripts/factory/smoke"]], manual: ["Confirm on a physical device"]}
#     promote: {steps: [["./scripts/factory/deploy", "--target", "production"]]}
"""

AGENT_NOTE = """
## Working through Dark Factory

Do not open a pull request on your own. Start from an issue:

1. `dark-factory work-start <repo> <issue>` creates or finds the root outcome.
2. `dark-factory work-plan <id>` submits your plan (or asks the factory planner for one).
   A person approves the plan before any code is written.
3. `dark-factory work-checkout <id>` gives you a worktree the factory owns. Edit only there.
4. `dark-factory work-submit <id>` records your candidate and opens the pull request.
5. `dark-factory work-status <id>` shows the stage and what blocks it.

The factory builds, tests and reviews the exact commit. A required status,
`dark-factory/gate`, blocks any pull request that did not come through this path.
The files in `factory/` and the scripts they name are protected: do not change them.
"""


def init_repository(repository: Path) -> list[str]:
    """Write the skeleton pipeline and the agent instructions; never overwrite."""
    repository = Path(repository)
    if not (repository / ".git").exists():
        raise PipelineError("not a git repository")
    written = []
    target = repository / PIPELINE_PATH
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SKELETON)
        written.append(PIPELINE_PATH)
    agents = repository / "AGENTS.md"
    text = agents.read_text() if agents.exists() else "# Agent notes\n"
    if "Working through Dark Factory" not in text:
        agents.write_text(text.rstrip() + "\n" + AGENT_NOTE)
        written.append("AGENTS.md")
    return written
