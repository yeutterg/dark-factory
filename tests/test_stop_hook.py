"""The development hook only publishes the intended repository and retains failed pushes."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "stop_hook", Path(__file__).parents[1] / "scripts/stop_commit_push.py"
)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def git(repo, *args):
    return (
        subprocess.check_output(
            ["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL
        )
        .decode()
        .strip()
    )


@pytest.fixture
def repo(tmp_path):
    source, remote = tmp_path / "source", tmp_path / "remote.git"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Hook test")
    git(source, "config", "user.email", "hook@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    git(source, "config", "core.hooksPath", "/dev/null")
    git(source, "init", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    (source / "README.md").write_text("Hello\n")
    return source, remote


def test_checkpoint_new_files_and_clean_retry(repo):
    source, remote = repo
    (source / "factory.toml").write_text("local credentials stay local")
    (source / "src").mkdir()
    (source / "src" / "hello.py").write_text("print('hello')\n")
    hook.checkpoint(source, {str(remote)})
    sha = git(source, "rev-parse", "HEAD")
    assert git(remote, "rev-parse", "main") == sha
    assert "factory.toml" not in git(source, "ls-files")
    assert "src/hello.py" in git(source, "ls-files")
    hook.checkpoint(source, {str(remote)})
    assert git(source, "rev-parse", "HEAD") == sha


def test_wrong_repository_and_push_override_rejected(repo):
    source, remote = repo
    with pytest.raises(RuntimeError, match="origin"):
        hook.checkpoint(source)
    git(
        source,
        "remote",
        "set-url",
        "--push",
        "origin",
        "https://example.invalid/other.git",
    )
    with pytest.raises(RuntimeError, match="origin"):
        hook.checkpoint(source, {str(remote)})
    assert not git(source, "ls-files")


def test_failed_push_keeps_commit_and_retries_without_changes(repo):
    source, remote = repo
    hook.checkpoint(source, {str(remote)})
    before = git(source, "rev-parse", "HEAD")
    reject = remote / "hooks" / "pre-receive"
    reject.write_text("#!/bin/sh\nexit 1\n")
    reject.chmod(0o755)
    (source / "README.md").write_text("Updated\n")
    with pytest.raises(RuntimeError, match="push failed"):
        hook.checkpoint(source, {str(remote)})
    after = git(source, "rev-parse", "HEAD")
    assert after != before
    assert git(remote, "rev-parse", "main") == before
    reject.unlink()
    hook.checkpoint(source, {str(remote)})
    assert git(remote, "rev-parse", "main") == after


def test_staged_local_profile_is_not_published(repo):
    source, remote = repo
    (source / "factory.toml").write_text("local only")
    git(source, "add", "factory.toml")
    with pytest.raises(RuntimeError, match="outside development scope"):
        hook.checkpoint(source, {str(remote)})
