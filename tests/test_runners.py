"""A stage runs where the profile allows: local capacity, GitHub Actions or Blacksmith."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from dark_factory.config import ConfigError, validate_raw
from dark_factory.pipeline import parse_pipeline
from dark_factory.runners import (
    HostedRunner,
    RunnerError,
    capacity_blocker,
    check_pipeline_grants,
    runner_table,
)

PIPELINE = """
schemaVersion: 1
change:
  build: {steps: [["make"]], labels: [esp-idf]}
  test: {steps: [["make", "test"]]}
release:
  prod:
    build: {steps: [["make"]]}
    test: {steps: [["make", "test"]]}
    package: {steps: [["./pack"]], artifacts: ["*.bin"], runner: signer, secrets: [signing-key]}
    stage: {steps: [["./ota"]]}
    verify: {steps: [["./soak"]]}
    promote: {steps: [["./ota", "prod"]]}
"""
PATH = Path("examples/factory.toml").resolve()


def profile(**extra):
    raw = tomllib.loads(PATH.read_text())
    raw.update(extra)
    return raw


def test_local_always_exists_and_has_no_secrets():
    table = runner_table(profile())
    assert table["local"]["kind"] == "local" and table["local"]["secrets"] == []


def test_a_stage_may_only_use_runners_secrets_and_labels_the_profile_grants():
    pipeline = parse_pipeline(PIPELINE)
    with pytest.raises(RunnerError, match="labels"):
        check_pipeline_grants(pipeline, runner_table(profile()))
    granted = runner_table(
        profile(
            runners={
                "local": {"labels": ["esp-idf"]},
                "signer": {"kind": "gha", "secrets": ["signing-key"]},
            }
        )
    )
    check_pipeline_grants(pipeline, granted)
    ungranted = runner_table(
        profile(runners={"local": {"labels": ["esp-idf"]}, "signer": {"kind": "gha"}})
    )
    with pytest.raises(RunnerError, match="does not grant"):
        check_pipeline_grants(pipeline, ungranted)
    undefined = runner_table(profile(runners={"local": {"labels": ["esp-idf"]}}))
    with pytest.raises(RunnerError, match="does not define"):
        check_pipeline_grants(pipeline, undefined)


def test_a_signing_secret_cannot_be_granted_to_a_local_runner():
    raw = profile(
        secret_classes={"signing-key": "signing"},
        runners={"bench": {"kind": "local", "secrets": ["signing-key"]}},
    )
    with pytest.raises(ConfigError, match="signing secret"):
        validate_raw(raw, PATH, "digest")
    raw["runners"]["bench"]["allow_signing"] = True  # the explicit override (issue 18)
    validate_raw(raw, PATH, "digest")
    raw["runners"] = {"signer": {"kind": "gha", "secrets": ["signing-key"]}}
    validate_raw(raw, PATH, "digest")


def test_local_capacity_limits_parallel_load_and_disk(tmp_path, monkeypatch):
    runner = runner_table(profile())["local"]
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    assert capacity_blocker(runner, 0, tmp_path) is None
    assert "parallel" in capacity_blocker(runner, 1, tmp_path)
    monkeypatch.setattr(os, "getloadavg", lambda: (10_000.0, 0, 0))
    assert "load" in capacity_blocker(runner, 0, tmp_path)
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0))
    assert "disk" in capacity_blocker({**runner, "min_free_gb": 10**9}, 0, tmp_path)


def test_change_stages_on_a_hosted_runner_are_refused_until_the_candidate_is_published(
    tmp_path, monkeypatch
):
    import subprocess

    from test_pipeline_stages import with_pipeline
    from test_workflow import setup

    from dark_factory.controller import ControllerError
    from dark_factory.pipeline import PIPELINE_PATH  # noqa: F401

    hosted = (
        'schemaVersion: 1\nchange:\n  build: {steps: [["python", "-c", "pass"]], runner: gha}\n'
        '  test: {not_applicable: "none"}\n'
    )
    with_pipeline(monkeypatch, hosted)
    monkeypatch.setattr(subprocess, "check_output", subprocess.check_output)
    with pytest.raises(ControllerError, match="does not define|local runners"):
        setup(tmp_path)


class Host:
    def __init__(self, conclusion="success"):
        self.calls = []
        self.conclusion = conclusion
        self.polls = 0

    def __call__(self, command, operation, payload, request_id, timeout):
        self.calls.append((operation, payload))
        evidence = {
            "dispatch_workflow": {"observed": True},
            "find_run": {"run_id": 41 if self.polls else None},
            "get_run": {
                "status": "completed",
                "conclusion": self.conclusion,
                "url": "https://example.test/run/41",
                "head_sha": "a" * 40,
            },
            "list_run_artifacts": {
                "artifacts": [
                    {"name": "stage", "size": 3, "digest": "sha256:abc", "id": 1}
                ]
            },
        }[operation]
        if operation == "find_run":
            self.polls += 1
        return {"ok": True, "operation": operation, "evidence": evidence}


class Command:
    command = ["x"]
    timeout_seconds = 5


def run_hosted(host, kind="gha"):
    stage = parse_pipeline(PIPELINE).releases["prod"].stages["package"]
    runner = {**runner_table(profile())["local"], "kind": kind, "runs_on": ""}
    return HostedRunner(host, Command(), "o/n", runner, "req1").run(
        stage, "release", "a" * 40, 30, sleep=lambda s: None
    ), host


def test_a_hosted_stage_is_dispatched_found_and_its_artifacts_are_listed_by_the_host():
    result, host = run_hosted(Host())
    assert result.ok and result.url.endswith("/41")
    assert result.artifacts == [
        {"name": "stage", "size": 3, "digest": "sha256:abc", "id": 1}
    ]
    dispatched = host.calls[0][1]
    assert dispatched["inputs"]["runs_on"] == "ubuntu-24.04"
    assert json.loads(dispatched["inputs"]["steps_json"]) == [["./pack"]]


def test_blacksmith_is_the_same_adapter_with_another_label():
    result, host = run_hosted(Host(), kind="blacksmith")
    assert host.calls[0][1]["inputs"]["runs_on"] == "blacksmith"


def test_a_failed_hosted_stage_returns_no_artifacts():
    result, _ = run_hosted(Host("failure"))
    assert not result.ok and result.artifacts == []
