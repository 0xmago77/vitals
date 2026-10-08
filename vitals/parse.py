"""Parse a health-factor task from natural-language text and/or structured data.

Accepted inputs (any combination; structured fields win over text):
  * text such as Marque's MCS-HF-1 prompt ("Account: 0x..", "Block: 124010796",
    "... restore a health factor of 2.5"), or any sentence naming an address;
  * a JSON object (data part, MCP arguments, REST body, ERC-8183 job task) with
    address / account / wallet / subject, blockNumber / block / block_number,
    targetHealthFactor / target / policy.targetHealthFactor, task / input nests,
    or task_description text.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from eth_utils import is_checksum_address, to_checksum_address

DEFAULT_TARGET = Decimal("2.0")
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])")
NUM = r"(\d+(?:\.\d+)?)"

LABELED_ADDRESS = re.compile(
    r"\b(?:account|address|wallet|borrower|user|subject|owner|position)\b[^0-9a-zA-Z]{0,6}(?:is\s+|of\s+)?"
    r"(0x[a-fA-F0-9]{40})(?![a-fA-F0-9])",
    re.I,
)
BLOCK_RE = re.compile(
    r"\b(?:block(?:[\s_-]*(?:number|height|no\.?))?|blockNumber|block_number|height)\b\s*(?:\"\s*)?[:=#]?\s*\"?\s*#?"
    r"(\d{1,12})\b",
    re.I,
)
LATEST_RE = re.compile(r"\b(?:at\s+)?(?:the\s+)?latest\s+block\b|\bblock\s*[:=]?\s*latest\b", re.I)
_GAP = r"(?:0x[a-fA-F0-9]{40}|[^.?!\d]){0,60}?"
TARGET_PATTERNS = [
    # restore / restores / restored / restoring ... to N  (Keel's first rule)
    (re.compile(r"restor(?:e|es|ed|ing)\b" + _GAP + r"\bto\s+(?:a\s+)?(?:health\s*factor\s+(?:of\s+)?)?(?:HF\s*)?" + NUM, re.I), True),
    (re.compile(r"restor(?:e|es|ed|ing)\b" + _GAP + r"\bhealth\s*factor\s+(?:of\s+)?" + NUM, re.I), True),
    (re.compile(r"(?:bring|get|move|lift|raise|take)s?\b" + _GAP + r"\bto\s+(?:a\s+)?(?:health\s*factor\s+(?:of\s+)?)?(?:HF\s*)?" + NUM, re.I), True),
    (re.compile(r"\"?target(?:[\s_]*health[\s_]*factor|HealthFactor|[\s_]*hf)?\"?(?:\s+of|\s+is|\s*[:=])?\s*\"?" + NUM, re.I), True),
    (re.compile(r"\bhealth\s*factor(?:\s+(?:of|to|at|above))?\s+" + NUM, re.I), False),
    (re.compile(r"\bHF\s*(?:of|to|at|above|[:=]|>=?)?\s*" + NUM), False),
]

ADDRESS_KEYS = ("address", "account", "wallet", "borrower", "user", "subject", "owner")
BLOCK_KEYS = ("blockNumber", "block", "block_number", "blockHeight", "atBlock")
TARGET_KEYS = ("targetHealthFactor", "target_health_factor", "targetHf", "target_hf", "target", "targetHF")
TEXT_KEYS = ("task_description", "task", "prompt", "query", "message", "text", "description", "statement", "input")


class TaskError(ValueError):
    """The task cannot be answered; the message says what to send instead."""


@dataclass
class ParsedTask:
    address: str | None = None
    block_number: int | None = None
    target_health_factor: Decimal = DEFAULT_TARGET
    chain_id: int = 56
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def echo(self) -> dict[str, Any]:
        d = asdict(self)
        d["targetHealthFactor"] = float(self.target_health_factor)
        d["blockNumber"] = self.block_number if self.block_number is not None else "latest"
        d.pop("target_health_factor")
        d.pop("block_number")
        d["chainId"] = d.pop("chain_id")
        d["pool"] = "Venus Core Pool"
        return d


def _to_decimal(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _to_block(v: Any) -> int | None | str:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip().lower()
    if s in ("latest", "head", ""):
        return "latest"
    if s.startswith("0x"):
        try:
            return int(s, 16)
        except ValueError:
            return None
    return int(s) if s.isdigit() else None


def _addr(s: str, warnings: list[str]) -> str:
    if s != s.lower() and s[2:] != s[2:].upper() and not is_checksum_address(s):
        warnings.append(f"address {s} has a mixed-case checksum that does not verify; read it case-insensitively")
    return to_checksum_address(s.lower())


def _maybe_json(text: str) -> dict | None:
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            obj = json.loads(t)
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None


def _from_data(data: dict, out: ParsedTask, texts: list[str], depth: int = 0) -> None:
    if depth > 4:
        return
    for k in ADDRESS_KEYS:
        v = data.get(k)
        if out.address is None and isinstance(v, str) and ADDRESS_RE.fullmatch(v.strip()):
            out.address = _addr(v.strip(), out.warnings)
            out.sources["address"] = f"data.{k}"
        elif out.address is None and isinstance(v, dict):
            _from_data(v, out, texts, depth + 1)
    for k in BLOCK_KEYS:
        if k in data and "blockNumber" not in out.sources:
            b = _to_block(data[k])
            if b is None:
                raise TaskError(f"{k} must be a block number or \"latest\", got {data[k]!r}")
            out.block_number = None if b == "latest" else int(b)
            out.sources["blockNumber"] = f"data.{k}"
    for k in TARGET_KEYS:
        if k in data and "targetHealthFactor" not in out.sources and not isinstance(data[k], (dict, list)):
            t = _to_decimal(data[k])
            if t is None:
                raise TaskError(f"{k} must be a number, got {data[k]!r}")
            out.target_health_factor = t
            out.sources["targetHealthFactor"] = f"data.{k}"
    for k in ("chainId", "chain_id"):
        if k in data and str(data[k]).strip() not in ("56", "0x38", "bsc", "eip155:56"):
            raise TaskError(f"Vitals reads Venus Core Pool on BNB Smart Chain (chain 56) only; got chainId {data[k]!r}")
    for k in ("policy", "subject", "task", "input", "params", "arguments"):
        v = data.get(k)
        if isinstance(v, dict):
            _from_data(v, out, texts, depth + 1)
    for k in TEXT_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            nested = _maybe_json(v)
            if nested is not None:
                _from_data(nested, out, texts, depth + 1)
            else:
                texts.append(v)


def _from_text(text: str, out: ParsedTask) -> None:
    nested = _maybe_json(text)
    if nested is not None:
        _from_data(nested, out, [], 0)
    if out.address is None:
        m = LABELED_ADDRESS.search(text)
        found = m.group(1) if m else None
        if found is None:
            all_found = ADDRESS_RE.findall(text)
            if all_found:
                found = all_found[0]
                if len(set(a.lower() for a in all_found)) > 1:
                    out.warnings.append("several addresses in the task; read the first one")
        if found:
            out.address = _addr(found, out.warnings)
            out.sources["address"] = "text"
    if "blockNumber" not in out.sources:
        m = BLOCK_RE.search(text)
        if m:
            out.block_number = int(m.group(1))
            out.sources["blockNumber"] = "text"
        elif LATEST_RE.search(text):
            out.sources["blockNumber"] = "text (latest)"
    if "targetHealthFactor" not in out.sources:
        for pattern, explicit in TARGET_PATTERNS:
            for m in pattern.finditer(text):
                t = _to_decimal(m.group(1))
                if t is None:
                    continue
                if not explicit and t <= 1:
                    continue  # "reaches HF 1.0" describes liquidation, not a target
                out.target_health_factor = t
                out.sources["targetHealthFactor"] = "text"
                return


def parse_task(text: str | None = None, data: dict | None = None) -> ParsedTask:
    out = ParsedTask()
    texts: list[str] = []
    if isinstance(data, dict):
        _from_data(data, out, texts)
    if text:
        texts.append(text)
    for t in texts:
        _from_text(t, out)
    if out.address is None:
        raise TaskError(
            "no BNB Smart Chain address found in the task: send a 0x address (40 hex characters) of a Venus "
            "Core Pool account, e.g. {\"address\": \"0x...\", \"blockNumber\": 124010796, "
            "\"targetHealthFactor\": 2.5} or \"Account: 0x... restore a health factor of 2.5\""
        )
    if out.target_health_factor <= 1:
        raise TaskError(
            f"a target health factor of {out.target_health_factor} is at or below liquidation (1.0); "
            "state a target above 1, e.g. \"restore it to 2.5\""
        )
    if out.target_health_factor > 100:
        raise TaskError("target health factor must be at most 100")
    if out.block_number is not None and out.block_number <= 0:
        raise TaskError("block number must be positive")
    if "targetHealthFactor" not in out.sources:
        out.sources["targetHealthFactor"] = "default"
        out.warnings.append(f"no target health factor stated; repayment shown restores {DEFAULT_TARGET}")
    if "blockNumber" not in out.sources:
        out.sources["blockNumber"] = "default (latest)"
    return out
