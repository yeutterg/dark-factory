"""push_branch publishes one exact commit to a new dark-factory/ branch, nothing else."""

import subprocess
from pathlib import Path

import pytest

from dark_factory.adapters.git import github
from dark_factory.contracts import ContractError, make_request

SHA = "a" * 40


def request(**payload):
    base = {
        "repo": "owner/name",
        "branch": "dark-factory/improvement/x-aaaaaaaa",
        "head_sha": SHA,
    }
    return make_request("push_branch", {**base, **payload}, "req-1")


def test_unsafe_branch_names_are_refused():
    for branch in ("main", "feature/x", "dark-factory/../main", "dark-factory/a"):
        with pytest.raises(ContractError):
            github.handle(request(branch=branch))


def test_an_existing_branch_at_another_commit_is_never_overwritten(monkeypatch):
    monkeypatch.setattr(github, "api", lambda *a, **k: {"object": {"sha": "b" * 40}})
    with pytest.raises(ContractError, match="different commit"):
        github.handle(request())


def test_an_existing_branch_at_the_same_commit_is_already_done(monkeypatch):
    monkeypatch.setattr(github, "api", lambda *a, **k: {"object": {"sha": SHA}})
    assert github.handle(request())["evidence"]["observed"] is True


def test_a_lookup_failure_other_than_not_found_never_falls_through_to_a_push(
    monkeypatch, tmp_path
):
    def api(*a, **k):
        raise ContractError("GitHub request failed (1): API rate limit exceeded")

    monkeypatch.setattr(github, "api", api)
    monkeypatch.setattr(
        github,
        "_push_exact_commit",
        lambda *a: (_ for _ in ()).throw(AssertionError("pushed")),
    )
    with pytest.raises(ContractError, match="rate limit"):
        github.handle(request(source_path=str(tmp_path)))


def real_repo(tmp_path):
    repo = tmp_path / "candidate"
    repo.mkdir()

    def run(*a):
        return subprocess.check_output(["git", "-C", str(repo), *a], text=True).strip()  # noqa: E731

    run("init", "-b", "main")
    run("config", "user.email", "a@b.c")
    run("config", "user.name", "x")
    (repo / "f").write_text("1")
    run("add", ".")
    run("commit", "-m", "c")
    return repo, run("rev-parse", "HEAD"), run


def test_a_new_branch_is_pushed_from_a_fresh_repository_not_the_agents_checkout(
    monkeypatch, tmp_path
):
    repo, sha, run = real_repo(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    run("config", "core.sshCommand", f"touch {marker}")
    seen = {}

    def supervise(runner, argv, stdin, timeout, cwd, env, **kw):
        seen.update(argv=argv, cwd=Path(cwd))
        # The push happens from a repository that holds exactly the candidate commit.
        git_dir = argv[argv.index("--git-dir") + 1]
        seen["has_commit"] = (
            subprocess.run(
                ["git", "--git-dir", git_dir, "cat-file", "-e", sha]
            ).returncode
            == 0
        )
        seen["hooks"] = sorted(p.name for p in (Path(git_dir) / "hooks").glob("*"))
        return 0, b"", b""

    state = {"pushed": False}

    def api(path, *a, **k):
        if not state["pushed"]:
            state["pushed"] = True
            raise ContractError("not found")
        return {"object": {"sha": sha}}

    monkeypatch.setattr(github, "api", api)
    monkeypatch.setattr(github.CommandRunner, "supervise", supervise)
    result = github.handle(request(head_sha=sha, source_path=str(repo)))
    assert result["evidence"]["observed"]
    assert not marker.exists(), (
        "the agent's hook or config ran with controller credentials"
    )
    assert seen["has_commit"] and seen["hooks"] == []
    argv = seen["argv"]
    assert f"{sha}:refs/heads/dark-factory/improvement/x-aaaaaaaa" in argv
    assert "--force" not in argv and "-f" not in argv
    assert str(repo) not in argv and seen["cwd"] != repo


def test_a_checkout_not_at_the_exact_commit_is_refused(monkeypatch, tmp_path):
    repo, sha, run = real_repo(tmp_path)

    def api(*a, **k):
        raise ContractError("not found")

    monkeypatch.setattr(github, "api", api)
    with pytest.raises(ContractError, match="exact commit"):
        github.handle(request(head_sha="c" * 40, source_path=str(repo)))


def test_reconcile_never_pushes(monkeypatch):
    def api(*a, **k):
        raise ContractError("not found")

    monkeypatch.setattr(github, "api", api)
    monkeypatch.setattr(
        github.CommandRunner,
        "supervise",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("pushed")),
    )
    body = make_request(
        "reconcile_push_branch",
        {
            "repo": "owner/name",
            "branch": "dark-factory/improvement/x-aaaaaaaa",
            "head_sha": SHA,
        },
        "req-2",
    )
    assert github.handle(body)["evidence"] == {
        "observed": False,
        "safe_to_retry": True,
        "head_sha": SHA,
    }
