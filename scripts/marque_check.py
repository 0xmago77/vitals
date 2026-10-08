#!/usr/bin/env python3
"""Run Marque's MCS-HF-1 harness flow against a Vitals server, offline.

Reproduces Marque's adapters exactly (A2A: GET card -> POST message/send to
card.url with the HF-1 prompt; MCP: initialize, notifications/initialized,
tools/list, tools/call with argumentsForCase) and grades the parsed payload
with Marque's gradeHf against the reference answers in
tests/fixtures/hf1_reference.json (Keel's numbers).

    .venv312/bin/python scripts/marque_check.py --base http://127.0.0.1:9000
    .venv312/bin/python scripts/marque_check.py --base https://vitals.43-165-190-110.sslip.io --public
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from marque_format import (MCP_HEADERS, a2a_body, extract_json_payload, grade_hf, hf_prompt,  # noqa: E402
                           mcp_arguments_for, mcp_initialize)

WANTED = re.compile(r"position|liquid|health|factor|yield|apr|apy|grid|rebalanc|venus|pancake", re.I)


def ground_truth(case: dict, target: float) -> dict:
    k = case["keel"]
    return {"healthFactor": k["healthFactor"], "weightedCollateralUsd": case["weightedCollateralUsd"],
            "totalBorrowedUsd": case["totalBorrowedUsd"], "targetHealthFactor": target,
            "primaryCollateral": k["primaryCollateralSymbol"], "markets": case["markets"]}


def run_a2a(base: str, public: bool, prompt: str) -> tuple[dict | None, dict]:
    card_url = f"{base}/.well-known/agent-card.json"
    t = time.time()
    card = requests.get(card_url, timeout=15).json()
    endpoint = card["url"] if public else card["url"].replace(card["url"].split("/a2a")[0], base)
    r = requests.post(endpoint, data=json.dumps(a2a_body(prompt)), headers={"content-type": "application/json"},
                      timeout=45)
    meta = {"endpoint": endpoint, "status": r.status_code, "contentType": r.headers.get("content-type"),
            "latencyMs": int((time.time() - t) * 1000)}
    try:
        return extract_json_payload(r.json()), meta
    except ValueError:
        return None, meta


def run_mcp(base: str, prompt: str, block: int) -> tuple[dict | None, dict]:
    url = f"{base}/mcp"
    t = time.time()
    h = dict(MCP_HEADERS)
    r = requests.post(url, data=json.dumps(mcp_initialize()), headers=h, timeout=20)
    meta = {"initialize": r.status_code}
    sid = r.headers.get("mcp-session-id")
    if sid:
        h["mcp-session-id"] = sid
    r = requests.post(url, data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}),
                      headers=h, timeout=20)
    meta["initialized"] = r.status_code
    r = requests.post(url, data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                      headers=h, timeout=20)
    tools = r.json()["result"]["tools"]
    pick = None
    for tool in tools:
        args = mcp_arguments_for(tool, prompt, block)
        if WANTED.search(f"{tool.get('name', '')} {tool.get('description', '')}") and args is not None:
            pick = (tool, args)
            break
    if pick is None:
        return None, {**meta, "error": "no compatible tool"}
    meta["tool"] = pick[0]["name"]
    r = requests.post(url, data=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                            "params": {"name": pick[0]["name"], "arguments": pick[1]}}),
                      headers=h, timeout=45)
    meta.update({"status": r.status_code, "latencyMs": int((time.time() - t) * 1000)})
    return extract_json_payload(r.json()), meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:9000")
    ap.add_argument("--public", action="store_true", help="POST to the card's url as published")
    args = ap.parse_args()
    ref = json.loads((ROOT / "tests/fixtures/hf1_reference.json").read_text())
    target = ref["targetHealthFactor"]
    ok_all = True
    for block, case in ref["cases"].items():
        prompt = hf_prompt(ref["address"], int(block), target)
        gt = ground_truth(case, target)
        for face, fn in (("A2A", lambda: run_a2a(args.base, args.public, prompt)),
                         ("MCP", lambda: run_mcp(args.base, prompt, int(block)))):
            payload, meta = fn()
            diffs = grade_hf(gt, payload or {})
            passed = bool(payload) and all(d["pass"] for d in diffs)
            ok_all &= passed
            print(f"{face} block {block}: {'PASS' if passed else 'FAIL'} {json.dumps(meta)}")
            for d in diffs:
                print(f"    {'ok  ' if d['pass'] else 'FAIL'} {d['field']:24} expected {d['expected']}  got {d['actual']}")
    print("ALL PASS" if ok_all else "SOME FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
