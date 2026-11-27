"""Preflight is read-only and installation failures must not consume job allowance."""

import json
from pathlib import Path
from types import SimpleNamespace

from test_loop import cfg_for

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
    cfg.operator_token = "too-short"
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
    assert "too-short" not in json.dumps(result)


def test_preflight_missing_subscription_credential_is_a_blocker(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    route = cfg.routes["fake-coder"]
    route.harness = "claude-code"
    route.auth_env = "FACTORY_TEST_SUBSCRIPTION"
    monkeypatch.delenv(route.auth_env, raising=False)

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


def test_preflight_flags_a_broad_classic_github_token(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "a" * 36)
    result = inspect_configuration(cfg)
    check = next(c for c in result["checks"] if c["name"] == "github-token-scope")
    assert not check["passed"]
    monkeypatch.setenv("GH_TOKEN", "github_pat_" + "a" * 40)
    result = inspect_configuration(cfg)
    assert next(c for c in result["checks"] if c["name"] == "github-token-scope")[
        "passed"
    ]
    assert "ghp_" not in json.dumps(result)
