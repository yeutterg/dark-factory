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


@pytest.mark.parametrize("payload", [
    "ghp_" + "a" * 36,
    "/" + "Users" + "/test-person/private-project/",
    "eyJ" + "a" * 24 + "." + "b" * 24 + "." + "c" * 24,
])
def test_private_content_is_not_committed_or_echoed(repo, payload):
    source, remote = repo
    hook.checkpoint(source, {str(remote)})
    before = git(source, "rev-parse", "HEAD")
    (source / "README.md").write_text(payload)
    with pytest.raises(RuntimeError) as error:
        hook.checkpoint(source, {str(remote)})
    assert payload not in str(error.value)
    assert git(source, "rev-parse", "HEAD") == before
    assert git(remote, "rev-parse", "main") == before


def test_clean_checkout_does_not_push_private_intermediate_commit(repo):
    source, remote = repo
    hook.checkpoint(source, {str(remote)})
    before = git(remote, "rev-parse", "main")
    (source / "README.md").write_text("ghp_" + "b" * 36)
    git(source, "add", "README.md")
    git(source, "commit", "-m", "Unsafe local commit")
    (source / "README.md").write_text("Clean tip does not clean history\n")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "Remove from tip")
    assert not git(source, "status", "--porcelain")
    with pytest.raises(RuntimeError, match="credential"):
        hook.checkpoint(source, {str(remote)})
    assert git(remote, "rev-parse", "main") == before


@pytest.mark.parametrize("name", ["docs/auth.json", "examples/credentials/provider.json", "docs/factory.local.toml", ".codex/hooks.json", ".claude/settings.json", "docs/.codex/settings.json", "docs/.claude/settings.json"])
def test_nested_private_files_are_not_automatically_staged(repo, name):
    source, remote = repo
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("private fixture")
    hook.checkpoint(source, {str(remote)})
    assert name not in git(source, "ls-files")
    git(source, "add", name)
    with pytest.raises(RuntimeError, match="outside development scope"):
        hook.checkpoint(source, {str(remote)})


def test_web_source_is_published_without_dependency_artifacts(repo):
    source, remote = repo
    (source / "web" / "src").mkdir(parents=True)
    (source / "web" / "src" / "App.tsx").write_text("export const title = 'Factory';\n")
    (source / "web" / "node_modules").mkdir()
    (source / "web" / "node_modules" / "installed.js").write_text("generated")
    hook.checkpoint(source, {str(remote)})
    tracked = git(source, "ls-files")
    assert "web/src/App.tsx" in tracked
    assert "node_modules" not in tracked


def test_untracking_old_agent_settings_keeps_local_copy(repo):
    source, remote = repo
    path = source / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text("{}\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Previously tracked settings")
    git(source, "push", "-u", "origin", "main")
    git(source, "rm", "--cached", ".codex/hooks.json")
    hook.checkpoint(source, {str(remote)})
    assert path.read_text() == "{}\n"
    assert ".codex/hooks.json" not in git(source, "ls-files")
    assert git(source, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")
