"""Run the installed Pi against a deterministic local HTTP fixture; no inference."""

from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dark_factory.adapters.git.local import fixture
from dark_factory.config import parse_toml_bytes
from dark_factory.controller import Controller
from dark_factory.store import Store
from dark_factory.worker import Worker


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("pi"),
    reason="installed Pi and macOS required",
)
def test_installed_pi_tool_use_and_fresh_review(tmp_path, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            messages = payload["messages"]
            user = "\n".join(
                str(m.get("content", "")) for m in messages if m["role"] == "user"
            )
            content = None
            tool_calls = None
            if "You are the planner." in user:
                content = json.dumps(
                    {
                        "why": "Verify isolated source change",
                        "what": "Make message return ready",
                        "assumptions": [],
                        "needs_input": [],
                        "acceptance": [
                            {
                                "id": "AC-01",
                                "description": "message returns ready",
                                "checks": ["verify"],
                                "manual_gates": [],
                            },
                            {
                                "id": "AC-02",
                                "description": "canonical verification passes",
                                "checks": ["verify"],
                                "manual_gates": [],
                            },
                        ],
                        "tasks": [
                            {"description": "Change message.py", "checks": ["verify"]}
                        ],
                        "test_instructions": ["Run verify.py and inspect the diff"],
                    }
                )
            elif "You are an independent critic" in user:
                content = json.dumps(
                    {
                        "passed": True,
                        "summary": "Plan matches selected source and checks",
                        "findings": [],
                    }
                )
            elif "You are an independent reviewer" in user:
                assert "exact_diff" in user and "check_evidence" in user
                assert "Updated message.py" not in user
                content = json.dumps(
                    {
                        "passed": True,
                        "summary": "Exact candidate and canonical check match requirements",
                        "criteria": [
                            {
                                "id": cid,
                                "status": "satisfied",
                                "evidence": ["check:verify"],
                            }
                            for cid in ("AC-01", "AC-02")
                        ],
                        "findings": [],
                    }
                )
            elif any(m["role"] == "tool" for m in messages):
                content = "Implemented the approved message change."
            else:
                tool_calls = [
                    {
                        "id": "write_1",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps(
                                {
                                    "path": "message.py",
                                    "content": 'def message():\n    return "ready"\n',
                                }
                            ),
                        },
                    }
                ]
            answer = {
                "id": "completion",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": content,
                            **({"tool_calls": tool_calls} if tool_calls else {}),
                        },
                        "finish_reason": "tool_calls" if tool_calls else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            }
            if payload.get("stream"):
                delta = {"role": "assistant"}
                if content:
                    delta["content"] = content
                if tool_calls:
                    delta["tool_calls"] = [{"index": 0, **tool_calls[0]}]
                chunks = [
                    {
                        "id": "completion",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    },
                    {
                        "id": "completion",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": answer["choices"][0]["finish_reason"],
                            }
                        ],
                        "usage": answer["usage"],
                    },
                ]
                data = (
                    "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = fixture(tmp_path / "seed")
    import subprocess

    version = subprocess.check_output(["pi", "--version"], text=True).strip()
    text = (
        Path("examples/factory.toml")
        .read_text()
        .replace('state_dir = "./state"', f'state_dir = "{tmp_path / "state"}"')
    )
    text = (
        text.replace('harness = "fake"', 'harness = "pi"\nevidence_kind = "simulation"')
        .replace('model = "fake-local"', 'model = "fixture-model"')
        .replace('provider = "none"', 'provider = "fixture-provider"')
    )
    text = text.replace(
        'harness_version = "0.1"', f'harness_version = "{version}"'
    ).replace(
        'endpoint = "local"', f'endpoint = "http://127.0.0.1:{server.server_port}/v1"'
    )
    text = text.replace(
        'role = "coder"',
        'role = "coder"\nauth_env = "FACTORY_TEST_KEY"\ngeography_evidence = "Deterministic loopback fixture; no inference"\napi = "openai-completions"',
    )
    text += '\n[execution]\nprofile = "macos-sandbox"\n'
    text += f'\n[sources."yeutterg/dark-factory"]\npath = "{source["path"]}"\nrevision = "HEAD"\nchecks = ["verify"]\nallowed_paths = ["message.py"]\nprotected_paths = ["verify.py", "AGENTS.md"]\nmanual_acceptance = []\n'
    python = shutil.which("python3.12") or "/usr/bin/python3"
    text += (
        f'\n[commands.verify]\nargv = ["{python}", "verify.py"]\ntimeout_seconds = 30\n'
    )
    monkeypatch.setenv("FACTORY_TEST_KEY", "fixture-not-a-secret")
    cfg = parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())
    cfg.windows = []
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        wid = ctrl.intake_issues(
            [
                {
                    "repo": cfg.repositories[0],
                    "number": 1,
                    "title": "message ready",
                    "body": "message() should return ready",
                }
            ]
        )[0]
        ctrl.start_planning(wid)
        worker = Worker(cfg, store, ctrl)
        worker.run_loop(2)
        row = store.one("SELECT * FROM work_items")
        assert row["status"] == "awaiting_approval", row["packet_json"]
        ctrl.approve(wid, "qualification-test")
        worker.run_loop(3)
        row = store.one("SELECT * FROM work_items")
        assert row["status"] == "awaiting_review", row["packet_json"]
        assert json.loads(row["packet_json"])["simulation"] is True
        ctrl.accept(wid, "qualification-test")
        assert len(requests) == 5
        assert (
            len(
                {
                    json.loads(a["result_json"])["session_id"]
                    for a in store.query(
                        "SELECT * FROM attempts WHERE route_id IS NOT NULL"
                    )
                }
            )
            == 4
        )
    finally:
        store.close()
        server.shutdown()
        server.server_close()
