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
