"""Bootstrap acceptance tests: real Git, isolation, recovery and external contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_loop import cfg_for

from dark_factory import artifacts
from dark_factory.adapters.env.native import NativeEnvironment, process_environment
from dark_factory.adapters.git import github
from dark_factory.adapters.git.local import git
from dark_factory.adapters.projects import github as tracking
from dark_factory.contracts import make_request
from dark_factory.controller import Controller, ControllerError
from dark_factory.store import Store
from dark_factory.worker import Worker


@pytest.fixture
def loop(tmp_path):
    cfg = cfg_for(tmp_path / "state")
    cfg.windows = []
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    wid = ctrl.intake_issues(
        [
            {
                "repo": cfg.repositories[0],
                "number": 42,
                "title": "Message ready",
                "body": "message() must return ready",
            }
        ]
    )[0]
    ctrl.start_planning(wid)
    yield cfg, store, ctrl, wid
    store.close()


def build(loop):
    cfg, store, ctrl, wid = loop
    worker = Worker(cfg, store, ctrl)
    assert worker.run_loop(2) == 2
    ctrl.approve(wid, "human")
    assert worker.run_loop() == 3
    return worker


def test_real_candidate_checks_review_and_manual_acceptance(loop):
    cfg, store, ctrl, wid = loop
    build(loop)
    root = store.one("SELECT * FROM work_items WHERE id=?", (wid,))
    assert root["status"] == "awaiting_review"
    candidate = json.loads(root["candidate_commits"])[cfg.repositories[0]]
    source = Path(json.loads(root["snapshot_json"])["source"]["path"])
    assert "pending" in (source / "message.py").read_text(), (
        "developer source must remain unchanged"
    )
    assert (
        "ready" in (Path(candidate["archive"]) / "workspace" / "message.py").read_text()
    )
    assert (
        git(Path(candidate["archive"]) / "workspace", "rev-parse", "HEAD")
        == candidate["commit"]
    )
    review = store.one(
        "SELECT a.result_json FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.kind='review'"
    )
    assert json.loads(review[0])["inputs"]["candidate"] == json.loads(
        root["candidate_commits"]
    )
    with pytest.raises(ControllerError, match="manual gate missing"):
        ctrl.accept(wid, "human")
    ctrl.record_manual_gate(
        wid,
        "Inspect candidate diff",
        candidate["commit"],
        "human",
        "Exact diff inspected",
    )
    ctrl.accept(wid, "human")
    assert store.one("SELECT accepted_at FROM work_items")[0]
    packet = json.loads(store.one("SELECT packet_json FROM work_items")[0])
    assert packet["manual_remaining"] == []
    assert packet["missing_evidence"] == []
    assert ctrl.proposal(wid)["plan"]["proposal"]["what"]


def test_archive_tamper_blocks_acceptance(loop):
    cfg, store, ctrl, wid = loop
    build(loop)
    archive = Path(
        store.one("SELECT archive_path FROM attempts ORDER BY started_at LIMIT 1")[0]
    )
    (archive / "output.txt").write_text("tampered")
    with pytest.raises(ValueError, match="verification"):
        ctrl.accept(wid, "human")


def test_critic_rejection_cannot_approve(loop, monkeypatch):
    from dark_factory.adapters.harness.fake import FakeHarness

    cfg, store, ctrl, wid = loop
    monkeypatch.setattr(
        Worker,
        "_harness",
        lambda self, route: FakeHarness(
            {
                "critic": json.dumps(
                    {
                        "passed": False,
                        "findings": ["missing behavior"],
                        "summary": "blocked",
                    }
                )
            }
        ),
    )
    Worker(cfg, store, ctrl).run_loop(2)
    with pytest.raises(ControllerError):
        ctrl.approve(wid, "human")
    assert store.one("SELECT status FROM work_items")[0] == "failed"


def test_replan_preserves_prior_attempts_and_approval_history(loop):
    cfg, store, ctrl, wid = loop
    Worker(cfg, store, ctrl).run_loop(2)
    ctrl.reject(wid, "human", "revise")
    before = [r["id"] for r in store.query("SELECT id FROM attempts")]
    ctrl.start_planning(wid)
    Worker(cfg, store, ctrl).run_loop(2)
    assert store.one("SELECT plan_version FROM work_items")[0] == 2
    assert len(store.query("SELECT * FROM proposals")) == 2
    assert all(store.one("SELECT id FROM attempts WHERE id=?", (a,)) for a in before)


def test_backup_restore_preserves_candidate_evidence(loop, tmp_path):
    cfg, store, ctrl, wid = loop
    build(loop)
    target = tmp_path / "backup"
    artifacts.backup(store, target)
    restored = tmp_path / "restored"
    artifacts.restore(target, restored)
    other = Store(restored)
    try:
        old = other.one("SELECT archive_path FROM attempts")[0]
        assert other.resolve_path(old).is_relative_to(restored)
        artifacts.verify(other.resolve_path(old))
        assert other.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        other.close()
    with pytest.raises(ValueError):
        artifacts.restore(target, restored)


def test_backup_refuses_active_attempt(loop, tmp_path):
    cfg, store, ctrl, wid = loop
    ctrl.claim_job("worker", ["native", "fake"])
    with pytest.raises(ValueError, match="drain"):
        artifacts.backup(store, tmp_path / "backup")


def test_state_has_one_process_owner(loop):
    cfg, store, ctrl, wid = loop
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            'from dark_factory.store import Store; from pathlib import Path; Store(Path(__import__("sys").argv[1]))',
            str(cfg.state_dir),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "controller owner" in proc.stderr


def test_recover_expired_claim_releases_reservation_without_replacement(loop):
    cfg, store, ctrl, wid = loop
    ctrl.claim_job("worker", ["native", "fake"])
    store.execute("UPDATE attempts SET deadline='2000-01-01T00:00:00+00:00'")
    assert ctrl.recover() == 1
    assert store.one("SELECT reserved_cents FROM work_items")[0] == 0
    assert store.one("SELECT status FROM attempts")[0] == "timed_out"
    assert ctrl.claim_job("replacement", ["native", "fake"]) is None


def test_macos_denies_host_data_and_protected_writes(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("native macOS qualification")
    env = NativeEnvironment(tmp_path / "owned")
    prepared = env.prepare("attempt", None)
    (prepared.workspace / "allowed.txt").write_text("before")
    (prepared.workspace / "protected.txt").write_text("fixed")
    host_file = tmp_path / "host-secret"
    host_file.write_text("secret-marker")
    runtime = tmp_path / "runtime"
    prefix = env.prefix(
        prepared,
        runtime,
        network=False,
        writable=["allowed.txt"],
        protected=["protected.txt"],
    )
    from dark_factory.runner import CommandRunner

    code, out, err = CommandRunner(prepared.workspace)._run(
        prefix
        + [
            "/bin/sh",
            "-c",
            'echo after > allowed.txt; echo changed > protected.txt; cat "$1"',
            "probe",
            str(host_file),
        ],
        b"",
        5,
        prepared.workspace,
        process_environment(runtime),
    )
    assert code != 0 and b"secret-marker" not in out
    assert (prepared.workspace / "allowed.txt").read_text() == "after\n"
    assert (prepared.workspace / "protected.txt").read_text() == "fixed"


def test_github_intake_reads_selected_remote_issue(monkeypatch):
    calls = []

    def fake(path):
        calls.append(path)
        return {
            "title": "Live observed title",
            "body": "requirements",
            "html_url": "https://github.com/acme/tool/issues/3",
        }

    monkeypatch.setattr(tracking, "api", fake)
    result = tracking.handle(
        make_request(
            "list_selected_issues",
            {"issues": [{"repo": "acme/tool", "number": 3}]},
            "action",
        )
    )
    assert calls == ["repos/acme/tool/issues/3"]
    assert result["items"][0]["title"] == "Live observed title"
    assert result["evidence"]["observations"][0]["source"] == "github-api"


def test_pr_reconciles_without_duplicate_post(monkeypatch):
    sha = "a" * 40
    calls = []

    def fake(path, method="GET", payload=None):
        calls.append((method, path))
        if "/commits/" in path:
            return {"sha": sha}
        return [
            {
                "number": 7,
                "html_url": "https://github.com/acme/tool/pull/7",
                "head": {"sha": sha},
                "body": "<!-- dark-factory:action -->",
            }
        ]

    monkeypatch.setattr(github, "api", fake)
    result = github.handle(
        make_request(
            "create_pr",
            {
                "repo": "acme/tool",
                "head": "candidate",
                "base": "main",
                "head_sha": sha,
                "title": "Test",
            },
            "action",
        )
    )
    assert result["evidence"]["reconciled"] is True
    assert all(method == "GET" for method, path in calls)


def test_pr_changed_branch_rejected_before_write(monkeypatch):
    monkeypatch.setattr(github, "api", lambda *a, **k: {"sha": "b" * 40})
    with pytest.raises(ValueError, match="branch changed"):
        github.handle(
            make_request(
                "create_pr",
                {
                    "repo": "acme/tool",
                    "head": "candidate",
                    "base": "main",
                    "head_sha": "a" * 40,
                    "title": "Test",
                },
                "action",
            )
        )


def test_expired_lease_cannot_be_renewed(loop):
    cfg, store, ctrl, wid = loop
    claim = ctrl.claim_job("worker", ["native", "fake"])
    store.execute(
        "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
        (claim["job"]["id"],),
    )
    assert ctrl.heartbeat("worker", claim["attempt_id"]) is False
    assert ctrl.recover() == 1
    assert store.one("SELECT status FROM attempts")[0] == "timed_out"


def test_reload_keeps_last_valid_config(tmp_path):
    config = tmp_path / "factory.toml"
    text = Path("examples/factory.toml").read_text()
    config.write_text(text)
    from dark_factory.config import load_config

    cfg = load_config(config)
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        original = ctrl.cfg.digest
        config.write_text("schema_version = 'invalid'")
        assert ctrl.reload() is False
        assert ctrl.cfg.digest == original
        config.write_text(text.replace("root_cents = 2000", "root_cents = 1800"))
        assert ctrl.reload() is True
        assert ctrl.cfg.raw["budgets"]["root_cents"] == 1800
    finally:
        store.close()


def test_shell_process_descendant_stops_on_timeout(tmp_path):
    from dark_factory.runner import CommandRunner, RunnerError

    marker = tmp_path / "should-not-exist"
    script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(60)"
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).write_text('survived')"
    with pytest.raises(RunnerError, match="timed out"):
        CommandRunner(tmp_path)._run(
            [sys.executable, "-c", script, child],
            b"",
            0.3,
            tmp_path,
            process_environment(tmp_path / "runtime"),
        )
    import time

    time.sleep(2.1)
    assert not marker.exists()


def test_restored_pending_work_runs_without_original_state(loop, tmp_path):
    from dataclasses import replace

    cfg, store, ctrl, wid = loop
    Worker(cfg, store, ctrl).run_loop(2)
    ctrl.approve(wid, "human")
    artifacts.backup(store, tmp_path / "backup")
    artifacts.restore(tmp_path / "backup", tmp_path / "restored")
    # The original state remains owned, but make its input snapshot unavailable to
    # prove the restored runner resolves its own durable copy.
    original_inputs = cfg.state_dir / "inputs"
    original_inputs.rename(cfg.state_dir / "inputs-away")
    restored_cfg = replace(cfg, state_dir=tmp_path / "restored")
    restored_store = Store(restored_cfg.state_dir)
    try:
        restored_ctrl = Controller(restored_cfg, restored_store, ctrl.packaged_prompts)
        assert Worker(restored_cfg, restored_store, restored_ctrl).run_loop() == 3
        assert (
            restored_store.one("SELECT status FROM work_items")[0] == "awaiting_review"
        )
    finally:
        restored_store.close()
        (cfg.state_dir / "inputs-away").rename(original_inputs)


def test_ambiguous_pr_write_reconciles_same_identity_without_retry(loop, monkeypatch):
    from dark_factory.runner import RunnerError

    cfg, store, ctrl, wid = loop
    build(loop)
    candidate = json.loads(store.one("SELECT candidate_commits FROM work_items")[0])[
        cfg.repositories[0]
    ]
    payload = {
        "repo": cfg.repositories[0],
        "head": "candidate",
        "base": "main",
        "head_sha": candidate["commit"],
        "title": "Candidate",
    }
    calls = []

    def transport(argv, operation, body, request_id, timeout):
        calls.append((operation, request_id))
        if operation == "create_pr":
            raise RunnerError("connection lost after external write")
        return {
            "ok": True,
            "operation": operation,
            "evidence": {
                "observed": True,
                "url": "https://example.test/pr/1",
                "head_sha": candidate["commit"],
            },
        }

    monkeypatch.setattr(ctrl.runner, "run_json", transport)
    with pytest.raises(RunnerError):
        ctrl.external_action(wid, "create_pr", payload, "human")
    assert store.one("SELECT status FROM pending_actions")[0] == "ambiguous"
    assert ctrl.dispatch_allowed()[1] == "external_action_needs_reconciliation"
    result = ctrl.external_action(wid, "create_pr", payload, "human")
    assert result["evidence"]["observed"]
    assert [op for op, identity in calls] == ["create_pr", "reconcile_create_pr"]
    assert len({identity for op, identity in calls}) == 1
    assert store.one("SELECT status FROM pending_actions")[0] == "succeeded"


def test_check_failure_prevents_independent_review(loop, monkeypatch):
    from dark_factory.adapters.harness.fake import FakeHarness

    cfg, store, ctrl, wid = loop
    worker = Worker(cfg, store, ctrl)
    worker.run_loop(2)
    ctrl.approve(wid, "human")
    # A successful coding process that makes no valid change must fail the actual check.
    monkeypatch.setattr(
        Worker,
        "_harness",
        lambda self, route: FakeHarness({"coder": "Everything is implemented"}),
    )
    worker.run_loop()
    assert store.one("SELECT status FROM work_items")[0] == "failed"
    assert (
        store.one(
            "SELECT COUNT(*) FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.kind='review'"
        )[0]
        == 0
    )
    assert store.one("SELECT status FROM jobs WHERE kind='check'")[0] == "failed"


def test_readonly_roles_cannot_mutate_candidate_on_macos(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("native macOS qualification")
    environment = NativeEnvironment(tmp_path / "workspaces")
    prepared = environment.prepare("review", None)
    (prepared.workspace / "source.txt").write_text("unchanged")
    runtime = tmp_path / "runtime"
    prefix = environment.prefix(
        prepared, runtime, network=False, writable=[], protected=[]
    )
    from dark_factory.runner import CommandRunner

    code, out, err = CommandRunner(prepared.workspace)._run(
        prefix + ["/bin/sh", "-c", "echo modified > source.txt"],
        b"",
        5,
        prepared.workspace,
        process_environment(runtime),
    )
    assert code != 0
    assert (prepared.workspace / "source.txt").read_text() == "unchanged"


def test_project_prompt_override_is_used_in_actual_attempt(tmp_path, monkeypatch):
    from dark_factory.adapters.harness.fake import FakeHarness

    cfg = cfg_for(tmp_path / "state")
    cfg.windows = []
    prompt = tmp_path / "planner.md"
    prompt.write_text("PROJECT OVERRIDE: plan the bounded message change.")
    cfg.prompts["planner"] = str(prompt)
    seen = []

    class ObservedHarness(FakeHarness):
        def start(self, text, workspace, extra=None):
            seen.append(text)
            yield from super().start(text, workspace, extra)

    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        wid = ctrl.intake_issues(
            [{"repo": cfg.repositories[0], "number": 1, "title": "Fixture"}]
        )[0]
        ctrl.start_planning(wid)
        monkeypatch.setattr(Worker, "_harness", lambda self, route: ObservedHarness())
        assert Worker(cfg, store, ctrl).run_once()
        assert seen[0].startswith(prompt.read_text())
        assert "You are the planner." not in seen[0]
        assert store.one("SELECT status FROM attempts")[0] == "succeeded"
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["omitted", "unverified", "invented_evidence"])
def test_overall_review_pass_cannot_hide_incomplete_requirement(
    loop, monkeypatch, failure
):
    from dark_factory.adapters.harness.fake import FakeHarness

    cfg, store, ctrl, wid = loop

    class IncompleteReviewer(FakeHarness):
        def start(self, prompt, workspace, extra=None):
            if extra["role"] == "reviewer":
                criteria = [
                    {
                        "id": c["id"],
                        "status": "satisfied",
                        "evidence": ["check:" + name for name in c["checks"]],
                    }
                    for c in extra["criteria"]
                ]
                if failure == "omitted":
                    criteria.pop()
                elif failure == "unverified":
                    criteria[-1]["status"] = "unverified"
                else:
                    criteria[-1]["evidence"] = ["check:invented-success"]
                self.scripted["reviewer"] = json.dumps(
                    {
                        "passed": True,
                        "summary": "All done",
                        "findings": [],
                        "criteria": criteria,
                    }
                )
            yield from super().start(prompt, workspace, extra)

    monkeypatch.setattr(Worker, "_harness", lambda self, route: IncompleteReviewer())
    worker = Worker(cfg, store, ctrl)
    worker.run_loop(2)
    ctrl.approve(wid, "human")
    worker.run_loop()
    assert store.one("SELECT status FROM work_items")[0] == "failed"
    assert store.one("SELECT status FROM jobs WHERE kind='check'")[0] == "succeeded"
    assert all(c["status"] == "incomplete" for c in ctrl.coverage(wid))
    with pytest.raises(ControllerError):
        ctrl.accept(wid, "human")


def test_successful_job_flags_cannot_substitute_for_criterion_evidence(loop):
    cfg, store, ctrl, wid = loop
    build(loop)
    sha = json.loads(store.one("SELECT candidate_commits FROM work_items")[0])[
        cfg.repositories[0]
    ]["commit"]
    ctrl.record_manual_gate(wid, "Inspect candidate diff", sha, "human", "Inspected")
    # Simulate damaged persisted evidence while every job still says succeeded.
    review = store.one(
        "SELECT a.id FROM attempts a JOIN jobs j ON a.job_id=j.id WHERE j.kind='review'"
    )[0]
    store.execute(
        "UPDATE evidence SET payload_json=? WHERE attempt_id=?",
        (
            json.dumps(
                {
                    "output": json.dumps(
                        {
                            "passed": True,
                            "summary": "done",
                            "findings": [],
                            "criteria": [],
                        }
                    )
                }
            ),
            review,
        ),
    )
    assert all(j["status"] == "succeeded" for j in ctrl._jobs(wid))
    with pytest.raises(ControllerError, match="acceptance criteria remain incomplete"):
        ctrl.accept(wid, "human")


def test_simulation_cannot_satisfy_operator_live_requirement(loop):
    cfg, store, ctrl, wid = loop
    snapshot = json.loads(store.one("SELECT snapshot_json FROM work_items")[0])
    snapshot["source"]["require_live_evidence"] = True
    store.execute("UPDATE work_items SET snapshot_json=?", (json.dumps(snapshot),))
    Worker(cfg, store, ctrl).run_loop(2)
    with pytest.raises(ControllerError, match="live evidence required"):
        ctrl.approve(wid, "human")


def test_plan_rejects_unverifiable_acceptance(loop):
    cfg, store, ctrl, wid = loop
    Worker(cfg, store, ctrl).run_loop(1)
    row = store.one("SELECT * FROM work_items")
    proposal = json.loads(row["plan_json"])["proposal"]
    proposal["acceptance"][-1]["checks"] = []
    with pytest.raises(ValueError, match="no verification gate"):
        ctrl._parse_role("plan", json.dumps(proposal), json.loads(row["snapshot_json"]))
