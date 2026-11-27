"""The pipeline file: fixed stages, explicit exclusions, read from main by commit."""

from __future__ import annotations

import json
import subprocess

import pytest

from dark_factory.cli import main
from dark_factory.pipeline import (
    PIPELINE_PATH,
    PipelineError,
    init_repository,
    parse_pipeline,
    read_pipeline,
    unprotected_steps,
)

GOOD = """
schemaVersion: 1
change:
  build: {steps: [["./scripts/factory/build"]]}
  test: {not_applicable: "docs-only repository"}
release:
  beta:
    keep: 3
    build: {steps: [["make", "build"]]}
    test: {steps: [["make", "test"]]}
    package:
      steps: [["./scripts/package", "--sign"]]
      artifacts: ["*.bin"]
      runner: gha
      secrets: [signing-key]
    stage: {steps: [["./scripts/ota", "beta"]]}
    verify: {steps: [["./scripts/soak"]], manual: ["Confirm on a lamp"]}
    promote: {steps: [["./scripts/ota", "production"]]}
"""


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "a@b.c")
    git(root, "config", "user.name", "x")
    (root / "factory").mkdir()
    (root / PIPELINE_PATH).write_text(GOOD)
    git(root, "add", ".")
    git(root, "commit", "-m", "pipeline")
    return root


def test_a_valid_pipeline_binds_or_excludes_every_stage():
    pipeline = parse_pipeline(GOOD)
    assert pipeline.change["test"].not_applicable == "docs-only repository"
    beta = pipeline.releases["beta"]
    assert beta.keep == 3 and beta.stages["package"].secrets == ["signing-key"]
    assert pipeline.digest


@pytest.mark.parametrize(
    "mutate,message",
    [
        (
            lambda t: t.replace("test: {not_applicable", "tezt: {not_applicable"),
            "exactly",
        ),
        (
            lambda t: t.replace('{not_applicable: "docs-only repository"}', "{}"),
            "cannot be skipped silently",
        ),
        (
            lambda t: t.replace(
                '{not_applicable: "docs-only repository"}',
                '{not_applicable: "x", steps: [["a"]]}',
            ),
            "both",
        ),
        (lambda t: t.replace("schemaVersion: 1", "schemaVersion: 2"), "schemaVersion"),
        (lambda t: t.replace('artifacts: ["*.bin"]', "artifacts: []"), "artifacts"),
        (
            lambda t: t.replace('["./scripts/factory/build"]', '["../outside"]'),
            "leave the repository",
        ),
        (lambda t: t.replace("keep: 3", "keep: 99"), "keep"),
        (lambda t: t.replace("verify: {", "extra: 1\n    verify: {"), "unknown"),
    ],
)
def test_invalid_pipelines_are_refused(mutate, message):
    with pytest.raises(PipelineError, match=message):
        parse_pipeline(mutate(GOOD))


def test_a_release_kind_must_account_for_every_stage():
    broken = GOOD.replace(
        '    promote: {steps: [["./scripts/ota", "production"]]}\n', ""
    )
    with pytest.raises(PipelineError, match="promote"):
        parse_pipeline(broken)


def test_the_pipeline_is_read_from_main_by_commit_not_from_a_branch(repo):
    pipeline, commit = read_pipeline(repo, "main")
    assert pipeline is not None and commit == git(repo, "rev-parse", "main")
    git(repo, "checkout", "-b", "agent")
    (repo / PIPELINE_PATH).write_text(GOOD.replace("keep: 3", "keep: 5"))
    git(repo, "commit", "-am", "an agent edits the pipeline")
    # Main is unchanged until a person merges, so the edit has no effect.
    assert read_pipeline(repo, "main")[0].releases["beta"].keep == 3
    git(repo, "checkout", "main")
    assert read_pipeline(repo, "main")[0].releases["beta"].keep == 3


def test_a_repository_without_a_pipeline_reports_none(repo):
    git(repo, "rm", "-q", PIPELINE_PATH)
    git(repo, "commit", "-m", "remove")
    assert read_pipeline(repo, "main")[0] is None


def test_repository_scripts_must_be_protected_paths():
    pipeline = parse_pipeline(GOOD)
    assert unprotected_steps(pipeline.change, ["factory", "scripts"]) == []
    assert unprotected_steps(pipeline.change, ["factory"]) == ["scripts/factory/build"]


def test_validate_and_init_commands(repo, tmp_path, capsys):
    assert main(["pipeline-validate", str(repo)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["releases"] == ["beta"] and out["change"]["build"].startswith("1 step")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-b", "main")
    assert init_repository(fresh) == [PIPELINE_PATH, "AGENTS.md"]
    assert init_repository(fresh) == []  # never overwrites
    parse_pipeline((fresh / PIPELINE_PATH).read_text())
    assert "dark-factory work-start" in (fresh / "AGENTS.md").read_text()


def stages(*argvs):
    from dark_factory.pipeline import Stage

    return {"x": Stage(name="x", steps=[list(a) for a in argvs])}


def test_every_file_a_step_runs_must_be_protected_not_just_the_executable():
    protected = ["factory"]
    # An interpreter running an unprotected script is as bad as an unprotected executable.
    assert unprotected_steps(stages(["python", "scripts/check.py"]), protected) == [
        "scripts/check.py"
    ]
    assert unprotected_steps(stages(["node", "run.js"]), protected) == ["run.js"]
    assert unprotected_steps(stages(["sh", "-c", "./x && echo ok"]), protected) == ["x"]
    # Tools that read project files implicitly: only files that exist matter.
    tracked = {"Makefile", "src/a.py", "tests/conftest.py"}
    assert unprotected_steps(stages(["make"]), protected, tracked) == ["Makefile"]
    assert unprotected_steps(
        stages(["python", "-m", "pytest"]), protected, tracked
    ) == ["tests/conftest.py"]
    assert unprotected_steps(stages(["pytest"]), protected, {"src/a.py"}) == []
    # Once protected they are fine.
    assert unprotected_steps(stages(["make"]), ["Makefile"], tracked) == []
    assert (
        unprotected_steps(stages(["python", "-m", "pytest"]), ["tests"], tracked) == []
    )
    # Plain commands and flags are not paths.
    assert (
        unprotected_steps(stages(["git", "diff", "--check"], ["echo", "hi"]), protected)
        == []
    )
