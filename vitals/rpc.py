"""JSON-RPC pool for BSC with per-purpose endpoint lists and failover.

Two pools: `head` (recent state, fast public nodes that prune history) and
`archive` (nodes that still serve old state). A read pinned to a block that
is not near the head goes to the archive pool first. Every endpoint has a
timeout; transient failures (timeouts, HTTP 429/5xx, "missing trie node",
"header not found") move on to the next endpoint and put the failing one on a
short cooldown. Contract reverts are deterministic and are raised at once.

Provenance reports only the host name of the endpoint that answered, never
its full URL, because some providers put API keys in the path.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

# How far behind the head a block may be and still be served by a pruning node.
HEAD_WINDOW_BLOCKS = 64

_STATE_UNAVAILABLE = (
    "missing trie node",
    "header not found",
    "historical state",
    "state not available",
    "pruned",
    "unknown block",
    "block not found",
    "state is not available",
    "required historical state",
    "distance to target block",
)


class RpcError(Exception):
    """A JSON-RPC error that is the same on every node (bad params, revert)."""

    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class RpcRevert(RpcError):
    """eth_call / eth_estimateGas reverted."""


class RpcUnavailable(Exception):
    """No endpoint in the pool could answer."""


@dataclass
class _Endpoint:
    url: str
    failures: int = 0
    cooldown_until: float = 0.0

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or "unknown"


def host_of(url: str) -> str:
    return urlparse(url).hostname or "unknown"


class RpcPool:
    def __init__(
        self,
        head: list[str],
        archive: list[str] | None = None,
        *,
        timeout: float = 10.0,
        retries: int = 2,
        cooldown: float = 30.0,
    ):
        if not head:
            raise ValueError("at least one head RPC endpoint is required")
        self._pools = {
            "head": [_Endpoint(u) for u in head],
            "archive": [_Endpoint(u) for u in (archive or head)],
        }
        self.timeout = timeout
        self.retries = max(0, retries)
        self.cooldown = cooldown
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._local = threading.local()
        self.last_host: str | None = None

    # ------------------------------------------------------------- plumbing

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"content-type": "application/json", "user-agent": "vitals-hf/1.0"})
            self._local.session = s
        return s

    def _ordered(self, purpose: str) -> list[_Endpoint]:
        if purpose == "archive":
            primary = self._pools["archive"]
            secondary = [e for e in self._pools["head"] if e.url not in {p.url for p in primary}]
        else:
            primary = self._pools["head"]
            secondary = [e for e in self._pools["archive"] if e.url not in {p.url for p in primary}]
        now = time.monotonic()
        ready = [e for e in primary if e.cooldown_until <= now] + [e for e in secondary if e.cooldown_until <= now]
        cooling = [e for e in primary + secondary if e.cooldown_until > now]
        return ready + cooling

    def _mark(self, ep: _Endpoint, ok: bool) -> None:
        with self._lock:
            if ok:
                ep.failures = 0
                ep.cooldown_until = 0.0
            else:
                ep.failures += 1
                ep.cooldown_until = time.monotonic() + min(self.cooldown * ep.failures, 300.0)

    def _post(self, ep: _Endpoint, payload: dict) -> Any:
        resp = self._session().post(ep.url, json=payload, timeout=self.timeout)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Transient(f"http {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise _Transient(f"non-JSON answer (http {resp.status_code})") from exc
        if not isinstance(body, dict):
            raise _Transient("unexpected JSON-RPC answer")
        err = body.get("error")
        if err:
            msg = str(err.get("message", "")) if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            data = err.get("data") if isinstance(err, dict) else None
            low = msg.lower()
            if any(s in low for s in _STATE_UNAVAILABLE):
                raise _Transient(msg)
            if "revert" in low or code == 3:
                raise RpcRevert(msg, code, data)
            if "rate" in low and "limit" in low or "too many" in low or "limit exceeded" in low:
                raise _Transient(msg)
            if code in (-32005, -32603, -32000) and ("timeout" in low or "busy" in low or "capacity" in low):
                raise _Transient(msg)
            raise RpcError(msg, code, data)
        if "result" not in body:
            raise _Transient("JSON-RPC answer without result")
        return body["result"]

    # ------------------------------------------------------------------ API

    def call(self, method: str, params: list | None = None, *, purpose: str = "head") -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        errors: list[str] = []
        last_rpc_error: RpcError | None = None
        transient_seen = False
        for attempt in range(self.retries + 1):
            for ep in self._ordered(purpose):
                try:
                    result = self._post(ep, payload)
                except RpcRevert:
                    self._mark(ep, True)
                    raise
                except RpcError as exc:
                    # The same request may succeed elsewhere (node-specific limits); try on.
                    last_rpc_error = exc
                    errors.append(f"{ep.host}: {exc}")
                    continue
                except (_Transient, requests.RequestException) as exc:
                    transient_seen = True
                    self._mark(ep, False)
                    errors.append(f"{ep.host}: {exc}")
                    continue
                self._mark(ep, True)
                self.last_host = ep.host
                return result
            if last_rpc_error is not None and not transient_seen:
                # Every node gave a deterministic error: retrying will not help.
                raise last_rpc_error
            if attempt < self.retries:
                time.sleep(0.4 * (attempt + 1))
        raise RpcUnavailable(f"{method} failed on every endpoint: " + "; ".join(errors[-6:]))

    def call_with_host(self, method: str, params: list | None = None, *, purpose: str = "head") -> tuple[Any, str]:
        result = self.call(method, params, purpose=purpose)
        return result, self.last_host or "unknown"

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def purpose_for(self, block: int | str, head: int | None = None) -> str:
        if block in ("latest", "pending", "safe", "finalized"):
            return "head"
        if head is None:
            return "archive"
        return "head" if head - int(block) <= HEAD_WINDOW_BLOCKS else "archive"

    def eth_call(self, to: str, data: str, block: int | str = "latest", *, purpose: str | None = None,
                 sender: str | None = None) -> str:
        tag = hex(block) if isinstance(block, int) else block
        tx: dict[str, Any] = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        return self.call("eth_call", [tx, tag], purpose=purpose or self.purpose_for(block))

    def get_block(self, block: int | str = "latest", *, purpose: str | None = None) -> dict:
        tag = hex(block) if isinstance(block, int) else block
        res = self.call("eth_getBlockByNumber", [tag, False], purpose=purpose or self.purpose_for(block))
        if res is None:
            raise RpcUnavailable(f"block {block} not found")
        return res

    def get_logs(self, address: str, topics: list, from_block: int, to_block: int, *,
                 purpose: str = "head") -> list[dict]:
        params = [{"address": address, "topics": topics, "fromBlock": hex(from_block), "toBlock": hex(to_block)}]
        return self.call("eth_getLogs", params, purpose=purpose)

    def hosts(self) -> dict[str, list[str]]:
        return {k: [e.host for e in v] for k, v in self._pools.items()}


class _Transient(Exception):
    pass


def pool_from_config(cfg) -> RpcPool:
    return RpcPool(cfg.rpc_head, cfg.rpc_archive, timeout=cfg.rpc_timeout, retries=cfg.rpc_retries)
