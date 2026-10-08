"""Content-addressed deliverable store and SQLite state."""

from __future__ import annotations

import asyncio
import json

import pytest
from bnbagent.erc8183 import DeliverableManifest
from eth_utils import keccak

from vitals.db import DB
from vitals.storage import ContentStore, canonical, content_hash

BASE = "https://vitals.example/"


def manifest(content: str = '{"healthFactor":1.221}', job_id: int = 7) -> dict:
    return DeliverableManifest(
        version=1, job_id=job_id, chain_id=56,
        contracts={"commerce": "0xea4daa3100a767e86fded867729ae7446476eba6",
                   "router": "0x51895229e12f9876011789b04f8698af06ccd6da",
                   "policy": "0x9c01845705b3078aa2e8cff7520a6376fd766de5"},
        response={"content": content, "content_type": "application/json"},
        metadata={"job_id": job_id, "generator": "vitals-hf 1.0.0", "note": "Größe ’ — ✓"},
    ).to_dict()


@pytest.fixture
def store(tmp_path):
    return ContentStore(tmp_path / "deliverables", BASE)


# ------------------------------------------------------------------ storage


def test_put_writes_canonical_json_named_by_its_keccak(store):
    m = manifest()
    h, url = store.put(m)
    path = store.dir / f"{h}.json"
    raw = path.read_bytes()
    assert raw == json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
    assert raw.isascii()  # non-ASCII is escaped, as the SDK hashes it
    assert "0x" + keccak(raw).hex() == h
    assert h == content_hash(m) == "0x" + keccak(text=canonical(m)).hex()
    assert url == f"https://vitals.example/deliverables/{h}.json"
    assert store.url_for(h) == url


def test_hash_equals_the_sdk_manifest_hash(store):
    m = manifest()
    h, _ = store.put(m)
    sdk = DeliverableManifest.from_dict(m)
    assert sdk.manifest_hash() == bytes.fromhex(h[2:])
    assert sdk.verify(bytes.fromhex(h[2:]))
    # A verifier fetching the file reproduces the on-chain bytes32.
    fetched = json.loads((store.dir / f"{h}.json").read_bytes())
    assert DeliverableManifest.from_dict(fetched).manifest_hash() == bytes.fromhex(h[2:])


def test_put_is_idempotent_and_content_addressed(store):
    m = manifest()
    h1, _ = store.put(m)
    h2, _ = store.put(json.loads(json.dumps(m)))  # same content, new object, other key order
    assert h1 == h2
    assert len(list(store.dir.glob("*.json"))) == 1
    assert not list(store.dir.glob("*.tmp"))
    h3, _ = store.put(manifest(content='{"healthFactor":1.222}'))
    assert h3 != h1 and len(list(store.dir.glob("*.json"))) == 2


def test_read_bytes(store):
    h, _ = store.put(manifest())
    assert store.read_bytes(h) == (store.dir / f"{h}.json").read_bytes()
    absent = "0x" + keccak(text="nothing stored").hex()
    assert store.read_bytes(absent) is None


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "..",
    "0x" + "a" * 63,
    "0x" + "a" * 65,
    "0x" + "g" * 64,
    "0x" + "A" * 64,  # uppercase is not the canonical form
    "0X" + "a" * 64,
    "a" * 66,
    "0x" + "a" * 64 + "/../x",
    "0x" + "a" * 64 + "\n",
    "0x" + "a" * 64 + ".json",
    "/tmp/0x" + "a" * 64,
    "",
])
def test_bad_hashes_and_path_traversal_are_rejected(store, bad):
    with pytest.raises(ValueError):
        store.path_for(bad)
    with pytest.raises(ValueError):
        store.read_bytes(bad)


def test_paths_stay_inside_the_store(store):
    h = "0x" + keccak(text="x").hex()
    assert store.path_for(h).parent == store.dir


def test_sdk_storage_provider_interface(store):
    m = manifest()
    url = asyncio.run(store.upload(m, "ignored.json"))
    h = content_hash(m)
    assert url == store.url_for(h)
    assert asyncio.run(store.exists(url)) is True
    assert asyncio.run(store.download(url)) == m  # served from disk, no HTTP
    assert asyncio.run(store.exists(store.url_for("0x" + keccak(text="other").hex()))) is False
    assert asyncio.run(store.exists("https://elsewhere.example/file.json")) is False


# ----------------------------------------------------------------------- DB


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "state" / "vitals.sqlite3")
    yield d
    d.close()


def test_db_creates_its_directory_and_reopens(tmp_path):
    p = tmp_path / "a" / "b" / "v.sqlite3"
    d = DB(p)
    d.put("k", {"v": 1})
    d.close()
    d2 = DB(p)
    assert d2.get("k") == {"v": 1}
    d2.close()


def test_kv(db):
    assert db.get("missing") is None
    assert db.get("missing", "dflt") == "dflt"
    for value in (1, "text", {"a": [1, 2]}, [1, "x"], None, 3.5, True):
        db.put("k", value)
        assert db.get("k", "dflt") == value
    db.put("job_cursor", 10)
    db.put("job_cursor", 11)
    assert db.get("job_cursor") == 11


def test_upsert_job_insert_update_and_unknown_fields(db):
    row = db.upsert_job(1)
    assert row["state"] == "seen" and row["attempts"] == 0 and row["next_attempt_at"] == 0
    assert row["created_at"] == row["updated_at"] > 0
    row = db.upsert_job(1, state="funded", chain_status=1, budget=str(10**16), client="0xabc")
    assert (row["state"], row["chain_status"], row["budget"], row["client"]) == ("funded", 1, str(10**16), "0xabc")
    row = db.upsert_job(1, error=None, attempts=2)
    assert row["state"] == "funded" and row["attempts"] == 2 and row["client"] == "0xabc"
    assert db.upsert_job(2, state="submitted", task="Account: 0x1")["task"] == "Account: 0x1"
    with pytest.raises(KeyError):
        db.upsert_job(1, created_at=5)  # not an updatable field
    with pytest.raises(KeyError):
        db.upsert_job(1, state="funded", nonsense=1)
    assert db.job(99) is None


def test_jobs_listing_filters_orders_and_limits(db):
    for jid, st in [(1, "seen"), (2, "funded"), (3, "submitted"), (4, "funded"), (5, "completed")]:
        db.upsert_job(jid, state=st)
    assert [j["job_id"] for j in db.jobs()] == [5, 4, 3, 2, 1]
    assert [j["job_id"] for j in db.jobs(["funded"])] == [4, 2]
    assert [j["job_id"] for j in db.jobs({"funded", "submitted"})] == [4, 3, 2]
    assert [j["job_id"] for j in db.jobs(limit=2)] == [5, 4]
    assert db.jobs(["expired"]) == []
    assert db.job_counts() == {"seen": 1, "funded": 2, "submitted": 1, "completed": 1}
    assert db.claim_job(4, "funded", "delivering") and db.job_counts()["funded"] == 1


def test_quotes(db):
    h = "0x" + keccak(text="quote").hex()
    db.add_quote(h.upper().replace("0X", "0x"), 1_760_000_900, str(10**16), "Account: 0x...", "0xAddr", "1.2.3.4")
    db.add_quote(h, 1, "1", "other", None, None)  # same hash: ignored
    q = db.quote(h)
    assert q["negotiation_hash"] == h and q["expires_at"] == 1_760_000_900 and q["price"] == str(10**16)
    assert db.quote(h.upper().replace("0X", "0x")) == q
    assert db.quote_count() == 1
    assert db.quote("0x" + keccak(text="none").hex()) is None


def test_keeper_actions_and_last_tx(db):
    assert db.last_keeper_tx() is None
    tx = lambda n: "0x" + keccak(text=f"tx{n}").hex()  # noqa: E731
    db.add_keeper_action("borrow", amount="1", tx_hash=tx(1), status="confirmed", hf_before="2.3",
                         hf_after="2.0", dry_run=False, ts=1000)
    db.add_keeper_action("repay", amount="0.5", tx_hash=None, status="simulated", hf_before="1.8",
                         hf_after=None, dry_run=True, ts=3000)
    db.add_keeper_action("repay", amount="0.5", tx_hash=tx(2), status="confirmed", hf_before="1.8",
                         hf_after="2.0", dry_run=True, ts=4000)  # dry run never counts
    db.add_keeper_action("borrow", amount="9", tx_hash=None, status="refused", hf_before="2.5",
                         hf_after=None, dry_run=False, ts=5000, note="debt cap")
    last_id = db.add_keeper_action("repay", amount="0.02", tx_hash=tx(3), status="failed", hf_before="2.0",
                                   hf_after=None, dry_run=False, ts=6000)
    assert isinstance(last_id, int)
    last = db.last_keeper_tx()
    assert last["tx_hash"] == tx(1) and last["ts"] == 1000 and last["dry_run"] == 0
    db.add_keeper_action("maintenance_repay", amount="0.01", tx_hash=tx(4), status="confirmed", hf_before="2.0",
                         hf_after="2.0", dry_run=False, ts=2000)
    assert db.last_keeper_tx()["tx_hash"] == tx(4)
    acts = db.keeper_actions(3)
    assert [a["action"] for a in acts] == ["maintenance_repay", "repay", "borrow"]  # newest first
    assert acts[2]["note"] == "debt cap" and acts[2]["status"] == "refused"
    assert len(db.keeper_actions()) == 6


def test_events(db):
    db.event("job_seen", {"job_id": 1, "big": 10**30})
    db.event("job_state", {"from": None, "to": "funded"})
    ev = db.events(10)
    assert [e["kind"] for e in ev] == ["job_state", "job_seen"]
    assert json.loads(ev[1]["detail"]) == {"job_id": 1, "big": 10**30}
