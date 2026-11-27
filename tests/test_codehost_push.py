"""push_branch publishes one exact commit to a new dark-factory/ branch, nothing else."""

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
    result = github.handle(request())
    assert result["evidence"]["observed"] is True


def test_a_new_branch_is_pushed_by_exact_sha_and_read_back(monkeypatch, tmp_path):
    state = {"pushed": False}
    pushed = []

    def api(path, *a, **k):
        if not state["pushed"]:
            raise ContractError("not found")
        return {"object": {"sha": SHA}}

    def supervise(self, argv, stdin, timeout, cwd, env, **kw):
        pushed.append(argv)
        state["pushed"] = True
        return 0, b"", b""

    monkeypatch.setattr(github, "api", api)
    monkeypatch.setattr(github.CommandRunner, "supervise", supervise)
    result = github.handle(request(source_path=str(tmp_path)))
    assert result["evidence"] == {
        "observed": True,
        "branch": "dark-factory/improvement/x-aaaaaaaa",
        "head_sha": SHA,
    }
    argv = pushed[0]
    assert f"{SHA}:refs/heads/dark-factory/improvement/x-aaaaaaaa" in argv
    assert "--force" not in argv and "-f" not in argv


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
    result = github.handle(body)
    assert result["evidence"] == {
        "observed": False,
        "safe_to_retry": True,
        "head_sha": SHA,
    }
