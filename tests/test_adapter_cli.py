from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

from dark_factory.contracts import make_request


def _entrypoint() -> str:
    command = Path(sys.executable).with_name("dark-factory")
    assert command.is_file(), (
        "the canonical test environment must install dark-factory beside Python"
    )
    return str(command)


def _installed_env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "PATH": os.pathsep.join([str(Path(_entrypoint()).parent), "/usr/bin", "/bin"]),
    }


def _run_adapter(
    tmp_path: Path, name: str, request: dict, env: dict[str, str] | None = None
) -> dict:
    result = subprocess.run(
        [_entrypoint(), "adapter", name],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env or _installed_env(tmp_path),
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_tracking_adapter_runs_from_installed_entrypoint_without_config(tmp_path):
    request = make_request(
        "list_selected_issues",
        {
            "issues": [
                {
                    "repo": "example/project",
                    "number": 17,
                    "title": "Fixture issue",
                    "body": "Offline fixture",
                    "is_root": True,
                    "required_child_count": 0,
                }
            ]
        },
        "tracking-offline",
    )
    result = _run_adapter(tmp_path, "github-tracking", request)
    assert result["ok"] is True
    assert result["items"][0]["external_id"] == "github:example/project:issue:17"


def test_code_host_adapter_runs_offline_with_stubbed_gh(tmp_path):
    binary = tmp_path / "gh"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "path = sys.argv[-1]\n"
        "if path.endswith('/pulls/7'):\n"
        " print(json.dumps({'html_url':'https://example.test/pr/7','head':{'sha':'a'*40},'base':{'sha':'b'*40,'ref':'main'},'state':'open','merged':False,'mergeable':True}))\n"
        "elif 'check-runs' in path:\n"
        " print(json.dumps({'total_count':0,'check_runs':[]}))\n"
        "elif path.endswith('/status?per_page=100'):\n"
        " print(json.dumps({'total_count':0,'statuses':[]}))\n"
        "else:\n"
        " print('[]')\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    env = _installed_env(tmp_path)
    env["PATH"] = os.pathsep.join([str(tmp_path), env["PATH"]])
    result = _run_adapter(
        tmp_path,
        "github-codehost",
        make_request(
            "inspect_pr", {"repo": "example/project", "number": 7}, "pr-offline"
        ),
        env,
    )
    assert result["ok"] is True
    assert result["evidence"]["head_sha"] == "a" * 40


def test_jev_adapter_uses_installed_entrypoint_and_forwards_options(tmp_path):
    result = subprocess.run(
        [
            _entrypoint(),
            "adapter",
            "jev-decision",
            "--api-key-env",
            "MISSING_TEST_KEY",
            "--model",
            "test-model",
        ],
        input=json.dumps(
            make_request(
                "decide",
                {
                    "questions": {
                        "next": {
                            "type": "choice",
                            "instructions": "Choose",
                            "options": {"resume": "Resume", "stop": "Stop"},
                        }
                    }
                },
                "decision-offline",
            )
        ),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=_installed_env(tmp_path),
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "MISSING_TEST_KEY is not set" in result.stderr


def test_code_host_adapter_rejects_unknown_options(tmp_path):
    result = subprocess.run(
        [_entrypoint(), "adapter", "github-codehost", "--typo"],
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=_installed_env(tmp_path),
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "does not accept adapter options" in result.stderr


def test_example_profiles_use_exact_installed_adapter_argv():
    expected = {
        "tracking": ["dark-factory", "adapter", "github-tracking"],
        "code_host": ["dark-factory", "adapter", "github-codehost"],
    }
    for path in sorted(Path("examples").glob("factory*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if "commands" not in data:
            continue
        for name, argv in expected.items():
            assert data["commands"][name]["argv"] == argv, path
        assert "python3 -m dark_factory.adapters" not in path.read_text()
        if "decider" in data.get("commands", {}):
            assert data["commands"]["decider"]["argv"][0:3] == [
                "dark-factory",
                "adapter",
                "jev-decision",
            ]
