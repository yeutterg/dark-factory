"""Authenticated worker claim API (stdlib HTTP)."""

from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from dark_factory.controller import Controller, ControllerError


def make_handler(controller: Controller, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _auth(self, expected: str = token) -> bool:
            if not expected:
                return False
            header = self.headers.get("Authorization", "")
            return hmac.compare_digest(header, f"Bearer {expected}")

        def _read(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > controller.cfg.max_output_bytes:
                raise ValueError("invalid request size")
            if not length:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("request must be a JSON object")
            return data

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
                self._send(200, {"ok": True})
                return
            self._send(404, {"ok": False})

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._post()
            except ControllerError as exc:
                self._send(409, {"ok": False, "error": str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})

        def _post(self) -> None:
            path = urlparse(self.path).path
            if not self._auth(
                controller.cfg.operator_token if path == "/operator" else token
            ):
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            path = urlparse(self.path).path
            data = self._read()
            if path == "/operator":
                command = data.get("cmd")
                actor = data.get("actor") or "operator"
                wid = data.get("work_item_id")
                result = None
                if command == "status":
                    result = controller.status()
                elif command == "approve":
                    result = {"digest": controller.approve(wid, actor)}
                elif command == "reject":
                    controller.reject(wid, actor, data.get("reason", "rejected"))
                elif command == "plan":
                    result = {
                        "jobs": [
                            j.job_id for j in controller.start_planning(wid, actor).jobs
                        ]
                    }
                elif command == "intake":
                    result = {"ids": controller.intake_issues(data["issues"], actor)}
                elif command == "proposal":
                    result = controller.proposal(wid, data.get("version"))
                elif command == "review":
                    row = controller.store.one(
                        "SELECT packet_json FROM work_items WHERE id=?", (wid,)
                    )
                    if not row:
                        raise ControllerError("work item not found")
                    result = json.loads(row["packet_json"] or "{}")
                elif command == "pause":
                    controller.pause(actor)
                elif command == "resume":
                    controller.resume(actor)
                elif command == "drain":
                    controller.drain(actor)
                elif command == "undrain":
                    controller.undrain(actor)
                elif command == "cancel":
                    controller.cancel_job(data["job_id"], actor)
                elif command == "cleanup":
                    result = {"cleaned": controller.cleanup(actor)}
                elif command == "recover":
                    result = {"recovered": controller.recover(actor)}
                elif command == "reload":
                    result = {"reloaded": controller.reload(actor)}
                elif command == "manual-gate":
                    controller.record_manual_gate(
                        wid, data["name"], data["candidate"], actor, data["evidence"]
                    )
                elif command == "accept":
                    controller.accept(wid, actor)
                else:
                    raise ControllerError("unsupported operator command")
                self._send(200, {"ok": True, "result": result})
                return
            if path == "/claim":
                claimed = controller.claim_job(
                    data.get("worker_id") or "worker", data.get("capabilities") or []
                )
                self._send(200, {"ok": True, "claimed": claimed})
                return
            if path == "/heartbeat":
                controller.heartbeat(data.get("worker_id") or "worker")
                self._send(200, {"ok": True})
                return
            if path == "/finish":
                attempt = controller.store.one(
                    "SELECT worker_id FROM attempts WHERE id=?",
                    (data.get("attempt_id"),),
                )
                if not attempt or attempt["worker_id"] != data.get("worker_id"):
                    raise ControllerError("attempt belongs to another worker")
                for flag in ("ok", "timed_out", "cancelled"):
                    if flag in data and type(data[flag]) is not bool:
                        raise ValueError(f"{flag} must be a boolean")
                if type(data.get("cost_cents", 0)) is not int:
                    raise ValueError("cost_cents must be an integer")
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


def serve(
    controller: Controller, host: str, port: int, token: str
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ControllerError(
            "bootstrap API is local-only; remote workers require later qualification"
        )
    server = ThreadingHTTPServer((host, port), make_handler(controller, token))
    return server
