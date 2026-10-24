from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dark_factory.config import (
    ConfigError,
    load_config,
    load_operator_endpoint,
    parse_toml_bytes,
)


def test_load_example(tmp_path: Path) -> None:
    src = Path("examples/factory.toml").read_text(encoding="utf-8")
    src = src.replace('state_dir = "./state"', f'state_dir = "{tmp_path}"')
    cfg = parse_toml_bytes(src.encode(), Path("examples/factory.toml").resolve())
    assert cfg.project_name == "dark-factory"
    assert "fake-coder" in cfg.routes
    assert cfg.commands["tracking"].command[0] == str(
        Path(sys.executable).with_name("dark-factory").resolve()
    )


def test_unknown_region_blocked(tmp_path: Path) -> None:
    text = Path("examples/factory.toml").read_text(encoding="utf-8")
    text = text.replace('region = "US"', 'region = "unknown"')
    with pytest.raises(ConfigError, match="unknown processing geography"):
        parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())


def test_china_blocked() -> None:
    text = Path("examples/factory.toml").read_text(encoding="utf-8")
    text = text.replace('region = "US"', 'region = "CN"')
    with pytest.raises(ConfigError, match="blocked"):
        parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())


def test_missing_file() -> None:
    with pytest.raises(ConfigError):
        load_config("/tmp/does-not-exist-factory.toml")


def test_operator_endpoint_reads_only_runtime_and_normalizes_bind_all(tmp_path):
    profile = tmp_path / "operator.toml"
    profile.write_text('[runtime]\napi_host = "0.0.0.0"\napi_port = 9123\n')
    assert load_operator_endpoint(profile) == "http://127.0.0.1:9123"
    profile.write_text('[runtime]\napi_host = "::1"\napi_port = 9123\n')
    assert load_operator_endpoint(profile) == "http://[::1]:9123"


@pytest.mark.parametrize(
    "extra",
    [
        '[workflow]\nengine = "other"',
        "[workflow]\nengine = []",
        "[workflow]\nengine = 1",
        '[workflow]\nengine = "native"\nfallback = true',
        "[continuation]\nmax_repairs = 2",
        "[continuation]\nmax_repairs = true",
        '[operators]\nhumans = ["greg", "greg"]',
    ],
)
def test_runtime_policy_rejects_implicit_expansion(extra):
    text = Path("examples/factory.toml").read_text() + "\n" + extra
    with pytest.raises(ConfigError):
        parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())


def test_durable_example_is_simulation_only():
    cfg = load_config("examples/factory-durable.toml")
    assert cfg.raw["workflow"]["engine"] == "pi-durable"
    assert cfg.raw["continuation"]["max_repairs"] == 1
    assert all(r.harness == "fake" for r in cfg.routes.values())


def test_compose_live_example_has_only_live_routes(monkeypatch) -> None:
    path = Path("examples/compose/factory-live.toml").resolve()
    text = path.read_text(encoding="utf-8")
    text = text.replace("REPLACE_WITH_SOURCE_REVISION", "a" * 40)
    text = text.replace("REPLACE_WITH_DRIVER_SHA256", "b" * 64)
    text = text.replace('path = "/source"', f'path = "{Path.cwd()}"')
    monkeypatch.setenv("COMPOSE_LEGACY_WORKER_TOKEN", "legacy-test-token")
    monkeypatch.setenv("COMPOSE_OPERATOR_TOKEN", "operator-test-token")

    cfg = parse_toml_bytes(text.encode(), path)

    assert set(cfg.role_routes) == {"planner", "critic", "coder", "reviewer"}
    assert set(cfg.role_routes.values()) == set(cfg.routes)
    assert all(route.harness == "codex" for route in cfg.routes.values())
    assert all(route.evidence_kind == "live" for route in cfg.routes.values())
