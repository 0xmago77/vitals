#!/usr/bin/env python3
"""End-to-end test on an anvil fork of BSC mainnet (chain id 56). No mainnet writes.

    .venv312/bin/python tests/fork/e2e_fork.py [--fork-url URL] [--keep]

1. ERC-8004: register an identity for a throwaway agent key with scripts/register.
2. ERC-8183: a throwaway buyer obtains BNB and U on the fork, negotiates a quote
   over A2A, creates / registers / budgets / funds the job; the Vitals server
   (pointed at the fork) delivers on its own; time is warped past the
   OptimisticPolicy window; the server settles; JobCompleted, provider paid in U.
3. Keeper: open a BNB-collateral / USDT-debt position, move the BNB oracle price
   (mock oracle swapped into the Comptroller's oracle slot) and time, and check
   each rule (repay to target, borrow back, maintenance repay, emergency repay)
   and every hard limit refusal.

Keys are generated fresh and kept under .dev/fork/ (gitignored). Evidence JSON is
written to .dev/evidence/fork_e2e.json.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import requests  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

from vitals.abi import decode_result, encode_call_hex  # noqa: E402

DEV = ROOT / ".dev" / "fork"
EVIDENCE = ROOT / ".dev" / "evidence" / "fork_e2e.json"
ANVIL_PORT = 8545
SERVER_PORT = 9100
RPC = f"http://127.0.0.1:{ANVIL_PORT}"
BASE = f"http://127.0.0.1:{SERVER_PORT}"
COMPTROLLER = "0xfD36E2c2a6789Db23113685031d7F16329158384"
COMMERCE = "0xea4daa3100a767e86fded867729ae7446476eba6"
U_TOKEN = "0xcE24439F2D9C6a2289F741120FE202248B666666"
REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
CASE = "0x60AA3AEE06E2345A17E4d4B12c53E046F4F63CAf"
NATIVE_BNB = "0xbBbBBBBbbBBBbbbBbbBbbbbBBbBbbbbBbBbbBBbB"  # ResilientOracle's key for BNB
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
PRICE_ORACLE = {"address": None}  # set to the Comptroller's ResilientOracle once replaced

evidence: dict = {"steps": []}


def step(name: str, **data) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {name} " + json.dumps(data, default=str)[:600], flush=True)
    evidence["steps"].append({"step": name, **data})


def rpc(method: str, params: list | None = None):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}, timeout=120)
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    return body["result"]


def call(to: str, sig: str, *args, types=("uint256",), block="latest"):
    raw = rpc("eth_call", [{"to": to, "data": encode_call_hex(sig, *args)}, block])
    return decode_result(list(types), raw)


def wait_for(fn, timeout: float, what: str, interval: float = 1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


# ------------------------------------------------------------- mock oracle


def assemble(ops: list) -> bytes:
    """Tiny two-pass assembler: items are opcodes (str), ('PUSH', n, value) or ('LABEL', name) / ('PUSHL', name)."""
    table = {"STOP": 0x00, "EQ": 0x14, "ISZERO": 0x15, "SHR": 0x1C, "CALLDATALOAD": 0x35, "CALLDATASIZE": 0x36,
             "CALLDATACOPY": 0x37, "RETURNDATASIZE": 0x3D, "RETURNDATACOPY": 0x3E, "POP": 0x50, "MSTORE": 0x52,
             "SLOAD": 0x54, "JUMPI": 0x57, "GAS": 0x5A, "JUMPDEST": 0x5B, "DUP1": 0x80, "RETURN": 0xF3,
             "STATICCALL": 0xFA, "REVERT": 0xFD}

    def build(labels):
        out = bytearray()
        found = {}
        for op in ops:
            if isinstance(op, tuple) and op[0] == "LABEL":
                found[op[1]] = len(out)
                out.append(table["JUMPDEST"])
            elif isinstance(op, tuple) and op[0] == "PUSHL":
                out += bytes([0x60, labels.get(op[1], 0)])
            elif isinstance(op, tuple) and op[0] == "PUSH":
                n, v = op[1], op[2]
                out += bytes([0x5F + n]) + int(v).to_bytes(n, "big")
            else:
                out.append(table[op])
        return bytes(out), found

    _, labels = build({})
    code, labels2 = build(labels)
    assert labels == labels2
    return code


def pinned_oracle_code() -> bytes:
    """Replacement code for the ResilientOracle on the fork: getUnderlyingPrice(x) and getPrice(x)
    return sload(x) (revert when unset); every other selector (updatePrice...) is a no-op.
    Venus reads the oracle both through the Comptroller and from other contracts directly, so
    the oracle itself is replaced rather than the Comptroller's pointer."""
    return assemble([
        ("PUSH", 1, 0), "CALLDATALOAD", ("PUSH", 1, 0xE0), "SHR", "DUP1", ("PUSH", 4, 0xFC57D4DF), "EQ",
        ("PUSHL", "main"), "JUMPI", ("PUSH", 4, 0x41976E09), "EQ", ("PUSHL", "price"), "JUMPI", "STOP",
        ("LABEL", "main"), "POP",
        ("LABEL", "price"), ("PUSH", 1, 4), "CALLDATALOAD", "SLOAD", "DUP1", "ISZERO", ("PUSHL", "rev"), "JUMPI",
        ("PUSH", 1, 0), "MSTORE", ("PUSH", 1, 0x20), ("PUSH", 1, 0), "RETURN",
        ("LABEL", "rev"), ("PUSH", 1, 0), ("PUSH", 1, 0), "REVERT",
    ])


def set_price(key: str, price_mantissa: int) -> None:
    slot = "0x" + int(key, 16).to_bytes(32, "big").hex()
    rpc("anvil_setStorageAt", [PRICE_ORACLE["address"], slot, "0x" + price_mantissa.to_bytes(32, "big").hex()])


def set_bnb_price(vbnb: str, price_mantissa: int) -> None:
    for k in (vbnb, NATIVE_BNB, WBNB):
        set_price(k, price_mantissa)


# ------------------------------------------------------------ token funding


def give_erc20(token: str, holder: str, amount: int) -> str:
    """Write a balance into the token's balances mapping (scan likely slots), else borrow from escrow."""
    for slot in list(range(0, 12)) + [51, 101, 151]:
        key = "0x" + keccak(bytes.fromhex(holder[2:].lower().rjust(64, "0")) + slot.to_bytes(32, "big")).hex()
        before = rpc("eth_getStorageAt", [token, key, "latest"])
        rpc("anvil_setStorageAt", [token, key, "0x" + amount.to_bytes(32, "big").hex()])
        (bal,) = call(token, "balanceOf(address)", holder)
        if bal == amount:
            return f"storage slot {slot}"
        rpc("anvil_setStorageAt", [token, key, before])
    # Fallback: impersonate the AgenticCommerce escrow (it holds U) and transfer.
    rpc("anvil_impersonateAccount", [COMMERCE])
    rpc("anvil_setBalance", [COMMERCE, hex(10**18)])
    tx = rpc("eth_sendTransaction", [{"from": COMMERCE, "to": token,
                                      "data": encode_call_hex("transfer(address,uint256)", holder, amount)}])
    rpc("anvil_stopImpersonatingAccount", [COMMERCE])
    rcpt = wait_for(lambda: rpc("eth_getTransactionReceipt", [tx]), 30, "transfer receipt")
    assert int(rcpt["status"], 16) == 1
    return "transfer from impersonated escrow"


# ----------------------------------------------------------------- helpers


def new_key(name: str):
    acct = Account.create()
    DEV.mkdir(parents=True, exist_ok=True)
    p = DEV / f"{name}.key"
    p.write_text(acct.key.hex() + "\n")
    os.chmod(p, 0o600)
    return acct, p


def fork_env(agent_key: Path, agent_addr: str, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({
        "VITALS_RPC_HEAD": RPC, "VITALS_RPC_ARCHIVE": RPC, "VITALS_RPC_LOGS": RPC, "VITALS_RPC_WRITE": RPC,
        "VITALS_KEY_FILE": str(agent_key), "VITALS_OWNER": agent_addr, "VITALS_LIVE": "1",
        "VITALS_DATA_DIR": str(DEV / "data"), "VITALS_PORT": str(SERVER_PORT), "VITALS_BASE_URL": BASE,
        "VITALS_WATCH_INTERVAL": "2", "VITALS_WARM_CACHE": "0", "VITALS_RPC_RETRIES": "1", "VITALS_RPC_TIMEOUT": "120",
        # anvil reports ~3 gwei from eth_gasPrice on a BSC fork (mainnet: ~0.05-0.1 gwei); the cap itself
        # is exercised separately in the keeper section.
        "VITALS_GAS_PRICE_CAP_GWEI": "10", "VITALS_KEEPER": "0", "VITALS_RATE_LIMIT_PER_MINUTE": "1000",
    })
    env.update(extra or {})
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fork-url", default=os.environ.get("VITALS_FORK_URL", "https://bsc-mainnet.public.blastapi.io"))
    ap.add_argument("--keep", action="store_true", help="leave anvil and the server running")
    ap.add_argument("--skip-keeper", action="store_true")
    ap.add_argument("--skip-commerce", action="store_true")
    args = ap.parse_args()

    import shutil
    if (DEV / "data").exists():
        shutil.rmtree(DEV / "data")
    if (DEV / "agent.json").exists():
        (DEV / "agent.json").unlink()
    procs = []
    try:
        head = int(requests.post(args.fork_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber",
                                                      "params": []}, timeout=20).json()["result"], 16)
        fork_block = head - 5
        procs.append(start_anvil(args.fork_url, fork_block))
        chain_id = int(rpc("eth_chainId"), 16)
        step("anvil fork up", forkUrlHost=args.fork_url.split("/")[2], forkBlock=fork_block, chainId=chain_id)
        assert chain_id == 56

        agent, agent_key = new_key("agent")
        buyer, _ = new_key("buyer")
        for a in (agent.address, buyer.address):
            rpc("anvil_setBalance", [a, hex(5 * 10**18)])
        step("throwaway keys funded with BNB on the fork", agent=agent.address, buyer=buyer.address)

        # 1. ERC-8004 registration with scripts/register.
        env = fork_env(agent_key, agent.address)
        dry = subprocess.run([sys.executable, str(ROOT / "scripts/register"), "--skip-url-check",
                              "--agent-file", str(DEV / "agent.json"), "--no-readme"],
                             env={**env, "VITALS_LIVE": "0"}, capture_output=True, text=True, timeout=120)
        assert "dry run: nothing sent" in dry.stdout, dry.stdout + dry.stderr
        reg = subprocess.run([sys.executable, str(ROOT / "scripts/register"), "--send", "--skip-url-check",
                              "--agent-file", str(DEV / "agent.json"), "--no-readme"],
                             env=env, capture_output=True, text=True, timeout=180)
        assert reg.returncode == 0, reg.stdout + reg.stderr
        rec = json.loads((DEV / "agent.json").read_text())
        agent_id = rec["agentId"]
        (owner,) = call(REGISTRY, "ownerOf(uint256)", agent_id, types=("address",))
        (uri,) = call(REGISTRY, "tokenURI(uint256)", agent_id, types=("string",))
        assert owner.lower() == agent.address.lower() and uri.endswith("/.well-known/agent-registration.json")
        step("ERC-8004 identity registered", agentId=agent_id, owner=owner, agentURI=uri, tx=rec.get("registrationTx"))

        (job_counter,) = call(COMMERCE, "jobCounter()")
        env = fork_env(agent_key, agent.address, {"VITALS_AGENT_ID": str(agent_id),
                                                  "VITALS_WATCH_FROM_JOB": str(job_counter),
                                                  "VITALS_WATCH_FROM_BLOCK": str(fork_block)})
        # Oracle prices now, before any time warp makes the real feeds stale.
        (orig_oracle,) = call(COMPTROLLER, "oracle()", types=("address",))
        prices = {}
        for sym, v in (("vBNB", "0xA07c5b74C9B40447a954e1466938b865b6BBea36"),
                       ("vUSDT", "0xfD5840Cd36d94D7229439859C0112a4185BC0255")):
            (prices[sym],) = call(orig_oracle, "getUnderlyingPrice(address)", v)
        step("oracle prices recorded before any warp", oracle=orig_oracle,
             bnbUsd=prices["vBNB"] / 1e18, usdtUsd=prices["vUSDT"] / 1e18)
        if not args.skip_commerce:
            commerce_flow(env, agent, buyer, agent_id, procs)
        if not args.skip_keeper:
            if not args.skip_commerce:
                # The commerce flow warped time by the 7-day review window, which leaves the real
                # Venus oracle feeds stale; the keeper runs on a fresh fork of a newer block.
                for p in reversed(procs):
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except Exception:
                        pass
                procs.clear()
                time.sleep(2)
                head2 = int(requests.post(args.fork_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber",
                                                               "params": []}, timeout=20).json()["result"], 16)
                procs.append(start_anvil(args.fork_url, head2 - 5))
                rpc("anvil_setBalance", [agent.address, hex(5 * 10**18)])
                (orig_oracle,) = call(COMPTROLLER, "oracle()", types=("address",))
                for sym, v in (("vBNB", "0xA07c5b74C9B40447a954e1466938b865b6BBea36"),
                               ("vUSDT", "0xfD5840Cd36d94D7229439859C0112a4185BC0255")):
                    (prices[sym],) = call(orig_oracle, "getUnderlyingPrice(address)", v)
                step("fresh fork for the keeper", forkBlock=head2 - 5, bnbUsd=prices["vBNB"] / 1e18)
            keeper_flow(env, agent, orig_oracle, prices)
        evidence["ok"] = True
        step("ALL FORK CHECKS PASSED")
        return 0
    except Exception as exc:
        evidence["ok"] = False
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        print("FAILED:", evidence["error"], flush=True)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(evidence, indent=2, default=str))
        if not args.keep:
            for p in reversed(procs):
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except Exception:
                    pass


def start_anvil(fork_url: str, block: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        ["anvil", "--fork-url", fork_url, "--fork-block-number", str(block), "--chain-id", "56",
         "--port", str(ANVIL_PORT), "--retries", "20", "--fork-retry-backoff", "1500", "--timeout", "60000",
         "--no-rate-limit", "--gas-price", "100000000", "--block-base-fee-per-gas", "50000000", "--silent"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    wait_for(lambda: _alive(), 60, "anvil")
    return proc


def _alive() -> bool:
    try:
        rpc("eth_blockNumber")
        return True
    except Exception:
        return False


# ----------------------------------------------------------------- commerce


def commerce_flow(env: dict, agent, buyer, agent_id: int, procs: list) -> None:
    from marque_format import a2a_body  # noqa: F401  (documents the wire format used below)

    log = open(DEV / "server.log", "w")
    server = subprocess.Popen([sys.executable, "-m", "vitals", "serve"], cwd=str(ROOT), env=env,
                              stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(server)
    wait_for(lambda: _http_ok(f"{BASE}/health"), 60, "vitals server")
    card = requests.get(f"{BASE}/.well-known/agent-card.json", timeout=10).json()
    assert card["erc8004"]["agentId"] == agent_id and card["agentWallet"] == agent.address
    step("vitals server up on the fork", card=card["url"], agentId=card["erc8004"]["agentId"])

    # Buyer obtains U.
    (dec,) = call(U_TOKEN, "decimals()", types=("uint8",))
    how = give_erc20(U_TOKEN, buyer.address, 5 * 10**dec)
    (ubal,) = call(U_TOKEN, "balanceOf(address)", buyer.address)
    step("buyer obtained U on the fork", how=how, balance=ubal / 10**dec)

    # Negotiate over A2A exactly as Mandate does (data part, skill negotiate-erc8183-job).
    task = json.dumps({"address": CASE, "targetHealthFactor": 2.5})
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
        "role": "user", "kind": "message", "messageId": "fork-1",
        "parts": [{"kind": "data", "data": {"skill": "negotiate-erc8183-job", "task_description": task,
                                            "description": task, "terms": {"deliverables": "HF report JSON",
                                                                           "quality_standards": "read on chain"}}}]}}}
    res = requests.post(card["url"].replace("https://vitals.43-165-190-110.sslip.io", BASE), json=body, timeout=30).json()
    env_q = res["result"]["parts"][0]["data"]
    assert env_q["response"]["accepted"] is True, env_q
    from bnbagent.erc8183.quote_verify import verify_quote_signature
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(RPC))
    verdict = verify_quote_signature(envelope=env_q, provider=agent.address, w3=w3,
                                     expected_verifying_contract=COMMERCE)
    assert verdict.valid, verdict
    step("signed quote received and verified", price=env_q["response"]["terms"]["price"],
         currency=env_q["response"]["terms"]["currency"], expires=env_q["response"]["quote_expires_at"],
         negotiationHash=env_q["negotiation_hash"], signer=verdict.signer)

    # A task without an address is refused with an actionable reason (and a marker Marque reads).
    bad = json.loads(json.dumps(body))
    bad["params"]["message"]["parts"][0]["data"]["task_description"] = "price check for a health factor task"
    rej = requests.post(f"{BASE}/a2a", json=bad, timeout=30).json()["result"]["parts"][0]["data"]
    assert rej["response"]["accepted"] is False and rej["negotiation_hash"] == ""
    step("task without an address refused", reason=rej["response"]["reason"][:120])

    # Buyer creates, registers, budgets and funds the job with the SDK client.
    from bnbagent.erc8183 import ERC8183Client
    from bnbagent.erc8183.negotiation import build_job_description
    from bnbagent.wallets import EVMWalletProvider
    from vitals.chain import network_config
    from vitals.config import Config

    cfg = Config.from_env()
    cfg.rpc_write = [RPC]
    cfg.rpc_head = [RPC]
    net = network_config(cfg)
    bw = EVMWalletProvider(password="fork-only", private_key=buyer.key.hex(), persist=False)
    bc = ERC8183Client(bw, network=net)
    window = bc.policy.dispute_window()
    now = int(rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    description = build_job_description(env_q)
    created = bc.create_job(provider=agent.address, expired_at=now + window + 3600, description=description)
    job_id = created["jobId"]
    bc.register_job(job_id)
    price = int(env_q["response"]["terms"]["price"])
    bc.set_budget(job_id, price)
    fund = bc.fund(job_id, price, approve_floor=0)
    job = bc.get_job(job_id)
    step("job created, registered, budgeted and funded", jobId=job_id, budget=job.budget, status=job.status.name,
         evaluator=job.evaluator, hook=job.hook, fundTx=fund.get("transactionHash"))

    # notify_funded (optional for Vitals; it also watches the chain).
    note = requests.post(f"{BASE}/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {
        "message": {"role": "user", "messageId": "fork-2", "parts": [{"kind": "data", "data": {
            "skill": "notify_funded", "job_id": job_id}}]}}}, timeout=30).json()
    step("notify_funded answered", reply=note["result"]["parts"][0]["data"])

    def submitted():
        j = bc.get_job(job_id)
        return j if j.status.name in ("SUBMITTED", "COMPLETED") else None

    j = wait_for(submitted, 600, "provider delivery", interval=3)
    jobs = requests.get(f"{BASE}/api/jobs", timeout=10).json()["jobs"]
    row = next(r for r in jobs if r["jobId"] == job_id)
    deliverable_onchain = "0x" + bytes(j.deliverable).hex()
    url = row["deliverableUrl"]
    raw = requests.get(url, timeout=10).content
    file_hash = "0x" + keccak(raw).hex()
    manifest = json.loads(raw)
    report = json.loads(manifest["response"]["content"])
    assert file_hash == deliverable_onchain == row["deliverable"], (file_hash, deliverable_onchain, row)
    assert report["account"] == CASE and report["reconciliation"]["matches"] is True
    step("provider delivered automatically", status=j.status.name, deliverable=deliverable_onchain, url=url,
         fileHashMatches=True, healthFactor=report["healthFactor"], block=report["blockNumber"],
         submitTx=row["submitTx"])

    # Time-warp past the dispute window; Vitals settles on its own (router.settle is permissionless).
    (pbal_before,) = call(U_TOKEN, "balanceOf(address)", agent.address)
    rpc("evm_increaseTime", [window + 120])
    rpc("evm_mine")
    step("time warped past the OptimisticPolicy window", seconds=window + 120)

    def completed():
        jj = bc.get_job(job_id)
        return jj if jj.status.name == "COMPLETED" else None

    wait_for(completed, 120, "JobCompleted", interval=2)
    (pbal_after,) = call(U_TOKEN, "balanceOf(address)", agent.address)
    row = next(r for r in requests.get(f"{BASE}/api/jobs", timeout=10).json()["jobs"] if r["jobId"] == job_id)
    rcpt = rpc("eth_getTransactionReceipt", [row["settleTx"]])
    topics = {lg["topics"][0] for lg in rcpt["logs"]}
    completed_t = "0x" + keccak(text="JobCompleted(uint256,address,bytes32)").hex()
    paid_t = "0x" + keccak(text="PaymentReleased(uint256,address,uint256)").hex()
    assert completed_t in topics and paid_t in topics
    assert pbal_after > pbal_before
    step("job COMPLETED and provider paid", settleTx=row["settleTx"], providerReceived=(pbal_after - pbal_before) / 10**dec,
         events=["JobCompleted", "PaymentReleased"], localState=row["state"])

    # Restart safety: a fresh server on the same DB must not re-submit anything.
    os.killpg(server.pid, signal.SIGTERM)
    server.wait(20)
    server2 = subprocess.Popen([sys.executable, "-m", "vitals", "serve"], cwd=str(ROOT), env=env,
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(server2)
    wait_for(lambda: _http_ok(f"{BASE}/health"), 60, "vitals server restart")
    time.sleep(6)
    row2 = next(r for r in requests.get(f"{BASE}/api/jobs", timeout=10).json()["jobs"] if r["jobId"] == job_id)
    assert row2["state"] == "completed" and row2["submitTx"] == row["submitTx"]
    step("restart reconciled from chain without re-submitting", state=row2["state"])


def _http_ok(url: str) -> bool:
    try:
        return requests.get(url, timeout=3).status_code == 200
    except Exception:
        return False


# ------------------------------------------------------------------- keeper


def keeper_flow(env: dict, agent, orig: str, prices: dict) -> None:
    for k, v in env.items():
        if k.startswith("VITALS_"):
            os.environ[k] = v
    from vitals.chain import WriteRefused
    from vitals.config import Config
    from vitals.db import DB
    from vitals.keeper import Keeper, check_borrow
    from vitals.rpc import RpcPool

    cfg = Config.from_env()
    cfg.data_dir = DEV / "keeper-data"
    if cfg.data_dir.exists():
        import shutil
        shutil.rmtree(cfg.data_dir)
    pool = RpcPool([RPC], [RPC], logs=[RPC], timeout=60, retries=1)
    db = DB(cfg.db_path)
    from vitals.chain import load_account
    acct = load_account(cfg)
    k = Keeper(cfg, pool, db, acct, write_pool=pool)
    m = k.markets()
    step("keeper markets discovered on chain", **m)

    # Replace the ResilientOracle's code with pinned prices (the real feeds stop updating on a fork
    # and go stale as anvil's clock advances; Venus reads the oracle from several contracts).
    PRICE_ORACLE["address"] = orig
    rpc("anvil_setCode", [orig, "0x" + pinned_oracle_code().hex()])
    p_bnb = prices["vBNB"]
    set_bnb_price(m["vBNB"], p_bnb)
    set_price(m["vUSDT"], prices["vUSDT"])
    set_price(m["USDT"], prices["vUSDT"])
    (got,) = call(orig, "getUnderlyingPrice(address)", m["vBNB"])
    (got2,) = call(orig, "getPrice(address)", m["USDT"])
    assert got == p_bnb and got2 == prices["vUSDT"]
    step("ResilientOracle replaced by pinned prices on the fork", oracle=orig, bnbUsd=p_bnb / 1e18,
         usdtUsd=prices["vUSDT"] / 1e18)

    # Dry run changes nothing.
    k.cfg.live = False
    dry = k.open_position(Decimal("0.01"))
    assert dry["dryRun"] is True
    (vbal,) = call(m["vBNB"], "balanceOf(address)", agent.address)
    assert vbal == 0
    step("dry run simulates only", result=dry["steps"][0])
    k.cfg.live = True

    # Open: collateral sized so the first 3 USDT borrow lands at HF ~2.0.
    mkt = call(COMPTROLLER, "markets(address)", m["vBNB"], types=("bool", "uint256", "bool", "uint256", "uint256", "uint256", "bool"))
    cf = Decimal(mkt[1]) / Decimal(10**18)
    collateral = (Decimal(2) * Decimal(3) / (cf * Decimal(p_bnb) / Decimal(10**18))).quantize(Decimal("0.000001"))
    opened = k.open_position(collateral, Decimal("2.0"))
    s, _ = k.state()
    assert s.usdt_debt > 0 and abs(s.hf - Decimal(2)) < Decimal("0.05"), s.hf
    step("keeper open: supply BNB, enter market, borrow USDT", collateralBnb=str(collateral),
         txs=[x.get("txHash") for x in opened["steps"]], hf=float(s.hf), usdtDebt=str(s.usdt_debt))

    def cycle(label: str, expect: str):
        out = k.cycle()
        assert out["decision"] == expect, out
        assert not out.get("refused"), out
        step(f"keeper cycle: {label}", decision=out["decision"], amount=out["amount"], hfBefore=out["hf"],
             hfAfter=out.get("hfAfter"), tx=out.get("result", {}).get("txHash"))
        return out

    # Price drop -> HF < 1.85 -> repay to 2.0.
    set_bnb_price(m["vBNB"], p_bnb * 88 // 100)
    out = cycle("BNB -12% -> repay to target", "repay")
    assert abs(out["hfAfter"] - 2.0) < 0.02
    # Price rise -> HF > 2.15 -> borrow back (capped, HF after >= 1.9).
    set_bnb_price(m["vBNB"], p_bnb * 112 // 100)
    out = cycle("BNB +12% -> borrow back to target", "borrow")
    assert out["hfAfter"] >= 1.9
    # Inside the band: no action while the last tx is recent.
    s, _ = k.state()
    target_price = int(Decimal(p_bnb * 112 // 100) * Decimal(2) / s.hf)
    set_bnb_price(m["vBNB"], target_price)
    out = k.cycle()
    assert out["decision"] == "none", out
    step("keeper cycle: inside band, recent tx -> no action", reason=out["reason"])
    # Every hard limit refuses an out-of-bounds action.
    s, _ = k.state()
    refusals = {}
    for label, amount in (("max borrow per tx", Decimal("3.5")),):
        try:
            k.borrow(amount, s)
            raise AssertionError("borrow above the per-tx limit was sent")
        except WriteRefused as exc:
            refusals[label] = str(exc)
    s_big = type(s)(**{**s.__dict__, "usdt_debt": Decimal("4.5")})
    refusals["total debt cap"] = "; ".join(check_borrow(Decimal("1"), s_big, k.limits))
    assert "cap" in refusals["total debt cap"]
    big = (s.weighted_collateral_usd / Decimal("1.85") - s.debt_usd) / s.usdt_price
    try:
        k.borrow(min(big, Decimal("2.9")) if big > 0 else Decimal("2.9"), s)
        raise AssertionError("borrow below the HF floor was sent")
    except WriteRefused as exc:
        refusals["min HF after borrow"] = str(exc)
    try:
        k.repay(s.usdt_wallet + Decimal("10"), s)
        raise AssertionError("repay above USDT on hand was sent")
    except WriteRefused as exc:
        refusals["repay above balance"] = str(exc)
    k.guard.cap_wei = 1  # gas price cap
    try:
        k._send(m["vBNB"], bytes.fromhex(encode_call_hex("mint()")[2:]), value=10**15, label="cap test",
                expect_zero_return=False)
        raise AssertionError("tx above the gas cap was sent")
    except WriteRefused as exc:
        refusals["gas price cap"] = str(exc)
    k.guard.cap_wei = cfg.gas_price_cap_wei
    rpc("anvil_setBalance", [agent.address, hex(2_000_100_000_000_000)])  # 0.0020001 BNB
    try:
        k._send(m["vBNB"], bytes.fromhex(encode_call_hex("mint()")[2:]), value=10**15, label="reserve test",
                expect_zero_return=False)
        raise AssertionError("tx below the BNB reserve was sent")
    except WriteRefused as exc:
        refusals["BNB gas reserve"] = str(exc)
    rpc("anvil_setBalance", [agent.address, hex(5 * 10**18)])
    step("every keeper limit refused an out-of-bounds action", **{k_: v[:140] for k_, v in refusals.items()})
    # 21 h later: maintenance repay (accrued interest + 0.01 USDT).
    rpc("evm_increaseTime", [21 * 3600])
    rpc("evm_mine")
    # The DB clock is wall time; age the last action record to match the warped chain.
    with db._lock:
        db._conn.execute("UPDATE keeper_actions SET ts = ts - ?", (21 * 3600,))
    cycle("21 h idle -> maintenance repay", "maintenance_repay")
    # Crash: HF < 1.3 -> emergency repay of all USDT on hand.
    set_bnb_price(m["vBNB"], target_price * 60 // 100)
    out = cycle("BNB -40% -> emergency repay all USDT on hand", "emergency_repay")
    s, _ = k.state()
    assert s.usdt_wallet < Decimal("0.000001")

    status = k.status()
    step("keeper status", healthFactor=status.get("healthFactor"),
         actions=[(a["action"], a["status"], a["txHash"]) for a in status["lastActions"]][:12])


if __name__ == "__main__":
    raise SystemExit(main())
