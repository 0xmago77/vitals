"""Signing wallet, SDK wiring and transaction guards.

* The private key is read from the file named by VITALS_KEY_FILE, kept in
  memory only, and never logged. The SDK wallet is built with persist=False,
  so no keystore is ever written.
* The bnbagent SDK talks JSON-RPC through `PoolProvider`, a web3 provider
  backed by our RpcPool: failover across public BSC nodes, eth_getLogs split
  into windows the node accepts, raw transactions broadcast to the write pool.
* `GasGuard` refuses any write when the gas price is above the cap or the BNB
  balance would fall under the reserve.
"""

from __future__ import annotations

import itertools
import logging
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from eth_account import Account
from web3 import Web3
from web3.providers.base import JSONBaseProvider

from .rpc import RpcError, RpcPool, RpcRevert, RpcUnavailable

log = logging.getLogger("vitals.chain")


class WriteRefused(Exception):
    """A guard refused a transaction before it was sent."""


# ------------------------------------------------------------------ key file


def read_key_file(path: str | Path) -> str:
    raw = Path(path).read_text(encoding="utf-8").strip()
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    if len(raw) != 64 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        raise ValueError(f"key file {path} does not hold a 32-byte hex private key")
    return "0x" + raw


def load_account(cfg):
    """eth_account LocalAccount from VITALS_KEY_FILE, or None when not configured."""
    if not cfg.key_file:
        return None
    return Account.from_key(read_key_file(cfg.key_file))


def sdk_wallet(account):
    """bnbagent EVMWalletProvider holding the same key, in memory only."""
    from bnbagent.wallets import EVMWalletProvider

    return EVMWalletProvider(password=secrets.token_urlsafe(24), private_key=account.key.hex(), persist=False)


# --------------------------------------------------------- pool-backed web3


class PoolProvider(JSONBaseProvider):
    """web3 provider that sends every request through an RpcPool."""

    def __init__(self, pool: RpcPool, write_pool: RpcPool | None = None, log_window: int = 5000):
        super().__init__()
        self.pool = pool
        self.write_pool = write_pool or pool
        self.log_window = log_window
        self._ids = itertools.count(1)

    def is_connected(self, show_traceback: bool = False) -> bool:  # noqa: ARG002
        try:
            self.pool.block_number()
            return True
        except Exception:
            return False

    def make_request(self, method, params) -> dict:
        rid = next(self._ids)
        try:
            if method == "eth_sendRawTransaction":
                result = self._broadcast(params)
            elif method == "eth_getLogs":
                f = dict(params[0]) if params else {}
                head = None
                frm, to = f.get("fromBlock", "latest"), f.get("toBlock", "latest")
                if isinstance(frm, str) and frm.startswith("0x") and isinstance(to, str):
                    if not to.startswith("0x"):
                        head = self.pool.block_number()
                    start, end = int(frm, 16), (int(to, 16) if to.startswith("0x") else head)
                    result = self.pool.get_logs_range(f.get("address"), f.get("topics") or [], start, end,
                                                      window=self.log_window)
                else:
                    result = self.pool.call("eth_getLogs", params, purpose="logs")
            elif method in ("eth_call", "eth_getBalance", "eth_getCode", "eth_getStorageAt") and params:
                tag = params[-1]
                purpose = "head"
                if isinstance(tag, str) and tag.startswith("0x"):
                    purpose = self.pool.purpose_for(int(tag, 16), self.pool.block_number())
                result = self.pool.call(method, params, purpose=purpose)
            else:
                result = self.pool.call(method, params)
        except RpcRevert as exc:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": exc.code if exc.code is not None else 3, "message": str(exc), "data": exc.data}}
        except RpcError as exc:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": exc.code if exc.code is not None else -32000, "message": str(exc),
                              "data": exc.data}}
        except RpcUnavailable as exc:
            raise ConnectionError(str(exc)) from exc
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _broadcast(self, params) -> Any:
        """Send a signed tx to the write pool; "already known" means another node has it."""
        try:
            return self.write_pool.call("eth_sendRawTransaction", params)
        except RpcError as exc:
            low = str(exc).lower()
            if "already known" in low or "known transaction" in low or "already imported" in low:
                raw = params[0]
                return Web3.to_hex(Web3.keccak(hexstr=raw))
            raise


def pool_web3(pool: RpcPool, write_pool: RpcPool | None = None, log_window: int = 5000) -> Web3:
    w3 = Web3(PoolProvider(pool, write_pool, log_window))
    try:
        from web3.middleware import ExtraDataToPOAMiddleware

        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    except ImportError:  # pragma: no cover
        pass
    return w3


def network_config(cfg):
    """bnbagent NetworkConfig for our deployment (chain 56 or a fork of it)."""
    from bnbagent.config import resolve_network

    base = resolve_network("bsc-mainnet")
    return replace(
        base,
        chain_id=cfg.chain_id,
        rpc_url=(cfg.rpc_write or cfg.rpc_head)[0],
        use_paymaster=cfg.use_paymaster,
        registry_contract=Web3.to_checksum_address(cfg.identity_registry),
        commerce_contract=Web3.to_checksum_address(cfg.commerce),
        router_contract=Web3.to_checksum_address(cfg.router),
        policy_contract=Web3.to_checksum_address(cfg.policy),
    )


def install_pool_web3(pool: RpcPool, write_pool: RpcPool | None, log_window: int) -> None:
    """Make every bnbagent ERC-8183 client use the pool-backed provider."""
    import bnbagent.erc8183.client as client_mod

    def _create(rpc_url: str = "") -> Web3:  # noqa: ARG001 - URL is replaced by the pool
        return pool_web3(pool, write_pool, log_window)

    client_mod.create_web3 = _create


# ------------------------------------------------------------------- guards


@dataclass
class GasGuard:
    pool: RpcPool
    cap_wei: int
    reserve_wei: int
    live: bool = False

    def gas_price(self) -> int:
        return int(self.pool.call("eth_gasPrice"), 16)

    def balance(self, address: str) -> int:
        return int(self.pool.call("eth_getBalance", [address, "latest"]), 16)

    def check(self, address: str, gas_limit: int = 300_000, *, value_wei: int = 0) -> dict:
        """Raise WriteRefused unless a tx of `gas_limit` can be paid without breaching limits."""
        from bnbagent.core.contract_mixin import min_gas_price_wei

        price = self.gas_price()
        chain_id = int(self.pool.call("eth_chainId"), 16)
        effective = max(int(price * 1.2), min_gas_price_wei(chain_id))
        if effective > self.cap_wei:
            raise WriteRefused(f"gas price {effective / 1e9:.3f} gwei is above the cap {self.cap_wei / 1e9:.3f} gwei")
        bal = self.balance(address)
        cost = effective * gas_limit + value_wei
        if bal - cost < self.reserve_wei:
            raise WriteRefused(
                f"BNB balance {bal / 1e18:.6f} minus this tx ({cost / 1e18:.6f}) would breach the reserve "
                f"{self.reserve_wei / 1e18:.6f} BNB"
            )
        return {"gasPriceWei": effective, "balanceWei": bal, "estimatedCostWei": cost}
