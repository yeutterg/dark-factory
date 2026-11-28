"""The release loop: automatic until the package, then two human decisions."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from dark_factory.config import validate_raw
from dark_factory.controller import Controller, ControllerError
from dark_factory.pipeline import PIPELINE_PATH
from dark_factory.store import Store

PROMPTS = Path("src/dark_factory/agents/prompts")
PROFILE = Path("examples/factory.toml").resolve()

PIPELINE = """schemaVersion: 1
change:
  build: {not_applicable: "n/a"}
  test: {not_applicable: "n/a"}
release:
  prod:
    keep: 2
    build:
      steps: [["python", "-c", "import os, sys; sys.exit(1 if 'SIGNING_KEY' in os.environ else 0)"]]
    test: {steps: [["python", "-c", "pass"]]}
    package:
      steps:
        - ["python", "-c", "import os, sys; sys.exit(0 if os.environ.get('SIGNING_KEY') == 's3cret' else 1)"]
        - ["python", "-c", "import os; open(os.environ['FACTORY_OUT'] + '/app.bin', 'w').write(os.environ['FACTORY_COMMIT'])"]
      artifacts: ["*.bin"]
      secrets: [signing-key]
    stage: {steps: [["python", "-c", "pass"]]}
    verify: {steps: [["python", "-c", "pass"]], manual: ["Confirm on a lamp"]}
    promote: {steps: [["python", "-c", "pass"]]}
"""


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        text=True,
    ).strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DARK_FACTORY_SECRET_SIGNING_KEY", "s3cret")
    repo = tmp_path / "repo"
    (repo / "factory").mkdir(parents=True)
    git(repo, "init", "-b", "main")
    (repo / PIPELINE_PATH).write_text(PIPELINE)
    (repo / "app.txt").write_text("v1")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "v1")
    raw = tomllib.loads(PROFILE.read_text())
    name = raw["repositories"][0]
    raw["commands"]["verify"] = {"argv": ["true"], "timeout_seconds": 5}
    raw["sources"] = {
        name: {
            "path": str(repo),
            "revision": "HEAD",
            "checks": ["verify"],
            "allowed_paths": ["app.txt"],
            "protected_paths": ["factory"],
        }
    }
    raw["operators"] = {"humans": ["greg", "ada"]}
    raw["runners"] = {"local": {"secrets": ["signing-key"]}}
    raw["project"]["state_dir"] = str(tmp_path / "state")
    cfg = validate_raw(raw, PROFILE, "digest")
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, PROMPTS)
    yield cfg, store, ctrl, repo, name
    store.close()


def run_automatic(ctrl, rid):
    while ctrl.release_run_stage(rid):
        pass
    return ctrl.release_status(rid)


def test_a_release_runs_to_the_package_then_waits_for_a_person(world):
    cfg, store, ctrl, repo, name = world
    started = ctrl.release_start(name, "prod", "operator")
    assert started["stages"][0]["stage"] == "assemble"
    rid = started["release"]
    status = run_automatic(ctrl, rid)
    assert status["status"] == "awaiting_package_approval"
    assert {s["stage"]: s["status"] for s in status["stages"]} == {
        "assemble": "passed",
        "build": "passed",
        "test": "passed",
        "package": "passed",
    }
    assert len(status["package_digest"]) == 64
    assert (cfg.state_dir / "release-artifacts" / rid / "app.bin").exists()
    assert not (cfg.state_dir / "releases").exists() or not any(
        (cfg.state_dir / "releases").iterdir()
    )
    # Nothing moves without a person.
    assert ctrl.release_run_stage(rid) is None
    with pytest.raises(ControllerError):
        ctrl.release_approve(rid, "package", "contributor:agent")


def test_two_human_decisions_and_the_manual_items_gate_production(world):
    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    run_automatic(ctrl, rid)
    with pytest.raises(ControllerError, match="needs awaiting_promotion_approval"):
        ctrl.release_approve(rid, "promote", "greg")
    ctrl.release_approve(rid, "package", "greg")
    assert run_automatic(ctrl, rid)["status"] == "verifying"  # stage and verify ran
    with pytest.raises(ControllerError, match="awaiting_promotion_approval"):
        ctrl.release_approve(rid, "promote", "ada")  # the manual item is not confirmed
    with pytest.raises(ControllerError, match="not a manual verification item"):
        ctrl.release_confirm(rid, "something else", "ada")
    ctrl.release_confirm(rid, "Confirm on a lamp", "ada", "lamp 3 updated")
    assert ctrl.release_status(rid)["status"] == "awaiting_promotion_approval"
    done = ctrl.release_approve(rid, "promote", "ada")
    assert done["status"] == "released"
    assert "app" not in done["next"]
    notes = store.one("SELECT notes FROM releases WHERE id=?", (rid,))[0]
    assert rid in notes


def test_explicit_release_run_consumes_a_queued_promotion_once(world, monkeypatch):
    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    run_automatic(ctrl, rid)
    ctrl.release_approve(rid, "package", "greg")
    run_automatic(ctrl, rid)
    ctrl.release_confirm(rid, "Confirm on a lamp", "ada")
    queued = ctrl.release_approve(rid, "promote", "ada", asynchronous=True)
    assert queued["status"] == "promotion_approved"

    calls = 0
    original = ctrl._execute

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ctrl, "_execute", counted)
    assert ctrl.release_run_stage(rid)["status"] == "released"
    assert ctrl._run_promote(rid)["status"] == "released"
    assert calls == 1


def test_a_changed_package_voids_the_approval(world):
    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    run_automatic(ctrl, rid)
    ctrl.release_approve(rid, "package", "greg")
    run_automatic(ctrl, rid)
    ctrl.release_confirm(rid, "Confirm on a lamp", "ada")
    store.execute("UPDATE releases SET package_digest=? WHERE id=?", ("0" * 64, rid))
    with pytest.raises(ControllerError, match="no longer covers this package"):
        ctrl.release_approve(rid, "promote", "ada")


def test_secrets_reach_only_the_stage_that_asked_for_them(world, monkeypatch):
    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    assert run_automatic(ctrl, rid)["status"] == "awaiting_package_approval"
    # Without the profile's grant the release cannot even start.
    cfg.raw["runners"] = {"local": {"secrets": []}}
    with pytest.raises(ControllerError, match="does not grant"):
        ctrl.release_start(name, "prod", "operator")


def test_a_failing_stage_stops_the_release_with_its_evidence(world):
    cfg, store, ctrl, repo, name = world
    (repo / PIPELINE_PATH).write_text(
        PIPELINE.replace(
            "\"import os, sys; sys.exit(1 if 'SIGNING_KEY' in os.environ else 0)\"",
            '"raise SystemExit(7)"',
        )
    )
    git(repo, "commit", "-am", "break the build")
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    status = run_automatic(ctrl, rid)
    assert status["status"] == "failed" and "build stage failed" in status["reason"]
    evidence = store.one(
        "SELECT evidence_json FROM release_stages WHERE stage='build'"
    )[0]
    assert '"exit_code": 7' in evidence


def test_only_the_newest_packages_are_kept_and_rollback_redeploys_the_previous(world):
    cfg, store, ctrl, repo, name = world

    def ship(version):
        (repo / "app.txt").write_text(version)
        git(repo, "commit", "-am", version)
        rid = ctrl.release_start(name, "prod", "operator")["release"]
        run_automatic(ctrl, rid)
        ctrl.release_approve(rid, "package", "greg")
        run_automatic(ctrl, rid)
        ctrl.release_confirm(rid, "Confirm on a lamp", "ada")
        ctrl.release_approve(rid, "promote", "ada")
        return rid

    first, second, third = ship("v2"), ship("v3"), ship("v4")
    root = cfg.state_dir / "release-artifacts"
    assert not (root / first).exists(), "keep=2: the oldest package is removed"
    assert (root / second).exists() and (root / third).exists()
    assert store.one("SELECT pruned FROM releases WHERE id=?", (first,))[0] == 1
    rollback = ctrl.release_rollback(third, "greg")
    assert rollback["status"] == "awaiting_package_approval"
    assert rollback["rollback_of"] == third
    assert (
        rollback["package_digest"]
        == store.one("SELECT package_digest FROM releases WHERE id=?", (second,))[0]
    )


def test_a_release_needs_a_defined_kind_and_a_configured_repository(world):
    cfg, store, ctrl, repo, name = world
    with pytest.raises(ControllerError, match="defines no release kind"):
        ctrl.release_start(name, "nightly", "operator")
    with pytest.raises(ControllerError, match="no configured source"):
        ctrl.release_start("other/repo", "prod", "operator")


def test_the_tick_runs_automatic_stages_in_the_background(world):
    import time

    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ctrl.release_tick()
        if ctrl.release_status(rid)["status"] == "awaiting_package_approval":
            break
        time.sleep(0.1)
    assert ctrl.release_status(rid)["status"] == "awaiting_package_approval"


def test_a_stage_interrupted_by_a_restart_is_failed_not_left_running(world):
    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    ctrl._set_stage(rid, "build", "running")
    ctrl.recover_releases()
    status = ctrl.release_status(rid)
    assert status["status"] == "failed" and "restart" in status["reason"]


def test_one_report_covers_both_loops(world):
    from dark_factory.metrics import stage_metrics

    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    run_automatic(ctrl, rid)
    ctrl.release_approve(rid, "package", "greg")
    run_automatic(ctrl, rid)
    report = stage_metrics(store)
    stages = {(r["loop"], r["stage"]): r for r in report["stages"]}
    assert stages[("release", "package")]["runs"] == 1
    assert stages[("release", "package")]["runner"] == "local"
    assert stages[("release", "package")]["passed"] == 1
    assert report["slowest"] and report["slowest"][0]["loop"] == "release"


def test_two_threads_never_run_the_same_stage(world):
    import threading

    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    lock = ctrl._lock(rid)
    held = threading.Event()
    release_it = threading.Event()

    def hold():
        with lock:
            held.set()
            release_it.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(5)
    try:
        assert ctrl.release_run_stage(rid) is None  # another thread owns this release
    finally:
        release_it.set()
        thread.join()
    assert ctrl.release_run_stage(rid) is not None


def test_a_package_changed_after_approval_is_never_deployed(world):
    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    run_automatic(ctrl, rid)
    ctrl.release_approve(rid, "package", "greg")
    (cfg.state_dir / "release-artifacts" / rid / "app.bin").write_text("swapped")
    status = run_automatic(ctrl, rid)
    assert status["status"] == "failed" and "no longer matches" in status["reason"]
    assert not store.query(
        "SELECT 1 FROM release_stages WHERE stage IN ('stage','promote')"
    )


def test_stage_and_promote_steps_are_told_where_the_verified_package_is(world):
    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    run_automatic(ctrl, rid)
    stage = ctrl._pipeline_of(ctrl._release(rid)).releases["prod"].stages["stage"]
    probe = type(stage)(
        name="stage",
        steps=[
            [
                "python",
                "-c",
                "import os; assert os.path.isfile(os.environ['FACTORY_PACKAGE'] + '/app.bin')",
            ]
        ],
    )
    assert ctrl._execute(ctrl._release(rid), "stage", probe)["ok"]


def test_a_failed_release_can_be_retried_or_cancelled_by_a_person_only(world):
    cfg, store, ctrl, repo, name = world
    (repo / PIPELINE_PATH).write_text(
        PIPELINE.replace(
            'test: {steps: [["python", "-c", "pass"]]}',
            'test: {steps: [["python", "-c", "raise SystemExit(4)"]]}',
            1,
        )
    )
    git(repo, "commit", "-am", "failing test stage")
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    assert run_automatic(ctrl, rid)["status"] == "failed"
    with pytest.raises(ControllerError):
        ctrl.release_retry(rid, "contributor:agent")
    # The same commit would fail again; retry puts it back at the failed stage.
    retried = ctrl.release_retry(rid, "greg")
    assert retried["status"] == "assembled" and retried["reason"] == ""
    assert [s["stage"] for s in retried["stages"]] == ["assemble", "build"]
    cancelled = ctrl.release_cancel(rid, "greg")
    assert cancelled["status"] == "cancelled"
    assert not (cfg.state_dir / "release-artifacts" / rid).exists()
    with pytest.raises(ControllerError, match="already"):
        ctrl.release_cancel(rid, "greg")


def test_a_release_without_a_package_stage_is_approved_against_its_commit(world):
    cfg, store, ctrl, repo, name = world
    text = PIPELINE.replace(
        PIPELINE[PIPELINE.index("    package:") : PIPELINE.index("    stage:")],
        '    package: {not_applicable: "a server deploy has no artifact"}\n',
    )
    (repo / PIPELINE_PATH).write_text(text)
    git(repo, "commit", "-am", "no package")
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    status = run_automatic(ctrl, rid)
    assert (
        status["status"] == "awaiting_package_approval"
        and len(status["package_digest"]) == 64
    )
    ctrl.release_approve(rid, "package", "greg")  # no longer "there is no package"


def test_a_missing_secret_value_fails_the_stage_instead_of_running_with_nothing(
    world, monkeypatch
):
    cfg, store, ctrl, repo, name = world
    monkeypatch.delenv("DARK_FACTORY_SECRET_SIGNING_KEY")
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    status = run_automatic(ctrl, rid)
    assert status["status"] == "failed"
    evidence = store.one(
        "SELECT evidence_json FROM release_stages WHERE stage='package'"
    )[0]
    assert "is not provided" in evidence


def test_a_promotion_interrupted_by_a_restart_is_failed_not_stuck(world):
    cfg, store, ctrl, repo, name = world
    rid = ctrl.release_start(name, "prod", "operator")["release"]
    store.execute("UPDATE releases SET status='promoting' WHERE id=?", (rid,))
    ctrl.recover_releases()
    assert ctrl.release_status(rid)["status"] == "failed"
