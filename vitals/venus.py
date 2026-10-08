"""Venus Core Pool health-factor engine.

Reader: every value comes from the chain at ONE pinned block, through
Multicall3, so collateral factors, balances and oracle prices can never be
mixed across blocks.

Math: integers from the chain, Decimal with 60 significant digits in the
core; floats only appear when the report is rendered to JSON.

Conventions (Compound / Venus):
  * getUnderlyingPrice is scaled 1e(36 - underlyingDecimals), so
    USD = vTokenBalance * exchangeRateMantissa * price / 1e54 and
    debt USD = borrowBalance * price / 1e36;
  * collateralFactorMantissa and liquidationThresholdMantissa are 1e18-scaled;
  * only markets the account has entered (getAssetsIn) count as collateral,
    exactly as Comptroller.getAccountLiquidity does; VAI minted by the account
    is debt valued at 1 USD, also as getAccountLiquidity does.

healthFactor = sum(collateralUSD * collateralFactor) / sum(debtUSD), the
definition used by Marque's MCS-HF-1 ground truth (its reader takes word 1 of
`markets(vToken)`, the collateral factor). The liquidation-threshold variant
is reported alongside as healthFactorLiquidationThreshold.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import ENGINE_NAME, ENGINE_VERSION
from .abi import Call, decode_result, multicall

CTX = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN)
D = lambda v: CTX.create_decimal(v)  # noqa: E731
WAD = Decimal(10) ** 18
ZERO = Decimal(0)
ONE = Decimal(1)
NATIVE = "native"
RECONCILE_TOLERANCE = Decimal("1e-9")

COMPTROLLER_SIGS = {
    "assets_in": ("getAssetsIn(address)", ("address[]",)),
    "oracle": ("oracle()", ("address",)),
    "liquidity": ("getAccountLiquidity(address)", ("uint256", "uint256", "uint256")),
    "borrowing_power": ("getBorrowingPower(address)", ("uint256", "uint256", "uint256")),
    "all_markets": ("getAllMarkets()", ("address[]",)),
    "vai_controller": ("vaiController()", ("address",)),
}


class EngineError(Exception):
    pass


@dataclass
class RawMarket:
    vtoken: str
    vsymbol: str
    underlying: str  # token address, or "native" for vBNB
    underlying_symbol: str
    underlying_decimals: int
    entered: bool
    v_balance: int
    borrow_balance: int
    exchange_rate: int
    price: int
    cf_mantissa: int
    lt_mantissa: int | None
    is_listed: bool = True
    snapshot_error: int = 0
    markets_words: int = 3


@dataclass
class RawAccount:
    account: str
    block_number: int
    block_timestamp: int
    comptroller: str
    oracle: str
    markets: list[RawMarket]
    vai_debt: int = 0  # 1e18-scaled USD
    vai_controller: str | None = None
    liquidity: int | None = None
    shortfall: int | None = None
    liquidity_error: int | None = None
    bp_liquidity: int | None = None
    bp_shortfall: int | None = None
    rpc_hosts: list[str] = field(default_factory=list)
    assets_in: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- reader


def _words(raw: bytes) -> list[int]:
    return [int.from_bytes(raw[i : i + 32], "big") for i in range(0, len(raw) - len(raw) % 32, 32)]


def _decode_symbol(raw: bytes) -> str | None:
    if not raw:
        return None
    try:
        (s,) = decode_result(["string"], raw)
        return s
    except Exception:
        pass
    if len(raw) == 32:  # bytes32 symbols (old tokens)
        return raw.rstrip(b"\x00").decode("utf-8", "replace") or None
    return None


def read_account(pool, cfg, account: str, block_number: int | None = None) -> RawAccount:
    """Read everything needed for the HF report at one block."""
    from eth_utils import to_checksum_address

    account = to_checksum_address(account)
    comptroller = to_checksum_address(cfg.comptroller)
    head = pool.block_number()
    block = head if block_number is None else int(block_number)
    if block > head:
        raise EngineError(f"block {block} is in the future (head is {head})")
    purpose = pool.purpose_for(block, head)
    hosts: list[str] = []

    def mc(calls):
        res = multicall(pool, cfg.multicall, calls, block, purpose=purpose)
        if pool.last_host and pool.last_host not in hosts:
            hosts.append(pool.last_host)
        return res

    sig = COMPTROLLER_SIGS
    r1 = mc([
        Call(comptroller, sig["assets_in"][0], (account,), sig["assets_in"][1]),
        Call(comptroller, sig["oracle"][0], (), sig["oracle"][1]),
        Call(comptroller, sig["liquidity"][0], (account,), sig["liquidity"][1]),
        Call(comptroller, sig["borrowing_power"][0], (account,), sig["borrowing_power"][1]),
        Call(comptroller, sig["all_markets"][0], (), sig["all_markets"][1]),
        Call(comptroller, sig["vai_controller"][0], (), sig["vai_controller"][1]),
    ])
    if not r1[0].success or not r1[1].success:
        raise EngineError("the Venus Comptroller could not be read at this block")
    assets_in = [to_checksum_address(a) for a in r1[0].value[0]]
    oracle = to_checksum_address(r1[1].value[0])
    liq = r1[2].value if r1[2].success else None
    bp = r1[3].value if r1[3].success else None
    all_markets = [to_checksum_address(a) for a in r1[4].value[0]] if r1[4].success else list(assets_in)
    vai_ctrl = to_checksum_address(r1[5].value[0]) if r1[5].success and int(r1[5].value[0], 16) != 0 else None

    # Snapshots for every listed market, so a borrow in a market that is not in
    # getAssetsIn (should not happen on Venus, but costs nothing to check) is seen.
    universe = list(dict.fromkeys(assets_in + all_markets))
    snap_calls = [Call(v, "getAccountSnapshot(address)", (account,), ("uint256",) * 4) for v in universe]
    if vai_ctrl:
        snap_calls.append(Call(vai_ctrl, "getVAIRepayAmount(address)", (account,), ("uint256",)))
    r2 = mc(snap_calls)
    snaps = {}
    for v, res in zip(universe, r2[: len(universe)]):
        if res.success:
            snaps[v] = res.value
    vai_debt = 0
    if vai_ctrl and r2[-1].success:
        vai_debt = int(r2[-1].value[0])

    entered = set(assets_in)
    interesting = [v for v in universe if v in entered or (v in snaps and (snaps[v][1] > 0 or snaps[v][2] > 0))]
    for v in interesting:
        if v not in snaps:
            raise EngineError(f"getAccountSnapshot failed for market {v}")

    r3 = mc(
        [Call(comptroller, "markets(address)", (v,)) for v in interesting]
        + [Call(oracle, "getUnderlyingPrice(address)", (v,), ("uint256",)) for v in interesting]
        + [Call(v, "symbol()") for v in interesting]
        + [Call(v, "underlying()", (), ("address",)) for v in interesting]
    )
    n = len(interesting)
    r_markets, r_prices, r_vsym, r_under = r3[:n], r3[n : 2 * n], r3[2 * n : 3 * n], r3[3 * n :]
    underlyings: list[str] = []
    for res in r_under:
        underlyings.append(to_checksum_address(res.value[0]) if res.success and res.value else NATIVE)
    tokens = [u for u in dict.fromkeys(underlyings) if u != NATIVE]
    r4 = mc([Call(t, "decimals()", (), ("uint8",)) for t in tokens] + [Call(t, "symbol()") for t in tokens])
    dec_of = {t: (int(r4[i].value[0]) if r4[i].success else None) for i, t in enumerate(tokens)}
    sym_of = {t: _decode_symbol(r4[len(tokens) + i].raw) if r4[len(tokens) + i].success else None
              for i, t in enumerate(tokens)}

    markets: list[RawMarket] = []
    for i, v in enumerate(interesting):
        if not r_markets[i].success or not r_prices[i].success:
            raise EngineError(f"markets() or oracle price unreadable for {v}")
        words = _words(r_markets[i].raw)
        if len(words) < 2:
            raise EngineError(f"unexpected markets() layout for {v}")
        u = underlyings[i]
        if u == NATIVE:
            dec, usym = 18, "BNB"
        else:
            dec = dec_of.get(u)
            usym = sym_of.get(u) or "???"
            if dec is None:
                raise EngineError(f"decimals() unreadable for underlying {u}")
        err, vbal, borrow, xrate = (int(x) for x in snaps[v])
        markets.append(RawMarket(
            vtoken=v,
            vsymbol=_decode_symbol(r_vsym[i].raw) or "v???",
            underlying=u,
            underlying_symbol=usym,
            underlying_decimals=dec,
            entered=v in entered,
            v_balance=vbal,
            borrow_balance=borrow,
            exchange_rate=xrate,
            price=int(r_prices[i].value[0]),
            cf_mantissa=words[1],
            lt_mantissa=words[3] if len(words) >= 4 else None,
            is_listed=bool(words[0]),
            snapshot_error=err,
            markets_words=len(words),
        ))

    blk = pool.get_block(block, purpose=purpose)
    return RawAccount(
        account=account,
        block_number=block,
        block_timestamp=int(blk["timestamp"], 16),
        comptroller=comptroller,
        oracle=oracle,
        markets=markets,
        vai_debt=vai_debt,
        vai_controller=vai_ctrl,
        liquidity=int(liq[1]) if liq else None,
        shortfall=int(liq[2]) if liq else None,
        liquidity_error=int(liq[0]) if liq else None,
        bp_liquidity=int(bp[1]) if bp else None,
        bp_shortfall=int(bp[2]) if bp else None,
        rpc_hosts=hosts,
        assets_in=assets_in,
    )


# ----------------------------------------------------------------------- math


@dataclass
class MarketCalc:
    raw: RawMarket
    price_usd: Decimal
    supplied: Decimal
    supplied_usd: Decimal
    borrowed: Decimal
    debt_usd: Decimal
    cf: Decimal
    lt: Decimal | None
    collateral_usd: Decimal  # counts as collateral only when entered
    weighted_cf_usd: Decimal
    weighted_lt_usd: Decimal
    liquidation_price: Decimal | None = None
    liquidation_direction: str | None = None
    liquidation_price_collateral_only: Decimal | None = None


@dataclass
class Calc:
    raw: RawAccount
    markets: list[MarketCalc]
    weighted_cf: Decimal
    weighted_lt: Decimal
    total_collateral: Decimal
    total_debt: Decimal
    vai_debt_usd: Decimal
    unentered_debt_usd: Decimal
    hf: Decimal | None
    hf_lt: Decimal | None
    primary: MarketCalc | None


def compute(raw: RawAccount) -> Calc:
    ms: list[MarketCalc] = []
    for m in raw.markets:
        scale_u = Decimal(10) ** m.underlying_decimals
        price_usd = CTX.divide(D(m.price), Decimal(10) ** (36 - m.underlying_decimals))
        supplied = CTX.divide(CTX.divide(CTX.multiply(D(m.v_balance), D(m.exchange_rate)), WAD), scale_u)
        supplied_usd = CTX.divide(CTX.multiply(CTX.multiply(D(m.v_balance), D(m.exchange_rate)), D(m.price)),
                                  Decimal(10) ** 54)
        borrowed = CTX.divide(D(m.borrow_balance), scale_u)
        debt_usd = CTX.divide(CTX.multiply(D(m.borrow_balance), D(m.price)), Decimal(10) ** 36)
        cf = CTX.divide(D(m.cf_mantissa), WAD)
        lt = CTX.divide(D(m.lt_mantissa), WAD) if m.lt_mantissa is not None else None
        coll = supplied_usd if m.entered else ZERO
        ms.append(MarketCalc(
            raw=m, price_usd=price_usd, supplied=supplied, supplied_usd=supplied_usd, borrowed=borrowed,
            debt_usd=debt_usd, cf=cf, lt=lt, collateral_usd=coll,
            weighted_cf_usd=CTX.multiply(coll, cf),
            weighted_lt_usd=CTX.multiply(coll, lt if lt is not None else cf),
        ))

    weighted_cf = sum((m.weighted_cf_usd for m in ms), ZERO)
    weighted_lt = sum((m.weighted_lt_usd for m in ms), ZERO)
    total_collateral = sum((m.collateral_usd for m in ms), ZERO)
    vai_usd = CTX.divide(D(raw.vai_debt), WAD)
    market_debt = sum((m.debt_usd for m in ms if m.raw.entered), ZERO)
    unentered_debt = sum((m.debt_usd for m in ms if not m.raw.entered), ZERO)
    total_debt = market_debt + vai_usd
    hf = CTX.divide(weighted_cf, total_debt) if total_debt > 0 else None
    hf_lt = CTX.divide(weighted_lt, total_debt) if total_debt > 0 else None

    # Liquidation prices: vary one asset's price, hold all others fixed.
    # L0 = sum(a_k P_k CF_k) - sum(b_k P_k); HF hits 1 at P_i* = P_i - L0 / (a_i CF_i - b_i).
    l0 = weighted_cf - total_debt
    for m in ms:
        if not m.raw.entered or total_debt <= 0:
            continue
        coeff = CTX.multiply(m.supplied, m.cf) - m.borrowed
        if coeff != 0:
            p_star = m.price_usd - CTX.divide(l0, coeff)
            if p_star > 0:
                m.liquidation_price = p_star
                m.liquidation_direction = "down" if coeff > 0 else "up"
        # The collateral-only convention (MCS-HF-1 ground truth): debt USD held fixed.
        if m.supplied > 0 and m.cf > 0:
            other = weighted_cf - m.weighted_cf_usd
            p = CTX.divide(total_debt - other, CTX.multiply(m.supplied, m.cf))
            if p > 0:
                m.liquidation_price_collateral_only = p

    # Primary collateral, as MCS-HF-1 defines it: the entered market with the
    # largest supplied USD among those with a positive collateral factor
    # (stable order, so ties keep getAssetsIn order).
    candidates = [m for m in ms if m.raw.entered and m.supplied_usd > 0 and m.cf > 0]
    primary = sorted(candidates, key=lambda m: -m.supplied_usd)[0] if candidates else None

    return Calc(raw=raw, markets=ms, weighted_cf=weighted_cf, weighted_lt=weighted_lt,
                total_collateral=total_collateral, total_debt=total_debt, vai_debt_usd=vai_usd,
                unentered_debt_usd=unentered_debt, hf=hf, hf_lt=hf_lt, primary=primary)


def repay_to_target(weighted: Decimal, debt: Decimal, target: Decimal) -> Decimal:
    """USD of debt to repay so that weighted / (debt - R) == target: max(0, D - C/T), capped at D."""
    if target <= 0:
        raise ValueError("target health factor must be positive")
    if debt <= 0:
        return ZERO
    r = debt - CTX.divide(weighted, target)
    if r <= 0:
        return ZERO
    return min(r, debt)


def topup_to_target(weighted: Decimal, debt: Decimal, target: Decimal, cf: Decimal) -> Decimal | None:
    """USD of new collateral (with factor cf) so that (C + X*cf) / D == target."""
    if debt <= 0 or cf <= 0:
        return None
    x = CTX.divide(CTX.multiply(target, debt) - weighted, cf)
    return x if x > 0 else ZERO


def reconcile(calc: Calc) -> dict[str, Any]:
    raw = calc.raw
    out: dict[str, Any] = {"method": "Comptroller.getAccountLiquidity(account) at the same block"}
    if raw.liquidity is None or raw.shortfall is None:
        out.update({"available": False})
        return out
    chain = CTX.divide(D(raw.liquidity) - D(raw.shortfall), WAD)

    def rel(ours: Decimal) -> Decimal:
        diff = abs(ours - chain)
        denom = max(abs(chain), calc.weighted_cf, calc.total_debt, Decimal("1e-18"))
        return CTX.divide(diff, abs(chain)) if chain != 0 else CTX.divide(diff, denom)

    rel_cf = rel(calc.weighted_cf - calc.total_debt)
    rel_lt = rel(calc.weighted_lt - calc.total_debt)
    out.update({
        "available": True,
        "error": raw.liquidity_error,
        "liquidityUsd": chain if chain > 0 else ZERO,
        "chainLiquidityMinusShortfallUsd": chain,
        "oursCollateralFactorUsd": calc.weighted_cf - calc.total_debt,
        "relativeDiffCollateralFactor": rel_cf,
        "oursLiquidationThresholdUsd": calc.weighted_lt - calc.total_debt,
        "relativeDiffLiquidationThreshold": rel_lt,
        "tolerance": RECONCILE_TOLERANCE,
        "matches": rel_cf <= RECONCILE_TOLERANCE,
        "weighting": "collateralFactor" if rel_cf <= RECONCILE_TOLERANCE else
        ("liquidationThreshold" if rel_lt <= RECONCILE_TOLERANCE else "none"),
    })
    if raw.bp_liquidity is not None and raw.bp_shortfall is not None:
        bp = CTX.divide(D(raw.bp_liquidity) - D(raw.bp_shortfall), WAD)
        out["borrowingPowerMinusShortfallUsd"] = bp
    return out


# ------------------------------------------------------------------ rendering


def _f(x: Decimal | None, places: int | None = None) -> float | None:
    if x is None:
        return None
    if places is not None:
        x = x.quantize(Decimal(1).scaleb(-places), rounding=decimal.ROUND_HALF_UP, context=CTX)
    return float(x)


def _s(x: Decimal | None) -> str | None:
    if x is None:
        return None
    return format(x.quantize(Decimal("1e-18"), rounding=decimal.ROUND_HALF_EVEN, context=CTX).normalize(CTX), "f")


def build_report(calc: Calc, target: Decimal, *, cfg=None, inputs: dict | None = None,
                 explorer: str = "https://bscscan.com") -> dict[str, Any]:
    raw = calc.raw
    hf = calc.hf
    has_position = any(m.supplied > 0 or m.borrowed > 0 for m in calc.markets) or calc.vai_debt_usd > 0
    repay = repay_to_target(calc.weighted_cf, calc.total_debt, target)
    primary = calc.primary
    recon = reconcile(calc)

    markets_out = []
    for m in calc.markets:
        markets_out.append({
            "vToken": m.raw.vtoken,
            "symbol": m.raw.vsymbol,
            "underlying": m.raw.underlying,
            "underlyingSymbol": m.raw.underlying_symbol,
            "underlyingDecimals": m.raw.underlying_decimals,
            "entered": m.raw.entered,
            "priceUsd": _f(m.price_usd),
            "supplied": _f(m.supplied),
            "suppliedUsd": _f(m.supplied_usd),
            "collateralUsd": _f(m.collateral_usd),
            "collateralFactor": _f(m.cf),
            "liquidationThreshold": _f(m.lt),
            "weightedCollateralUsd": _f(m.weighted_cf_usd),
            "borrowed": _f(m.borrowed),
            "borrowedUsd": _f(m.debt_usd),
            "debtUsd": _f(m.debt_usd),
            "liquidationPrice": _f(m.liquidation_price),
            "liquidationPriceUsd": _f(m.liquidation_price),
            "liquidationPriceDirection": m.liquidation_direction,
            "liquidationPriceMovePct": _f(((m.liquidation_price - m.price_usd) / m.price_usd * 100)
                                          if m.liquidation_price is not None and m.price_usd > 0 else None),
            "liquidationPriceCollateralOnly": _f(m.liquidation_price_collateral_only),
            "raw": {
                "vTokenBalance": str(m.raw.v_balance),
                "borrowBalance": str(m.raw.borrow_balance),
                "exchangeRateMantissa": str(m.raw.exchange_rate),
                "underlyingPriceMantissa": str(m.raw.price),
                "collateralFactorMantissa": str(m.raw.cf_mantissa),
                "liquidationThresholdMantissa": None if m.raw.lt_mantissa is None else str(m.raw.lt_mantissa),
                "snapshotError": m.raw.snapshot_error,
            },
        })

    per_asset_repay = []
    remaining = repay
    for m in sorted([m for m in calc.markets if m.raw.entered and m.debt_usd > 0], key=lambda m: -m.debt_usd):
        alone_tokens = CTX.divide(repay, m.price_usd) if m.price_usd > 0 else None
        take = min(remaining, m.debt_usd)
        remaining -= take
        per_asset_repay.append({
            "underlyingSymbol": m.raw.underlying_symbol,
            "vToken": m.raw.vtoken,
            "debt": _f(m.borrowed),
            "repayAllInThisAssetTokens": _f(alone_tokens),
            "repayAllInThisAssetCoversTarget": repay <= m.debt_usd,
            "suggestedTokens": _f(CTX.divide(take, m.price_usd) if m.price_usd > 0 else None),
            "suggestedUsd": _f(take),
        })
    if calc.vai_debt_usd > 0:
        take = min(remaining, calc.vai_debt_usd)
        per_asset_repay.append({"underlyingSymbol": "VAI", "debt": _f(calc.vai_debt_usd),
                                "suggestedTokens": _f(take), "suggestedUsd": _f(take)})
    topups = []
    for m in calc.markets:
        if m.raw.entered and m.cf > 0 and m.price_usd > 0:
            x = topup_to_target(calc.weighted_cf, calc.total_debt, target, m.cf)
            if x is not None:
                topups.append({"underlyingSymbol": m.raw.underlying_symbol, "vToken": m.raw.vtoken,
                               "usd": _f(x), "tokens": _f(CTX.divide(x, m.price_usd))})

    distance = None
    if hf is not None and hf > 0:
        distance = max(ZERO, (ONE - CTX.divide(ONE, hf)) * 100)

    if hf is None:
        status = "no_debt" if has_position else "no_position"
    elif hf < 1:
        status = "liquidatable"
    elif hf < Decimal("1.1"):
        status = "at_risk"
    elif hf < Decimal("1.5"):
        status = "watch"
    else:
        status = "healthy"

    report: dict[str, Any] = {
        # MCS-HF-1 graded fields, flat at the top level.
        "healthFactor": _f(hf, 3),
        "primaryCollateralSymbol": primary.raw.underlying_symbol if primary else None,
        "primaryCollateralFactor": _f(primary.cf) if primary else None,
        # MCS-HF-1 convention: debt USD held fixed. It equals markets[].liquidationPrice
        # unless the primary collateral asset is also borrowed.
        "primaryLiquidationPriceUsd": _f(primary.liquidation_price_collateral_only) if primary else None,
        "repayUsdToReachTarget": _f(repay),
        "targetHealthFactor": _f(target),
        # Full report.
        "account": raw.account,
        "chainId": 56,
        "pool": "Venus Core Pool",
        "blockNumber": raw.block_number,
        "blockTimestamp": raw.block_timestamp,
        "status": status,
        "hasPosition": has_position,
        "healthFactorExact": _s(hf),
        "healthFactorDefinition": "sum(collateralUSD * collateralFactor) / sum(debtUSD); entered markets only; VAI debt at 1 USD",
        "healthFactorLiquidationThreshold": _f(calc.hf_lt, 6),
        "totalCollateralUsd": _f(calc.total_collateral),
        "weightedCollateralUsd": _f(calc.weighted_cf),
        "weightedCollateralLiquidationThresholdUsd": _f(calc.weighted_lt),
        "totalDebtUsd": _f(calc.total_debt),
        "totalBorrowedUsd": _f(calc.total_debt),
        "vaiDebtUsd": _f(calc.vai_debt_usd),
        "unenteredDebtUsd": _f(calc.unentered_debt_usd),
        "liquidityUsd": _f(max(ZERO, calc.weighted_cf - calc.total_debt)),
        "shortfallUsd": _f(max(ZERO, calc.total_debt - calc.weighted_cf)),
        "distanceToLiquidationPct": _f(distance, 4),
        "repayToTarget": {
            "targetHealthFactor": _f(target),
            "usd": _f(repay),
            "usdExact": _s(repay),
            "formula": "max(0, D - C/T)",
            "perAsset": per_asset_repay,
            "collateralTopUpAlternative": topups,
        },
        "markets": markets_out,
        "reconciliation": {k: (_f(v) if isinstance(v, Decimal) else v) for k, v in recon.items()},
        "provenance": {
            "chainId": 56,
            "blockNumber": raw.block_number,
            "blockTimestamp": raw.block_timestamp,
            "rpcHosts": raw.rpc_hosts,
            "comptroller": raw.comptroller,
            "oracle": raw.oracle,
            "vaiController": raw.vai_controller,
            "assetsIn": raw.assets_in,
            "method": "Multicall3.aggregate3 eth_call at the pinned block: Comptroller.getAssetsIn, "
                      "getAccountLiquidity, markets, oracle; vToken.getAccountSnapshot; "
                      "PriceOracle.getUnderlyingPrice; ERC20 decimals/symbol",
            "explorer": f"{explorer}/address/{raw.account}",
        },
        "engine": {"name": ENGINE_NAME, "version": ENGINE_VERSION},
    }
    if hf is None:
        report["note"] = ("this account carries no debt, so it has no health factor" if has_position
                          else "this address has no Venus Core Pool position at this block")
    if inputs is not None:
        report["input"] = inputs
    return report


def health_report(pool, cfg, account: str, block_number: int | None = None,
                  target: Decimal | float | str = Decimal("2.0"), inputs: dict | None = None) -> dict[str, Any]:
    target_d = Decimal(str(target))
    raw = read_account(pool, cfg, account, block_number)
    return build_report(compute(raw), target_d, cfg=cfg, inputs=inputs)
