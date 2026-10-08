"""Byte-for-byte reproduction of the requests Marque's MCS-HF-1 harness sends.

Source: Marque's open-source conformance package (packages/conformance/src/
tests/mcs-hf-1.ts `hfPrompt` and adapters.ts `a2aAdapter` / `mcpAdapter`).
Used by the tests and by scripts/diff_reference.py.
"""

from __future__ import annotations

import json

HF1_ADDRESS = "0x60AA3AEE06E2345A17E4d4B12c53E046F4F63CAf"
HF1_TARGET = 2.5
HF1_STATEMENT = (
    "report the account’s current health factor and the exact USD of debt that must be repaid "
    "to restore a health factor of 2.5"
)
HF1_POLICY = {"targetHealthFactor": HF1_TARGET, "statement": HF1_STATEMENT}


def _fmt_target(t: float) -> str:
    # JS template literal rendering of a number: 2.5 -> "2.5", 2 -> "2".
    return str(int(t)) if float(t).is_integer() else repr(float(t))


def hf_prompt(address: str = HF1_ADDRESS, block: int = 124010796, target: float = HF1_TARGET,
              statement: str = HF1_STATEMENT) -> str:
    return "\n".join([
        "You are given a Venus Core lending account on BNB Smart Chain (chain 56).",
        "",
        f"Account: {address}",
        f"Block: {block} — answer for this block only.",
        "",
        f"POLICY YOU MUST FOLLOW: {statement}",
        "",
        "Return strict JSON with exactly these fields:",
        "{",
        '  "healthFactor": <number to 3 decimal places>,',
        '  "primaryCollateralSymbol": <string, the underlying symbol of the largest collateral market>,',
        '  "primaryCollateralFactor": <number 0..1, that market\'s collateral factor>,',
        '  "primaryLiquidationPriceUsd": <number, price at which this account reaches HF 1.0>,',
        f'  "repayUsdToReachTarget": <number, USD of debt to repay to reach HF {_fmt_target(target)}>',
        "}",
        "",
        "No prose. JSON only.",
    ])


def a2a_body(prompt: str, message_id: str = "marque-mcs-1760000000000") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": {"role": "user", "messageId": message_id, "parts": [{"kind": "text", "text": prompt}]}},
    }


def mcp_initialize() -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "marque-mcs", "version": "0.1.0"}}}


MCP_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


def mcp_arguments_for(tool: dict, prompt: str, block: int, address: str = HF1_ADDRESS) -> dict | None:
    """Port of adapters.ts argumentsForCase for the HF-1 case."""
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object" or not schema.get("properties"):
        return None
    task = {"testId": "MCS-HF-1", "subject": {"address": address}, "policy": HF1_POLICY, "blockNumber": str(block)}
    values = {
        "query": prompt, "prompt": prompt, "message": prompt, "task": task, "input": task,
        "address": address, "subject": address, "wallet": address, "account": address,
        "blockNumber": str(block), "block_number": str(block), "chainId": 56, "chain_id": 56,
        "policy": HF1_POLICY,
    }
    args = {k: values[k] for k in schema["properties"] if k in values}
    required = [k for k in schema.get("required", []) if isinstance(k, str)]
    return args if all(k in args for k in required) else None


def extract_json_payload(body):
    """Port of adapters.ts extractJsonPayload: what the grader actually reads."""
    if body is None:
        return None
    if isinstance(body, dict):
        if "result" in body:
            return extract_json_payload(body["result"])
        for key in ("artifacts", "parts", "messages"):
            arr = body.get(key)
            if isinstance(arr, list):
                for item in arr:
                    found = extract_json_payload(item)
                    if isinstance(found, dict):
                        return found
        if isinstance(body.get("text"), str):
            return extract_json_payload(body["text"])
        return body
    if isinstance(body, str):
        import re
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", body)
        candidate = fenced.group(1) if fenced else body
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except ValueError:
                return None
    return None


def grade_hf(gt: dict, r: dict) -> list[dict]:
    """Port of mcs-hf-1.ts gradeHf. gt keys: healthFactor (round3), weightedCollateralUsd,
    totalBorrowedUsd, targetHealthFactor, markets[{underlyingSymbol, collateralFactor,
    liquidationPriceUsd}], primaryCollateral."""

    def num(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            cleaned = v.replace(",", "").replace("%", "").replace(" ", "")
            try:
                return float(cleaned) if cleaned else None
            except ValueError:
                return None
        return None

    diffs = []
    hf = num(r.get("healthFactor"))
    diffs.append({"field": "healthFactor", "pass": hf is not None and abs(hf - gt["healthFactor"]) <= 0.005,
                  "expected": gt["healthFactor"], "actual": hf})
    sym = r.get("primaryCollateralSymbol")
    sym = sym.strip() if isinstance(sym, str) and sym.strip() else None
    market = next((m for m in gt["markets"] if sym and m["underlyingSymbol"].lower() == sym.lower()), None)
    expected_market = next((m for m in gt["markets"] if m["underlyingSymbol"] == gt["primaryCollateral"]), None)
    diffs.append({"field": "primaryCollateralSymbol", "pass": sym is not None and market is not None,
                  "expected": gt["primaryCollateral"], "actual": sym})
    ref = market or expected_market
    if ref is not None:
        cf = num(r.get("primaryCollateralFactor"))
        diffs.append({"field": "collateralFactor",
                      "pass": cf is not None and abs(cf - ref["collateralFactor"]) <= 0.001,
                      "expected": ref["collateralFactor"], "actual": cf})
        liq = ref.get("liquidationPriceUsd")
        if liq is not None:
            got = num(r.get("primaryLiquidationPriceUsd"))
            ok = got is not None and abs(got - liq) / abs(liq) * 100 <= 1
            diffs.append({"field": "liquidationPrice", "pass": ok, "expected": liq, "actual": got})
    repay = num(r.get("repayUsdToReachTarget"))
    if repay is None:
        diffs.append({"field": "repayToTarget", "pass": False, "expected": "?", "actual": None})
    else:
        left = gt["totalBorrowedUsd"] - repay
        reached = gt["weightedCollateralUsd"] / left if left > 0 else float("inf")
        diffs.append({"field": "repayToTarget", "pass": abs(reached - gt["targetHealthFactor"]) <= 0.005,
                      "expected": gt["targetHealthFactor"], "actual": reached})
    return diffs
