"""HF engine math on synthetic accounts: exact Decimal results, no network."""

from __future__ import annotations

import json
from decimal import Decimal, localcontext

import pytest

from conftest import account, addr, ground_truth, hf1_account, market, reference, usd_wad
from marque_format import grade_hf
from vitals.venus import (CTX, RECONCILE_TOLERANCE, RawMarket, build_report, compute, reconcile, repay_to_target,
                          topup_to_target)

D = Decimal


def by_symbol(calc, symbol):
    return next(m for m in calc.markets if m.raw.underlying_symbol == symbol)


def report_market(report, symbol):
    return next(m for m in report["markets"] if m["underlyingSymbol"] == symbol)


def roundtrip(report):
    """The report must be strict JSON (what A2A/MCP serialise with allow_nan=False)."""
    return json.loads(json.dumps(report, allow_nan=False))


# ----------------------------------------------------------- USD conversions


def test_usd_conversion_18_decimals_hand_encoded():
    # 1.5 BTCB at $60,000: price mantissa 6e4 * 1e(36-18); 7.5e9 vToken units at 0.02 BTCB/vBTC (2e26).
    m = RawMarket(vtoken=addr(10), vsymbol="vBTC", underlying=addr(11), underlying_symbol="BTCB",
                  underlying_decimals=18, entered=True, v_balance=7_500_000_000, borrow_balance=0,
                  exchange_rate=2 * 10**26, price=60_000 * 10**18, cf_mantissa=8 * 10**17, lt_mantissa=None)
    mc = compute(account([m])).markets[0]
    assert mc.price_usd == D(60_000)
    assert mc.supplied == D("1.5")
    assert mc.supplied_usd == D(90_000)
    assert mc.supplied_usd == D(m.v_balance) * D(m.exchange_rate) * D(m.price) / D(10) ** 54
    assert mc.cf == D("0.8")
    assert mc.collateral_usd == D(90_000)
    assert mc.weighted_cf_usd == D(72_000)
    assert mc.weighted_lt_usd == D(72_000)  # no liquidation threshold word: falls back to the CF


def test_usd_conversion_8_decimals_hand_encoded():
    # 100 tokens of an 8-decimals token at $2: price mantissa 2 * 1e(36-8) = 2e28; 0.02 token per vToken = 2e16.
    m = RawMarket(vtoken=addr(12), vsymbol="vX8", underlying=addr(13), underlying_symbol="X8",
                  underlying_decimals=8, entered=True, v_balance=5 * 10**11, borrow_balance=25 * 10**8,
                  exchange_rate=2 * 10**16, price=2 * 10**28, cf_mantissa=5 * 10**17, lt_mantissa=6 * 10**17)
    mc = compute(account([m])).markets[0]
    assert mc.price_usd == D(2)
    assert mc.supplied == D(100)
    assert mc.supplied_usd == D(200)
    assert mc.borrowed == D(25)
    assert mc.debt_usd == D(50)  # borrowBalance * price / 1e36
    assert mc.weighted_cf_usd == D(100)
    assert mc.lt == D("0.6")
    assert mc.weighted_lt_usd == D(120)


def test_usd_conversion_with_a_realistic_exchange_rate():
    # vUSDT-like: 8-decimal vToken over an 18-decimal token, exchange rate 0.0215... USDT per vUSDT.
    xr = 215_345_678_901_234_567_890_123_456  # 1e(18-8+18) scale
    v_balance = 4_644_000_000_000
    m = RawMarket(vtoken=addr(14), vsymbol="vUSDT", underlying=addr(15), underlying_symbol="USDT",
                  underlying_decimals=18, entered=True, v_balance=v_balance, borrow_balance=0, exchange_rate=xr,
                  price=999_800_000_000_000_000, cf_mantissa=8 * 10**17, lt_mantissa=None)
    mc = compute(account([m])).markets[0]
    with localcontext() as ctx:
        ctx.prec = 100  # exact reference values
        supplied = D(v_balance) * D(xr) / D(10) ** 36
        supplied_usd = supplied * D("0.9998")
    assert mc.supplied == supplied
    assert mc.price_usd == D("0.9998")
    assert mc.supplied_usd == supplied_usd


def test_market_helper_encodes_like_the_chain():
    m = market("X8", 8, "2", supplied="100", borrowed="25", cf="0.5")
    assert m.price == 2 * 10**28 and m.exchange_rate == 2 * 10**16 and m.v_balance == 5 * 10**11
    assert m.borrow_balance == 25 * 10**8 and m.cf_mantissa == 5 * 10**17


# ------------------------------------------------------------- health factor


def hf_account(**kw):
    return account([
        market("BTCB", 18, "60000", supplied="1", cf="0.8", lt="0.85"),
        market("X8", 8, "2", supplied="100", cf="0.5"),
        market("USDT", 18, "1", borrowed="20000", cf="0.8"),
    ], **kw)


def test_health_factor_is_cf_weighted_collateral_over_debt():
    calc = compute(hf_account())
    assert calc.weighted_cf == D(48_100)
    assert calc.total_collateral == D(60_200)
    assert calc.total_debt == D(20_000)
    assert calc.hf == D("2.405")
    # Liquidation-threshold variant: BTCB at 0.85, X8 falls back to its CF.
    assert calc.weighted_lt == D(51_100)
    assert calc.hf_lt == D("2.555")
    report = build_report(calc, D(2))
    assert report["healthFactor"] == 2.405
    assert report["healthFactorExact"] == "2.405"
    assert report["healthFactorLiquidationThreshold"] == 2.555
    assert report["weightedCollateralUsd"] == 48_100.0
    assert report["totalDebtUsd"] == report["totalBorrowedUsd"] == 20_000.0
    assert report["status"] == "healthy"
    # Venus' liquidity is liquidation-threshold weighted; borrowing power is CF weighted.
    assert report["liquidityUsd"] == 31_100.0 and report["shortfallUsd"] == 0.0
    assert report["borrowingPowerUsd"] == 28_100.0


def test_health_factor_rounds_to_three_decimals_half_up():
    # 48000 / 29_000 = 1.6551724... -> 1.655 ; 48000 / 28_800 = 1.66666... -> 1.667
    for debt, want in (("29000", 1.655), ("28800", 1.667)):
        calc = compute(account([market("BTCB", 18, "60000", supplied="1"),
                                market("USDT", 18, "1", borrowed=debt)]))
        assert build_report(calc, D(2))["healthFactor"] == want


def test_only_entered_markets_count_as_collateral():
    base = [market("BTCB", 18, "60000", supplied="1"), market("USDT", 18, "1", borrowed="20000")]
    outside = market("BNB", 18, "600", supplied="10", cf="0.8", entered=False)
    calc = compute(account(base + [outside]))
    bnb = by_symbol(calc, "BNB")
    assert bnb.supplied_usd == D(6000)  # still valued ...
    assert bnb.collateral_usd == 0 and bnb.weighted_cf_usd == 0  # ... but not collateral
    assert calc.weighted_cf == D(48_000) and calc.total_collateral == D(60_000)
    assert calc.hf == D("2.4")
    assert bnb.liquidation_price is None and bnb.liquidation_price_collateral_only is None
    entered = market("BNB", 18, "600", supplied="10", cf="0.8", entered=True)
    assert compute(account(base + [entered])).hf == D(52_800) / D(20_000)


def test_vai_debt_is_counted_at_one_usd():
    calc = compute(hf_account(vai="1000"))
    assert calc.vai_debt_usd == D(1000)
    assert calc.total_debt == D(21_000)
    assert calc.hf == CTX.divide(D(48_100), D(21_000))
    report = build_report(calc, D(2))
    assert report["vaiDebtUsd"] == 1000.0
    vai = [r for r in report["repayToTarget"]["perAsset"] if r["underlyingSymbol"] == "VAI"]
    assert vai and vai[0]["debt"] == 1000.0


def test_vai_only_debt_has_a_health_factor():
    calc = compute(account([market("BTCB", 18, "60000", supplied="1")], vai="12000"))
    assert calc.total_debt == D(12_000)
    assert calc.hf == D(4)


def test_debt_in_an_unentered_market_is_reported_apart():
    # Mirrors getAccountLiquidity, which only iterates getAssetsIn (Venus enters on borrow).
    calc = compute(account([market("BTCB", 18, "60000", supplied="1"),
                            market("USDT", 18, "1", borrowed="20000"),
                            market("ETH", 18, "3000", borrowed="1", entered=False)]))
    assert calc.total_debt == D(20_000)
    assert calc.unentered_debt_usd == D(3000)
    assert build_report(calc, D(2))["unenteredDebtUsd"] == 3000.0


def test_no_debt_has_no_health_factor():
    calc = compute(account([market("BTCB", 18, "60000", supplied="1")]))
    assert calc.hf is None and calc.hf_lt is None
    report = roundtrip(build_report(calc, D(2)))
    assert report["status"] == "no_debt"
    assert report["hasPosition"] is True
    assert report["healthFactor"] is None and report["healthFactorExact"] is None
    assert report["repayUsdToReachTarget"] == 0.0
    assert report["distanceToLiquidationPct"] is None
    assert report["primaryCollateralSymbol"] == "BTCB"
    assert report["primaryLiquidationPriceUsd"] is None
    assert "no debt" in report["note"]


@pytest.mark.parametrize("markets", [
    [],
    # entered markets with nothing supplied or borrowed
    [market("BTCB", 18, "60000"), market("USDT", 18, "1")],
])
def test_empty_account_has_no_position(markets):
    calc = compute(account(markets))
    report = roundtrip(build_report(calc, D(2)))
    assert report["status"] == "no_position"
    assert report["hasPosition"] is False
    assert report["healthFactor"] is None
    assert report["primaryCollateralSymbol"] is None and report["primaryCollateralFactor"] is None
    assert report["repayUsdToReachTarget"] == 0.0
    assert "no Venus Core Pool position" in report["note"]


@pytest.mark.parametrize("price,debt,status", [
    ("60000", "50000", "liquidatable"),  # 0.96
    ("60000", "48000", "at_risk"),  # exactly 1.0 is not liquidatable
    ("60000", "45000", "at_risk"),  # 1.0667
    ("55000", "40000", "watch"),  # exactly 1.1
    ("60000", "40000", "watch"),  # 1.2
    ("60000", "32000", "healthy"),  # exactly 1.5
    ("60000", "20000", "healthy"),  # 2.4
])
def test_status_thresholds(price, debt, status):
    calc = compute(account([market("BTCB", 18, price, supplied="1"), market("USDT", 18, "1", borrowed=debt)]))
    assert build_report(calc, D(2))["status"] == status


def test_distance_to_liquidation():
    calc = compute(account([market("BTCB", 18, "60000", supplied="1"), market("USDT", 18, "1", borrowed="20000")]))
    # 1 - 1/2.4 = 58.3333%
    assert build_report(calc, D(2))["distanceToLiquidationPct"] == 58.3333
    under = compute(account([market("BTCB", 18, "60000", supplied="1"), market("USDT", 18, "1", borrowed="50000")]))
    assert build_report(under, D(2))["distanceToLiquidationPct"] == 0.0


@pytest.mark.parametrize("debt,hf_exact", [
    ("0.000001", "48000000000"),  # 1e12 wei of USDT dust
    ("0.000000000001", "48000000000000000"),
    ("0.000000000000000001", "48000000000000000000000"),  # 1 wei
])
def test_dust_debt_renders_a_huge_health_factor(debt, hf_exact):
    # Accounts that "repaid everything" often keep a few wei of accrued interest.
    calc = compute(account([market("BTCB", 18, "60000", supplied="1"), market("USDT", 18, "1", borrowed=debt)]))
    report = roundtrip(build_report(calc, D(2)))
    assert report["healthFactorExact"] == hf_exact
    assert report["healthFactor"] == float(hf_exact)
    assert report["status"] == "healthy"
    assert report["repayUsdToReachTarget"] == 0.0


# --------------------------------------------------------- liquidation price


def test_liquidation_price_closed_form_collateral_and_debt_assets():
    calc = compute(account([market("BTCB", 18, "60000", supplied="1", cf="0.8"),
                            market("USDT", 18, "1", borrowed="20000", cf="0.8")]))
    btc, usdt = by_symbol(calc, "BTCB"), by_symbol(calc, "USDT")
    # L0 = 48000 - 20000 = 28000; P* = 60000 - 28000 / (1 * 0.8 - 0) = 25000
    assert btc.liquidation_price == D(25_000)
    assert btc.liquidation_direction == "down"
    assert btc.liquidation_price_collateral_only == D(25_000)
    # USDT is net short: P* = 1 - 28000 / (0 - 20000) = 2.4, reached when USDT goes UP.
    assert usdt.liquidation_price == D("2.4")
    assert usdt.liquidation_direction == "up"
    assert usdt.liquidation_price_collateral_only is None  # nothing supplied
    # Re-pricing at P* gives exactly HF 1.
    at_btc = compute(account([market("BTCB", 18, "25000", supplied="1"), market("USDT", 18, "1", borrowed="20000")]))
    at_usdt = compute(account([market("BTCB", 18, "60000", supplied="1"), market("USDT", 18, "2.4", borrowed="20000")]))
    assert at_btc.hf == 1 and at_usdt.hf == 1
    report = build_report(calc, D(2))
    assert report_market(report, "BTCB")["liquidationPriceUsd"] == 25_000.0
    assert report_market(report, "BTCB")["liquidationPriceDirection"] == "down"
    assert report_market(report, "BTCB")["liquidationPriceMovePct"] == pytest.approx(-58.3333333333)
    assert report_market(report, "USDT")["liquidationPriceDirection"] == "up"
    assert report["primaryLiquidationPriceUsd"] == 25_000.0


def test_liquidation_price_when_the_same_asset_is_supplied_and_borrowed():
    # 10 BNB supplied (CF 0.8) and 2 BNB borrowed at $600, plus 1000 USDT debt.
    calc = compute(account([market("BNB", 18, "600", supplied="10", borrowed="2", cf="0.8"),
                            market("USDT", 18, "1", borrowed="1000")]))
    bnb = by_symbol(calc, "BNB")
    assert calc.weighted_cf == D(4800) and calc.total_debt == D(2200)
    # coefficient a*CF - b = 10*0.8 - 2 = 6; P* = 600 - 2600/6 = 500/3
    assert abs(bnb.liquidation_price - D(500) / D(3)) < D("1e-20")
    assert bnb.liquidation_direction == "down"
    # At P*, collateral 8P equals debt 2P + 1000.
    p = bnb.liquidation_price
    assert abs(8 * p - (2 * p + 1000)) < D("1e-18")
    # Marque's collateral-only convention holds debt USD fixed: (2200 - 0) / (10 * 0.8) = 275.
    assert bnb.liquidation_price_collateral_only == D(275)
    report = build_report(calc, D(2))
    assert report["primaryCollateralSymbol"] == "BNB"
    assert report["primaryLiquidationPriceUsd"] == 275.0
    assert report_market(report, "BNB")["liquidationPriceUsd"] == pytest.approx(500 / 3, rel=1e-12)
    assert report_market(report, "BNB")["liquidationPriceCollateralOnly"] == 275.0


def test_net_short_asset_liquidates_upward():
    # 1 ETH supplied, 2 ETH borrowed: ETH rising hurts the account.
    calc = compute(account([market("BTCB", 18, "60000", supplied="1", cf="0.8"),
                            market("ETH", 18, "3000", supplied="1", borrowed="2", cf="0.8")]))
    eth = by_symbol(calc, "ETH")
    # L0 = (48000 + 2400) - 6000 = 44400; coefficient 0.8 - 2 = -1.2; P* = 3000 + 37000 = 40000
    assert eth.liquidation_price == D(40_000)
    assert eth.liquidation_direction == "up"
    at = compute(account([market("BTCB", 18, "60000", supplied="1", cf="0.8"),
                          market("ETH", 18, "40000", supplied="1", borrowed="2", cf="0.8")]))
    assert at.hf == 1


def test_no_positive_liquidation_price_is_none():
    calc = compute(account([market("USDC", 18, "1", supplied="100000", cf="0.8"),
                            market("BTCB", 18, "60000", supplied="1", cf="0.8"),
                            market("USDT", 18, "1", borrowed="10000", cf="0.8")]))
    btc = by_symbol(calc, "BTCB")
    # Even at BTCB = 0 the USDC collateral keeps HF above 1.
    assert btc.liquidation_price is None and btc.liquidation_direction is None
    assert btc.liquidation_price_collateral_only is None
    report = build_report(calc, D(2))
    assert report["primaryCollateralSymbol"] == "USDC"
    assert report["primaryLiquidationPriceUsd"] is None
    assert report_market(report, "BTCB")["liquidationPriceUsd"] is None
    assert report_market(report, "BTCB")["liquidationPriceMovePct"] is None


def test_zero_coefficient_has_no_liquidation_price():
    # supplied * CF == borrowed: the asset's price does not move the account's liquidity.
    calc = compute(account([market("BNB", 18, "600", supplied="1", borrowed="0.5", cf="0.5"),
                            market("USDT", 18, "1", borrowed="100")]))
    assert by_symbol(calc, "BNB").liquidation_price is None


def test_zero_cf_collateral_has_no_collateral_only_price():
    calc = compute(account([market("XVS", 18, "10", supplied="100", cf="0"),
                            market("BTCB", 18, "60000", supplied="1"),
                            market("USDT", 18, "1", borrowed="20000")]))
    assert by_symbol(calc, "XVS").liquidation_price_collateral_only is None


# ------------------------------------------------------- primary collateral


def test_primary_collateral_is_largest_entered_with_positive_cf():
    calc = compute(account([
        market("BNB", 18, "600", supplied="1000", cf="0.8", entered=False),  # $600k, not entered
        market("XVS", 18, "10", supplied="10000", cf="0"),  # $100k, CF 0
        market("ETH", 18, "3000", supplied="1", cf="0.8"),  # $3k
        market("BTCB", 18, "60000", supplied="1", cf="0.7"),  # $60k -> primary
        market("USDT", 18, "1", borrowed="10000"),
    ]))
    assert calc.primary.raw.underlying_symbol == "BTCB"
    report = build_report(calc, D(2))
    assert report["primaryCollateralSymbol"] == "BTCB"
    assert report["primaryCollateralFactor"] == 0.7
    # collateral-only: (10000 - 3000*0.8) / (1 * 0.7)
    assert report["primaryLiquidationPriceUsd"] == pytest.approx(7600 / 0.7, rel=1e-15)


@pytest.mark.parametrize("first,second", [("ETH", "WBETH"), ("WBETH", "ETH")])
def test_primary_collateral_ties_keep_market_order(first, second):
    calc = compute(account([
        market(first, 18, "3000", supplied="2", cf="0.8"),
        market(second, 18, "2000", supplied="3", cf="0.75"),  # same $6000
        market("USDT", 18, "1", borrowed="1000"),
    ]))
    assert by_symbol(calc, first).supplied_usd == by_symbol(calc, second).supplied_usd
    assert calc.primary.raw.underlying_symbol == first


# ------------------------------------------------------------ repay / top-up


def test_repay_to_target_formula_and_application():
    w, d, t = D(48_000), D(30_000), D(2)
    r = repay_to_target(w, d, t)
    assert r == D(6000)  # max(0, D - C/T)
    assert w / (d - r) == t


def test_repay_to_target_on_the_reference_numbers_is_exact():
    case = reference()["cases"]["124010796"]
    w, b, t = case["weightedCollateralUsd"], case["totalBorrowedUsd"], D("2.5")
    r = repay_to_target(w, b, t)
    assert r == D("5627.943790476111")
    assert w / (b - r) == t


@pytest.mark.parametrize("w,d,t", [
    ("13096.151280579354", "10921.915125326654", "2.5"),
    ("123456.789", "100000.123", "1.85"),
    ("1.000000001", "0.999", "3"),
    ("98765.4321", "54321.9876", "2.2"),
])
def test_applying_the_repay_reaches_the_target(w, d, t):
    w, d, t = D(w), D(d), D(t)
    r = repay_to_target(w, d, t)
    assert 0 < r <= d
    assert abs(w / (d - r) - t) < D("1e-20")


def test_repay_to_target_edges():
    assert repay_to_target(D(48_000), D(10_000), D(2)) == 0  # already above target
    assert repay_to_target(D(48_000), D(24_000), D(2)) == 0  # exactly at target
    assert repay_to_target(D(48_000), D(0), D(2)) == 0  # no debt
    assert repay_to_target(D(0), D(500), D(2)) == D(500)  # capped at the debt
    assert repay_to_target(D(-10), D(500), D(2)) == D(500)
    with pytest.raises(ValueError):
        repay_to_target(D(1), D(1), D(0))


def test_topup_to_target():
    w, d, t, cf = D(48_000), D(30_000), D(2), D("0.8")
    x = topup_to_target(w, d, t, cf)
    assert x == D(15_000)
    assert (w + x * cf) / d == t
    assert topup_to_target(D(48_000), D(10_000), D(2), cf) == 0
    assert topup_to_target(w, D(0), t, cf) is None
    assert topup_to_target(w, d, t, D(0)) is None


def test_report_repay_split_and_topups():
    calc = compute(account([market("BTCB", 18, "60000", supplied="1", cf="0.8"),
                            market("USDT", 18, "1", borrowed="25000"),
                            market("USDC", 18, "1", borrowed="5000")], vai="0"))
    report = build_report(calc, D(2))
    assert report["repayUsdToReachTarget"] == 6000.0
    assert report["repayToTarget"]["usdExact"] == "6000"
    per = report["repayToTarget"]["perAsset"]
    assert [p["underlyingSymbol"] for p in per] == ["USDT", "USDC"]  # largest debt first
    assert sum(p["suggestedUsd"] for p in per) == 6000.0
    assert per[0]["repayAllInThisAssetCoversTarget"] is True
    top = report["repayToTarget"]["collateralTopUpAlternative"]
    assert {t["underlyingSymbol"]: t["usd"] for t in top}["BTCB"] == 15_000.0
    assert {t["underlyingSymbol"]: t["tokens"] for t in top}["BTCB"] == 0.25


# ------------------------------------------------------------- reconcile


def recon_account(liquidity_usd: Decimal | None = None, shortfall_usd: Decimal = D(0), **kw):
    return account([market("BTCB", 18, "60000", supplied="1", cf="0.8", lt="0.85"),
                    market("USDT", 18, "1", borrowed="20000", cf="0.8", lt="0.85")],
                   liquidity=None if liquidity_usd is None else usd_wad(liquidity_usd),
                   shortfall=usd_wad(shortfall_usd), **kw)


def test_reconcile_detects_collateral_factor_weighting():
    calc = compute(recon_account(D(48_000) - D(20_000)))
    rec = reconcile(calc)
    assert rec["available"] is True
    assert rec["chainLiquidityMinusShortfallUsd"] == D(28_000)
    assert rec["relativeDiffCollateralFactor"] == 0
    assert rec["matches"] is True
    assert rec["weighting"] == "collateralFactor"
    assert rec["tolerance"] == RECONCILE_TOLERANCE


def test_reconcile_mismatch_is_reported():
    calc = compute(recon_account(D(28_000) * D("1.01")))
    rec = reconcile(calc)
    assert rec["matches"] is False
    assert rec["weighting"] == "none"
    assert rec["relativeDiffCollateralFactor"] == CTX.divide(D(280), D(28_280))


def test_reconcile_one_wei_is_within_tolerance():
    raw = recon_account(D(28_000))
    raw.liquidity += 1
    assert reconcile(compute(raw))["matches"] is True


def test_reconcile_detects_liquidation_threshold_weighting():
    # Chain liquidity equal to the LT-weighted value (51000 - 20000): what getAccountLiquidity
    # returns on BSC since Venus added liquidation thresholds.
    rec = reconcile(compute(recon_account(D(31_000))))
    assert rec["matches"] is True
    assert rec["weighting"] == rec["accountLiquidityWeighting"] == "liquidationThreshold"
    assert rec["relativeDiffLiquidationThreshold"] == 0


def test_reconcile_shortfall_side():
    raw = account([market("BTCB", 18, "60000", supplied="1", cf="0.8"), market("USDT", 18, "1", borrowed="50000")],
                  liquidity=0, shortfall=usd_wad(D(2000)))
    rec = reconcile(compute(raw))
    assert rec["chainLiquidityMinusShortfallUsd"] == rec["accountLiquidityMinusShortfallUsd"] == D(-2000)
    assert rec["matches"] is True


def test_reconcile_unavailable_and_borrowing_power():
    rec0 = reconcile(compute(recon_account(None)))
    assert rec0["available"] is False and rec0["matches"] is False
    # getAccountLiquidity = LT-weighted (31000), getBorrowingPower = CF-weighted (28000): both hold.
    raw = recon_account(D(31_000))
    raw.bp_liquidity, raw.bp_shortfall = usd_wad(D(28_000)), 0
    rec = reconcile(compute(raw))
    assert rec["borrowingPowerMinusShortfallUsd"] == D(28_000)
    assert rec["relativeDiffBorrowingPowerCollateralFactor"] == 0
    report = roundtrip(build_report(compute(raw), D(2)))
    assert report["reconciliation"]["matches"] is True
    assert report["reconciliation"]["relativeDiffLiquidationThreshold"] == 0.0
    # A borrowing power that disagrees with the CF-weighted sum fails the reconciliation.
    raw.bp_liquidity = usd_wad(D(31_000))
    assert reconcile(compute(raw))["matches"] is False


# ------------------------------------------------- MCS-HF-1 reference fixture


@pytest.mark.parametrize("block", sorted(reference()["cases"]))
def test_report_reproduces_the_reference_fixture(block):
    case = reference()["cases"][block]
    keel = case["keel"]
    calc = compute(hf1_account(block))
    assert abs(calc.weighted_cf / case["weightedCollateralUsd"] - 1) < D("1e-15")
    assert abs(calc.total_debt - case["totalBorrowedUsd"]) == 0
    report = roundtrip(build_report(calc, D("2.5")))
    assert report["blockNumber"] == int(block)
    assert report["healthFactor"] == float(keel["healthFactor"])
    assert report["primaryCollateralSymbol"] == keel["primaryCollateralSymbol"] == "BTCB"
    assert report["primaryCollateralFactor"] == float(keel["primaryCollateralFactor"]) == 0.8
    assert report["primaryLiquidationPriceUsd"] == pytest.approx(float(keel["primaryLiquidationPriceUsd"]), rel=1e-12)
    assert report["repayUsdToReachTarget"] == pytest.approx(float(keel["repayUsdToReachTarget"]), abs=1e-6)
    assert report["targetHealthFactor"] == 2.5
    diffs = grade_hf(ground_truth(block), report)
    assert {d["field"] for d in diffs} == {"healthFactor", "primaryCollateralSymbol", "collateralFactor",
                                           "liquidationPrice", "repayToTarget"}
    assert all(d["pass"] for d in diffs), diffs


def test_grader_rejects_a_wrong_answer():
    # Sanity check of the ported grader itself.
    report = build_report(compute(hf1_account("124010796")), D("2.5"))
    bad = {**report, "healthFactor": 1.3, "primaryCollateralSymbol": "ETH",
           "repayUsdToReachTarget": report["repayUsdToReachTarget"] * 0.9}
    failed = {d["field"] for d in grade_hf(ground_truth("124010796"), bad) if not d["pass"]}
    assert failed == {"healthFactor", "primaryCollateralSymbol", "repayToTarget"}


def test_report_provenance_fields():
    raw = hf1_account("124010796")
    report = build_report(compute(raw), D("2.5"), inputs={"x": 1})
    assert report["chainId"] == 56 and report["pool"] == "Venus Core Pool"
    assert report["account"] == raw.account
    assert report["provenance"]["assetsIn"] == raw.assets_in
    assert report["provenance"]["explorer"].endswith(raw.account)
    assert report["input"] == {"x": 1}
    assert report["engine"]["name"] == "vitals-hf"
    btc = report_market(report, "BTCB")
    assert btc["raw"]["collateralFactorMantissa"] == str(8 * 10**17)
    assert int(btc["raw"]["vTokenBalance"]) == raw.markets[0].v_balance
