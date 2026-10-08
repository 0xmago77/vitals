"""Comptroller integer replica and Venus E-mode factor reporting."""

from __future__ import annotations

import json
from decimal import Decimal

from conftest import account, market
from vitals.venus import WAD, build_report, compute, comptroller_liquidity, reconcile


def test_integer_replica_matches_the_comptroller_exactly():
    mk = [market("BTCB", 18, "65000", supplied="0.5", cf="0.8", lt="0.85"),
          market("USDT", 18, "1", borrowed="12000")]
    raw = account(mk)
    lt_wei = comptroller_liquidity(raw, True)
    cf_wei = comptroller_liquidity(raw, False)
    # Feed the replica back as the chain's answer: the reconciliation must be exact (0 wei).
    raw.liquidity, raw.shortfall = (lt_wei, 0) if lt_wei >= 0 else (0, -lt_wei)
    raw.bp_liquidity, raw.bp_shortfall = (cf_wei, 0) if cf_wei >= 0 else (0, -cf_wei)
    rec = reconcile(compute(raw))
    assert rec["matches"] is True
    assert rec["integerReplica"]["accountLiquidityDiffWei"] == 0
    assert rec["integerReplica"]["borrowingPowerDiffWei"] == 0
    assert rec["accountLiquidityWeighting"] == "liquidationThreshold"
    # And the replica is within rounding of the exact Decimal value.
    calc = compute(raw)
    assert abs(Decimal(lt_wei) / WAD - (calc.weighted_lt - calc.total_debt)) < Decimal("1e-12")


def test_vai_debt_is_in_the_replica():
    mk = [market("BNB", 18, "600", supplied="1", cf="0.8")]
    raw = account(mk, vai="100")
    assert comptroller_liquidity(raw, False) == comptroller_liquidity(account(mk), False) - 100 * 10**18


def test_e_mode_factors_are_reported_with_their_source():
    uni = market("UNI", 18, "7.5", supplied="40", cf="0", lt="0.55")
    usdt = market("USDT", 18, "1", borrowed="73")
    # Effective factors for an account in E-mode pool 5 (Comptroller.poolMarkets(5, vUNI)).
    uni.core_cf_mantissa, uni.core_lt_mantissa = uni.cf_mantissa, uni.lt_mantissa
    uni.cf_mantissa, uni.lt_mantissa, uni.factor_source = 5 * 10**17, 55 * 10**16, "e-mode pool 5"
    raw = account([uni, usdt])
    raw.pool_id, raw.pool_label, raw.pool_active, raw.pool_fallback = 5, "UNI", True, False
    report = json.loads(json.dumps(build_report(compute(raw), Decimal(2))))
    m = next(x for x in report["markets"] if x["underlyingSymbol"] == "UNI")
    assert m["collateralFactor"] == 0.5 and m["coreCollateralFactor"] == 0.0
    assert m["factorSource"] == "e-mode pool 5"
    assert report["eMode"]["poolId"] == 5 and report["eMode"]["label"] == "UNI"
    assert report["primaryCollateralSymbol"] == "UNI" and report["primaryCollateralFactor"] == 0.5
    assert abs(report["healthFactor"] - round(40 * 7.5 * 0.5 / 73, 3)) < 1e-9
