"""Runtime configuration, read from VITALS_* environment variables.

Every default here is a public, non-secret value. The signing key is never in
the environment: VITALS_KEY_FILE names a file that holds it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_FILE = REPO_ROOT / "config" / "agent.json"

# BSC mainnet (chain 56) deployments, as published by Marque's phase-2 config
# and the bnbagent SDK network preset. Venus addresses beyond the Comptroller
# are discovered on chain at runtime.
IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
REPUTATION_REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
AGENTIC_COMMERCE = "0xea4daa3100a767e86fded867729ae7446476eba6"
EVALUATOR_ROUTER = "0x51895229e12f9876011789b04f8698af06ccd6da"
OPTIMISTIC_POLICY = "0x9c01845705b3078aa2e8cff7520a6376fd766de5"
PAYMENT_TOKEN_U = "0xcE24439F2D9C6a2289F741120FE202248B666666"
VENUS_COMPTROLLER = "0xfD36E2c2a6789Db23113685031d7F16329158384"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CAMPAIGN_WALLET = "0x15A97307cAA68C24E4b5a0b83D331A6AA1EA195F"

DEFAULT_HEAD_RPCS = (
    "https://bsc-dataseed.bnbchain.org,"
    "https://bsc-dataseed1.bnbchain.org,"
    "https://bsc-dataseed2.bnbchain.org,"
    "https://bsc-mainnet.public.blastapi.io"
)
DEFAULT_ARCHIVE_RPCS = "https://bsc-mainnet.public.blastapi.io,https://1rpc.io/bnb"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


def load_agent_file(path: Path = AGENT_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@dataclass
class Config:
    base_url: str = "https://vitals.43-165-190-110.sslip.io"
    host: str = "127.0.0.1"
    port: int = 9000
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")
    key_file: str | None = None
    live: bool = False
    chain_id: int = 56

    rpc_head: list[str] = field(default_factory=lambda: DEFAULT_HEAD_RPCS.split(","))
    rpc_archive: list[str] = field(default_factory=lambda: DEFAULT_ARCHIVE_RPCS.split(","))
    rpc_write: list[str] = field(default_factory=list)
    rpc_timeout: float = 10.0
    rpc_retries: int = 2

    identity_registry: str = IDENTITY_REGISTRY
    commerce: str = AGENTIC_COMMERCE
    router: str = EVALUATOR_ROUTER
    policy: str = OPTIMISTIC_POLICY
    payment_token: str = PAYMENT_TOKEN_U
    comptroller: str = VENUS_COMPTROLLER
    multicall: str = MULTICALL3

    agent_id: int | None = None
    owner: str = CAMPAIGN_WALLET

    price_u: Decimal = Decimal("0.01")
    quote_ttl: int = 900
    gas_price_cap_gwei: Decimal = Decimal("3")
    bnb_reserve: Decimal = Decimal("0.002")
    use_paymaster: bool = False

    watch_enabled: bool = True
    watch_interval: float = 15.0
    watch_from_block: int | None = None
    log_window: int = 5000
    auto_settle: bool = True

    keeper_enabled: bool = False
    keeper_hour_utc: int = 9
    keeper_target_hf: Decimal = Decimal("2.0")
    keeper_low_hf: Decimal = Decimal("1.85")
    keeper_high_hf: Decimal = Decimal("2.15")
    keeper_min_borrow_hf: Decimal = Decimal("1.9")
    keeper_emergency_hf: Decimal = Decimal("1.3")
    keeper_max_borrow_per_tx: Decimal = Decimal("3")
    keeper_debt_cap: Decimal = Decimal("5")
    keeper_maintenance_min: Decimal = Decimal("0.01")
    keeper_idle_hours: Decimal = Decimal("20")

    rate_limit_per_minute: int = 60
    github_url: str = "https://github.com/0xmago77/vitals"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vitals.sqlite3"

    @property
    def deliverables_dir(self) -> Path:
        return self.data_dir / "deliverables"

    @property
    def a2a_url(self) -> str:
        return f"{self.base_url}/a2a"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def card_url(self) -> str:
        return f"{self.base_url}/.well-known/agent-card.json"

    @property
    def registration_url(self) -> str:
        return f"{self.base_url}/.well-known/agent-registration.json"

    @property
    def gas_price_cap_wei(self) -> int:
        return int(self.gas_price_cap_gwei * Decimal(10**9))

    @property
    def bnb_reserve_wei(self) -> int:
        return int(self.bnb_reserve * Decimal(10**18))

    @classmethod
    def from_env(cls) -> "Config":
        agent = load_agent_file()
        agent_id_raw = _env("VITALS_AGENT_ID", "" if agent.get("agentId") is None else str(agent["agentId"]))
        from_block = _env("VITALS_WATCH_FROM_BLOCK", "")
        dec = lambda name, default: Decimal(_env(name, default))  # noqa: E731
        return cls(
            base_url=_env("VITALS_BASE_URL", "https://vitals.43-165-190-110.sslip.io").rstrip("/"),
            host=_env("VITALS_HOST", "127.0.0.1"),
            port=int(_env("VITALS_PORT", "9000")),
            data_dir=Path(_env("VITALS_DATA_DIR", str(REPO_ROOT / "data"))),
            key_file=_env("VITALS_KEY_FILE", "") or None,
            live=_env_bool("VITALS_LIVE"),
            chain_id=int(_env("VITALS_CHAIN_ID", "56")),
            rpc_head=_env_list("VITALS_RPC_HEAD", DEFAULT_HEAD_RPCS),
            rpc_archive=_env_list("VITALS_RPC_ARCHIVE", DEFAULT_ARCHIVE_RPCS),
            rpc_write=_env_list("VITALS_RPC_WRITE", _env("VITALS_RPC_HEAD", DEFAULT_HEAD_RPCS)),
            rpc_timeout=float(_env("VITALS_RPC_TIMEOUT", "10")),
            rpc_retries=int(_env("VITALS_RPC_RETRIES", "2")),
            identity_registry=_env("VITALS_IDENTITY_REGISTRY", IDENTITY_REGISTRY),
            commerce=_env("VITALS_COMMERCE", AGENTIC_COMMERCE),
            router=_env("VITALS_ROUTER", EVALUATOR_ROUTER),
            policy=_env("VITALS_POLICY", OPTIMISTIC_POLICY),
            payment_token=_env("VITALS_PAYMENT_TOKEN", PAYMENT_TOKEN_U),
            comptroller=_env("VITALS_COMPTROLLER", VENUS_COMPTROLLER),
            multicall=_env("VITALS_MULTICALL", MULTICALL3),
            agent_id=int(agent_id_raw) if agent_id_raw.isdigit() else None,
            owner=_env("VITALS_OWNER", agent.get("owner") or CAMPAIGN_WALLET),
            price_u=dec("VITALS_PRICE_U", "0.01"),
            quote_ttl=min(900, int(_env("VITALS_QUOTE_TTL", "900"))),
            gas_price_cap_gwei=dec("VITALS_GAS_PRICE_CAP_GWEI", "3"),
            bnb_reserve=dec("VITALS_BNB_RESERVE", "0.002"),
            use_paymaster=_env_bool("VITALS_USE_PAYMASTER"),
            watch_enabled=_env_bool("VITALS_WATCH", True),
            watch_interval=float(_env("VITALS_WATCH_INTERVAL", "15")),
            watch_from_block=int(from_block) if from_block.isdigit() else None,
            log_window=int(_env("VITALS_LOG_WINDOW", "5000")),
            auto_settle=_env_bool("VITALS_AUTO_SETTLE", True),
            keeper_enabled=_env_bool("VITALS_KEEPER"),
            keeper_hour_utc=int(_env("VITALS_KEEPER_HOUR_UTC", "9")),
            keeper_target_hf=dec("VITALS_KEEPER_TARGET_HF", "2.0"),
            keeper_low_hf=dec("VITALS_KEEPER_LOW_HF", "1.85"),
            keeper_high_hf=dec("VITALS_KEEPER_HIGH_HF", "2.15"),
            keeper_min_borrow_hf=dec("VITALS_KEEPER_MIN_BORROW_HF", "1.9"),
            keeper_emergency_hf=dec("VITALS_KEEPER_EMERGENCY_HF", "1.3"),
            keeper_max_borrow_per_tx=dec("VITALS_KEEPER_MAX_BORROW_PER_TX", "3"),
            keeper_debt_cap=dec("VITALS_KEEPER_DEBT_CAP", "5"),
            keeper_maintenance_min=dec("VITALS_KEEPER_MAINTENANCE_MIN", "0.01"),
            keeper_idle_hours=dec("VITALS_KEEPER_IDLE_HOURS", "20"),
            rate_limit_per_minute=int(_env("VITALS_RATE_LIMIT_PER_MINUTE", "60")),
        )
