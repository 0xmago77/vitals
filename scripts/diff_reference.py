#!/usr/bin/env python3
"""Ask a reference HF agent (default: Marque's Keel, ERC-8004 agent 341556) the
MCS-HF-1 question in Marque's exact wire format, compute the same answer with
the Vitals engine, and diff every graded field with the MCS-HF-1 tolerances.

    .venv312/bin/python scripts/diff_reference.py [--card URL] [--block N ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from marque_format import HF1_ADDRESS, HF1_TARGET, a2a_body, extract_json_payload, grade_hf, hf_prompt  # noqa: E402
from vitals.config import Config  # noqa: E402
from vitals.rpc import pool_from_config  # noqa: E402
from vitals.venus import compute, read_account, repay_to_target  # noqa: E402

GRADED = ["healthFactor", "primaryCollateralSymbol", "primaryCollateralFactor",
          "primaryLiquidationPriceUsd", "repayUsdToReachTarget"]


def ground_truth(calc, target: float) -> dict:
    """Marque's hfGroundTruth, computed from our chain reads."""
    w, b = float(calc.weighted_cf), float(calc.total_debt)
    cands = [m for m in calc.markets if m.raw.entered and m.supplied_usd > 0 and m.cf > 0]
    cands.sort(key=lambda m: -m.supplied_usd)
    return {
        "healthFactor": round(w / b * 1000) / 1000,
        "weightedCollateralUsd": w,
        "totalBorrowedUsd": b,
        "targetHealthFactor": target,
        "repayUsd": float(repay_to_target(calc.weighted_cf, calc.total_debt, Decimal(str(target)))),
        "primaryCollateral": cands[0].raw.underlying_symbol if cands else None,
        "markets": [{"underlyingSymbol": m.raw.underlying_symbol, "collateralFactor": float(m.cf),
                     "liquidationPriceUsd": None if m.liquidation_price_collateral_only is None
                     else float(m.liquidation_price_collateral_only)} for m in calc.markets if m.raw.entered],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default="https://marque.trade/agents/keel/.well-known/agent-card.json")
    ap.add_argument("--block", type=int, action="append")
    ap.add_argument("--ours", default=None, help="also query a Vitals endpoint card URL")
    args = ap.parse_args()
    blocks = args.block or [124010796, 122829508]
    cfg = Config.from_env()
    pool = pool_from_config(cfg)
    card = requests.get(args.card, timeout=20).json()
    endpoint = card["url"]
    ok_all = True
    for block in blocks:
        prompt = hf_prompt(HF1_ADDRESS, block, HF1_TARGET)
        t = time.time()
        resp = requests.post(endpoint, json=a2a_body(prompt), headers={"content-type": "application/json"},
                             timeout=60)
        ref_ms = int((time.time() - t) * 1000)
        ref = extract_json_payload(resp.json())
        calc = compute(read_account(pool, cfg, HF1_ADDRESS, block))
        gt = ground_truth(calc, HF1_TARGET)
        from vitals.venus import build_report
        ours = build_report(calc, Decimal(str(HF1_TARGET)))
        print(f"\n== block {block}  reference {card.get('name')} answered http {resp.status_code} in {ref_ms} ms")
        print(f"{'field':28} {'reference':>22} {'vitals':>22}")
        for f in GRADED:
            print(f"{f:28} {str(ref.get(f) if isinstance(ref, dict) else None):>22} {str(ours.get(f)):>22}")
        for who, payload in (("reference", ref), ("vitals", ours)):
            diffs = grade_hf(gt, payload if isinstance(payload, dict) else {})
            verdict = all(d["pass"] for d in diffs)
            ok_all &= verdict if who == "vitals" else True
            print(f"  graded vs Marque ground truth ({who}): {'PASS' if verdict else 'FAIL'} "
                  + ", ".join(f"{d['field']}={'ok' if d['pass'] else 'FAIL'}" for d in diffs))
        if isinstance(ref, dict):
            # Field-by-field agreement between the two agents, under MCS-HF-1 tolerances.
            cross = grade_hf({**gt, "healthFactor": float(ref["healthFactor"])}, ours) if ref.get("healthFactor") is not None else []
            print("  vitals graded against the reference's own numbers:",
                  ", ".join(f"{d['field']}={'ok' if d['pass'] else 'FAIL'}" for d in cross))
            print("  reference extra fields:", json.dumps({k: v for k, v in ref.items() if k not in GRADED})[:400])
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
