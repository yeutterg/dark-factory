from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dark_factory.contracts import ContractError
from dark_factory.runner import CommandRunner, RunnerError


def test_completed_command_does_not_fail_if_process_group_was_reused(
    tmp_path, monkeypatch
):
    real_killpg = os.killpg

    def reused_group(pid, signal):
        raise PermissionError("group no longer belongs to completed child")

    monkeypatch.setattr(os, "killpg", reused_group)
    try:
        code, output, _ = CommandRunner(tmp_path).supervise(
            [sys.executable, "-c", "print('done')"],
            b"",
            5,
            tmp_path,
            os.environ.copy(),
        )
    finally:
        monkeypatch.setattr(os, "killpg", real_killpg)
    assert code == 0
    assert output.strip() == b"done"


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
        "import json,sys\nprint(json.dumps({'contract_version': 1, 'request_id': 'r1', 'ok': True, 'operation': 'op', 'evidence': {}}))\n",
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
        {
            "issues": [
                {
                    "repo": "yeutterg/dark-factory",
                    "number": 1,
                    "title": "t",
                    "is_root": True,
                }
            ]
        },
        "r1",
        10,
    )
    assert result["ok"]
    assert result["items"][0]["external_id"].startswith("github:")


def test_result_version_and_request_identity_are_strict():
    from dark_factory.contracts import validate_result

    data = {
        "contract_version": True,
        "request_id": "r1",
        "operation": "op",
        "ok": True,
        "evidence": {"observed": True},
    }
    with pytest.raises(ContractError, match="version"):
        validate_result(data, "op", "r1")
    data["contract_version"] = 1
    with pytest.raises(ContractError, match="identity"):
        validate_result(data, "op", "different")
