# Vitals

**Health-factor checkups for Venus Core Pool loans on BNB Chain.**

Vitals is an agent in the **health factor** category. Give it a Venus Core Pool
borrower address (and optionally a block and a target health factor) and it
reads the Comptroller, the vTokens and the Venus oracle on chain at one pinned
block, then reports:

- the health factor to three decimals, and the liquidation-threshold variant;
- every market's collateral factor, liquidation threshold, supplied and borrowed amounts in tokens and USD;
- the liquidation price of every asset (closed form, holding other prices fixed, including the case where the same asset is also borrowed) and the distance to liquidation;
- the exact USD of debt to repay to restore the target health factor (`max(0, D - C/T)`), per-asset repay amounts in tokens, and the collateral top-up alternative;
- a reconciliation against `Comptroller.getAccountLiquidity` at the same block (must agree to 1e-9 relative), and full provenance (block, timestamp, RPC host, contract addresses, method, engine version).

It answers for free over A2A, MCP and REST, and it sells the same report as a
paid **ERC-8183** job (escrowed in U on BSC), delivered as a content-addressed
JSON file whose hash is submitted on chain. It also keeps a small Venus
position of its own (BNB collateral, USDT debt) near health factor 2.0.

Vitals passes Marque's conformance test **MCS-HF-1** (case `HF-1-venus-at-risk`)
over both A2A and MCP.

## Registry

Agent ID: pending · Chain: BNB Smart Chain mainnet (56) · Registry: 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432

- Owner and agent wallet: `0x15A97307cAA68C24E4b5a0b83D331A6AA1EA195F`
- Agent card: https://vitals.43-165-190-110.sslip.io/.well-known/agent-card.json
- ERC-8004 registration file (the on-chain `agentURI`): https://vitals.43-165-190-110.sslip.io/.well-known/agent-registration.json
- Source: https://github.com/0xmago77/vitals

## Pricing

| What | Price |
|---|---|
| A2A `message/send`, MCP `tools/call`, REST `/api/hf` | free (rate limited) |
| ERC-8183 job (signed, escrowed, delivered on chain) | **0.01 U** (`0xcE24439F2D9C6a2289F741120FE202248B666666`, 18 decimals) |

Quotes are signed by the agent wallet (EIP-191 over the bnbagent SDK
`negotiation_hash`), bound to chain 56 and AgenticCommerce
`0xea4daa3100a767e86fded867729ae7446476eba6`, and valid for 15 minutes.

## How to hire it

All paid hires use the canonical BSC ERC-8183 stack: AgenticCommerce
`0xea4daa3100a767e86fded867729ae7446476eba6`, EvaluatorRouter
`0x51895229e12f9876011789b04f8698af06ccd6da` (as evaluator and hook) and
OptimisticPolicy `0x9c01845705b3078aa2e8cff7520a6376fd766de5` (7-day review
window). Vitals delivers automatically as soon as the job is funded and calls
the permissionless `EvaluatorRouter.settle` itself once the window has passed,
so the job reaches `JobCompleted` without anyone else acting.

- **Marque** (https://marque.trade): open Vitals in the health-factor category, enter the Venus account to check (your connected wallet by default), take the signed quote and approve the funding steps in your wallet.
- **Mandate** (https://www.mandatemarkets.com): Vitals is discovered from the ERC-8004 registry through its `negotiate-erc8183-job` skill; hire it from its agent page with the account address as the task.
- **Dolphin** (https://www.dolphinamp.xyz): listed automatically from the mainnet registry; hire it with a task that names the account address.
- **Any ERC-8183 client** (for example the bnbagent SDK):
  1. ask for a quote: A2A `message/send` to `https://vitals.43-165-190-110.sslip.io/a2a` with a data part
     `{"skill": "negotiate-erc8183-job", "task_description": "{\"address\": \"0x...\", \"targetHealthFactor\": 2.5}", "terms": {"deliverables": "...", "quality_standards": "..."}}`;
  2. `createJob(provider = agent wallet, evaluator = router, expiredAt >= now + 7 days + 1 h, description = build_job_description(quote), hook = router)`, `router.registerJob(jobId, OptimisticPolicy)`, `setBudget(jobId, price)`, approve U, `fund(jobId, price)`;
  3. optionally send `{"skill": "notify_funded", "job_id": N}`; the deliverable URL rides in the `submit` transaction and is listed at `/api/jobs/N`.

A task with no address is declined with the reason; a funded job whose task
names no address is reported on the job client's own wallet.

## API reference

Base URL: `https://vitals.43-165-190-110.sslip.io`. Every endpoint answers
`application/json`; a GET on a POST endpoint returns a JSON description of it.

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/agent-card.json` (alias `/.well-known/agent.json`) | A2A 0.3.0 agent card |
| `GET /.well-known/agent-registration.json` | ERC-8004 registration file |
| `POST /a2a` | A2A JSON-RPC 2.0: `message/send`, `tasks/get`, `tasks/cancel` |
| `POST /mcp` | MCP streamable HTTP (`initialize`, `tools/list`, `tools/call`); tool `venus_health_factor` |
| `GET/POST /api/hf?address=0x..&block=N&target=2.5` | REST report |
| `POST /api/negotiate` | ERC-8183 quote (same as the `negotiate-erc8183-job` skill) |
| `GET /api/jobs`, `GET /api/jobs/{id}` | paid jobs served and their on-chain state |
| `GET /deliverables/{hash}.json` | delivered reports, named by the hash submitted on chain |
| `GET /api/keeper/status` | the agent's own Venus position and its last keeper transactions |
| `GET /health` | liveness |

### A2A example

```bash
curl -s https://vitals.43-165-190-110.sslip.io/a2a -H 'content-type: application/json' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text",
    "text": "Account: 0x60AA3AEE06E2345A17E4d4B12c53E046F4F63CAf\nBlock: 124010796\nReport the health factor and the exact USD of debt to repay to restore a health factor of 2.5"}]}}}'
```

The result is a completed Task whose artifact holds the report twice: a text
part containing only the JSON object, then a data part with the same object.
The top-level fields graded by MCS-HF-1 are `healthFactor`,
`primaryCollateralSymbol`, `primaryCollateralFactor`,
`primaryLiquidationPriceUsd` and `repayUsdToReachTarget`; the full report
adds `markets`, `repayToTarget`, `reconciliation`, `provenance` and the parsed
`input`.

### MCP example

```bash
curl -s https://vitals.43-165-190-110.sslip.io/mcp -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"venus_health_factor","arguments":{"address":"0x60AA3AEE06E2345A17E4d4B12c53E046F4F63CAf","blockNumber":124010796,"targetHealthFactor":2.5}}}'
```

## How results can be verified

- **Every number is on chain at one block.** The report names its block, the Comptroller, the oracle and the RPC host. Re-run the same reads (`getAssetsIn`, `getAccountSnapshot`, `markets`, `oracle().getUnderlyingPrice`) at that block, or run `python -m vitals report <address> --block <N>` from this repository.
- **Reconciliation.** `reconciliation.relativeDiffCollateralFactor` compares `sum(collateralUSD * CF) - sum(debtUSD)` with `getAccountLiquidity(account)` at the same block; Vitals reports `matches: true` only within 1e-9.
- **Deliverables.** A paid job's `deliverable` (bytes32 on AgenticCommerce) is the keccak256 of the file at `/deliverables/<hash>.json`; the URL is also in the `submit` transaction's `optParams`. Fetch the file, hash its bytes, compare.
- **Quotes.** `negotiation_hash` is keccak256 of the canonical quote JSON (bnbagent SDK schema v1) and `provider_sig` recovers to the agent wallet.
- **Conformance.** `scripts/marque_check.py` replays Marque's MCS-HF-1 harness (A2A and MCP) against any Vitals URL; `scripts/diff_reference.py` diffs Vitals against Marque's reference agent field by field.

### Definitions

- `healthFactor = sum(collateralUSD * collateralFactor) / sum(debtUSD)` over the markets the account has entered (VAI debt at 1 USD), as `getAccountLiquidity` and MCS-HF-1 use. No debt: `healthFactor` is `null` with `status: "no_debt"`.
- `primaryCollateralSymbol`: the underlying symbol of the entered market with the largest supplied USD among markets with a positive collateral factor (vBNB is `BNB`).
- `markets[].liquidationPrice`: `P* = P - L0 / (a*CF - b)` with `L0 = sum(a_k P_k CF_k) - sum(b_k P_k)`; `null` when no positive price brings HF to 1. `primaryLiquidationPriceUsd` follows MCS-HF-1 (debt USD held fixed); the two agree unless the primary collateral is also borrowed.

## Running it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.lock
.venv/bin/python -m vitals serve                 # HTTP on 127.0.0.1:9000
.venv/bin/python -m vitals report 0x60AA3AEE06E2345A17E4d4B12c53E046F4F63CAf --block 124010796 --target 2.5
.venv/bin/python -m pytest -q tests              # unit tests (no network)
```

Configuration is environment only (`VITALS_*`, see `.env.example`). The signing
key is read from the file named by `VITALS_KEY_FILE`; nothing writes it
anywhere. Every transaction (job submit and settle, keeper actions) is a dry
run unless `VITALS_LIVE=1`, is simulated with `eth_call` first, and is refused
when the gas price is above `VITALS_GAS_PRICE_CAP_GWEI` (3) or the BNB balance
would fall below `VITALS_BNB_RESERVE` (0.002).

Production files are in `deploy/` (systemd unit, Caddy site block,
`install.sh`); `scripts/status.sh` prints a one-screen health summary;
`scripts/register` registers the ERC-8004 identity (dry run unless `--send`
with `VITALS_LIVE=1`).

### Keeper

`python -m vitals keeper open --collateral-bnb <x> --target-hf 2.0` supplies BNB
to vBNB, enters the market and borrows USDT to the target. Once a day, at a
randomised minute in `VITALS_KEEPER_HOUR_UTC`, the keeper repays to 2.0 when HF
< 1.85, borrows back to 2.0 when HF > 2.15 (only if HF stays >= 1.9), repays all
USDT on hand when HF < 1.3, and otherwise makes a maintenance repay (accrued
interest plus at least 0.01 USDT) when it has not transacted for 20 hours.
Limits: 3 USDT per borrow, 5 USDT total debt.

## License

MIT, see `LICENSE`.
