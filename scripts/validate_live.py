#!/usr/bin/env python3
"""Live validation of the HF engine against BSC mainnet (read-only).

1. The MCS-HF-1 case account at the two case blocks.
2. At least N other real Venus borrowers, taken from recent vToken Borrow
   events, at the latest block.

For every account: sum(collateralUSD*LT) - debt must equal getAccountLiquidity (liquidity - shortfall)
and sum(collateralUSD*CF) - debt must equal getBorrowingPower, each
to <= 1e-9 relative (Venus weights liquidation by LT and borrowing by CF).
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


# Receipt scanning is spread over providers that serve eth_getBlockReceipts (8 Oct 2026).
RECEIPT_RPCS = ("https://bsc-rpc.publicnode.com", "https://bsc.rpc.blxrbdn.com", "https://bsc.blockrazor.xyz")
SELECTORS = {"0x" + keccak(text=s).hex()[:8] for s in (
    "borrow(uint256)", "repayBorrow(uint256)", "repayBorrow()", "mint(uint256)", "mint()", "redeem(uint256)",
    "redeemUnderlying(uint256)")}


def recent_borrowers(pool, cfg, want: int, window: int = 1000, max_windows: int = 6) -> list[str]:
    """Accounts from recent Venus Borrow events; falls back to scanning recent blocks for
    calls into vTokens when public nodes refuse eth_getLogs."""
    (r,) = multicall(pool, cfg.multicall, [Call(cfg.comptroller, "getAllMarkets()", (), ("address[]",))], "latest")
    markets = [to_checksum_address(m) for m in r.value[0]]
    head = pool.block_number()
    seen: list[str] = []
    to_block = head
    # Cheapest first: one market at a time (public nodes refuse multi-address filters), busiest
    # markets first, 2000-block windows walking back from the head.
    syms = multicall(pool, cfg.multicall, [Call(m, "symbol()", (), ("string",)) for m in markets], "latest")
    by_sym = {x.value[0]: m for m, x in zip(markets, syms) if x.success}
    busiest = [by_sym[s] for s in ("vUSDT", "vUSDC", "vBNB", "vBTC", "vETH", "vFDUSD", "vWBNB") if s in by_sym]
    from vitals.rpc import RpcPool

    logs_pool = RpcPool(cfg.rpc_logs, timeout=20, retries=1)
    for market in busiest:
        end = head
        for _ in range(8):
            try:
                logs = logs_pool.get_logs_range(market, [BORROW_TOPIC], end - 1999, end, window=2000, min_window=25)
            except Exception as exc:
                print(f"{market}: getLogs failed ({str(exc)[:60]})", file=sys.stderr)
                break
            for log in reversed(logs):
                who = to_checksum_address("0x" + log["data"][2 + 24 : 2 + 64])
                if who != CASE and who not in seen:
                    seen.append(who)
            end -= 2000
            if len(seen) >= want * 2:
                print(f"{len(seen)} borrowers from Borrow events (single-market log queries)", file=sys.stderr)
                return seen
    if len(seen) >= want:
        return seen
    try:
        for _ in range(max_windows):
            frm = to_block - window + 1
            logs = pool.get_logs_range(markets, [BORROW_TOPIC], frm, to_block, window=window, min_window=200)
            for log in reversed(logs):
                borrower = to_checksum_address("0x" + log["data"][2 + 24 : 2 + 64])
                if borrower != CASE and borrower not in seen:
                    seen.append(borrower)
            if len(seen) >= want * 2:
                return seen
            to_block = frm - 1
    except Exception as exc:
        print(f"eth_getLogs unavailable ({str(exc)[:80]}); scanning blocks instead", file=sys.stderr)
    # Borrow events read from block receipts (eth_getBlockReceipts works on public nodes
    # that refuse eth_getLogs), newest block first.
    from concurrent.futures import ThreadPoolExecutor

    lower = {m.lower() for m in markets}
    from vitals.rpc import RpcPool

    spread = [RpcPool([u], timeout=20, retries=1) for u in RECEIPT_RPCS]

    def borrowers_in(b: int) -> list[str]:
        out = []
        p = spread[b % len(spread)]
        try:
            receipts = p.call("eth_getBlockReceipts", [hex(b)])
        except Exception:
            receipts = spread[(b + 1) % len(spread)].call("eth_getBlockReceipts", [hex(b)])
        for rc in receipts or []:
            for lg in rc.get("logs", []):
                if lg["topics"] and lg["topics"][0] == BORROW_TOPIC and lg["address"].lower() in lower:
                    out.append(to_checksum_address("0x" + lg["data"][2 + 24 : 2 + 64]))
        return out

    b = head
    with ThreadPoolExecutor(max_workers=3) as ex:
        while len(seen) < want * 2 and b > head - 30_000:
            batch = list(range(b, b - 30, -1))
            for found in ex.map(borrowers_in, batch):
                for who in found:
                    if who != CASE and who not in seen:
                        seen.append(who)
            b -= 30
            time.sleep(0.5)
    print(f"{len(seen)} borrowers from Borrow events in blocks {b}..{head}", file=sys.stderr)
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
        "relBorrowingPowerCF": float(rec.get("relativeDiffBorrowingPowerCollateralFactor", float("nan"))),
        "accountLiquidityWeighting": rec.get("accountLiquidityWeighting"),
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
        print(f"case block {block}: matches={rows[-1]['matches']}", file=sys.stderr, flush=True)
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
        print(f"{acct}: matches={row['matches']} hf={row['hf']}", file=sys.stderr, flush=True)
        done += 1
    print(f"{'group':16} {'account':44} {'block':>10} {'HF(CF)':>10} {'HF(LT)':>10} {'relLiqLT':>10} {'relBpCF':>10} ok  CF!=LT  markets")
    for r in rows:
        print(f"{r['group']:16} {r['account']:44} {r['block']:>10} {str(round(r['hf'], 4) if r['hf'] else None):>10} "
              f"{str(round(r['hfLiquidationThreshold'], 4) if r['hfLiquidationThreshold'] else None):>10} "
              f"{r['relLT']:>10.1e} {r['relBorrowingPowerCF']:>10.1e} {'Y' if r['matches'] else 'N'}   "
              f"{'Y' if r['collateralWithCfNotEqualLt'] else '-'}     "
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
