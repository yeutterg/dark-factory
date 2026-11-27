"""Authenticated worker claim API (stdlib HTTP)."""

from __future__ import annotations

import hmac
import json
import secrets
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from dark_factory import operator_commands
from dark_factory.controller import Controller, ControllerError

SESSION_LIMIT = 200
LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60
SOCKET_TIMEOUT_SECONDS = 30


def make_handler(controller: Controller) -> type[BaseHTTPRequestHandler]:
    sessions: dict[str, dict[str, Any]] = {}
    failures: dict[str, list[float]] = {}

    def purge_sessions() -> None:
        now = time.time()
        for key in [k for k, v in sessions.items() if v["expires"] <= now]:
            del sessions[key]
        while len(sessions) >= SESSION_LIMIT:
            del sessions[min(sessions, key=lambda k: sessions[k]["expires"])]

    def throttled(source: str) -> bool:
        now = time.time()
        recent = [t for t in failures.get(source, []) if now - t < LOGIN_WINDOW_SECONDS]
        if recent:
            failures[source] = recent
        else:
            failures.pop(source, None)
        return len(recent) >= LOGIN_FAILURES

    class Handler(BaseHTTPRequestHandler):
        # Bounds every header and body read, so a silent client cannot hold a thread.
        timeout = SOCKET_TIMEOUT_SECONDS

        def setup(self) -> None:
            # TLS handshakes happen here, in the connection's own thread and under
            # the socket timeout, never on the accepting thread.
            do_handshake = getattr(self.request, "do_handshake", None)
            if do_handshake:
                self.request.settimeout(SOCKET_TIMEOUT_SECONDS)
                do_handshake()
            super().setup()

        def _auth(self, expected: str) -> bool:
            if not expected:
                return False
            header = self.headers.get("Authorization", "")
            return hmac.compare_digest(header, f"Bearer {expected}")

        def _session(self) -> dict[str, Any] | None:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("dark_factory_session")
            session = sessions.get(morsel.value) if morsel else None
            if session and session["expires"] > time.time():
                return session
            return None

        def _operator_auth(self) -> bool:
            return (
                self._auth(controller.cfg.operator_token) or self._session() is not None
            )

        def _session_mutation_allowed(self, session: dict[str, Any]) -> bool:
            if not hmac.compare_digest(
                self.headers.get("X-CSRF-Token", ""), session["csrf"]
            ):
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            scheme = (
                "https" if self.connection.__class__.__module__ == "ssl" else "http"
            )
            return hmac.compare_digest(
                origin, f"{scheme}://{self.headers.get('Host', '')}"
            )

        def _read(self, maximum=None) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > (maximum or controller.cfg.max_output_bytes):
                raise ValueError("invalid request size")
            if not length:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("request must be a JSON object")
            return data

        def _send(
            self,
            code: int,
            payload: dict[str, Any],
            headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _html(self, code: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            self._guarded(self._get)

        def _guarded(self, handler) -> None:
            try:
                handler()
            except ControllerError as exc:
                self._send(409, {"ok": False, "error": str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                # Never drop the connection without an answer, and never echo internals.
                controller.store.audit(
                    "api", "api.error", {"error": type(exc).__name__}
                )
                self._send(500, {"ok": False, "error": "internal error"})

        def _get(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._send(200, {"ok": True})
                return
            if path == "/operator/login":
                from dark_factory.ui.web import LOGIN_HTML

                self._html(200, LOGIN_HTML)
                return
            if path == "/operator/dashboard":
                session = self._session()
                if not session:
                    self.send_response(303)
                    self.send_header("Location", "/operator/login")
                    self.end_headers()
                    return
                from dark_factory.ui.web import dashboard_html

                self._html(200, dashboard_html(session["csrf"], session["actor"]))
                return
            if path == "/operator/view":
                if not self._operator_auth():
                    self._send(401, {"ok": False, "error": "unauthorized"})
                    return
                from dark_factory.ui.projection import controller_projection

                query = parse_qs(urlparse(self.path).query)
                detail_id = query.get("work_item_id", [None])[0]
                self._send(
                    200,
                    {
                        "ok": True,
                        "result": controller_projection(controller, detail_id),
                    },
                )
                return
            self._send(404, {"ok": False})

        def do_POST(self) -> None:  # noqa: N802
            self._guarded(self._post)

        def _post(self) -> None:
            path = urlparse(self.path).path
            if path == "/operator/session":
                source = self.client_address[0]
                if throttled(source):
                    self._send(
                        429,
                        {"ok": False, "error": "too many failed logins; wait"},
                        {"Retry-After": str(LOGIN_WINDOW_SECONDS)},
                    )
                    return
                data = self._read(8192)
                supplied = data.get("token")
                actor = data.get("actor")
                if (
                    not isinstance(supplied, str)
                    or not controller.cfg.operator_token
                    or not hmac.compare_digest(supplied, controller.cfg.operator_token)
                    or not isinstance(actor, str)
                    or not actor.strip()
                    or len(actor) > 100
                ):
                    failures.setdefault(source, []).append(time.time())
                    self._send(401, {"ok": False, "error": "unauthorized"})
                    return
                purge_sessions()
                session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                sessions[session_id] = {
                    "actor": actor.strip(),
                    "csrf": csrf,
                    "expires": time.time() + 8 * 3600,
                }
                secure = (
                    "; Secure" if self.connection.__class__.__module__ == "ssl" else ""
                )
                self._send(
                    200,
                    {"ok": True, "csrf": csrf},
                    {
                        "Set-Cookie": f"dark_factory_session={session_id}; HttpOnly; SameSite=Strict; Path=/operator; Max-Age=28800{secure}"
                    },
                )
                return
            if path.startswith("/v1/execution/"):
                from dark_factory.execution.packages import MAX_BYTES
                from dark_factory.execution.service import ExecutionService

                pieces = path.strip("/").split("/")
                if len(pieces) != 5 or pieces[2] != controller.cfg.project_id:
                    self._send(401, {"ok": False, "error": "unauthorized"})
                    return
                worker, operation = pieces[3:]
                service = ExecutionService(controller)
                header = self.headers.get("Authorization", "")
                credential = (
                    header.removeprefix("Bearer ")
                    if header.startswith("Bearer ")
                    else ""
                )
                if not service.authenticate(worker, credential):
                    self._send(401, {"ok": False, "error": "unauthorized"})
                    return
                data = self._read(MAX_BYTES * 2)
                if operation == "preflight":
                    result = service.preflight(worker)
                elif operation == "claim":
                    result = service.claim(worker, data["request_id"])
                elif operation == "heartbeat":
                    result = service.heartbeat(worker, data["attempt_id"])
                elif operation == "start":
                    result = service.start(
                        worker, data["attempt_id"], data["execution_id"]
                    )
                elif operation == "task-progress":
                    result = service.task_progress(worker, data)
                elif operation == "task-instructions":
                    result = service.task_instructions(worker, data["attempt_id"])
                elif operation == "task-ack":
                    result = service.task_ack(worker, data)
                elif operation == "checkpoint":
                    result = service.checkpoint(worker, data)
                elif operation == "finish":
                    result = service.finish_remote(worker, data)
                elif operation == "cleanup":
                    result = service.cleanup(
                        worker, data["attempt_id"], data["result_digest"]
                    )
                else:
                    raise ValueError("unsupported execution operation")
                self._send(200, {"ok": True, "result": result})
                return
            if path != "/operator":
                self._send(404, {"ok": False})
                return
            session = self._session()
            operator_bearer = self._auth(controller.cfg.operator_token)
            contributor_bearer = self._auth(controller.cfg.contributor_token)
            if session and not self._session_mutation_allowed(session):
                self._send(403, {"ok": False, "error": "invalid browser request"})
                return
            if not (operator_bearer or session or contributor_bearer):
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            data = self._read()
            command = operator_commands.COMMANDS.get(str(data.get("cmd")))
            if command is None or not command.api:
                raise ControllerError("unsupported operator command")
            if contributor_bearer and not (operator_bearer or session):
                if not command.contributor:
                    self._send(
                        403,
                        {"ok": False, "error": "contributor credential cannot do this"},
                    )
                    return
                actor = "contributor:" + str(data.get("actor") or "agent")[:60]
            else:
                actor = session["actor"] if session else data.get("actor") or "operator"
            params = {name: data[name] for name in command.params if name in data}
            result = operator_commands.run(controller, command.name, params, actor)
            self._send(200, {"ok": True, "result": result})
            return

    return Handler


def serve(
    controller: Controller, host: str, port: int, tls=None
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"} and not tls:
        raise ControllerError(
            "bootstrap API is local-only; remote workers require later qualification"
        )
    server = ThreadingHTTPServer((host, port), make_handler(controller))
    if tls:
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(*tls)
            server.socket = context.wrap_socket(
                server.socket, server_side=True, do_handshake_on_connect=False
            )
        except BaseException:
            server.server_close()
            raise
    return server
