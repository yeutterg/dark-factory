from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dark_factory.contracts import ContractError, make_request, validate_result
from dark_factory.runner import CommandRunner, RunnerError


def test_invalid_json(tmp_path: Path) -> None:
    script = tmp_path / "bad.py"
    script.write_text("print('not json')\n", encoding="utf-8")
    runner = CommandRunner(tmp_path)
    with pytest.raises(ContractError):
        runner.run_json([sys.executable, str(script)], "op", {}, "r1", 5)


def test_timeout(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    runner = CommandRunner(tmp_path)
    with pytest.raises(RunnerError, match="timed out"):
        runner.run_json([sys.executable, str(script)], "op", {}, "r1", 1)


def test_success_requires_evidence(tmp_path: Path) -> None:
    script = tmp_path / "empty.py"
    script.write_text(
        "import json,sys\nprint(json.dumps({'ok': True, 'operation': 'op', 'evidence': {}}))\n",
        encoding="utf-8",
    )
    runner = CommandRunner(tmp_path)
    with pytest.raises(ContractError, match="evidence"):
        runner.run_json([sys.executable, str(script)], "op", {}, "r1", 5)


def test_github_tracking_roundtrip(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path)
    result = runner.run_json(
        [sys.executable, "-m", "dark_factory.adapters.projects.github"],
        "list_selected_issues",
        {"issues": [{"repo": "yeutterg/dark-factory", "number": 1, "title": "t", "is_root": True}]},
        "r1",
        10,
    )
    assert result["ok"]
    assert result["items"][0]["external_id"].startswith("github:")
