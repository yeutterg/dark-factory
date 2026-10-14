"""Preflight is read-only and installation failures must not consume job allowance."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_loop import cfg_for
from test_workflow import setup

from dark_factory.adapters.workflow import pi_durable
from dark_factory.cli import main
from dark_factory.preflight import inspect_configuration


def test_preflight_does_not_open_or_create_state(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path / "absent-state")
    monkeypatch.setenv("FACTORY_TEST_SECRET", "do-not-print-this-token")
    cfg.routes["fake-coder"].auth_env = "FACTORY_TEST_SECRET"
    result = inspect_configuration(cfg)
    assert result["preflight_passed"]
    assert not result["live_routes_configured"]
    assert not cfg.state_dir.exists()
    assert "do-not-print-this-token" not in json.dumps(result)
    assert result["credential_references"][0]["environment_present"]


def test_cli_preflight_bypasses_controller_even_with_api_url(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "factory.toml"
    text = (
        Path("examples/factory.toml")
        .read_text()
        .replace('state_dir = "./state"', 'state_dir = "./absent-state"')
    )
    path.write_text(text)

    def forbidden(*args):
        raise AssertionError("must not build controller")

    monkeypatch.setattr("dark_factory.cli.build_controller", forbidden)
    assert (
        main(["--config", str(path), "--api-url", "http://127.0.0.1:1", "preflight"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["preflight_passed"]
    assert not (tmp_path / "absent-state").exists()


def test_preflight_reports_unavailable_command_without_running_it(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.commands["tracking"].command = ["/definitely-not-an-installed-binary"]
    result = inspect_configuration(cfg)
    assert not result["preflight_passed"]
    assert any(
        c["name"] == "command:tracking" and not c["passed"] for c in result["checks"]
    )


def test_live_preflight_requires_human_and_secure_operator_channel(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.routes["fake-coder"].evidence_kind = "live"
    # Mutated fixture config tests diagnostics only; live fake routes remain
    # prohibited by normal configuration parsing and controller evidence gates.
    result = inspect_configuration(cfg)
    failed = {c["name"] for c in result["checks"] if not c["passed"]}
    assert {
        "operator-channel",
        "human-identities",
        "live-evidence-policy",
        "native-isolation",
    } <= failed
    assert cfg.operator_token not in json.dumps(result)


def test_preflight_missing_subscription_credential_is_a_blocker(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    route = cfg.routes["fake-coder"]
    route.harness = "claude-code"
    route.auth_env = "FACTORY_TEST_SUBSCRIPTION"
    monkeypatch.delenv(route.auth_env, raising=False)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "dark_factory.preflight.adapter_for",
        lambda name: SimpleNamespace(
            name=name, roles={"planner", "critic", "reviewer"}, open=lambda *args: None
        ),
    )
    result = inspect_configuration(cfg)
    assert not result["preflight_passed"]
    assert any(
        c["name"] == "credential-source:fake-coder" and not c["passed"]
        for c in result["checks"]
    )


def test_missing_durable_install_leaves_job_unleased_and_unbilled(
    tmp_path, monkeypatch
):
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    # Installation validation runs before any claim, even for queued native work.
    cfg.raw["workflow"] = {"engine": "pi-durable"}
    monkeypatch.setattr(pi_durable, "ENGINE_DIR", tmp_path / "missing-engine")
    try:
        with pytest.raises(RuntimeError, match="npm ci"):
            worker.run_once()
        assert not store.query("SELECT * FROM attempts")
        assert not store.query("SELECT * FROM costs")
        root = store.one(
            "SELECT spent_cents,reserved_cents,status FROM work_items WHERE id=?",
            (wid,),
        )
        assert root["spent_cents"] == root["reserved_cents"] == 0
        assert root["status"] == "planning"
        assert all(
            j["status"] not in {"leased", "running", "failed"}
            for j in store.query("SELECT status FROM jobs")
        )
        assert not (tmp_path / "workflow").exists()
    finally:
        store.close()


@pytest.mark.parametrize(
    "version", ["v22.18.0", "v20.19.0", "unexpected", "v22.19.0-extra"]
)
def test_durable_rejects_unsupported_node_before_loading_engine(
    tmp_path, monkeypatch, version
):
    engine = tmp_path / "engine"
    for name in ("pi-durable", "pi-ai", "chord"):
        path = engine / "node_modules" / "@earendil-works" / name
        path.mkdir(parents=True)
        (path / "package.json").write_text('{"version":"1.0.3"}')
    monkeypatch.setattr(pi_durable, "ENGINE_DIR", engine)
    calls = []

    def run(self, argv, stdin, timeout, cwd, env):
        calls.append(argv)
        return 0, version.encode(), b""

    monkeypatch.setattr(pi_durable.CommandRunner, "supervise", run)
    with pytest.raises(RuntimeError, match="Node >=22.19.0"):
        pi_durable.PiDurableRuntime(
            SimpleNamespace(raw={}, node_binary="node"), None
        ).validate()
    assert len(calls) == 1


def test_durable_load_probe_strips_secrets_and_node_hooks(tmp_path, monkeypatch):
    engine = tmp_path / "engine"
    for name in ("pi-durable", "pi-ai", "chord"):
        path = engine / "node_modules" / "@earendil-works" / name
        path.mkdir(parents=True)
        (path / "package.json").write_text('{"version":"1.0.3"}')
    monkeypatch.setattr(pi_durable, "ENGINE_DIR", engine)
    monkeypatch.setenv("NODE_OPTIONS", "--require=untrusted-code")
    monkeypatch.setenv("OPENAI_API_KEY", "never-inherit")

    def run(self, argv, stdin, timeout, cwd, env):
        assert "NODE_OPTIONS" not in env and "OPENAI_API_KEY" not in env
        if "--version" not in argv:
            assert "--experimental-strip-types" in argv and "--preflight" in argv
        return (
            0,
            b"v22.19.0" if "--version" in argv else b'{"version":1,"type":"ready"}',
            b"",
        )

    monkeypatch.setattr(pi_durable.CommandRunner, "supervise", run)
    result = pi_durable.PiDurableRuntime(
        SimpleNamespace(raw={}, node_binary="node"), None
    ).validate()
    assert result["node_version"] == "22.19.0"
    assert not (tmp_path / "workflow").exists()
