"""ERC-8183 job state machine, chain mapping, CAS claims and job decoding."""

from __future__ import annotations

import json
import threading
import time

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from conftest import addr
from marque_format import HF1_ADDRESS
from vitals import jobs as J
from vitals.db import DB
from vitals.seller import JOB_TUPLE, MAX_ATTEMPTS, Seller, decode_job, task_text_from_description

STATES = ["seen", "funded", "delivering", "submitted", "settling", "completed", "rejected", "expired", "skipped"]
CHAIN = [J.OPEN, J.FUNDED, J.SUBMITTED, J.COMPLETED, J.REJECTED, J.EXPIRED]

# The legal moves, written out from the lifecycle (not copied from the module).
LEGAL = {
    ("seen", "funded"), ("seen", "submitted"), ("seen", "completed"), ("seen", "rejected"), ("seen", "expired"),
    ("seen", "skipped"),
    ("funded", "delivering"), ("funded", "submitted"), ("funded", "completed"), ("funded", "rejected"),
    ("funded", "expired"), ("funded", "skipped"),
    ("delivering", "funded"), ("delivering", "submitted"), ("delivering", "completed"), ("delivering", "rejected"),
    ("delivering", "expired"), ("delivering", "skipped"),
    ("submitted", "settling"), ("submitted", "completed"), ("submitted", "rejected"), ("submitted", "expired"),
    ("settling", "submitted"), ("settling", "completed"), ("settling", "rejected"), ("settling", "expired"),
    ("skipped", "submitted"), ("skipped", "completed"), ("skipped", "rejected"), ("skipped", "expired"),
}


# ---------------------------------------------------------------- transition


@pytest.mark.parametrize("current", STATES)
@pytest.mark.parametrize("new", STATES)
def test_every_transition(current, new):
    if current == new:
        assert J.transition(current, new) == current
    elif (current, new) in LEGAL:
        assert J.transition(current, new) == new
    else:
        with pytest.raises(J.IllegalTransition):
            J.transition(current, new)


def test_state_sets():
    assert set(J.ALLOWED) == set(STATES)
    assert J.TERMINAL | J.ACTIVE == set(STATES) and not (J.TERMINAL & J.ACTIVE)
    for s in J.TERMINAL - {"skipped"}:
        assert J.ALLOWED[s] == frozenset()
    assert all("seen" not in targets for targets in J.ALLOWED.values())
    for s in ("submitted", "settling"):  # never back before a submit
        assert not ({"seen", "funded", "delivering", "skipped"} & J.ALLOWED[s])


def test_unknown_state_cannot_move():
    with pytest.raises(J.IllegalTransition):
        J.transition("weird", "funded")
    with pytest.raises(J.IllegalTransition):
        J.transition("funded", "weird")


# ---------------------------------------------------------------- from_chain

# Rows: local state; columns: OPEN, FUNDED, SUBMITTED, COMPLETED, REJECTED, EXPIRED on chain.
FROM_CHAIN = {
    None: ["seen", "funded", "submitted", "completed", "rejected", "expired"],
    "seen": ["seen", "funded", "submitted", "completed", "rejected", "expired"],
    "funded": ["seen", "funded", "submitted", "completed", "rejected", "expired"],
    "delivering": ["seen", "delivering", "submitted", "completed", "rejected", "expired"],
    # A lagging node may still say OPEN/FUNDED right after our submit: never reopen delivery.
    "submitted": ["submitted", "submitted", "submitted", "completed", "rejected", "expired"],
    "settling": ["settling", "settling", "settling", "completed", "rejected", "expired"],
    # Final on chain: nothing moves them.
    "completed": ["completed"] * 6,
    "rejected": ["rejected"] * 6,
    "expired": ["expired"] * 6,
    "skipped": ["skipped", "skipped", "submitted", "completed", "rejected", "expired"],
}


@pytest.mark.parametrize("current", list(FROM_CHAIN))
@pytest.mark.parametrize("status", CHAIN)
def test_from_chain_mapping(current, status):
    assert J.from_chain(current, status) == FROM_CHAIN[current][status]


@pytest.mark.parametrize("current", ["submitted", "settling", "completed", "rejected"])
@pytest.mark.parametrize("status", [J.OPEN, J.FUNDED])
def test_from_chain_never_goes_back_past_a_submit(current, status):
    assert J.from_chain(current, status) not in ("seen", "funded", "delivering")


@pytest.mark.parametrize("current", [None, "seen", "submitted", "settling", "skipped"])
@pytest.mark.parametrize("status", [J.SUBMITTED, J.COMPLETED, J.REJECTED, J.EXPIRED])
def test_from_chain_forward_moves_are_legal(current, status):
    new = J.from_chain(current, status)
    if current is not None:
        assert J.transition(current, new) == new


def test_chain_status_names():
    assert J.CHAIN_STATUS_NAMES == {0: "OPEN", 1: "FUNDED", 2: "SUBMITTED", 3: "COMPLETED", 4: "REJECTED",
                                    5: "EXPIRED"}


# ----------------------------------------------------------- may_deliver / settle


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("status", CHAIN)
def test_may_deliver_only_funded_both_sides(state, status):
    assert J.may_deliver(state, status) is (state == "funded" and status == J.FUNDED)


@pytest.mark.parametrize("now,disputed,status,submitted_at,want", [
    (1000 + 3600, False, J.SUBMITTED, 1000, False),  # exactly at the end of the window
    (1000 + 3601, False, J.SUBMITTED, 1000, True),  # strictly after
    (1000 + 3599, False, J.SUBMITTED, 1000, False),
    (10**9, True, J.SUBMITTED, 1000, False),  # disputed: never by silence
    (10**9, False, J.FUNDED, 1000, False),
    (10**9, False, J.COMPLETED, 1000, False),
    (10**9, False, J.SUBMITTED, 0, False),  # policy never saw the submit
])
def test_settle_due_boundaries(now, disputed, status, submitted_at, want):
    assert J.settle_due(status, submitted_at, 3600, now, disputed) is want


def test_settle_due_with_zero_window():
    assert J.settle_due(J.SUBMITTED, 500, 0, 500, False) is False
    assert J.settle_due(J.SUBMITTED, 500, 0, 501, False) is True


# ----------------------------------------------------------------- claim_job


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "jobs.sqlite3")
    yield d
    d.close()


def test_claim_job_compare_and_swap(db):
    db.upsert_job(7, state="funded")
    assert db.claim_job(7, "funded", "delivering") is True
    assert db.claim_job(7, "funded", "delivering") is False  # second claim loses
    assert db.job(7)["state"] == "delivering"
    assert db.claim_job(7, "submitted", "settling") is False  # wrong from-state
    assert db.claim_job(8, "funded", "delivering") is False  # no such job
    assert db.job(8) is None


def test_claim_job_has_one_winner_under_threads(db):
    db.upsert_job(9, state="funded")
    wins = []
    barrier = threading.Barrier(12)

    def worker():
        barrier.wait()
        wins.append(db.claim_job(9, "funded", "delivering"))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1 and wins.count(False) == 11


# ------------------------------------------------------------- job decoding


def job_tuple(job_id=42, status=J.FUNDED, description="Account: 0x... health factor", deliverable=b"\x00" * 32,
              submitted_at=0):
    client, provider, evaluator, hook = (Account.create().address for _ in range(4))
    return (job_id, client, provider, evaluator, description, 10**16, 1_760_003_600, status, hook, submitted_at,
            deliverable)


def test_decode_job_from_an_abi_encoded_get_job_tuple():
    t = job_tuple(description="Account: ’unicode’ — {\"task\": 1}")
    raw = encode([JOB_TUPLE], [t])
    job = decode_job(raw)
    assert job == {"id": 42, "client": t[1], "provider": t[2], "evaluator": t[3], "description": t[4],
                   "budget": 10**16, "expiredAt": 1_760_003_600, "status": J.FUNDED, "hook": t[8],
                   "submittedAt": 0, "deliverable": "0x" + "00" * 32}


def test_decode_job_lowercase_addresses_come_back_checksummed():
    deliverable = keccak(text="manifest")
    t = list(job_tuple(status=J.SUBMITTED, deliverable=deliverable, submitted_at=1_760_000_100))
    t[1] = t[1].lower()
    job = decode_job(encode([JOB_TUPLE], [tuple(t)]))
    assert job["client"] != t[1] and job["client"].lower() == t[1]
    assert job["deliverable"] == "0x" + deliverable.hex()
    assert job["status"] == J.SUBMITTED and job["submittedAt"] == 1_760_000_100


def test_decode_job_rejects_garbage():
    with pytest.raises(Exception):
        decode_job(b"\x00" * 31)


# --------------------------------------------------------- task in description


def test_task_text_from_sdk_schema_v1_description():
    desc = json.dumps({"version": 1, "negotiated_at": 1_760_000_000, "task": f"Account: {HF1_ADDRESS}",
                       "terms": {"deliverables": "d", "quality_standards": "q"}, "price": "1",
                       "currency": addr(5), "negotiation_hash": "0x" + "ab" * 32, "provider_sig": "0x" + "cd" * 65},
                      sort_keys=True, separators=(",", ":"))
    assert task_text_from_description(desc) == f"Account: {HF1_ADDRESS}"
    assert task_text_from_description("  \n" + desc + "\n") == f"Account: {HF1_ADDRESS}"


@pytest.mark.parametrize("description,want", [
    (f"  Account: {HF1_ADDRESS} restore it to 2  ", f"Account: {HF1_ADDRESS} restore it to 2"),
    # JSON without a task text is kept whole (compact) so its fields can still be parsed.
    ('{"address": "0x1", "blockNumber": 5}', '{"address":"0x1","blockNumber":5}'),
    ('{"task": {"subject": "x"}}', '{"subject":"x"}'),  # nested task object
    ("{not json", "{not json"),
    ('["task"]', '["task"]'),
    ("", ""),
    (None, ""),
])
def test_task_text_from_other_descriptions(description, want):
    assert task_text_from_description(description) == want


# ------------------------------------------------------ seller ingest / retry


@pytest.fixture
def seller(cfg, db):
    me = Account.create()
    cfg.owner = me.address
    return Seller(cfg, None, db, None, me)


def chain_job(seller, status, job_id=42, deliverable="0x" + "00" * 32):
    t = job_tuple(job_id=job_id, status=status, description=json.dumps({"version": 1, "task": f"Account: {HF1_ADDRESS}"}))
    job = decode_job(encode([JOB_TUPLE], [t]))
    job["provider"] = seller.address
    job["deliverable"] = deliverable
    return job


def test_ingest_follows_the_chain_and_survives_a_lagging_node(seller, db):
    row = seller.ingest(chain_job(seller, J.FUNDED))
    assert row["state"] == "funded" and row["chain_status"] == J.FUNDED
    assert row["task"] == f"Account: {HF1_ADDRESS}"
    assert J.may_deliver(row["state"], row["chain_status"])
    # We submitted; the next read hits a node that is one block behind and still says FUNDED.
    db.upsert_job(42, state="submitted")
    assert seller.ingest(chain_job(seller, J.FUNDED))["state"] == "submitted"
    assert not db.jobs(["funded"])  # so the watcher will not deliver it again
    done = "0x" + keccak(text="deliverable").hex()
    assert seller.ingest(chain_job(seller, J.SUBMITTED, deliverable=done))["deliverable_hash"] == done
    assert seller.ingest(chain_job(seller, J.COMPLETED))["state"] == "completed"
    assert seller.ingest(chain_job(seller, J.SUBMITTED))["state"] == "completed"
    kinds = [e["kind"] for e in db.events(50)]
    assert kinds.count("job_seen") == 1 and "job_state" in kinds


def test_failed_delivery_backs_off_then_skips(seller, db):
    db.upsert_job(5, state="delivering")
    before = int(time.time())
    seller._fail(5, "rpc hiccup", permanent=False)
    row = db.job(5)
    assert row["state"] == "funded" and row["attempts"] == 1
    assert before + 30 <= row["next_attempt_at"] <= int(time.time()) + 30
    for _ in range(MAX_ATTEMPTS - 1):
        seller._fail(5, "rpc hiccup", permanent=False)
    assert db.job(5)["state"] == "skipped" and db.job(5)["attempts"] == MAX_ATTEMPTS
    db.upsert_job(6, state="delivering")
    seller._fail(6, "provider is not this agent", permanent=True)
    assert db.job(6)["state"] == "skipped" and db.job(6)["attempts"] == 1


def test_public_jobs_view(seller, db):
    seller.ingest(chain_job(seller, J.FUNDED, job_id=3))
    (view,) = seller.public_jobs()
    assert view["jobId"] == 3 and view["state"] == "funded" and view["chainStatus"] == "FUNDED"
