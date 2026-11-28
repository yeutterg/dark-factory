"""Dependency-free stdio MCP façade with contributor-only tools."""

from __future__ import annotations

import json
import sys

TOOLS = {
    "work_start": ("work-start", ("repo", "number", "title", "body")),
    "work_plan": ("work-plan", ("work_item_id", "proposal")),
    "work_checkout": ("work-checkout", ("work_item_id",)),
    "work_submit": ("work-submit", ("work_item_id", "bundle")),
    "work_status": ("work-status", ("work_item_id",)),
}


def definitions():
    return [
        {
            "name": name,
            "description": f"Dark Factory contributor operation {command}",
            "inputSchema": {
                "type": "object",
                "properties": {key: {} for key in params},
                "additionalProperties": False,
            },
        }
        for name, (command, params) in TOOLS.items()
    ]


def serve(transport, actor="mcp"):
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            method = request.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "dark-factory", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": definitions()}
            elif method == "tools/call":
                params = request.get("params", {})
                name = params.get("name")
                if name not in TOOLS:
                    raise ValueError("unknown or unauthorized tool")
                command, allowed = TOOLS[name]
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict) or set(arguments) - set(allowed):
                    raise ValueError("invalid tool arguments")
                if command == "work-plan" and isinstance(
                    arguments.get("proposal"), dict
                ):
                    arguments = {
                        **arguments,
                        "proposal": json.dumps(arguments["proposal"]),
                    }
                value = transport.command(command, arguments, actor)
                result = {
                    "content": [{"type": "text", "text": json.dumps(value)}],
                    "structuredContent": value,
                }
            elif method == "notifications/initialized":
                continue
            else:
                raise ValueError("unsupported MCP method")
            response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32602, "message": str(exc)[:300]},
            }
        print(json.dumps(response), flush=True)
