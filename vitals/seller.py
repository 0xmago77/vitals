"""ERC-8183 seller: signed quotes, funded-job detection, delivery, auto-settle.

Lifecycle of a paid hire (AgenticCommerce + EvaluatorRouter + OptimisticPolicy):
  1. buyer asks `negotiate-erc8183-job`; we answer with the bnbagent SDK
     NegotiationResult, signed EIP-191 by our wallet (price 0.01 U, <= 900 s);
  2. buyer: createJob(provider=us, evaluator=router, hook=router, description =
     the signed quote), registerJob(policy), setBudget, fund;
  3. we detect the funded job (notify_funded skill, a jobCounter scan, and a
     JobFunded log watcher), verify it on chain (SDK verify_job + router/policy
     checks), compute the report, store it content-addressed, submit its hash;
  4. after OptimisticPolicy's dispute window passes in silence, anyone may call
     EvaluatorRouter.settle(jobId): we do it ourselves, so the job reaches
     JobCompleted and PaymentReleased pays us.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import threading
import time
from decimal import Decimal
from typing import Any

from eth_abi import decode
from eth_utils import keccak, to_checksum_address

from . import ENGINE_NAME, ENGINE_VERSION, jobs as J
from .abi import Call, encode_call_hex, multicall, topic_address
from .card import price_atomic
from .chain import WRITE_LOCK, GasGuard, WriteRefused, install_pool_web3, network_config, sdk_wallet
from .parse import TaskError, parse_task
from .rpc import RpcPool
from .storage import ContentStore

log = logging.getLogger("vitals.seller")

JOB_TUPLE = "(uint256,address,address,address,string,uint256,uint256,uint8,address,uint256,bytes32)"
JOB_FUNDED_TOPIC = "0x" + keccak(text="JobFunded(uint256,address,address,uint256)").hex()
DEFAULT_TERMS = {
    "deliverables": "Venus Core Pool health-factor report as JSON (health factor, collateral factors, "
                    "liquidation prices, repay-to-target)",
    "quality_standards": "Read on chain at one pinned block; weighted collateral minus debt reconciles with "
                         "Comptroller.getAccountLiquidity",
}
MAX_ATTEMPTS = 8
# SDK error codes that will not change by retrying.
PERMANENT_CODES = frozenset({"not_assigned", "wrong_status", "job_expired", "submit_deadline_passed",
                             "description_invalid", "quote_invalid", "budget_too_low", "job_token_mismatch",
                             "payload_too_large", "not_found"})


def decode_job(raw: bytes) -> dict:
    (t,) = decode([JOB_TUPLE], raw)
    return {"id": t[0], "client": to_checksum_address(t[1]), "provider": to_checksum_address(t[2]),
            "evaluator": to_checksum_address(t[3]), "description": t[4], "budget": t[5], "expiredAt": t[6],
            "status": t[7], "hook": to_checksum_address(t[8]), "submittedAt": t[9], "deliverable": "0x" + t[10].hex()}


ADDRESS_IN_TEXT = re.compile(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])")
TASK_KEYS = ("task", "task_description", "prompt", "query", "message", "description", "request")


def classify_description(description: str) -> dict:
    """What a job.description carries.

    kind "sdk":      bnbagent schema v1 (flat signed content + negotiation_hash + provider_sig)
    kind "envelope": a whole NegotiationResult {request, response, negotiation_hash, ...} (Dolphin)
    kind "other":    any other JSON envelope or plain text
    """
    d = (description or "").strip()
    obj = None
    if d.startswith("{"):
        try:
            obj = json.loads(d)
        except ValueError:
            obj = None
    if isinstance(obj, dict):
        if isinstance(obj.get("request"), dict) and isinstance(obj.get("response"), dict) and "negotiation_hash" in obj:
            return {"kind": "envelope", "task": str(obj["request"].get("task_description") or ""), "envelope": obj}
        if "negotiation_hash" in obj and "version" in obj and "price" in obj and isinstance(obj.get("task"), str):
            return {"kind": "sdk", "task": obj["task"], "envelope": obj}
        return {"kind": "other", "task": _task_text(obj) or d, "envelope": None}
    return {"kind": "other", "task": d, "envelope": None}


def _task_text(obj: Any, depth: int = 0) -> str | None:
    if depth > 3:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in TASK_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):
                found = _task_text(v, depth + 1)
                if found:
                    return found
        # Keep the whole object as text so addresses and numbers in it can still be read.
        return json.dumps(obj, separators=(",", ":"))
    return None


def task_text_from_description(description: str) -> str:
    """The buyer's task as anchored in job.description (any of the formats above)."""
    return classify_description(description)["task"]


class Seller:
    def __init__(self, cfg, pool: RpcPool, db, hf_service, account=None, *, write_pool: RpcPool | None = None):
        self.cfg = cfg
        self.pool = pool
        self.write_pool = write_pool or pool
        self.db = db
        self.hf = hf_service
        self.account = account
        self.address = account.address if account is not None else to_checksum_address(cfg.owner)
        self.store = ContentStore(cfg.deliverables_dir, cfg.base_url)
        self.guard = GasGuard(self.write_pool, cfg.gas_price_cap_wei, cfg.bnb_reserve_wei, cfg.live)
        self.price = price_atomic(cfg)
        self.wake = threading.Event()
        self._sdk_lock = threading.Lock()
        self._client = None
        self._ops = None
        self._handler = None
        self._limiter = None
        self._dispute_window: int | None = None
        self.last_tick: float | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------ SDK wiring

    @property
    def can_sign(self) -> bool:
        return self.account is not None

    @property
    def signer_matches_owner(self) -> bool:
        return self.account is not None and self.account.address.lower() == self.cfg.owner.lower()

    def sdk(self):
        """(ERC8183Client, ERC8183JobOps, NegotiationHandler), built once."""
        with self._sdk_lock:
            if self._client is None:
                if self.account is None:
                    raise RuntimeError("no signing key configured (VITALS_KEY_FILE)")
                from bnbagent.erc8183 import ERC8183Client, ERC8183JobOps, NegotiationHandler

                install_pool_web3(self.pool, self.write_pool, self.cfg.log_window)
                wallet = sdk_wallet(self.account)
                net = network_config(self.cfg)
                client = ERC8183Client(wallet, network=net)
                token = to_checksum_address(client.payment_token)
                if token != to_checksum_address(self.cfg.payment_token):
                    raise RuntimeError(f"AgenticCommerce pays in {token}, expected {self.cfg.payment_token}")
                self._handler = NegotiationHandler(
                    service_price=str(self.price), currency=token, wallet_provider=wallet,
                    quote_ttl_seconds=int(self.cfg.quote_ttl), chain_id=net.chain_id,
                    verifying_contract=client.commerce.address,
                )
                self._ops = ERC8183JobOps(wallet, network=net, storage_provider=self.store,
                                          service_price=self.price)
                self._ops._client = client  # share one client (and its nonce manager)
                self._client = client
            return self._client, self._ops, self._handler

    def limiter(self):
        if self._limiter is None:
            from bnbagent.utils import SlidingWindowLimiter

            self._limiter = (SlidingWindowLimiter(max_requests=30, window_seconds=60.0, max_keys=10_000),
                             SlidingWindowLimiter(max_requests=600, window_seconds=60.0, max_keys=1))
        return self._limiter

    # ------------------------------------------------------------- negotiate

    @staticmethod
    def _rejection(request: dict, code: str, reason: str) -> dict:
        # An empty negotiation_hash is what lets Marque's quote reader see a decline (and retry
        # with the card's own JSON example) instead of "no quote in the response".
        return {"request": request, "request_hash": "", "negotiation_hash": "",
                "response": {"accepted": False, "reason_code": code, "reason": reason}, "response_hash": ""}

    def negotiate(self, data: dict, client: str = "unknown") -> dict:
        td = data.get("task_description")
        if td is None:
            td = data.get("description") or data.get("task") or data.get("prompt")
        if isinstance(td, (dict, list)):
            td = json.dumps(td, separators=(",", ":"))
        terms = data.get("terms") if isinstance(data.get("terms"), dict) else {}
        terms = {**DEFAULT_TERMS, **{k: v for k, v in terms.items() if v not in (None, "")}}
        request = {"task_description": td if isinstance(td, str) else "", "terms": terms}
        if not isinstance(td, str) or not td.strip():
            return self._rejection(request, "0x04", "task_description is required: name the 0x address of the "
                                   "Venus Core Pool account to report on (optionally a block and a target HF)")
        try:
            task = parse_task(td)
        except TaskError as exc:
            return self._rejection(request, "0x04", str(exc))
        if not self.can_sign:
            return self._rejection(request, "0x05", "this instance has no signing key; quotes are unavailable")
        if not self.signer_matches_owner:
            return self._rejection(request, "0x05", "signing wallet does not match the registered owner; refusing "
                                   "to quote")
        try:
            per_ip, global_ = self.limiter()
            per_ip.check(client)
            global_.check("global")
        except Exception:
            return self._rejection(request, "0x05", "rate limited: retry in a minute")
        _, _, handler = self.sdk()
        result = handler.negotiate(request)
        env = result.to_dict()
        if result.accepted:
            self.db.add_quote(env["negotiation_hash"], int(env["response"]["quote_expires_at"]),
                              str(self.price), td, task.address, client)
            env["provider_address"] = self.address
            env["parsed_task"] = task.echo()
        return env

    # ---------------------------------------------------------------- notify

    def notify_funded(self, data: dict) -> dict:
        raw = data.get("job_id", data.get("jobId"))
        if raw in (None, ""):
            self.wake.set()
            return {"status": "accepted", "note": "no job_id: scanning funded jobs; read the result from the chain"}
        try:
            job_id = int(str(raw), 0)
        except ValueError:
            return {"status": "rejected", "error": f"invalid job_id: {raw!r}"}
        try:
            job = self.read_jobs([job_id])[job_id]
        except Exception:
            self.wake.set()
            return {"status": "accepted", "job_id": job_id, "note": "chain read failed; delivery will retry"}
        if job is None:
            return {"status": "rejected", "job_id": job_id, "reason": "job not found"}
        if job["provider"].lower() != self.address.lower():
            return {"status": "rejected", "job_id": job_id, "reason": "this agent is not the job's provider"}
        if job["status"] not in (J.FUNDED, J.SUBMITTED, J.COMPLETED):
            return {"status": "rejected", "job_id": job_id,
                    "reason": f"job status is {J.CHAIN_STATUS_NAMES.get(job['status'])}, expected FUNDED"}
        self.ingest(job)
        self.wake.set()
        return {"status": "accepted", "job_id": job_id,
                "note": "delivery started; poll the chain (SUBMITTED / deliverable) or GET /api/jobs/" + str(job_id)}

    def job_status(self, raw_id: Any) -> dict:
        try:
            job_id = int(str(raw_id), 0)
        except (TypeError, ValueError):
            return {"error": "job_id must be an integer"}
        job = self.read_jobs([job_id]).get(job_id)
        local = self.db.job(job_id)
        if job is None:
            return {"job_id": job_id, "error": "job not found"}
        return {
            "job_id": job_id, "client": job["client"], "provider": job["provider"],
            "status": J.CHAIN_STATUS_NAMES.get(job["status"], str(job["status"])), "budget": str(job["budget"]),
            "expired_at": job["expiredAt"], "submitted_at": job["submittedAt"], "deliverable": job["deliverable"],
            "deliverable_url": (local or {}).get("deliverable_url"), "local_state": (local or {}).get("state"),
        }

    # ------------------------------------------------------------ chain reads

    def read_jobs(self, ids: list[int]) -> dict[int, dict | None]:
        out: dict[int, dict | None] = {}
        if not ids:
            return out
        res = multicall(self.pool, self.cfg.multicall, [Call(self.cfg.commerce, "getJob(uint256)", (i,)) for i in ids],
                        "latest")
        for i, r in zip(ids, res):
            try:
                job = decode_job(r.raw) if r.success and r.raw else None
            except Exception:
                job = None
            out[i] = job if job and job["id"] == i else None
        return out

    def job_counter(self) -> int:
        raw = self.pool.eth_call(self.cfg.commerce, encode_call_hex("jobCounter()"))
        return int(raw, 16)

    def router_policy(self, job_id: int) -> str:
        raw = self.pool.eth_call(self.cfg.router, encode_call_hex("jobPolicy(uint256)", job_id))
        return to_checksum_address("0x" + raw[-40:])

    def job_token(self, job_id: int) -> str:
        raw = self.pool.eth_call(self.cfg.commerce, encode_call_hex("jobPaymentToken(uint256)", job_id))
        return to_checksum_address("0x" + raw[-40:])

    def dispute_window(self) -> int:
        if self._dispute_window is None:
            self._dispute_window = int(self.pool.eth_call(self.cfg.policy, encode_call_hex("disputeWindow()")), 16)
        return self._dispute_window

    def policy_state(self, job_id: int) -> dict:
        res = multicall(self.pool, self.cfg.multicall, [
            Call(self.cfg.policy, "submittedAt(uint256)", (job_id,), ("uint64",)),
            Call(self.cfg.policy, "disputed(uint256)", (job_id,), ("bool",)),
            Call(self.cfg.policy, "check(uint256,bytes)", (job_id, b""), ("uint8", "bytes32")),
        ], "latest")
        return {
            "submittedAt": int(res[0].value[0]) if res[0].success else 0,
            "disputed": bool(res[1].value[0]) if res[1].success else False,
            "verdict": int(res[2].value[0]) if res[2].success else None,
        }

    # ------------------------------------------------------------- watcher

    def ingest(self, job: dict) -> dict:
        local = self.db.job(job["id"])
        current = local["state"] if local else None
        new = J.from_chain(current, job["status"])
        fields = dict(state=new, chain_status=job["status"], client=job["client"], provider=job["provider"],
                      evaluator=job["evaluator"], hook=job["hook"], budget=str(job["budget"]),
                      expired_at=job["expiredAt"], submitted_at=job["submittedAt"])
        if local is None:
            fields["description"] = job["description"][:8192]
            fields["task"] = task_text_from_description(job["description"])[:4096]
            self.db.event("job_seen", {"job_id": job["id"], "status": job["status"]})
        if job["deliverable"] != "0x" + "00" * 32:
            fields["deliverable_hash"] = job["deliverable"]
        if current != new:
            self.db.event("job_state", {"job_id": job["id"], "from": current, "to": new})
        return self.db.upsert_job(job["id"], **fields)

    def scan_new_jobs(self) -> int:
        counter = self.job_counter()
        cursor = self.db.get("job_cursor")
        if cursor is None:
            start = self.cfg.watch_from_job if getattr(self.cfg, "watch_from_job", None) else max(0, counter - 2000)
            cursor = start
        found = 0
        ids = list(range(int(cursor) + 1, counter + 1))
        for i in range(0, len(ids), 150):
            chunk = ids[i : i + 150]
            for jid, job in self.read_jobs(chunk).items():
                if job and job["provider"].lower() == self.address.lower():
                    self.ingest(job)
                    found += 1
            self.db.put("job_cursor", chunk[-1])
        return found

    def scan_funded_logs(self) -> int:
        """Independent watcher: JobFunded logs where provider (topic 3) is us."""
        head = self.pool.block_number()
        cursor = self.db.get("log_cursor")
        if cursor is None:
            cursor = self.cfg.watch_from_block or max(0, head - 2000)
        start = int(cursor) + 1
        if start > head:
            return 0
        end = min(head, start + 20_000)
        logs = self.pool.get_logs_range(self.cfg.commerce, [JOB_FUNDED_TOPIC, None, None, topic_address(self.address)],
                                        start, end, window=self.cfg.log_window)
        ids = sorted({int(lg["topics"][1], 16) for lg in logs})
        for jid, job in self.read_jobs(ids).items():
            if job:
                self.ingest(job)
        self.db.put("log_cursor", end)
        return len(ids)

    def refresh_active(self) -> None:
        active = [j["job_id"] for j in self.db.jobs(J.ACTIVE)]
        for jid, job in self.read_jobs(active).items():
            if job:
                self.ingest(job)

    # -------------------------------------------------------------- deliver

    def _fail(self, job_id: int, error: str, *, permanent: bool) -> None:
        row = self.db.job(job_id) or {}
        attempts = int(row.get("attempts") or 0) + 1
        if permanent or attempts >= MAX_ATTEMPTS:
            self.db.upsert_job(job_id, state="skipped", error=error[:500], attempts=attempts)
            self.db.event("job_skipped", {"job_id": job_id, "error": error[:300]})
        else:
            delay = min(30 * 2 ** (attempts - 1), 1800)
            self.db.upsert_job(job_id, state="funded", error=error[:500], attempts=attempts,
                               next_attempt_at=int(time.time()) + delay)

    def deliver(self, job_id: int) -> dict:
        """Verify, compute, store and submit one funded job. Safe to call repeatedly."""
        if not self.db.claim_job(job_id, "funded", "delivering"):
            return {"ok": False, "reason": "not in funded state (already handled or in progress)"}
        try:
            job = self.read_jobs([job_id]).get(job_id)
            if job is None:
                self._fail(job_id, "job not readable", permanent=False)
                return {"ok": False}
            state = self.ingest(job)["state"]
            if job["status"] != J.FUNDED:
                return {"ok": False, "reason": f"chain status {J.CHAIN_STATUS_NAMES.get(job['status'])}"}
            if state != "delivering":
                self.db.upsert_job(job_id, state="delivering")
            me = self.address.lower()
            problems = []
            if job["provider"].lower() != me:
                problems.append("provider is not this agent")
            if job["evaluator"].lower() != self.cfg.router.lower():
                problems.append("evaluator is not the canonical EvaluatorRouter")
            if job["hook"].lower() != self.cfg.router.lower():
                problems.append("hook is not the canonical EvaluatorRouter")
            policy = self.router_policy(job_id)
            if policy.lower() != self.cfg.policy.lower():
                problems.append(f"router policy {policy} is not the canonical OptimisticPolicy")
            if self.job_token(job_id).lower() != self.cfg.payment_token.lower():
                problems.append("payment token is not U")
            if int(job["budget"]) < self.price:
                problems.append(f"budget {job['budget']} is below the quoted price {self.price}")
            if int(job["expiredAt"]) - self.dispute_window() <= int(time.time()):
                problems.append("submission deadline passed (expiredAt - disputeWindow): the policy would revert")
            if problems:
                self._fail(job_id, "; ".join(problems), permanent=True)
                return {"ok": False, "reason": problems}
            client, ops, _ = self.sdk()
            desc = classify_description(job["description"])
            quote_check = self.check_own_quote(desc)
            path = "sdk"
            if desc["kind"] == "sdk":
                verdict = asyncio.run(ops.verify_job(job_id))
                if not verdict.get("valid"):
                    if verdict.get("retryable"):
                        self._fail(job_id, f"SDK verify_job: {verdict.get('error')}", permanent=False)
                        return {"ok": False, "reason": verdict.get("error")}
                    # The SDK refuses quotes funded after their 900 s window. Our price is fixed and
                    # the on-chain checks above passed, so a late-funded job is still delivered.
                    path = "direct"
                    quote_check["sdkVerify"] = verdict.get("error")
            else:
                # Not the SDK schema (a NegotiationResult envelope, a marketplace envelope or plain
                # text): the SDK cannot verify it, the on-chain checks above are what matter.
                path = "direct"
            task = self.task_for(desc["task"], job)
            report = self.hf.report_for(task)
            report["job"] = {"jobId": job_id, "chainId": self.cfg.chain_id, "commerce": self.cfg.commerce,
                             "client": job["client"], "provider": job["provider"], "budget": str(job["budget"]),
                             "token": self.cfg.payment_token, "descriptionKind": desc["kind"], "quote": quote_check}
            self.guard.check(self.address, 400_000)
            content = json.dumps(report, separators=(",", ":"), allow_nan=False)
            metadata = {
                "job_id": job_id, "generator": f"{ENGINE_NAME} {ENGINE_VERSION}", "account": task.address,
                "blockNumber": report.get("blockNumber"), "content_type": "application/json",
                "built_with": "https://github.com/bnb-chain/bnbagent-sdk",
            }
            with WRITE_LOCK:
                if path == "sdk":
                    res = asyncio.run(ops.submit_result(job_id, content, metadata=metadata))
                else:
                    res = self.submit_direct(client, job_id, content, metadata)
            if not res.get("success"):
                self._fail(job_id, f"submit failed: {res.get('error')}",
                           permanent=res.get("error_code") in PERMANENT_CODES)
                return {"ok": False, "reason": res.get("error")}
            self.db.upsert_job(job_id, state="submitted", submit_tx=res["txHash"], deliverable_hash=res["deliverable"],
                               deliverable_url=res["deliverableUrl"], error=None, submitted_at=int(time.time()))
            self.db.event("job_submitted", {"job_id": job_id, "tx": res["txHash"], "deliverable": res["deliverable"]})
            self.refresh_active()
            return {"ok": True, **res}
        except WriteRefused as exc:
            self._fail(job_id, f"write refused: {exc}", permanent=False)
            return {"ok": False, "reason": str(exc)}
        except Exception as exc:
            log.exception("delivery of job %s failed", job_id)
            self._fail(job_id, f"{type(exc).__name__}: {exc}", permanent=False)
            return {"ok": False, "reason": str(exc)}

    def task_for(self, text: str, job: dict):
        """Parse the buyer's task; with no address in it, report on the job client's own wallet."""
        try:
            return parse_task(text)
        except TaskError as exc:
            found = ADDRESS_IN_TEXT.findall(text or "")
            addr = found[0] if found else job["client"]
            task = parse_task(None, {"address": addr})
            task.warnings.append(
                f"task could not be fully read ({exc}); reporting on "
                + ("the address it names" if found else "the job client's own wallet") + " with defaults")
            task.sources["address"] = "task text" if found else "job.client"
            return task

    def check_own_quote(self, desc: dict) -> dict:
        """Was the quote carried by this job signed by this agent? (informational for non-SDK formats)."""
        env = desc.get("envelope")
        out: dict[str, Any] = {"present": env is not None}
        if env is None:
            return out
        try:
            from bnbagent.erc8183.negotiation import _build_description_content
            from eth_account import Account
            from eth_account.messages import encode_defunct

            if desc["kind"] == "envelope":
                content = _build_description_content(env, chain_id=env.get("chain_id"),
                                                     verifying_contract=env.get("verifying_contract"))
            else:
                content = {k: v for k, v in env.items() if k not in ("negotiation_hash", "provider_sig")}
            recomputed = "0x" + keccak(text=json.dumps(content, sort_keys=True, separators=(",", ":"))).hex()
            nh = str(env.get("negotiation_hash", ""))
            signer = Account.recover_message(encode_defunct(text=nh), signature=env.get("provider_sig"))
            out.update({
                "hashMatches": recomputed.lower() == nh.lower(),
                "signedByUs": signer.lower() == self.address.lower(),
                "issuedHere": self.db.quote(nh) is not None,
                "price": content.get("price"),
                "currency": content.get("currency"),
            })
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}"
        return out

    def submit_direct(self, client, job_id: int, content: str, metadata: dict) -> dict:
        """Build the SDK DeliverableManifest, store it content-addressed, and submit its hash
        with the SDK client (the path for job descriptions the SDK cannot verify)."""
        from bnbagent.erc8183.schema import SCHEMA_VERSION, DeliverableManifest

        try:
            manifest = DeliverableManifest(
                version=SCHEMA_VERSION, job_id=job_id, chain_id=self.cfg.chain_id,
                contracts={"commerce": client.commerce.address, "router": client.router.address,
                           "policy": client.policy.address},
                response={"content": content, "content_type": "text/plain"},
                metadata=metadata,
            )
            digest = manifest.manifest_hash()
            h, url = self.store.put(manifest.to_dict())
            if h.lower() != "0x" + digest.hex().lower().removeprefix("0x"):
                raise RuntimeError("stored deliverable hash differs from the manifest hash")
            res = client.submit(job_id, digest, {"deliverable_url": url})
            tx = res.get("transactionHash")
            tx = tx if isinstance(tx, str) else ("0x" + bytes(tx).hex() if tx is not None else None)
            return {"success": True, "txHash": tx, "deliverableUrl": url, "deliverable": h}
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    # --------------------------------------------------------------- settle

    def settle_due_jobs(self, now: int | None = None) -> list[dict]:
        out = []
        if not self.cfg.auto_settle or not self.can_sign:
            return out
        for row in self.db.jobs(["submitted"]):
            jid = row["job_id"]
            ps = self.policy_state(jid)
            now_ts = now if now is not None else int(self.pool.get_block("latest")["timestamp"], 16)
            if not J.settle_due(J.SUBMITTED, ps["submittedAt"], self.dispute_window(), now_ts, ps["disputed"]):
                continue
            if ps["verdict"] != 1:  # Verdict.APPROVE
                continue
            if not self.db.claim_job(jid, "submitted", "settling"):
                continue
            try:
                self.guard.check(self.address, 400_000)
                client, _, _ = self.sdk()
                with WRITE_LOCK:
                    res = client.settle(jid)
                tx = res.get("transactionHash") if isinstance(res, dict) else None
                tx = tx.hex() if hasattr(tx, "hex") and not isinstance(tx, str) else tx
                self.db.upsert_job(jid, settle_tx=tx)
                self.db.event("job_settled", {"job_id": jid, "tx": tx})
                out.append({"job_id": jid, "tx": tx})
            except Exception as exc:
                self.db.upsert_job(jid, state="submitted", error=f"settle: {type(exc).__name__}: {exc}"[:500])
                log.warning("settle of job %s failed: %s", jid, exc)
            self.refresh_active()
        return out

    # ----------------------------------------------------------------- tick

    def tick(self, *, logs: bool = True) -> dict:
        summary: dict[str, Any] = {}
        try:
            summary["new"] = self.scan_new_jobs()
        except Exception as exc:
            summary["scan_error"] = str(exc)[:200]
        if logs:
            try:
                summary["logs"] = self.scan_funded_logs()
            except Exception as exc:
                summary["log_error"] = str(exc)[:200]
        try:
            self.refresh_active()
        except Exception as exc:
            summary["refresh_error"] = str(exc)[:200]
        delivered = []
        now = int(time.time())
        if self.can_sign:
            for row in self.db.jobs(["funded"]):
                if int(row.get("next_attempt_at") or 0) > now:
                    continue
                if row.get("expired_at") and int(row["expired_at"]) <= now:
                    continue
                delivered.append(self.deliver(row["job_id"]))
            summary["settled"] = self.settle_due_jobs()
        summary["delivered"] = delivered
        self.last_tick = time.time()
        return summary

    def reconcile_on_start(self) -> None:
        """Crash recovery: a job left in delivering/settling is re-read from the chain."""
        for row in self.db.jobs(["delivering", "settling"]):
            back = "funded" if row["state"] == "delivering" else "submitted"
            self.db.upsert_job(row["job_id"], state=back)
        try:
            self.refresh_active()
        except Exception as exc:
            log.warning("startup reconcile failed: %s", exc)

    def public_jobs(self, limit: int = 100) -> list[dict]:
        out = []
        for r in self.db.jobs(limit=limit):
            out.append({
                "jobId": r["job_id"], "state": r["state"],
                "chainStatus": J.CHAIN_STATUS_NAMES.get(r["chain_status"]) if r["chain_status"] is not None else None,
                "client": r["client"], "budget": r["budget"], "expiredAt": r["expired_at"],
                "deliverable": r["deliverable_hash"], "deliverableUrl": r["deliverable_url"],
                "submitTx": r["submit_tx"], "settleTx": r["settle_tx"], "error": r["error"],
                "updatedAt": r["updated_at"],
            })
        return out
