"""Authenticated worker claim API (stdlib HTTP)."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

from dark_factory.controller import Controller


def make_handler(controller: Controller, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _auth(self) -> bool:
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {token}"

        def _read(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path == "/health":
                self._send(200, {"ok": True, "status": controller.status()})
                return
            self._send(404, {"ok": False})

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth():
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            path = urlparse(self.path).path
            data = self._read()
            if path == "/claim":
                claimed = controller.claim_job(data.get("worker_id") or "worker", data.get("capabilities") or [])
                self._send(200, {"ok": True, "claimed": claimed})
                return
            if path == "/heartbeat":
                controller.heartbeat(data.get("worker_id") or "worker")
                self._send(200, {"ok": True})
                return
            if path == "/finish":
                controller.finish_attempt(
                    data["attempt_id"],
                    ok=bool(data.get("ok")),
                    output=data.get("output") or "",
                    cost_cents=int(data.get("cost_cents") or 0),
                    archive_path=data.get("archive_path"),
                    error=data.get("error"),
                    timed_out=bool(data.get("timed_out")),
                    cancelled=bool(data.get("cancelled")),
                )
                self._send(200, {"ok": True})
                return
            self._send(404, {"ok": False})

    return Handler


def serve(controller: Controller, host: str, port: int, token: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(controller, token))
    return server
