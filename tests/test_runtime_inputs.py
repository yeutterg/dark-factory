"""Model tools cannot replace approved harness inputs inside a writable runtime."""

import platform

import pytest

from dark_factory.adapters.env.native import NativeEnvironment, process_environment
from dark_factory.adapters.harness.registry import adapter_for
from dark_factory.runner import CommandRunner


@pytest.mark.skipif(
    platform.system() != "Darwin", reason="macOS native sandbox required"
)
def test_runtime_configuration_is_read_only_but_sessions_can_write(tmp_path):
    environment = NativeEnvironment(tmp_path / "workspaces")
    prepared = environment.prepare("fixture")
    runtime = tmp_path / "runtime"
    agent = runtime / "agent"
    agent.mkdir(parents=True)
    for name in ("models.json", "settings.json"):
        (agent / name).write_text("approved-input")
    prefix = environment.prefix(
        prepared,
        runtime,
        network=False,
        writable=["message.py"],
        protected=[],
        immutable_runtime_files=adapter_for("pi").immutable_runtime_files,
    )
    env = process_environment(runtime)
    runner = CommandRunner(prepared.workspace)
    for path in (
        agent / "models.json",
        agent / "settings.json",
        runtime / "sandbox.sb",
    ):
        code, _, _ = runner._run(
            prefix + ["/usr/bin/touch", str(path)], b"", 10, prepared.workspace, env
        )
        assert code != 0
    code, _, _ = runner._run(
        prefix + ["/usr/bin/touch", str(agent / "session-fixture")],
        b"",
        10,
        prepared.workspace,
        env,
    )
    assert code == 0
    assert (agent / "models.json").read_text() == "approved-input"


@pytest.mark.skipif(
    platform.system() != "Darwin", reason="macOS native sandbox required"
)
@pytest.mark.parametrize("name", ["../escape", "/absolute/escape", "."])
def test_runtime_input_guards_refuse_escaping_paths(tmp_path, name):
    environment = NativeEnvironment(tmp_path / "workspaces")
    prepared = environment.prepare("fixture")
    with pytest.raises(ValueError, match="runtime input"):
        environment.prefix(
            prepared,
            tmp_path / "runtime",
            network=False,
            writable=[],
            protected=[],
            immutable_runtime_files=(name,),
        )
