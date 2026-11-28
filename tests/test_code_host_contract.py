"""Every code-host implementation must pass this suite; core trusts nothing else."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import unquote

import pytest

from dark_factory.adapters.git import github
from dark_factory.adapters.git.fake_host import FakeCodeHost
from dark_factory.contracts import CODE_HOST_OPERATIONS, ContractError, make_request

REPO = "owner/name"
SHA = "a" * 40
BRANCH = "dark-factory/work/abc-aaaaaaaa"


class GitHubWorld:
    """Just enough of GitHub's REST API behind the real adapter."""

    def __init__(self, monkeypatch, tmp_path):
        self.branches: dict[str, str] = {}
        self.prs: list[dict] = []
        self.runs: dict[int, dict] = {}
        self.source = str(tmp_path)
        monkeypatch.setattr(github, "api", self.api)
        monkeypatch.setattr(
            github,
            "_push_exact_commit",
            lambda repo, source, branch, sha: self.branches.__setitem__(branch, sha),
        )

    def push(self, argv):
        sha, _, ref = argv[-1].partition(":refs/heads/")
        self.branches[ref] = sha
        return 0, b"", b""

    def api(self, path, method="GET", payload=None):
        m = re.fullmatch(rf"repos/{REPO}/git/ref/heads/(.+)", path)
        if m:
            branch = unquote(m.group(1))
            if branch not in self.branches:
                raise ContractError("not found")
            return {"object": {"sha": self.branches[branch]}}
        m = re.fullmatch(rf"repos/{REPO}/commits/(.+)", path)
        if m:
            return {"sha": self.branches[unquote(m.group(1))]}
        if path.startswith(f"repos/{REPO}/pulls?state=open"):
            return [self._raw(p) for p in self.prs]
        if path.startswith(f"repos/{REPO}/pulls?state=all"):
            return [self._raw(p) for p in self.prs]
        if path == f"repos/{REPO}/pulls" and method == "POST":
            pr = {
                "number": len(self.prs) + 1,
                "head": payload["head"],
                "body": payload["body"],
                "title": payload["title"],
                "sha": self.branches[payload["head"]],
            }
            self.prs.append(pr)
            return self._raw(pr)
        m = re.fullmatch(rf"repos/{REPO}/pulls/(\d+)", path)
        if m:
            return self._raw(self.prs[int(m.group(1)) - 1])
        if path.startswith(f"repos/{REPO}/statuses/"):
            return {}
        if path.endswith("/dispatches"):
            inputs = payload["inputs"]
            self.runs[41] = {
                "name": f"dark-factory {inputs['request_id']} {inputs.get('commit', '')} release/package"
            }
            return {}
        if path.startswith(f"repos/{REPO}/actions/runs?"):
            return {"workflow_runs": [{"id": i, **r} for i, r in self.runs.items()]}
        m = re.fullmatch(rf"repos/{REPO}/actions/runs/(\d+)", path)
        if m:
            return {
                "status": "completed",
                "conclusion": "success",
                "html_url": "u",
                "head_sha": SHA,
            }
        if "/artifacts" in path:
            return {"artifacts": []}
        raise AssertionError(f"unstubbed GitHub call {method} {path}")

    @staticmethod
    def _raw(pr):
        return {
            "number": pr["number"],
            "html_url": f"https://x/pull/{pr['number']}",
            "title": pr["title"],
            "body": pr["body"],
            "head": {"sha": pr["sha"], "ref": pr["head"]},
        }

    def add_branch(self, branch, sha):
        self.branches[branch] = sha


class FakeWorld:
    def __init__(self):
        self.host = FakeCodeHost()

    def add_branch(self, branch, sha):
        self.host.branches[(REPO, branch)] = sha


@pytest.fixture(params=["fake", "github"])
def host(request, monkeypatch, tmp_path):
    if request.param == "fake":
        world = FakeWorld()
        return world.host.handle, world, tmp_path
    world = GitHubWorld(monkeypatch, tmp_path)
    return github.handle, world, tmp_path


def call(handle, op, **payload):
    payload = {"repo": REPO, **payload}
    result = handle(make_request(op, payload, "req-1"))
    assert (
        result["ok"] and result["operation"] == op and result["request_id"] == "req-1"
    )
    return result["evidence"]


def test_unknown_operations_are_refused(host):
    handle, _, _ = host
    with pytest.raises(ContractError):
        handle(make_request("delete_everything", {"repo": REPO}, "r"))


def test_a_branch_is_new_exact_and_never_overwritten(host):
    handle, _, tmp = host
    for bad in ("main", "feature/x", "dark-factory/../main", "dark-factory/a"):
        with pytest.raises(ContractError):
            call(handle, "push_branch", branch=bad, head_sha=SHA, source_path=str(tmp))
    with pytest.raises(ContractError):
        call(
            handle, "push_branch", branch=BRANCH, head_sha="short", source_path=str(tmp)
        )
    assert call(handle, "reconcile_push_branch", branch=BRANCH, head_sha=SHA) == {
        "observed": False,
        "safe_to_retry": True,
        "head_sha": SHA,
    }
    first = call(
        handle, "push_branch", branch=BRANCH, head_sha=SHA, source_path=str(tmp)
    )
    assert first["observed"] and first["head_sha"] == SHA
    again = call(
        handle, "push_branch", branch=BRANCH, head_sha=SHA, source_path=str(tmp)
    )
    assert again["observed"]  # the same commit already there is already done
    with pytest.raises(ContractError, match="different commit"):
        call(
            handle,
            "push_branch",
            branch=BRANCH,
            head_sha="b" * 40,
            source_path=str(tmp),
        )
    assert call(handle, "reconcile_push_branch", branch=BRANCH, head_sha=SHA)[
        "observed"
    ]


def test_a_pull_request_is_a_draft_for_the_exact_head_and_idempotent(host):
    handle, world, _ = host
    world.add_branch(BRANCH, SHA)
    body = {
        "head": BRANCH,
        "base": "main",
        "head_sha": SHA,
        "title": "Change",
        "body": "why",
    }
    with pytest.raises(ContractError):
        call(handle, "create_pr", **{**body, "head_sha": "b" * 40})  # the head moved
    with pytest.raises(ContractError):
        call(handle, "create_pr", **{**body, "title": " "})
    created = call(handle, "create_pr", **body)
    assert created["observed"] and created["number"] == 1 and created["draft"] is True
    again = call(handle, "create_pr", **body)
    assert again["number"] == 1 and again.get("reconciled") is True
    assert call(handle, "reconcile_create_pr", **body)["observed"]
    listing = call(handle, "list_open_prs")["prs"]
    assert [p["number"] for p in listing] == [1] and listing[0]["head_sha"] == SHA
    assert set(listing[0]) >= {"number", "head_sha", "head_ref", "title", "url", "body"}


def test_the_status_is_restricted_to_the_gate_namespace(host):
    handle, _, _ = host
    base = {"sha": SHA, "state": "success", "context": "dark-factory/gate"}
    assert call(handle, "set_status", **base)["observed"]
    for bad in ({"context": "ci/build"}, {"state": "ok"}, {"sha": "x"}):
        with pytest.raises(ContractError):
            call(handle, "set_status", **{**base, **bad})


def test_a_hosted_run_is_dispatched_found_by_request_and_read(host):
    handle, _, _ = host
    with pytest.raises(ContractError):
        call(
            handle,
            "dispatch_workflow",
            workflow="not-a-workflow",
            ref="main",
            inputs={"request_id": "r"},
        )
    with pytest.raises(ContractError):
        call(handle, "dispatch_workflow", workflow="s.yml", ref="main", inputs={"n": 1})
    assert call(handle, "find_run", request_id="r9", commit=SHA)["run_id"] is None
    with pytest.raises(ContractError):
        call(handle, "find_run", request_id="r9")  # a run is found for an exact commit
    call(
        handle,
        "dispatch_workflow",
        workflow="s.yml",
        ref="main",
        inputs={"request_id": "r9", "commit": SHA},
    )
    assert call(handle, "find_run", request_id="r9", commit="b" * 40)["run_id"] is None
    run_id = call(handle, "find_run", request_id="r9", commit=SHA)["run_id"]
    assert run_id is not None
    state = call(handle, "get_run", run_id=run_id)
    assert state["status"] == "completed" and state["conclusion"] == "success"
    assert call(handle, "list_run_artifacts", run_id=run_id)["artifacts"] == []


def test_artifact_download_streams_beyond_command_output_limit(tmp_path, monkeypatch):
    fixture = tmp_path / "artifact.zip"
    with zipfile.ZipFile(fixture, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("package.bin", os.urandom(3_000_000))
    binary = tmp_path / "gh"
    binary.write_text(
        '#!/bin/sh\nexec python3 -c "import os,sys; '
        "sys.stdout.buffer.write(open(os.environ['ARTIFACT_FIXTURE'],'rb').read())\"\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ARTIFACT_FIXTURE", str(fixture))
    destination = tmp_path / "download"
    evidence = github.download_artifact(
        REPO,
        {
            "artifact_id": 7,
            "expected_digest": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "destination_path": str(destination),
        },
    )
    assert evidence["files"][0]["size"] == 3_000_000
    assert (destination / "package.bin").stat().st_size == 3_000_000


def test_every_operation_core_uses_is_implemented(host):
    handle, _, _ = host
    for op in CODE_HOST_OPERATIONS:
        try:
            call(handle, op)
        except ContractError as error:
            assert "unsupported" not in str(error), op
        except (KeyError, TypeError, AssertionError):
            pass  # needs real input; what matters is it is not "unsupported"


def test_nothing_outside_the_two_github_adapters_calls_github():
    allowed = {"adapters/git/github.py", "adapters/projects/github.py"}
    root = Path(__file__).parents[1] / "src" / "dark_factory"
    pattern = re.compile(r'"gh"|api\.github\.com|gh auth|https://github\.com/')
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if str(p.relative_to(root)) not in allowed and pattern.search(p.read_text())
    ]
    assert offenders == []
    assert subprocess  # the suite imports it for the adapter stubs above


def test_the_documented_operations_match_the_contract():
    text = (Path(__file__).parents[1] / "docs" / "code-host-contract.md").read_text()
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", text, re.MULTILINE))
    assert documented == set(CODE_HOST_OPERATIONS)
