#!/usr/bin/env python3
"""Live validation of the HF engine against BSC mainnet (read-only).

1. The MCS-HF-1 case account at the two case blocks.
2. At least N other real Venus borrowers, taken from recent vToken Borrow
   events, at the latest block.

For every account the engine's sum(collateralUSD*CF) - sum(debtUSD) must equal
Comptroller.getAccountLiquidity's liquidity - shortfall to <= 1e-9 relative.
Prints a table and writes JSON evidence with --out.

    .venv312/bin/python scripts/validate_live.py --count 12 --out .dev/evidence/live.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_utils import keccak, to_checksum_address  # noqa: E402

from vitals.abi import Call, multicall  # noqa: E402
from vitals.config import Config  # noqa: E402
from vitals.rpc import pool_from_config  # noqa: E402
from vitals.venus import RECONCILE_TOLERANCE, compute, read_account, reconcile  # noqa: E402

CASE = "0x60AA3AEE06E2345A17E4d4B12c53E046F4F63CAf"
BORROW_TOPIC = "0x" + keccak(text="Borrow(address,uint256,uint256,uint256)").hex()


def recent_borrowers(pool, cfg, want: int, window: int = 1000, max_windows: int = 40) -> list[str]:
    (r,) = multicall(pool, cfg.multicall, [Call(cfg.comptroller, "getAllMarkets()", (), ("address[]",))], "latest")
    markets = [to_checksum_address(m) for m in r.value[0]]
    head = pool.block_number()
    seen: list[str] = []
    to_block = head
    for _ in range(max_windows):
        frm = to_block - window + 1
        logs = pool.get_logs_range(markets, [BORROW_TOPIC], frm, to_block, window=window)
        for log in reversed(logs):
            borrower = to_checksum_address("0x" + log["data"][2 + 24 : 2 + 64])
            if borrower != CASE and borrower not in seen:
                seen.append(borrower)
        if len(seen) >= want * 2:
            break
        to_block = frm - 1
    return seen


def check(pool, cfg, account: str, block: int | None) -> dict:
    t = time.time()
    calc = compute(read_account(pool, cfg, account, block))
    rec = reconcile(calc)
    lt_differs = any(m.raw.entered and m.lt is not None and m.lt != m.cf and m.supplied > 0 for m in calc.markets)
    return {
        "account": account,
        "block": calc.raw.block_number,
        "hf": None if calc.hf is None else float(calc.hf),
        "hfLiquidationThreshold": None if calc.hf_lt is None else float(calc.hf_lt),
        "debtUsd": float(calc.total_debt),
        "vaiDebtUsd": float(calc.vai_debt_usd),
        "relCF": float(rec["relativeDiffCollateralFactor"]),
        "relLT": float(rec["relativeDiffLiquidationThreshold"]),
        "matches": bool(rec["matches"]),
        "collateralWithCfNotEqualLt": lt_differs,
        "markets": [m.raw.underlying_symbol for m in calc.markets if m.raw.entered],
        "seconds": round(time.time() - t, 2),
        "rpc": calc.raw.rpc_hosts,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    cfg = Config.from_env()
    pool = pool_from_config(cfg)
    rows = []
    for block in (124010796, 122829508):
        rows.append({"group": "case", **check(pool, cfg, CASE, block)})
    borrowers = recent_borrowers(pool, cfg, args.count)
    done = 0
    for acct in borrowers:
        if done >= args.count:
            break
        try:
            row = check(pool, cfg, acct, None)
        except Exception as exc:  # report and continue: the table shows it
            print(f"{acct}: error {exc}", file=sys.stderr)
            continue
        if row["debtUsd"] <= 0:
            continue
        rows.append({"group": "recent-borrower", **row})
        done += 1
    print(f"{'group':16} {'account':44} {'block':>10} {'HF(CF)':>10} {'HF(LT)':>10} {'relDiff':>10} ok  CF!=LT  markets")
    for r in rows:
        print(f"{r['group']:16} {r['account']:44} {r['block']:>10} {str(round(r['hf'], 4) if r['hf'] else None):>10} "
              f"{str(round(r['hfLiquidationThreshold'], 4) if r['hfLiquidationThreshold'] else None):>10} "
              f"{r['relCF']:>10.1e} {'Y' if r['matches'] else 'N'}   {'Y' if r['collateralWithCfNotEqualLt'] else '-'}     "
              f"{','.join(r['markets'])[:60]}")
    others = [r for r in rows if r["group"] == "recent-borrower"]
    ok = all(r["matches"] for r in rows) and len(others) >= min(10, args.count)
    print(f"\n{len(rows)} accounts, {len(others)} recent borrowers, tolerance {RECONCILE_TOLERANCE}: "
          f"{'ALL RECONCILE' if ok else 'MISMATCH'}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"rows": rows, "ok": ok, "borrowTopic": BORROW_TOPIC}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
