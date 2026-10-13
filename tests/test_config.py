from __future__ import annotations

from pathlib import Path

import pytest

from dark_factory.config import ConfigError, load_config, parse_toml_bytes


def test_load_example(tmp_path: Path) -> None:
    src = Path("examples/factory.toml").read_text(encoding="utf-8")
    src = src.replace('state_dir = "./state"', f'state_dir = "{tmp_path}"')
    cfg = parse_toml_bytes(src.encode(), Path("examples/factory.toml").resolve())
    assert cfg.project_name == "dark-factory"
    assert "fake-coder" in cfg.routes


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


@pytest.mark.parametrize(
    "extra",
    [
        '[workflow]\nengine = "other"',
        '[workflow]\nengine = []',
        '[workflow]\nengine = 1',
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
