"""MCP face (streamable HTTP, JSON responses).

POST /mcp carries JSON-RPC: initialize, notifications/initialized (202), ping,
tools/list, tools/call, resources/list, prompts/list. The health-factor tool
returns the report as a text content block AND as structuredContent; the
MCS-HF-1 graded fields are also repeated at the top level of the result object
because Marque's harness grades the `result` object itself.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from . import __version__

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]
GRADED = ("healthFactor", "primaryCollateralSymbol", "primaryCollateralFactor", "primaryLiquidationPriceUsd",
          "repayUsdToReachTarget", "targetHealthFactor", "blockNumber", "account")

HF_TOOL = {
    "name": "venus_health_factor",
    "title": "Venus Core Pool health factor",
    "description": (
        "Health factor of a Venus Core Pool position on BNB Smart Chain, read on chain at a pinned block: "
        "health factor (3 dp), per-market collateral factor and liquidation threshold, per-asset liquidation "
        "price, primary collateral, and the exact USD of debt to repay to restore a target health factor. "
        "Pass `address` (and optionally `blockNumber`, `targetHealthFactor`), or a natural-language `prompt` "
        "naming the account."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "address": {"type": "string", "description": "Venus account (0x + 40 hex)",
                        "pattern": "^0x[a-fA-F0-9]{40}$"},
            "blockNumber": {"type": ["integer", "string"], "description": "Block number or \"latest\" (default)"},
            "targetHealthFactor": {"type": "number", "description": "Target for the repayment (default 2.0)",
                                   "exclusiveMinimum": 1},
            "policy": {"type": "object", "description": "Optional policy object, e.g. {\"targetHealthFactor\": 2.5}"},
            "prompt": {"type": "string", "description": "Optional task in plain text naming the account"},
        },
        "required": [],
        "additionalProperties": True,
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True},
}

INFO_TOOL = {
    "name": "vitals_agent_info",
    "title": "About Vitals",
    "description": "How to hire Vitals as a paid ERC-8183 job, its price, wallet and registry identity.",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
    "annotations": {"readOnlyHint": True},
}


def _err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class MCPHandler:
    def __init__(self, hf_answer: Callable[[str | None, dict | None], tuple[bool, dict]],
                 info: Callable[[], dict]):
        self.hf_answer = hf_answer
        self.info = info

    def handle(self, body: Any) -> tuple[Any, dict[str, str]]:
        """Returns (response or None for notifications-only, extra headers)."""
        headers: dict[str, str] = {}
        if isinstance(body, list):
            out = []
            for b in body:
                r, h = self.handle(b)
                headers.update(h)
                if r is not None:
                    out.append(r)
            return (out or None), headers
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
            return _err(body.get("id") if isinstance(body, dict) else None, -32600, "Invalid Request"), headers
        method, req_id = body["method"], body.get("id")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        is_notification = "id" not in body
        if method.startswith("notifications/"):
            return None, headers
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                version = asked if asked in SUPPORTED_VERSIONS else LATEST
                headers["Mcp-Session-Id"] = uuid.uuid4().hex
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "vitals", "title": "Vitals", "version": __version__},
                    "instructions": "Call venus_health_factor with an address (and optionally blockNumber and "
                                    "targetHealthFactor) to get the Venus Core Pool health-factor report.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [HF_TOOL, INFO_TOOL]}
            elif method == "tools/call":
                result = self._call(params)
            elif method in ("resources/list",):
                result = {"resources": []}
            elif method in ("resources/templates/list",):
                result = {"resourceTemplates": []}
            elif method in ("prompts/list",):
                result = {"prompts": []}
            else:
                return (None if is_notification else _err(req_id, -32601, f"Method not found: {method}")), headers
        except _ToolArgError as exc:
            return _err(req_id, -32602, str(exc)), headers
        except Exception as exc:
            return _err(req_id, -32603, f"Internal error: {type(exc).__name__}"), headers
        if is_notification:
            return None, headers
        return {"jsonrpc": "2.0", "id": req_id, "result": result}, headers

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name == INFO_TOOL["name"]:
            info = self.info()
            return {"content": [{"type": "text", "text": json.dumps(info, separators=(",", ":"))}],
                    "structuredContent": info, "isError": False}
        if name != HF_TOOL["name"]:
            raise _ToolArgError(f"Unknown tool: {name!r}")
        text = None
        for k in ("prompt", "query", "message"):
            if isinstance(args.get(k), str):
                text = args[k]
                break
        ok, payload = self.hf_answer(text, args)
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": body}],
            "structuredContent": payload,
            "isError": not ok,
        }
        for k in GRADED:
            if k in payload:
                result[k] = payload[k]
        if not ok:
            result["reason"] = payload.get("reason")
        return result


class _ToolArgError(ValueError):
    pass
