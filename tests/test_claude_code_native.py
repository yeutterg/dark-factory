"""Installed Claude CLI against a local, fake OAuth provider; no paid inference."""

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_claude_code import MODEL, extra_for

from dark_factory.adapters.env.native import NativeEnvironment
from dark_factory.adapters.harness import claude_code


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("claude"),
    reason="macOS and Claude CLI required",
)
def test_installed_claude_subscription_stream_and_read_isolation(tmp_path, monkeypatch):
    private = tmp_path / "private.txt"
    private.write_text("PRIVATE_FIXTURE_SECRET")
    env = NativeEnvironment(tmp_path / "workspaces")
    prepared = env.prepare("claude-test")
    (prepared.workspace / "README.md").write_text("PUBLIC_FIXTURE_SOURCE")
    (prepared.workspace / "escape.txt").symlink_to(private)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers.get("Authorization") == "Bearer sk-ant-oat01-fixture"
            assert self.headers.get("X-Api-Key") is None
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            text = json.dumps(payload.get("messages", []))
            has_results = "tool_result" in text
            content = (
                [{"type": "text", "text": "Complete root plan"}]
                if has_results
                else [
                    {
                        "type": "tool_use",
                        "id": "read_inside",
                        "name": "Read",
                        "input": {"file_path": str(prepared.workspace / "README.md")},
                    },
                    {
                        "type": "tool_use",
                        "id": "read_escape",
                        "name": "Read",
                        "input": {"file_path": str(prepared.workspace / "escape.txt")},
                    },
                ]
            )
            message = {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": content,
                "stop_reason": "end_turn" if has_results else "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 40},
            }
            if not payload.get("stream"):
                body = json.dumps(message).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {**message, "content": [], "stop_reason": None},
                    },
                )
            ]
            for i, block in enumerate(content):
                initial = (
                    {**block, "input": {}}
                    if block["type"] == "tool_use"
                    else {**block, "text": ""}
                )
                delta = (
                    {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    }
                    if block["type"] == "tool_use"
                    else {"type": "text_delta", "text": block["text"]}
                )
                events.extend(
                    [
                        (
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": i,
                                "content_block": initial,
                            },
                        ),
                        (
                            "content_block_delta",
                            {"type": "content_block_delta", "index": i, "delta": delta},
                        ),
                        (
                            "content_block_stop",
                            {"type": "content_block_stop", "index": i},
                        ),
                    ]
                )
            events.extend(
                [
                    (
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": message["stop_reason"],
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": 40},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ]
            )
            body = "".join(
                "event: " + event + "\ndata: " + json.dumps(data) + "\n\n"
                for event, data in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = claude_code.subscription_environment

    def fixture_environment(base, runtime, token):
        result = original(base, runtime, token)
        # Test-only endpoint injection. The production adapter never accepts this.
        result["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
        return result

    monkeypatch.setattr(claude_code, "subscription_environment", fixture_environment)
    opts = extra_for(tmp_path)
    opts["timeout_seconds"] = 45
    h = claude_code.ClaudeCodeHarness()
    opts["prefix"] = env.prefix(
        prepared,
        Path(opts["runtime"]),
        network=True,
        writable=[],
        protected=[],
        readable_files=[Path(h.binary)],
    )
    try:
        list(
            h.start(
                "Read README.md and escape.txt; produce a root plan.",
                prepared.workspace,
                opts,
            )
        )
        result = h.wait()
        assert result.ok, result.error
        assert result.output == "Complete root plan"
        assert len(requests) >= 2
        assert "PUBLIC_FIXTURE_SOURCE" in json.dumps(requests[-1])
        assert "PRIVATE_FIXTURE_SECRET" not in json.dumps(requests)
        assert private.read_text() == "PRIVATE_FIXTURE_SECRET"
        assert (prepared.workspace / "README.md").read_text() == "PUBLIC_FIXTURE_SOURCE"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
