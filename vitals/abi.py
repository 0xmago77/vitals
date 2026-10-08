"""Minimal ABI encoding and a Multicall3 `aggregate3` reader."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Sequence

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address


@lru_cache(maxsize=512)
def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _arg_types(signature: str) -> list[str]:
    inner = signature[signature.index("(") + 1 : signature.rindex(")")]
    if not inner:
        return []
    # Only flat argument lists are used in this codebase (no tuples as inputs
    # except Multicall3's, which is encoded separately).
    return [t.strip() for t in inner.split(",")]


def encode_call(signature: str, *args: Any) -> bytes:
    types = _arg_types(signature)
    return selector(signature) + (encode(types, list(args)) if types else b"")


def encode_call_hex(signature: str, *args: Any) -> str:
    return "0x" + encode_call(signature, *args).hex()


def decode_result(types: Sequence[str], data: bytes | str) -> tuple:
    if isinstance(data, str):
        data = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    return decode(list(types), data)


def topic_address(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().replace("0x", "")


def address_from_topic(topic: str) -> str:
    return to_checksum_address("0x" + topic[-40:])


@dataclass
class Call:
    target: str
    signature: str
    args: tuple = ()
    returns: tuple[str, ...] = ()
    allow_failure: bool = True
    # Accept and decode only the first `len(returns)` words of a longer answer
    # (for structs that grew fields across upgrades).
    prefix: bool = False


@dataclass
class CallResult:
    success: bool
    raw: bytes
    value: tuple | None


AGGREGATE3 = "aggregate3((address,bool,bytes)[])"


def multicall(pool, multicall_addr: str, calls: Sequence[Call], block: int | str, *, purpose: str | None = None,
              chunk: int = 120) -> list[CallResult]:
    """Run `calls` through Multicall3.aggregate3 at one block, decoding each."""
    out: list[CallResult] = []
    for start in range(0, len(calls), chunk):
        part = calls[start : start + chunk]
        payload = [(to_checksum_address(c.target), c.allow_failure, encode_call(c.signature, *c.args)) for c in part]
        data = selector(AGGREGATE3) + encode(["(address,bool,bytes)[]"], [payload])
        raw = pool.eth_call(multicall_addr, "0x" + data.hex(), block, purpose=purpose)
        (results,) = decode_result(["(bool,bytes)[]"], raw)
        for c, (ok, ret) in zip(part, results):
            value = None
            if ok and c.returns:
                try:
                    if c.prefix:
                        need = 32 * len(c.returns)
                        value = decode(list(c.returns), ret[:need]) if len(ret) >= need else None
                    else:
                        value = decode(list(c.returns), ret)
                except Exception:
                    value = None
            out.append(CallResult(success=bool(ok) and (value is not None or not c.returns), raw=bytes(ret), value=value))
    return out
