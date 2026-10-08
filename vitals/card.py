"""The A2A agent card and the ERC-8004 registration file."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from . import __version__

NAME = "Vitals"
TAGLINE = "Health-factor checkups for Venus Core Pool loans on BNB Chain"
CATEGORY = "health_factor"
DESCRIPTION = (
    "Vitals: health factor monitoring for Venus Core Pool positions on BNB Smart Chain. "
    "Category: health factor. Give it any Venus borrower address (and optionally a block and a "
    "target health factor) and it reads the Comptroller, vTokens and the Venus oracle on chain at "
    "one pinned block, then reports the health factor to three decimals, each market's collateral "
    "factor and liquidation threshold, the liquidation price of every asset, the liquidation risk "
    "as distance to liquidation, and the exact USD of debt to repay (or collateral to add) to "
    "restore a target health factor and avoid liquidation. Every number reconciles with "
    "Comptroller.getAccountLiquidity. Free over A2A and MCP; paid ERC-8183 jobs (0.01 U) deliver a "
    "signed, content-addressed JSON report."
)
EXAMPLE_ADDRESS = "0x60AA3AEE06E2345A17E4d4B12c53E046F4F63CAf"


def price_atomic(cfg, decimals: int = 18) -> int:
    return int(Decimal(cfg.price_u) * (Decimal(10) ** decimals))


def agent_card(cfg, *, agent_id: int | None = None, provider_address: str | None = None) -> dict[str, Any]:
    agent_id = cfg.agent_id if agent_id is None else agent_id
    provider_address = provider_address or cfg.owner
    price = price_atomic(cfg)
    hf_example_data = json.dumps({"address": EXAMPLE_ADDRESS, "blockNumber": 124010796, "targetHealthFactor": 2.5})
    hf_example_text = (f"Account: {EXAMPLE_ADDRESS}. Report its Venus health factor and the USD of debt to repay "
                       "to restore a health factor of 2.5.")
    registry = f"eip155:{cfg.chain_id}:{cfg.identity_registry}"
    card: dict[str, Any] = {
        "protocolVersion": "0.3.0",
        "name": NAME,
        "description": DESCRIPTION,
        "url": cfg.a2a_url,
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [{"url": cfg.a2a_url, "transport": "JSONRPC"}],
        "version": __version__,
        "provider": {"organization": NAME, "url": cfg.github_url},
        "documentationUrl": cfg.github_url + "#readme",
        "iconUrl": f"{cfg.base_url}/icon.svg",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "venus-health-factor",
                "name": "Venus health factor report",
                "description": (
                    "Health factor (3 dp), per-market collateral factor and liquidation threshold, per-asset "
                    "liquidation price, primary collateral, and the exact USD repayment that restores a target "
                    "health factor, read from the Venus Comptroller at a given block (default latest). Send text "
                    "naming the account (and optionally 'Block: N' and a target) or a data part "
                    "{\"address\", \"blockNumber\", \"targetHealthFactor\"}. The answer is a JSON object in a "
                    "text part (and the same object in a data part)."
                ),
                "tags": ["health_factor", "health factor", "liquidation price", "liquidation risk", "venus",
                         "lending", "bnb-chain"],
                "examples": [hf_example_data, hf_example_text],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["application/json", "text/plain"],
            },
            {
                "id": "negotiate-erc8183-job",
                "name": "Negotiate an ERC-8183 health-factor job",
                "description": (
                    "Send a data part {\"skill\": \"negotiate-erc8183-job\", \"task_description\": \"<text or JSON "
                    "naming a 0x Venus account>\", \"terms\": {\"deliverables\": \"...\", \"quality_standards\": "
                    "\"...\"}} and receive the bnbagent-SDK NegotiationResult: a quote signed (EIP-191) by the "
                    f"agent wallet {provider_address}, {price} base units of U "
                    f"({cfg.payment_token}) on BNB Smart Chain, valid {cfg.quote_ttl} s, bound to chain "
                    f"{cfg.chain_id} and AgenticCommerce {cfg.commerce}. Put the description built from it "
                    "into createJob (evaluator = hook = EvaluatorRouter), register the OptimisticPolicy, "
                    "setBudget, fund: Vitals delivers automatically."
                ),
                "tags": ["erc8183", "negotiation", "bnb-chain", "health_factor"],
                "examples": [json.dumps({"skill": "negotiate-erc8183-job", "task_description": hf_example_data,
                                         "terms": {"deliverables": "Venus health factor report as JSON",
                                                   "quality_standards": "Read on chain, reconciles with "
                                                                        "getAccountLiquidity"}})],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "negotiate",
                "name": "Negotiate (short name)",
                "description": "The same as negotiate-erc8183-job.",
                "tags": ["erc8183"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "notify_funded",
                "name": "Notify that a job is funded",
                "description": (
                    "Optional: after funding, send {\"skill\": \"notify_funded\", \"job_id\": <int>}. Vitals also "
                    "watches the chain, so a funded job is delivered without this. Replies at once with "
                    "{\"status\": \"accepted\"|\"rejected\", \"job_id\"}; the deliverable URL rides in the "
                    "submit transaction."
                ),
                "tags": ["erc8183", "delivery"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
            {
                "id": "erc8183-job-status",
                "name": "ERC-8183 job status",
                "description": "Send {\"skill\": \"erc8183-job-status\", \"job_id\": <int>} for the on-chain state "
                               "of a job and its deliverable.",
                "tags": ["erc8183", "status"],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            },
        ],
        # Fields read by marketplaces and indexers beyond the A2A core.
        "category": CATEGORY,
        "categories": [CATEGORY],
        "tags": [CATEGORY, "health factor", "venus", "liquidation", "bnb-chain", "erc8183", "erc8004"],
        "pricing": {
            "model": "per_job",
            "amount": str(cfg.price_u),
            "currency": "U",
            "token": cfg.payment_token,
            "decimals": 18,
            "atomic": str(price),
            "chainId": cfg.chain_id,
            "settlement": "ERC-8183 escrow (AgenticCommerce) with OptimisticPolicy review",
            "freeTiers": ["A2A message/send", "MCP tools/call", "REST /api/hf"],
        },
        "erc8183": {
            "chainId": cfg.chain_id,
            "commerce": cfg.commerce,
            "router": cfg.router,
            "policy": cfg.policy,
            "paymentToken": cfg.payment_token,
            "provider": provider_address,
            "price": str(price),
            "quoteTtlSeconds": cfg.quote_ttl,
            "deliverables": f"{cfg.base_url}/deliverables/<deliverable-hash>.json",
        },
        "erc8004": {
            "agentId": agent_id,
            "agentRegistry": registry,
            "owner": cfg.owner,
            "registration": cfg.registration_url,
        },
        "endpoints": {
            "a2a": cfg.a2a_url,
            "mcp": cfg.mcp_url,
            "rest": f"{cfg.base_url}/api/hf",
            "keeperStatus": f"{cfg.base_url}/api/keeper/status",
            "web": cfg.base_url + "/",
        },
    }
    return card


def registration_file(cfg, *, agent_id: int | None = None) -> dict[str, Any]:
    """ERC-8004 registration file (type registration-v1)."""
    agent_id = cfg.agent_id if agent_id is None else agent_id
    reg: dict[str, Any] = {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": NAME,
        "description": DESCRIPTION,
        "image": f"{cfg.base_url}/icon.svg",
        "services": [
            {"name": "A2A", "endpoint": cfg.card_url, "version": "0.3.0"},
            {"name": "agent-card", "endpoint": cfg.card_url},
            {"name": "MCP", "endpoint": cfg.mcp_url, "version": "2025-06-18"},
            {"name": "web", "endpoint": cfg.base_url + "/"},
        ],
        "x402Support": False,
        "active": True,
        "registrations": (
            [{"agentId": agent_id, "agentRegistry": f"eip155:{cfg.chain_id}:{cfg.identity_registry}"}]
            if agent_id is not None else []
        ),
        "supportedTrust": ["reputation"],
        "category": CATEGORY,
        "tags": [CATEGORY, "venus", "bnb-chain"],
    }
    return reg
