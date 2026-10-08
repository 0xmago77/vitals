"""A2A JSON-RPC 2.0 face (protocol 0.3.0): `message/send`, `tasks/get`, `tasks/cancel`.

Routing inside `message/send`:
  * a data part (or a text part holding JSON) with "skill" = negotiate-erc8183-job | negotiate
    -> signed ERC-8183 quote (bnbagent SDK NegotiationResult) in a data part;
  * skill = notify_funded -> delivery acknowledgement; skill = erc8183-job-status -> job state;
  * anything else -> the free health-factor report.

The report comes back as a completed Task whose artifact's FIRST part is a text
part containing only the JSON object (what Marque's MCS harness parses), and
whose second part is the same object as a data part.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable

NEGOTIATE_SKILLS = {"negotiate-erc8183-job", "negotiate", "negotiate_erc8183_job"}
NOTIFY_SKILLS = {"notify_funded", "notify-funded", "notifyFunded"}
STATUS_SKILLS = {"erc8183-job-status", "job_status", "job-status"}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, TASK_NOT_CANCELABLE, UNSUPPORTED = -32001, -32002, -32004


def rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def split_message(message: dict) -> tuple[str, dict | None, str | None]:
    """(joined text, merged data, skill) from an A2A message."""
    texts: list[str] = []
    data: dict | None = None
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "data" and isinstance(part.get("data"), dict):
            data = {**(data or {}), **part["data"]}
        elif "text" in part and isinstance(part["text"], str):
            texts.append(part["text"])
    text = "\n".join(texts)
    skill = None
    if data and isinstance(data.get("skill"), str):
        skill = data["skill"]
    if skill is None and text.strip().startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("skill"), str):
                skill, data = obj["skill"], {**obj, **(data or {})}
        except ValueError:
            pass
    meta = message.get("metadata") or {}
    if skill is None and isinstance(meta, dict):
        s = meta.get("skill") or meta.get("skillId") or meta.get("skill_id")
        if isinstance(s, str):
            skill = s
    return text, data, skill


class A2AHandler:
    def __init__(self, hf_answer: Callable[[str | None, dict | None], tuple[bool, dict]],
                 seller=None, max_tasks: int = 500):
        self.hf_answer = hf_answer
        self.seller = seller
        self.tasks: OrderedDict[str, dict] = OrderedDict()
        self.max_tasks = max_tasks

    def _remember(self, task: dict) -> None:
        self.tasks[task["id"]] = task
        while len(self.tasks) > self.max_tasks:
            self.tasks.popitem(last=False)

    @staticmethod
    def agent_message(data: dict, context_id: str | None = None, text: bool = False) -> dict:
        parts: list[dict] = []
        if text:
            parts.append({"kind": "text", "text": json.dumps(data, separators=(",", ":"))})
        parts.append({"kind": "data", "data": data})
        msg = {"kind": "message", "role": "agent", "messageId": str(uuid.uuid4()), "parts": parts}
        if context_id:
            msg["contextId"] = context_id
        return msg

    def report_task(self, payload: dict, ok: bool, context_id: str | None) -> dict:
        task_id = str(uuid.uuid4())
        ctx = context_id or str(uuid.uuid4())
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        state = "completed" if ok else ("failed" if payload.get("retryable") else "rejected")
        task = {
            "kind": "task",
            "id": task_id,
            "contextId": ctx,
            "status": {"state": state, "timestamp": _now()},
            "artifacts": [{
                "artifactId": str(uuid.uuid4()),
                "name": "venus-health-factor-report" if ok else "refusal",
                "parts": [{"kind": "text", "text": body}, {"kind": "data", "data": payload}],
            }],
            "metadata": {"skill": "venus-health-factor"},
        }
        if not ok:
            task["status"]["message"] = {"kind": "message", "role": "agent", "messageId": str(uuid.uuid4()),
                                         "parts": [{"kind": "text", "text": body}], "taskId": task_id,
                                         "contextId": ctx}
        self._remember(task)
        return task

    def handle(self, body: Any, *, client: str = "unknown") -> dict | list | None:
        if isinstance(body, list):
            out = [r for r in (self.handle(b, client=client) for b in body) if r is not None]
            return out or None
        if not isinstance(body, dict):
            return rpc_error(None, INVALID_REQUEST, "Invalid Request: expected a JSON-RPC 2.0 object")
        req_id = body.get("id")
        method = body.get("method")
        if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return rpc_error(req_id, INVALID_REQUEST, "Invalid Request: jsonrpc must be \"2.0\" and method a string")
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        try:
            if method in ("message/send", "tasks/send", "message/stream", "tasks/sendSubscribe"):
                return {"jsonrpc": "2.0", "id": req_id, "result": self._send(params, client)}
            if method == "tasks/get":
                task = self.tasks.get(str(params.get("id")))
                if task is None:
                    return rpc_error(req_id, TASK_NOT_FOUND, "Task not found")
                return {"jsonrpc": "2.0", "id": req_id, "result": task}
            if method == "tasks/cancel":
                if str(params.get("id")) not in self.tasks:
                    return rpc_error(req_id, TASK_NOT_FOUND, "Task not found")
                return rpc_error(req_id, TASK_NOT_CANCELABLE, "Task is already completed")
            if method.startswith("tasks/pushNotificationConfig"):
                return rpc_error(req_id, -32003, "Push notifications are not supported")
            if method in ("agent/getAuthenticatedExtendedCard", "agent/card"):
                return rpc_error(req_id, UNSUPPORTED, "No extended card; GET /.well-known/agent-card.json")
            return rpc_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        except _ParamsError as exc:
            return rpc_error(req_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # never leak a stack trace, always answer JSON
            return rpc_error(req_id, INTERNAL, f"Internal error: {type(exc).__name__}")

    def _send(self, params: dict, client: str) -> dict:
        message = params.get("message")
        if not isinstance(message, dict):
            raise _ParamsError("params.message is required (an A2A Message with parts)")
        text, data, skill = split_message(message)
        context_id = message.get("contextId") if isinstance(message.get("contextId"), str) else None
        if skill in NEGOTIATE_SKILLS:
            if self.seller is None:
                return self.agent_message({"accepted": False, "reason": "quotes are not enabled on this instance"},
                                          context_id)
            envelope = self.seller.negotiate(data or {}, client=client)
            return self.agent_message(envelope, context_id)
        if skill in NOTIFY_SKILLS:
            if self.seller is None:
                return self.agent_message({"status": "rejected", "reason": "commerce disabled"}, context_id)
            return self.agent_message(self.seller.notify_funded(data or {}), context_id)
        if skill in STATUS_SKILLS:
            if self.seller is None:
                return self.agent_message({"error": "commerce disabled"}, context_id)
            return self.agent_message(self.seller.job_status((data or {}).get("job_id")), context_id)
        ok, payload = self.hf_answer(text or None, data)
        return self.report_task(payload, ok, context_id)


class _ParamsError(ValueError):
    pass
