"""The health-factor service shared by every face (A2A, MCP, REST, paid jobs)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from . import ENGINE_NAME, ENGINE_VERSION
from .parse import ParsedTask, TaskError, parse_task
from .rpc import RpcError, RpcUnavailable
from .venus import EngineError, health_report


class HFService:
    def __init__(self, cfg, pool, *, cache_size: int = 256):
        self.cfg = cfg
        self.pool = pool
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._cache_size = cache_size
        self.served = 0

    def _cached(self, key: tuple) -> dict | None:
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
            return hit

    def _store(self, key: tuple, value: dict) -> None:
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def report_for(self, task: ParsedTask) -> dict[str, Any]:
        """Compute the report; historical blocks are immutable, so they are cached."""
        key = None
        if task.block_number is not None:
            key = (task.address, task.block_number, str(task.target_health_factor))
            hit = self._cached(key)
            if hit is not None:
                return {**hit, "input": task.echo(), "cached": True}
        started = time.monotonic()
        report = health_report(self.pool, self.cfg, task.address, task.block_number,
                               task.target_health_factor, inputs=task.echo())
        report["latencyMs"] = int((time.monotonic() - started) * 1000)
        if task.warnings:
            report["assumptions"] = list(task.warnings)
        else:
            report["assumptions"] = []
        if key is not None:
            self._store(key, report)
        self.served += 1
        return report

    def answer(self, text: str | None = None, data: dict | None = None) -> tuple[bool, dict[str, Any]]:
        """(ok, payload). A refusal or failure is a JSON object that says why."""
        try:
            task = parse_task(text, data)
        except TaskError as exc:
            return False, refusal(str(exc), "a 0x address of a Venus Core Pool account")
        try:
            return True, self.report_for(task)
        except EngineError as exc:
            return False, refusal(f"could not read Venus state: {exc}", inputs=task.echo())
        except (RpcUnavailable, RpcError) as exc:
            return False, refusal(f"BNB Smart Chain RPC unavailable, retry shortly ({type(exc).__name__})",
                                  inputs=task.echo(), retryable=True)


def refusal(reason: str, needs: str | None = None, *, inputs: dict | None = None,
            retryable: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "error": "refused" if not retryable else "unavailable",
        "refused": not retryable,
        "reason": reason,
        "healthFactor": None,
        "engine": {"name": ENGINE_NAME, "version": ENGINE_VERSION},
    }
    if needs:
        out["needs"] = needs
    if inputs is not None:
        out["input"] = inputs
    if retryable:
        out["retryable"] = True
    return out


def decimal_or(value: Any, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default
