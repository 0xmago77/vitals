"""Own-position keeper: a small Venus Core position (BNB collateral, USDT debt)
held by the agent wallet and kept near a target health factor.

Daily cycle (at a randomised minute inside a configured UTC hour):
  * HF < emergency (1.3)  -> repay all USDT on hand
  * HF < low (1.85)       -> repay USDT back to the target (2.0)
  * HF > high (2.15)      -> borrow back to the target, only if HF after >= 1.9
  * otherwise, if no keeper tx in the last 20 h -> maintenance repay of the
    accrued interest plus at least 0.01 USDT
Hard limits: max borrow per tx, total debt cap, minimum HF after a borrow,
BNB gas reserve, gas price cap. Every tx is simulated with eth_call first,
then sent, then confirmed by receipt and reconciled. Dry run unless VITALS_LIVE=1.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from eth_utils import keccak, to_checksum_address

from .abi import Call, decode_result, encode_call, encode_call_hex, multicall
from .chain import WRITE_LOCK, GasGuard, WriteRefused, pool_web3, reset_sdk_nonces
from .rpc import RpcPool, RpcRevert
from .venus import CTX, compute, read_account

log = logging.getLogger("vitals.keeper")
WAD = Decimal(10) ** 18
FAILURE_TOPIC = "0x" + keccak(text="Failure(uint256,uint256,uint256)").hex()
EXPLORER = "https://bscscan.com"


@dataclass
class Limits:
    target_hf: Decimal = Decimal("2.0")
    low_hf: Decimal = Decimal("1.85")
    high_hf: Decimal = Decimal("2.15")
    min_borrow_hf: Decimal = Decimal("1.9")
    emergency_hf: Decimal = Decimal("1.3")
    max_borrow_per_tx: Decimal = Decimal("3")
    debt_cap: Decimal = Decimal("5")
    maintenance_min: Decimal = Decimal("0.01")
    idle_hours: Decimal = Decimal("20")

    @classmethod
    def from_config(cls, cfg) -> "Limits":
        return cls(cfg.keeper_target_hf, cfg.keeper_low_hf, cfg.keeper_high_hf, cfg.keeper_min_borrow_hf,
                   cfg.keeper_emergency_hf, cfg.keeper_max_borrow_per_tx, cfg.keeper_debt_cap,
                   cfg.keeper_maintenance_min, cfg.keeper_idle_hours)


@dataclass
class KeeperState:
    weighted_collateral_usd: Decimal  # sum(collateral USD * CF)
    debt_usd: Decimal  # all debt, USD
    usdt_debt: Decimal  # USDT tokens owed (current, interest accrued)
    usdt_price: Decimal  # USD per USDT (oracle)
    usdt_wallet: Decimal  # USDT tokens held by the wallet
    now: int
    last_tx_ts: int | None = None
    debt_after_last_action: Decimal | None = None

    @property
    def hf(self) -> Decimal | None:
        return CTX.divide(self.weighted_collateral_usd, self.debt_usd) if self.debt_usd > 0 else None

    def hf_after(self, delta_usdt: Decimal) -> Decimal | None:
        """HF after borrowing (delta > 0) or repaying (delta < 0) USDT."""
        debt = self.debt_usd + delta_usdt * self.usdt_price
        return CTX.divide(self.weighted_collateral_usd, debt) if debt > 0 else None


@dataclass
class Decision:
    action: str  # none | repay | borrow | maintenance_repay | emergency_repay
    amount: Decimal = Decimal(0)
    reason: str = ""
    refused: list[str] = field(default_factory=list)


def q6(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def check_borrow(amount: Decimal, s: KeeperState, lim: Limits) -> list[str]:
    """Every limit a borrow must respect; an empty list means allowed."""
    out = []
    if amount <= 0:
        out.append("borrow amount must be positive")
    if amount > lim.max_borrow_per_tx:
        out.append(f"borrow {amount} USDT exceeds the per-tx limit {lim.max_borrow_per_tx} USDT")
    if s.usdt_debt + amount > lim.debt_cap:
        out.append(f"debt after borrow {s.usdt_debt + amount} USDT exceeds the cap {lim.debt_cap} USDT")
    after = s.hf_after(amount)
    if after is None or after < lim.min_borrow_hf:
        out.append(f"health factor after borrow {None if after is None else round(after, 4)} is below "
                   f"{lim.min_borrow_hf}")
    return out


def check_repay(amount: Decimal, s: KeeperState) -> list[str]:
    out = []
    if amount <= 0:
        out.append("repay amount must be positive")
    if amount > s.usdt_wallet:
        out.append(f"repay {amount} USDT exceeds USDT on hand {s.usdt_wallet}")
    if amount > s.usdt_debt + Decimal("0.000001"):
        out.append(f"repay {amount} USDT exceeds the debt {s.usdt_debt}")
    return out


def decide(s: KeeperState, lim: Limits) -> Decision:
    hf = s.hf
    if hf is None:
        return Decision("none", reason="no debt: open a position first (keeper open)")
    if hf < lim.emergency_hf:
        amount = q6(min(s.usdt_wallet, s.usdt_debt))
        d = Decision("emergency_repay", amount, f"HF {hf:.4f} < emergency {lim.emergency_hf}: repay all USDT on hand")
        d.refused = check_repay(amount, s)
        return d
    if hf < lim.low_hf:
        need_usd = s.debt_usd - CTX.divide(s.weighted_collateral_usd, lim.target_hf)
        need = q6(CTX.divide(need_usd, s.usdt_price)) + Decimal("0.000001")
        amount = q6(min(need, s.usdt_wallet, s.usdt_debt))
        d = Decision("repay", amount, f"HF {hf:.4f} < {lim.low_hf}: repay {amount} USDT toward {lim.target_hf}")
        d.refused = check_repay(amount, s)
        return d
    if hf > lim.high_hf:
        room_usd = CTX.divide(s.weighted_collateral_usd, lim.target_hf) - s.debt_usd
        want = q6(CTX.divide(room_usd, s.usdt_price))
        amount = q6(min(want, lim.max_borrow_per_tx, max(Decimal(0), lim.debt_cap - s.usdt_debt)))
        d = Decision("borrow", amount, f"HF {hf:.4f} > {lim.high_hf}: borrow {amount} USDT back toward {lim.target_hf}")
        d.refused = check_borrow(amount, s, lim)
        if d.refused and amount <= 0:
            return Decision("none", reason=f"HF {hf:.4f} > {lim.high_hf} but the debt cap leaves no room to borrow",
                            refused=d.refused)
        return d
    idle = None if s.last_tx_ts is None else (s.now - s.last_tx_ts) / 3600
    if idle is None or Decimal(str(idle)) >= lim.idle_hours:
        accrued = Decimal(0)
        if s.debt_after_last_action is not None and s.usdt_debt > s.debt_after_last_action:
            accrued = s.usdt_debt - s.debt_after_last_action
        amount = q6(min(accrued + lim.maintenance_min, s.usdt_wallet, s.usdt_debt))
        d = Decision("maintenance_repay", amount,
                     f"no keeper tx for {'ever' if idle is None else f'{idle:.1f} h'}: repay accrued interest "
                     f"{q6(accrued)} + {lim.maintenance_min} USDT")
        d.refused = check_repay(amount, s)
        if amount < lim.maintenance_min:
            d.refused.append(f"maintenance repay {amount} is below the minimum {lim.maintenance_min} USDT")
        return d
    return Decision("none", reason=f"HF {hf:.4f} inside [{lim.low_hf}, {lim.high_hf}] and last tx {idle:.1f} h ago")


def scheduled_minute(date: dt.date, address: str, hour: int) -> dt.datetime:
    """A randomised but stable run time for a UTC day: hour:MM with MM drawn per (date, wallet)."""
    rnd = random.Random(f"{date.isoformat()}:{address.lower()}")
    return dt.datetime(date.year, date.month, date.day, hour, rnd.randint(0, 59), rnd.randint(0, 59),
                       tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------- chain


class Keeper:
    def __init__(self, cfg, pool: RpcPool, db, account=None, *, write_pool: RpcPool | None = None):
        self.cfg = cfg
        self.pool = pool
        self.write_pool = write_pool or pool
        self.db = db
        self.account = account
        self.address = account.address if account is not None else to_checksum_address(cfg.owner)
        self.limits = Limits.from_config(cfg)
        self.guard = GasGuard(self.write_pool, cfg.gas_price_cap_wei, cfg.bnb_reserve_wei, cfg.live)
        self._markets: dict[str, str] | None = None
        self.w3 = pool_web3(self.pool, self.write_pool, cfg.log_window)

    @property
    def live(self) -> bool:
        return bool(self.cfg.live) and self.account is not None

    def markets(self) -> dict[str, str]:
        """vBNB, vUSDT and USDT, discovered from the Comptroller and verified on chain."""
        if self._markets is None:
            (r,) = multicall(self.pool, self.cfg.multicall,
                             [Call(self.cfg.comptroller, "getAllMarkets()", (), ("address[]",))], "latest")
            ms = [to_checksum_address(m) for m in r.value[0]]
            res = multicall(self.pool, self.cfg.multicall, [Call(m, "symbol()", (), ("string",)) for m in ms], "latest")
            by_sym = {x.value[0]: m for m, x in zip(ms, res) if x.success}
            vbnb, vusdt = by_sym.get("vBNB"), by_sym.get("vUSDT")
            if not vbnb or not vusdt:
                raise RuntimeError("vBNB or vUSDT not found among Venus Core markets")
            chk = multicall(self.pool, self.cfg.multicall, [
                Call(vusdt, "underlying()", (), ("address",)),
                Call(vbnb, "underlying()", (), ("address",)),
            ], "latest")
            usdt = to_checksum_address(chk[0].value[0])
            sym = multicall(self.pool, self.cfg.multicall, [Call(usdt, "symbol()", (), ("string",))], "latest")[0]
            if not sym.success or sym.value[0] != "USDT" or chk[1].success:
                raise RuntimeError("market verification failed (vUSDT underlying must be USDT; vBNB must be native)")
            self._markets = {"vBNB": vbnb, "vUSDT": vusdt, "USDT": usdt}
        return self._markets

    def state(self) -> tuple[KeeperState, Any]:
        m = self.markets()
        calc = compute(read_account(self.pool, self.cfg, self.address, None))
        usdt_price = Decimal(0)
        for mk in calc.markets:
            if mk.raw.vtoken == m["vUSDT"]:
                usdt_price = mk.price_usd
        res = multicall(self.pool, self.cfg.multicall, [
            Call(m["vUSDT"], "borrowBalanceCurrent(address)", (self.address,), ("uint256",)),
            Call(m["USDT"], "balanceOf(address)", (self.address,), ("uint256",)),
            Call(m["vUSDT"], "borrowBalanceStored(address)", (self.address,),
                 ("uint256",)),
        ], "latest")
        current = Decimal(res[0].value[0]) if res[0].success else Decimal(res[2].value[0])
        stored = Decimal(res[2].value[0]) if res[2].success else current
        usdt_debt = CTX.divide(current, WAD)
        if usdt_price == 0:
            raw = self.pool.eth_call(calc.raw.oracle, encode_call_hex("getUnderlyingPrice(address)", m["vUSDT"]))
            usdt_price = CTX.divide(Decimal(int(raw, 16)), WAD)
        # Debt in USD with USDT's unaccrued interest included.
        debt_usd = calc.total_debt + CTX.multiply(CTX.divide(current - stored, WAD), usdt_price)
        last = self.db.last_keeper_tx()
        s = KeeperState(
            weighted_collateral_usd=calc.weighted_cf,
            debt_usd=debt_usd,
            usdt_debt=usdt_debt,
            usdt_price=usdt_price,
            usdt_wallet=CTX.divide(Decimal(res[1].value[0]), WAD),
            now=int(time.time()),
            last_tx_ts=int(last["ts"]) if last else None,
            debt_after_last_action=Decimal(self.db.get("keeper_debt_after_last", "0")) if last else None,
        )
        return s, calc

    # ----------------------------------------------------------- tx helpers

    def _send(self, to: str, data: bytes, *, value: int = 0, label: str, expect_zero_return: bool = True) -> dict:
        """Simulate, then (live only) sign, send, wait for the receipt and check it."""
        sender = self.address
        tx = {"from": sender, "to": to, "data": "0x" + data.hex(), "value": hex(value)}
        try:
            ret = self.pool.call("eth_call", [tx, "latest"])
        except RpcRevert as exc:
            raise WriteRefused(f"{label}: simulation reverted: {exc}") from exc
        if expect_zero_return and ret not in ("0x", None) and len(ret) >= 66:
            code = int(ret[2:66], 16)
            if code != 0:
                raise WriteRefused(f"{label}: simulation returned Venus error code {code}")
        gas_est = int(self.pool.call("eth_estimateGas", [tx]), 16)
        gas = int(gas_est * 1.25) + 10_000
        g = self.guard.check(sender, gas, value_wei=value)
        if not self.live:
            return {"dryRun": True, "label": label, "gas": gas, "gasPriceWei": g["gasPriceWei"], "simulated": True}
        with WRITE_LOCK:
            try:
                return self._sign_send_wait(to, data, value, gas, g["gasPriceWei"], label)
            finally:
                reset_sdk_nonces(sender)

    def _sign_send_wait(self, to: str, data: bytes, value: int, gas: int, gas_price: int, label: str) -> dict:
        sender = self.address
        nonce = int(self.write_pool.call("eth_getTransactionCount", [sender, "pending"]), 16)
        chain_id = int(self.pool.call("eth_chainId"), 16)
        signed = self.account.sign_transaction({
            "to": to_checksum_address(to), "data": data, "value": value, "gas": gas, "gasPrice": gas_price,
            "nonce": nonce, "chainId": chain_id,
        })
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = self.write_pool.call("eth_sendRawTransaction", ["0x" + raw.hex()])
        receipt = None
        deadline = time.time() + 180
        while time.time() < deadline:
            receipt = self.pool.call("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                break
            time.sleep(1.5)
        if not receipt:
            raise RuntimeError(f"{label}: no receipt for {tx_hash} within 180 s")
        if int(receipt["status"], 16) != 1:
            raise RuntimeError(f"{label}: tx {tx_hash} reverted")
        for lg in receipt.get("logs", []):
            if lg.get("topics") and lg["topics"][0].lower() == FAILURE_TOPIC:
                raise RuntimeError(f"{label}: tx {tx_hash} emitted a Venus Failure event")
        return {"dryRun": False, "label": label, "txHash": tx_hash, "gasUsed": int(receipt["gasUsed"], 16),
                "block": int(receipt["blockNumber"], 16)}

    def _record(self, action: str, amount: Decimal | None, res: dict, hf_before, hf_after, note: str = "") -> None:
        self.db.add_keeper_action(action, amount=None if amount is None else str(amount),
                                  tx_hash=res.get("txHash"), status="confirmed" if res.get("txHash") else
                                  ("simulated" if res.get("dryRun") else "failed"),
                                  hf_before=None if hf_before is None else str(round(hf_before, 6)),
                                  hf_after=None if hf_after is None else str(round(hf_after, 6)),
                                  dry_run=bool(res.get("dryRun", True)), note=note[:300])

    def approve_usdt(self, amount_wei: int) -> dict | None:
        m = self.markets()
        raw = self.pool.eth_call(m["USDT"], encode_call_hex("allowance(address,address)", self.address, m["vUSDT"]))
        if int(raw, 16) >= amount_wei:
            return None
        # Exact approval, never unlimited.
        return self._send(m["USDT"], encode_call("approve(address,uint256)", m["vUSDT"], amount_wei),
                          label="approve USDT", expect_zero_return=False)

    def borrow(self, amount: Decimal, s: KeeperState) -> dict:
        refused = check_borrow(amount, s, self.limits)
        if refused:
            raise WriteRefused("; ".join(refused))
        wei = int(amount * WAD)
        return self._send(self.markets()["vUSDT"], encode_call("borrow(uint256)", wei), label="borrow USDT")

    def repay(self, amount: Decimal, s: KeeperState) -> dict:
        refused = check_repay(amount, s)
        if refused:
            raise WriteRefused("; ".join(refused))
        wei = int(amount * WAD)
        appr = self.approve_usdt(wei)
        if appr and not appr.get("dryRun"):
            self._record("approve", amount, appr, s.hf, s.hf, "exact USDT approval for repay")
        if appr and appr.get("dryRun"):
            # The repay simulation needs the allowance; report the approval and stop in dry run.
            return {**appr, "label": "approve USDT (repay simulated after approval in live mode)"}
        return self._send(self.markets()["vUSDT"], encode_call("repayBorrow(uint256)", wei), label="repay USDT")

    # ------------------------------------------------------------- commands

    def open_position(self, collateral_bnb: Decimal, target_hf: Decimal | None = None) -> dict:
        target = target_hf or self.limits.target_hf
        if collateral_bnb <= 0:
            raise WriteRefused("collateral must be positive")
        if target < self.limits.min_borrow_hf:
            raise WriteRefused(f"target HF {target} is below the minimum {self.limits.min_borrow_hf}")
        m = self.markets()
        steps = []
        value = int(collateral_bnb * WAD)
        s0, _ = self.state()
        res = self._send(m["vBNB"], encode_call("mint()"), value=value, label="supply BNB", expect_zero_return=False)
        self._record("supply", collateral_bnb, res, s0.hf, None, "supply BNB to vBNB")
        steps.append(res)
        (assets,) = decode_result(["address[]"], self.pool.eth_call(
            self.cfg.comptroller, encode_call_hex("getAssetsIn(address)", self.address)))
        if m["vBNB"].lower() not in [a.lower() for a in assets]:
            res = self._send(self.cfg.comptroller, encode_call("enterMarkets(address[])", [m["vBNB"]]),
                             label="enter vBNB market", expect_zero_return=False)
            self._record("enter_market", None, res, s0.hf, None, "enterMarkets([vBNB])")
            steps.append(res)
        if not self.live:
            return {"dryRun": True, "steps": steps,
                    "note": "dry run: supply simulated; the borrow is sized after the supply lands (live mode)"}
        s, _ = self.state()
        room_usd = CTX.divide(s.weighted_collateral_usd, target) - s.debt_usd
        amount = q6(min(CTX.divide(room_usd, s.usdt_price), self.limits.max_borrow_per_tx,
                        max(Decimal(0), self.limits.debt_cap - s.usdt_debt)))
        if amount > 0:
            res = self.borrow(amount, s)
            s2, _ = self.state()
            self._record("borrow", amount, res, s.hf, s2.hf, f"open: borrow to HF {target}")
            self.db.put("keeper_debt_after_last", str(s2.usdt_debt))
            steps.append(res)
        return {"dryRun": False, "steps": steps}

    def cycle(self, *, force: bool = False) -> dict:
        s, calc = self.state()
        d = decide(s, self.limits)
        out: dict[str, Any] = {"hf": None if s.hf is None else float(s.hf), "decision": d.action,
                               "amount": str(d.amount), "reason": d.reason, "refused": d.refused,
                               "dryRun": not self.live}
        if d.action == "none" or d.refused:
            if d.refused:
                self.db.add_keeper_action(d.action, amount=str(d.amount), tx_hash=None, status="refused",
                                          hf_before=None if s.hf is None else str(round(s.hf, 6)), hf_after=None,
                                          dry_run=not self.live, note="; ".join(d.refused)[:300])
            return out
        try:
            if d.action == "borrow":
                res = self.borrow(d.amount, s)
            else:
                res = self.repay(d.amount, s)
        except WriteRefused as exc:
            out["refused"] = [str(exc)]
            self.db.add_keeper_action(d.action, amount=str(d.amount), tx_hash=None, status="refused",
                                      hf_before=None if s.hf is None else str(round(s.hf, 6)), hf_after=None,
                                      dry_run=not self.live, note=str(exc)[:300])
            return out
        s2, _ = self.state() if not res.get("dryRun") else (s, None)
        self._record(d.action, d.amount, res, s.hf, s2.hf if s2 else None, d.reason)
        if not res.get("dryRun"):
            self.db.put("keeper_debt_after_last", str(s2.usdt_debt))
        out["result"] = res
        out["hfAfter"] = None if s2.hf is None else float(s2.hf)
        return out

    def due(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        today = now.date()
        if self.db.get("keeper_last_run_date") == today.isoformat():
            return False
        return now >= scheduled_minute(today, self.address, self.cfg.keeper_hour_utc)

    def run_if_due(self) -> dict | None:
        if not self.due():
            return None
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        self.db.put("keeper_last_run_date", today)
        try:
            return self.cycle()
        except Exception as exc:
            log.exception("keeper cycle failed")
            self.db.add_keeper_action("cycle", amount=None, tx_hash=None, status="failed", hf_before=None,
                                      hf_after=None, dry_run=not self.live, note=f"{type(exc).__name__}: {exc}"[:300])
            return {"error": str(exc)}

    def status(self) -> dict:
        out: dict[str, Any] = {"address": self.address, "live": self.live, "dryRun": not self.live,
                               "limits": {k: str(v) for k, v in self.limits.__dict__.items()},
                               "schedule": {"hourUtc": self.cfg.keeper_hour_utc,
                                            "today": scheduled_minute(dt.datetime.now(dt.timezone.utc).date(),
                                                                      self.address,
                                                                      self.cfg.keeper_hour_utc).isoformat()}}
        try:
            s, calc = self.state()
            out["healthFactor"] = None if s.hf is None else round(float(s.hf), 6)
            out["position"] = {
                "collateralUsd": round(float(calc.total_collateral), 6),
                "weightedCollateralUsd": round(float(calc.weighted_cf), 6),
                "debtUsd": round(float(s.debt_usd), 6),
                "usdtDebt": str(q6(s.usdt_debt)),
                "usdtOnHand": str(q6(s.usdt_wallet)),
                "markets": [{"symbol": m.raw.underlying_symbol, "supplied": float(m.supplied),
                             "borrowed": float(m.borrowed), "collateralFactor": float(m.cf)}
                            for m in calc.markets if m.supplied > 0 or m.borrowed > 0],
            }
            nd = decide(s, self.limits)
            out["nextDecision"] = {"action": nd.action, "amount": str(nd.amount), "reason": nd.reason,
                                   "refused": nd.refused}
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        acts = []
        for a in self.db.keeper_actions(20):
            acts.append({**{k: a[k] for k in ("ts", "action", "amount", "status", "hf_before", "hf_after", "note")},
                         "dryRun": bool(a["dry_run"]), "txHash": a["tx_hash"],
                         "bscscan": f"{EXPLORER}/tx/{a['tx_hash']}" if a["tx_hash"] else None})
        out["lastActions"] = acts
        last = self.db.last_keeper_tx()
        out["lastTxAgeHours"] = None if not last else round((time.time() - last["ts"]) / 3600, 2)
        return out
