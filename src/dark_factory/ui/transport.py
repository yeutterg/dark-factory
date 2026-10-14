"""Local and authenticated remote operator transports for the TUI."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from dark_factory import operator_commands
from dark_factory.controller import Controller, ControllerError
from dark_factory.ui.projection import controller_projection


class OperatorTransport(ABC):
    @abstractmethod
    def snapshot(self, detail_id: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def command(
        self, name: str, params: dict[str, Any] | None = None, actor: str = "operator"
    ) -> Any: ...


class LocalTransport(OperatorTransport):
    def __init__(self, controller: Controller) -> None:
        self.controller = controller

    def snapshot(self, detail_id: str | None = None) -> dict[str, Any]:
        return controller_projection(self.controller, detail_id)

    def command(
        self, name: str, params: dict[str, Any] | None = None, actor: str = "operator"
    ) -> Any:
        return operator_commands.run(self.controller, name, params or {}, actor)


class RemoteTransport(OperatorTransport):
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        target = urlparse(self.base_url)
        if target.scheme not in {"http", "https"} or not target.hostname:
            raise ControllerError("operator API URL must be HTTP or HTTPS")
        if target.scheme != "https" and target.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ControllerError("remote operator API requires HTTPS")
        if not token:
            raise ControllerError("operator credential is required")
        self.token = token

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            self.base_url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", str(exc))
            except (ValueError, AttributeError):
                detail = str(exc)
            raise ControllerError(detail) from exc
        if not result.get("ok"):
            raise ControllerError(result.get("error", "operator request failed"))
        return result.get("result")

    def snapshot(self, detail_id: str | None = None) -> dict[str, Any]:
        query = "?" + urlencode({"work_item_id": detail_id}) if detail_id else ""
        result = self._request("/operator/view" + query)
        if not isinstance(result, dict):
            raise ControllerError("invalid controller projection")
        return result

    def command(
        self, name: str, params: dict[str, Any] | None = None, actor: str = "operator"
    ) -> Any:
        command = operator_commands.COMMANDS.get(name)
        if not command or not command.api:
            raise ControllerError("unsupported remote operator command")
        return self._request(
            "/operator", {"cmd": name, "actor": actor, **(params or {})}
        )
