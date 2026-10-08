"""Shared helpers for the unit tests: synthetic Venus accounts built from human
units, the MCS-HF-1 reference fixture, and fakes. Nothing here touches the network."""

from __future__ import annotations

import itertools
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for _p in (str(ROOT), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eth_utils import to_checksum_address  # noqa: E402

from marque_format import HF1_ADDRESS  # noqa: E402
from vitals.config import Config  # noqa: E402
from vitals.venus import CTX, RawAccount, RawMarket  # noqa: E402

WAD = 10**18
FIXTURE = TESTS / "fixtures" / "hf1_reference.json"

# Public BSC mainnet contract addresses (Venus vTokens and their underlyings).
VBTC = "0x882C173bC7Ff3b7786CA16dfeD3DFFfb9Ee7847B"
VUSDT = "0xfD5840Cd36d94D7229439859C0112a4185BC0255"
BTCB = "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c"
USDT = "0x55d398326f99059fF775485246999027B3197955"

_seq = itertools.count(1)


def addr(n: int) -> str:
    """A deterministic checksummed address that is not a real contract."""
    return to_checksum_address(f"0x{0xA11CE << 100 | n:040x}")


def _exact_int(value: Decimal, what: str) -> int:
    i = int(value)
    if Decimal(i) != value:
        raise AssertionError(f"{what} {value} is not an integer in base units; pick another test value")
    return i


def market(symbol: str, decimals: int, price_usd: str, *, supplied: str = "0", borrowed: str = "0",
           cf: str = "0.8", lt: str | None = None, entered: bool = True, exchange_rate: int | None = None,
           vtoken: str | None = None, underlying: str | None = None) -> RawMarket:
    """RawMarket from human units, encoded exactly as the chain would return it.

    price mantissa = priceUsd * 10^(36 - decimals); exchange rate is 1e18-scaled
    (underlying base units per vToken base unit), default 0.02 underlying per vToken."""
    n = next(_seq)
    xr = exchange_rate if exchange_rate is not None else 2 * 10 ** (8 + decimals)
    sup_units = _exact_int(Decimal(supplied) * 10**decimals, "supplied")
    v_balance, rem = divmod(sup_units * WAD, xr)
    if rem:
        raise AssertionError(f"supplied {supplied} is not representable with exchange rate {xr}")
    return RawMarket(
        vtoken=vtoken or addr(1000 + n),
        vsymbol="v" + symbol,
        underlying=underlying or addr(2000 + n),
        underlying_symbol=symbol,
        underlying_decimals=decimals,
        entered=entered,
        v_balance=v_balance,
        borrow_balance=_exact_int(Decimal(borrowed) * 10**decimals, "borrowed"),
        exchange_rate=xr,
        price=_exact_int(Decimal(price_usd) * 10 ** (36 - decimals), "price"),
        cf_mantissa=_exact_int(Decimal(cf) * WAD, "cf"),
        lt_mantissa=None if lt is None else _exact_int(Decimal(lt) * WAD, "lt"),
    )


def account(markets: list[RawMarket], *, vai: str = "0", liquidity: int | None = None,
            shortfall: int | None = None, address: str | None = None, block: int = 124010796,
            timestamp: int = 1_760_000_000) -> RawAccount:
    return RawAccount(
        account=address or addr(1),
        block_number=block,
        block_timestamp=timestamp,
        comptroller="0xfD36E2c2a6789Db23113685031d7F16329158384",
        oracle=addr(3),
        markets=markets,
        vai_debt=_exact_int(Decimal(vai) * WAD, "vai"),
        liquidity=liquidity,
        shortfall=shortfall,
        liquidity_error=None if liquidity is None else 0,
        assets_in=[m.vtoken for m in markets if m.entered],
    )


def usd_wad(x: Decimal) -> int:
    """A USD Decimal as the 1e18-scaled integer the Comptroller returns (must be exact)."""
    return _exact_int(x * WAD, "usd")


# ------------------------------------------------------------------ MCS-HF-1


def reference() -> dict:
    """tests/fixtures/hf1_reference.json with every number as an exact Decimal."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"), parse_float=Decimal)


def reference_floats() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def hf1_account(block: str | int, address: str = HF1_ADDRESS) -> RawAccount:
    """A synthetic account that reproduces the fixture's C (weighted collateral),
    B (total borrowed) and the reference liquidation price: one BTCB collateral
    market (CF 0.8) and one USDT debt market (CF 0.8, nothing supplied)."""
    case = reference()["cases"][str(block)]
    c, b = case["weightedCollateralUsd"], case["totalBorrowedUsd"]
    p_star = case["keel"]["primaryLiquidationPriceUsd"]
    cf = Decimal("0.8")
    btc_wei = int(CTX.multiply(CTX.divide(b, CTX.multiply(cf, p_star)), WAD).to_integral_value())
    price = CTX.divide(c, CTX.multiply(cf, CTX.divide(Decimal(btc_wei), WAD)))
    btc = RawMarket(vtoken=VBTC, vsymbol="vBTC", underlying=BTCB, underlying_symbol="BTCB", underlying_decimals=18,
                    entered=True, v_balance=btc_wei, borrow_balance=0, exchange_rate=WAD,
                    price=int(CTX.multiply(price, WAD).to_integral_value()), cf_mantissa=8 * 10**17,
                    lt_mantissa=8 * 10**17)
    usdt = RawMarket(vtoken=VUSDT, vsymbol="vUSDT", underlying=USDT, underlying_symbol="USDT", underlying_decimals=18,
                     entered=True, v_balance=0, borrow_balance=usd_wad(b), exchange_rate=WAD, price=WAD,
                     cf_mantissa=8 * 10**17, lt_mantissa=8 * 10**17)
    raw = RawAccount(account=address, block_number=int(block), block_timestamp=1_760_000_000,
                     comptroller="0xfD36E2c2a6789Db23113685031d7F16329158384", oracle=addr(3),
                     markets=[btc, usdt], assets_in=[VBTC, VUSDT])
    return raw


def ground_truth(block: str | int) -> dict:
    """Marque's gradeHf ground-truth input for a fixture case (as scripts/marque_check.py builds it)."""
    ref = reference_floats()
    case = ref["cases"][str(block)]
    k = case["keel"]
    return {"healthFactor": k["healthFactor"], "weightedCollateralUsd": case["weightedCollateralUsd"],
            "totalBorrowedUsd": case["totalBorrowedUsd"], "targetHealthFactor": ref["targetHealthFactor"],
            "primaryCollateral": k["primaryCollateralSymbol"], "markets": case["markets"]}


def synthetic_hf_answer(text, data):
    """An hf_answer with the real parser and math, but the chain replaced by the
    fixture accounts: what HFService.answer does, minus RPC."""
    from vitals.parse import TaskError, parse_task
    from vitals.service import refusal
    from vitals.venus import build_report, compute

    try:
        task = parse_task(text, data)
    except TaskError as exc:
        return False, refusal(str(exc), "a 0x address of a Venus Core Pool account")
    cases = reference()["cases"]
    if str(task.block_number) not in cases:
        return False, refusal(f"no synthetic state for block {task.block_number}")
    calc = compute(hf1_account(task.block_number, task.address))
    return True, build_report(calc, task.target_health_factor, inputs=task.echo())


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def cfg(tmp_path) -> Config:
    """A Config whose data directory is a temp dir (never the repository)."""
    c = Config()
    c.data_dir = tmp_path / "data"
    c.key_file = None
    c.live = False
    return c
