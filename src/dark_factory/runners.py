"""Where a stage runs: a machine with spare capacity, GitHub Actions, or Blacksmith.

The pipeline asks for a runner and secrets by name; this module checks the request
against what the host profile grants, and carries it out. Every runner runs the same
step argv, so evidence has the same shape wherever the work happened.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from dark_factory.pipeline import Pipeline, Stage

KINDS = {"local", "gha", "blacksmith"}
HOSTED = {"gha", "blacksmith"}
DEFAULT_RUNNER = {
    "kind": "local",
    "labels": [],
    "secrets": [],
    "max_parallel": 1,
    "max_load": 0.8,
    "min_free_gb": 2,
    "workflow": "dark-factory-stage.yml",
    "runs_on": "",
    "gh_environment": "",
    "allow_signing": False,
}


class RunnerError(ValueError):
    pass


def runner_table(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The profile's runners with defaults; `local` always exists."""
    runners = {"local": dict(DEFAULT_RUNNER)}
    for name, item in raw.get("runners", {}).items():
        runners[name] = {**DEFAULT_RUNNER, **item}
        if runners[name]["kind"] == "blacksmith" and not runners[name]["runs_on"]:
            runners[name]["runs_on"] = "blacksmith-4vcpu-ubuntu-2404"
    return runners


def validate_runner_table(raw: dict[str, Any]) -> None:
    from dark_factory.config import ConfigError

    table = raw.get("runners", {})
    if not isinstance(table, dict):
        raise ConfigError("runners must be a table")
    allowed = set(DEFAULT_RUNNER)
    secret_classes = raw.get("secret_classes", {})
    for name, item in table.items():
        if not isinstance(item, dict) or set(item) - allowed:
            raise ConfigError(f"runner {name} has invalid fields")
        if item.get("kind", "local") not in KINDS:
            raise ConfigError(f"runner {name} kind must be one of {sorted(KINDS)}")
        for key in ("labels", "secrets"):
            if not isinstance(item.get(key, []), list):
                raise ConfigError(f"runner {name} {key} must be a list")
        for key in ("workflow", "runs_on", "gh_environment"):
            if key in item and not isinstance(item[key], str):
                raise ConfigError(f"runner {name} {key} must be a string")
        merged = {**DEFAULT_RUNNER, **item}
        for secret_name in merged["secrets"]:
            if (
                secret_classes.get(secret_name) == "signing"
                and merged["kind"] == "local"
                and not merged["allow_signing"]
            ):
                raise ConfigError(
                    f"signing secret {secret_name} cannot be granted to a local runner "
                    "(set allow_signing to override; see issue 18)"
                )


def check_grants(
    stages: dict[str, Stage], runners: dict[str, dict[str, Any]], label: str
) -> None:
    """Every runner and secret a stage asks for must be granted by the profile."""
    for stage in stages.values():
        if not stage.applicable:
            continue
        runner = runners.get(stage.runner)
        if runner is None:
            raise RunnerError(
                f"{label}.{stage.name} names runner {stage.runner!r}, which the profile does not define"
            )
        for secret_name in stage.secrets:
            if secret_name not in runner["secrets"]:
                raise RunnerError(
                    f"{label}.{stage.name} asks for secret {secret_name!r}, which the "
                    f"profile does not grant to runner {stage.runner!r}"
                )
        missing = [x for x in stage.labels if x not in runner["labels"]]
        if missing and runner["kind"] == "local":
            raise RunnerError(
                f"{label}.{stage.name} needs labels {missing} the local runner lacks"
            )


def check_pipeline_grants(
    pipeline: Pipeline, runners: dict[str, dict[str, Any]]
) -> None:
    check_grants(pipeline.change, runners, "change")
    for kind, release in pipeline.releases.items():
        check_grants(release.stages, runners, f"release.{kind}")


def capacity_blocker(
    runner: dict[str, Any], running: int, state_dir: Any
) -> str | None:
    """Why a local machine should not take another stage now, or None."""
    if running >= runner["max_parallel"]:
        return "at its parallel limit"
    cpus = os.cpu_count() or 1
    if os.getloadavg()[0] / cpus > runner["max_load"]:
        return "load is above its ceiling"
    if shutil.disk_usage(state_dir).free < runner["min_free_gb"] * 1024**3:
        return "free disk is below its floor"
    return None


@dataclass
class HostedResult:
    ok: bool
    url: str = ""
    conclusion: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    run_id: int | None = None


class HostedRunner:
    """One stage on a hosted runner through the code-host command contract."""

    def __init__(self, run_json, command, repo, runner, request_id):
        self.run_json, self.command = run_json, command
        self.repo, self.runner, self.request_id = repo, runner, request_id

    def _call(self, operation, payload):
        result = self.run_json(
            self.command.command,
            operation,
            {"repo": self.repo, **payload},
            self.request_id + ":" + operation,
            self.command.timeout_seconds,
        )
        if not result.get("ok"):
            raise RunnerError(f"hosted runner {operation} failed")
        return result["evidence"]

    def run(
        self,
        stage: Stage,
        kind: str,
        commit: str,
        timeout: float,
        sleep=time.sleep,
        poll_seconds: float = 10.0,
        branch: str = "main",
        extra_inputs: dict[str, str] | None = None,
    ) -> HostedResult:
        self._call(
            "dispatch_workflow",
            {
                "workflow": self.runner["workflow"],
                # GitHub dispatches a branch or tag; the workflow checks out `commit`.
                "ref": branch,
                "inputs": {
                    "request_id": self.request_id,
                    "stage": stage.name,
                    "kind": kind,
                    "commit": commit,
                    "steps_json": __import__("json").dumps(stage.steps),
                    "runs_on": self.runner["runs_on"]
                    or (
                        "blacksmith"
                        if self.runner["kind"] == "blacksmith"
                        else "ubuntu-24.04"
                    ),
                    "gh_environment": self.runner.get("gh_environment", ""),
                    **(extra_inputs or {}),
                },
            },
        )
        deadline = time.monotonic() + timeout
        run_id = None
        while time.monotonic() < deadline:
            found = self._call(
                "find_run", {"request_id": self.request_id, "commit": commit}
            )
            run_id = found.get("run_id")
            if run_id:
                state = self._call("get_run", {"run_id": run_id})
                if state["status"] == "completed":
                    ok = state["conclusion"] == "success"
                    artifacts = (
                        self._call("list_run_artifacts", {"run_id": run_id})[
                            "artifacts"
                        ]
                        if ok
                        else []
                    )
                    return HostedResult(
                        ok, state["url"], state["conclusion"], artifacts, run_id
                    )
            sleep(poll_seconds)
        raise RunnerError("hosted stage did not finish within its time limit")

    def download(self, run_id, artifact, destination):
        return self._call(
            "download_run_artifact",
            {
                "run_id": run_id,
                "artifact_id": artifact["id"],
                "expected_digest": artifact["digest"],
                "destination_path": str(destination),
            },
        )
