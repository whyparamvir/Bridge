"""Newline-delimited JSON-RPC 2.0 over stdio: the slice of MCP the bridge needs. Nothing
but protocol messages is ever written to stdout; logs go to stderr."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, TextIO

from . import __version__

log = logging.getLogger("bridge_mcp")

DEFAULT_PROTOCOL = "2025-06-18"


class Tools(Protocol):
    def specs(self) -> list[dict]: ...
    def call(self, name: str, args: dict | None) -> dict: ...
    def start(self) -> None: ...
    def leave(self) -> None: ...


def _reply(out: TextIO, msg_id: Any, result: Any = None, error: dict | None = None) -> None:
    msg: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    out.write(json.dumps(msg, ensure_ascii=False) + "\n")
    out.flush()


def _handle(msg: dict, tools: Tools) -> tuple[Any, dict | None]:
    method, params = msg.get("method"), msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return None, {"code": -32602, "message": "Invalid params: params must be an object"}
    if method == "initialize":
        try:
            tools.start()
        except Exception:  # noqa: BLE001 - the session must start even when the seat cannot
            log.exception("could not take a seat on the board")
        return {"protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bridge", "version": __version__}}, None
    if method == "ping":
        return {}, None
    if method == "tools/list":
        return {"tools": tools.specs()}, None
    if method == "tools/call":
        name = params.get("name")
        try:
            return tools.call(str(name), params.get("arguments")), None
        except Exception as e:  # noqa: BLE001 - a tool bug must not kill the session
            log.exception("tool %s failed", name)
            return {"content": [{"type": "text", "text": f"Bridge MCP error: {e}"}], "isError": True}, None
    return None, {"code": -32601, "message": f"Method not found: {method}"}


def serve(stdin: TextIO, stdout: TextIO, tools: Tools) -> None:
    """Reads requests until end of input, then leaves the seat."""
    try:
        for line in iter(stdin.readline, ""):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning("ignoring a line that is not JSON")
                _reply(stdout, None, error={"code": -32700, "message": "Parse error"})
                continue
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
                if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                    continue  # a response to something we never send; ignore
                _reply(stdout, msg.get("id") if isinstance(msg, dict) else None,
                       error={"code": -32600, "message": "Invalid Request"})
                continue
            if "id" not in msg:
                continue  # notifications (initialized, cancelled, ...) get no reply
            try:
                result, error = _handle(msg, tools)
            except Exception:  # noqa: BLE001 - one bad request must not end the session
                log.exception("request %s failed", msg["method"])
                result, error = None, {"code": -32603, "message": "Internal error"}
            _reply(stdout, msg["id"], result, error)
    finally:
        try:
            tools.leave()
        except Exception:  # noqa: BLE001 - leaving is best effort
            log.exception("could not leave the seat")
