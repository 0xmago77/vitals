"""Own-position keeper decisions, hard limits, schedule, and the gas guard."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from decimal import Decimal

import pytest
from eth_account import Account

from vitals.chain import GasGuard, WriteRefused
from vitals.config import Config
from vitals.keeper import KeeperState, Limits, check_borrow, check_repay, decide, q6, scheduled_minute
from vitals.venus import CTX

D = Decimal
NOW = 1_760_000_000
H = 3600
LIM = Limits()


def state(weighted, usdt_debt, *, price="1", wallet="10", last_tx_ago_h=None, debt_after_last=None):
    debt = D(usdt_debt)
    return KeeperState(weighted_collateral_usd=D(weighted), debt_usd=debt * D(price), usdt_debt=debt,
                       usdt_price=D(price), usdt_wallet=D(wallet), now=NOW,
                       last_tx_ts=None if last_tx_ago_h is None else NOW - int(D(last_tx_ago_h) * H),
                       debt_after_last_action=None if debt_after_last is None else D(debt_after_last))


def test_limits_defaults_match_the_config():
    assert Limits.from_config(Config()) == LIM
    assert (LIM.target_hf, LIM.low_hf, LIM.high_hf, LIM.min_borrow_hf, LIM.emergency_hf) == (
        D("2.0"), D("1.85"), D("2.15"), D("1.9"), D("1.3"))
    assert (LIM.max_borrow_per_tx, LIM.debt_cap, LIM.maintenance_min, LIM.idle_hours) == (
        D(3), D(5), D("0.01"), D(20))


def test_state_health_factor():
    s = state("10", "4", price="0.9998")
    assert s.hf == CTX.divide(D(10), D("3.9992"))
    assert s.hf_after(D(1)) == CTX.divide(D(10), D("4.999"))
    assert s.hf_after(D(-4)) is None
    assert state("10", "0").hf is None


# ---------------------------------------------------------------- decide


def test_below_emergency_repays_all_usdt_on_hand():
    s = state("6", "5", wallet="2.1234567")  # HF 1.2
    d = decide(s, LIM)
    assert d.action == "emergency_repay"
    assert d.amount == D("2.123456")  # all on hand, 6 dp, rounded down
    assert d.refused == []
    s2 = state("6", "5", wallet="50")  # more on hand than owed: repay the debt
    assert decide(s2, LIM).amount == D(5)


def test_below_low_repays_back_to_target():
    s = state("9", "5", price="0.9998")  # HF ~1.8004
    assert s.hf < LIM.low_hf
    d = decide(s, LIM)
    assert d.action == "repay" and d.refused == []
    after = s.hf_after(-d.amount)
    assert after >= LIM.target_hf
    assert abs(after - LIM.target_hf) < D("0.00001")


def test_below_low_with_little_usdt_repays_what_is_on_hand():
    s = state("9", "5", wallet="0.2")
    d = decide(s, LIM)
    assert d.action == "repay" and d.amount == D("0.2") and d.refused == []


def test_below_low_with_no_usdt_is_refused():
    d = decide(state("9", "5", wallet="0"), LIM)
    assert d.action == "repay" and d.amount == 0
    assert any("positive" in r for r in d.refused)


@pytest.mark.parametrize("weighted,debt,want", [
    ("12", "4", D(1)),  # wants 2, the 5 USDT debt cap leaves 1
    ("20", "1", D(3)),  # wants 9, max 3 per tx
    ("9", "4", D("0.5")),  # HF 2.25: wants exactly 0.5
])
def test_above_high_borrows_back_within_limits(weighted, debt, want):
    s = state(weighted, debt)
    assert s.hf > LIM.high_hf
    d = decide(s, LIM)
    assert d.action == "borrow"
    assert d.amount == want
    assert d.refused == []
    assert d.amount <= LIM.max_borrow_per_tx
    assert s.usdt_debt + d.amount <= LIM.debt_cap
    assert s.hf_after(d.amount) >= LIM.min_borrow_hf
    assert s.hf_after(d.amount) >= LIM.target_hf


def test_above_high_at_the_debt_cap_does_nothing():
    d = decide(state("20", "5"), LIM)
    assert d.action == "none"
    assert "debt cap" in d.reason and d.refused


def test_inside_band_with_a_recent_tx_does_nothing():
    for weighted in ("9.25", "10", "10.75"):  # HF 1.85, 2.0, 2.15: the band is inclusive
        d = decide(state(weighted, "5", last_tx_ago_h="1"), LIM)
        assert d.action == "none" and d.amount == 0 and "inside" in d.reason
    assert decide(state("10", "5", last_tx_ago_h="19.99"), LIM).action == "none"


@pytest.mark.parametrize("ago", ["20", "36"])
def test_inside_band_idle_for_20h_repays_interest_plus_minimum(ago):
    s = state("10", "5", last_tx_ago_h=ago, debt_after_last="4.987654")
    d = decide(s, LIM)
    assert d.action == "maintenance_repay"
    assert d.amount == D("0.012346") + D("0.01")  # accrued interest + 0.01
    assert d.refused == []


def test_maintenance_when_no_tx_ever():
    d = decide(state("10", "5"), LIM)
    assert d.action == "maintenance_repay" and d.amount == D("0.01") and "ever" in d.reason


def test_maintenance_without_usdt_on_hand_is_refused():
    d = decide(state("10", "5", last_tx_ago_h="30", wallet="0.005"), LIM)
    assert d.action == "maintenance_repay" and d.amount == D("0.005")
    assert any("below the minimum" in r for r in d.refused)


def test_no_debt_does_nothing():
    d = decide(state("10", "0"), LIM)
    assert d.action == "none" and "no debt" in d.reason


# --------------------------------------------------------------- checks


def test_check_borrow_allows_a_borrow_within_every_limit():
    assert check_borrow(D(1), state("12", "4"), LIM) == []


@pytest.mark.parametrize("amount,weighted,debt,needle", [
    ("3.5", "100", "0", "per-tx limit"),
    ("2", "100", "4", "exceeds the cap"),
    ("1", "10", "4.5", "below 1.9"),  # 10 / 5.5 = 1.818
    ("0", "100", "1", "positive"),
    ("-1", "100", "1", "positive"),
])
def test_check_borrow_refusals(amount, weighted, debt, needle):
    refused = check_borrow(D(amount), state(weighted, debt), LIM)
    assert any(needle in r for r in refused), refused


def test_check_borrow_reports_every_breach():
    refused = check_borrow(D(4), state("10", "4"), LIM)
    assert len(refused) == 3  # per-tx, cap, HF after


def test_check_repay():
    s = state("10", "5", wallet="2")
    assert check_repay(D(1), s) == []
    assert any("on hand" in r for r in check_repay(D("2.5"), s))
    assert any("exceeds the debt" in r for r in check_repay(D("5.1"), state("10", "5", wallet="9")))
    assert check_repay(D("5.000001"), state("10", "5", wallet="9")) == []  # 1e-6 interest slack
    assert any("positive" in r for r in check_repay(D(0), s))


def test_q6_rounds_down():
    assert q6(D("1.23456789")) == D("1.234567")
    assert q6(D("0.0000009")) == 0


# ------------------------------------------------------------- schedule


def test_scheduled_minute_is_deterministic_and_inside_the_hour():
    a = Account.create().address
    day = dt.date(2026, 10, 8)
    first = scheduled_minute(day, a, 9)
    assert first == scheduled_minute(day, a, 9) == scheduled_minute(day, a.lower(), 9)
    assert first.tzinfo == dt.timezone.utc
    assert (first.year, first.month, first.day, first.hour) == (2026, 10, 8, 9)
    days = [day + dt.timedelta(days=i) for i in range(60)]
    runs = [scheduled_minute(x, a, 13) for x in days]
    assert all(r.hour == 13 and r.date() == x and 0 <= r.minute <= 59 for r, x in zip(runs, days))
    assert len({(r.minute, r.second) for r in runs}) > 30  # it does vary from day to day


def test_scheduled_minute_is_stable_across_processes():
    a = Account.create().address
    code = ("import datetime as dt, sys; from vitals.keeper import scheduled_minute; "
            f"print(scheduled_minute(dt.date(2026, 10, 9), {a!r}, 9).isoformat())")
    here = scheduled_minute(dt.date(2026, 10, 9), a, 9).isoformat()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True,
                             check=True)
        assert out.stdout.strip() == here


# ------------------------------------------------------------- gas guard


class FakePool:
    """Canned JSON-RPC answers; records every call."""

    def __init__(self, gas_price_wei: int, balance_wei: int, chain_id: str = "0x38"):
        self.answers = {"eth_gasPrice": hex(gas_price_wei), "eth_chainId": chain_id,
                        "eth_getBalance": hex(balance_wei)}
        self.calls = []

    def call(self, method, params=None, **kwargs):  # noqa: ARG002
        self.calls.append((method, params))
        return self.answers[method]


GWEI = 10**9
CAP = Config().gas_price_cap_wei
RESERVE = Config().bnb_reserve_wei


@pytest.fixture(autouse=True)
def _no_gas_floor_override(monkeypatch):
    monkeypatch.delenv("BNBAGENT_MIN_GAS_PRICE_WEI", raising=False)


def test_guard_defaults():
    assert CAP == 3 * GWEI and RESERVE == 2 * 10**15


def test_guard_allows_a_normal_tx():
    pool = FakePool(GWEI, 10**16)
    me = Account.create().address
    g = GasGuard(pool, CAP, RESERVE).check(me, 300_000)
    assert g == {"gasPriceWei": int(GWEI * 1.2), "balanceWei": 10**16, "estimatedCostWei": int(GWEI * 1.2) * 300_000}
    assert ("eth_getBalance", [me, "latest"]) in pool.calls
    assert ("eth_chainId", None) in pool.calls


def test_guard_applies_the_sdk_gas_floor():
    g = GasGuard(FakePool(GWEI // 20, 10**16), CAP, RESERVE).check(Account.create().address)
    assert g["gasPriceWei"] == GWEI // 10  # 0.1 gwei floor on BSC


@pytest.mark.parametrize("price", [4 * GWEI, 3 * GWEI, int(2.6 * GWEI)])
def test_guard_refuses_gas_above_the_cap(price):
    # The guard prices with 20% headroom, so 2.6 gwei on the node is 3.12 gwei on the tx.
    with pytest.raises(WriteRefused, match="above the cap"):
        GasGuard(FakePool(price, 10**18), CAP, RESERVE).check(Account.create().address)


def test_guard_refuses_breaching_the_bnb_reserve():
    me = Account.create().address
    cost = int(GWEI * 1.2) * 300_000  # 0.00036 BNB
    with pytest.raises(WriteRefused, match="reserve"):
        GasGuard(FakePool(GWEI, 23 * 10**14), CAP, RESERVE).check(me, 300_000)  # 0.0023 - 0.00036 < 0.002
    with pytest.raises(WriteRefused, match="reserve"):
        GasGuard(FakePool(GWEI, RESERVE + cost - 1), CAP, RESERVE).check(me, 300_000)
    assert GasGuard(FakePool(GWEI, RESERVE + cost), CAP, RESERVE).check(me, 300_000)["balanceWei"] == RESERVE + cost
    assert GasGuard(FakePool(GWEI, 3 * 10**15), CAP, RESERVE).check(me, 300_000)


def test_guard_counts_the_value_sent():
    me = Account.create().address
    with pytest.raises(WriteRefused, match="reserve"):
        GasGuard(FakePool(GWEI, 10**16), CAP, RESERVE).check(me, 21_000, value_wei=9 * 10**15)
    assert GasGuard(FakePool(GWEI, 10**16), CAP, RESERVE).check(me, 21_000, value_wei=7 * 10**15)
