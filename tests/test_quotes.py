"""ERC-8183 quotes: signing, validation and buyer-side verification.

Every key is a throwaway generated at runtime; the SDK handler is injected so
no RPC is ever touched."""

from __future__ import annotations

import copy
import json
import secrets
import time

import pytest
from bnbagent.erc8183 import verify_quote_signature
from bnbagent.erc8183.negotiation import (NegotiationHandler, _build_description_content, build_job_description,
                                          parse_job_description)
from bnbagent.wallets import EVMWalletProvider
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, keccak, to_checksum_address

from marque_format import HF1_ADDRESS, hf_prompt
from vitals.config import AGENTIC_COMMERCE, PAYMENT_TOKEN_U, Config
from vitals.db import DB
from vitals.parse import parse_task
from vitals.seller import DEFAULT_TERMS, Seller, task_text_from_description

U = to_checksum_address(PAYMENT_TOKEN_U)
COMMERCE = to_checksum_address(AGENTIC_COMMERCE)
PRICE = str(10**16)  # 0.01 U, 18 decimals
TASK = f"Account: {HF1_ADDRESS}. Block: 124010796. Report the USD of debt to repay to restore a health factor of 2.5."


# ------------------------------------------------------------------ fixtures


def sdk_handler(account) -> NegotiationHandler:
    wallet = EVMWalletProvider(password=secrets.token_urlsafe(24), private_key=account.key.hex(), persist=False)
    return NegotiationHandler(service_price=PRICE, currency=U, wallet_provider=wallet, quote_ttl_seconds=900,
                              chain_id=56, verifying_contract=COMMERCE)


@pytest.fixture
def key():
    return Account.create()


@pytest.fixture
def make_seller(cfg, monkeypatch):
    """Seller(account, owner) with the SDK handler injected (or a tripwire when none is expected)."""
    made = []

    def _make(account, owner=None, *, with_handler=True):
        cfg.owner = owner or (account.address if account is not None else Account.create().address)
        db = DB(cfg.db_path)
        made.append(db)
        seller = Seller(cfg, None, db, None, account)
        if with_handler:
            handler = sdk_handler(account)
            monkeypatch.setattr(Seller, "sdk", lambda self: (None, None, handler))
        else:
            def tripwire(self):
                raise AssertionError("the SDK must not be reached")
            monkeypatch.setattr(Seller, "sdk", tripwire)
        return seller, db

    yield _make
    for db in made:
        db.close()


@pytest.fixture
def quote(make_seller, key):
    seller, db = make_seller(key)
    env = seller.negotiate({"task_description": TASK, "terms": dict(DEFAULT_TERMS)}, client="10.0.0.1")
    return env, key, db


def recompute_hash(env: dict) -> str:
    content = _build_description_content(env, chain_id=env.get("chain_id"),
                                         verifying_contract=env.get("verifying_contract"))
    return "0x" + keccak(text=json.dumps(content, sort_keys=True, separators=(",", ":"))).hex()


def recover(h: str, sig: str) -> str:
    return Account.recover_message(encode_defunct(text=h), signature=sig)


class _FakeEth:
    chain_id = 56

    def __init__(self, now: int):
        self.now = now

    def get_block(self, ident):  # noqa: ARG002
        return {"timestamp": self.now}

    def get_code(self, *args, **kwargs):  # noqa: ARG002
        return b""


class FakeW3:
    def __init__(self, now: int):
        self.eth = _FakeEth(now)


# ------------------------------------------------------------ accepted quote


def test_accepted_envelope_shape(quote):
    env, key, _ = quote
    assert env["response"]["accepted"] is True
    assert env["negotiation_hash"].startswith("0x") and len(env["negotiation_hash"]) == 66
    assert env["provider_sig"].startswith("0x") and len(env["provider_sig"]) == 132
    assert env["chain_id"] == 56
    assert env["verifying_contract"] == COMMERCE
    terms = env["response"]["terms"]
    assert terms["price"] == "10000000000000000" == PRICE
    assert terms["currency"] == U
    assert env["request"]["task_description"] == TASK
    assert env["provider_address"] == key.address
    assert env["parsed_task"]["address"] == HF1_ADDRESS
    assert env["parsed_task"]["blockNumber"] == 124010796
    assert env["parsed_task"]["targetHealthFactor"] == 2.5


def test_seller_price_matches_the_quoted_price(make_seller, key):
    seller, _ = make_seller(key)
    assert seller.price == 10**16
    assert seller.address == key.address and seller.signer_matches_owner


def test_negotiation_hash_recomputes_from_the_sdk_content(quote):
    env, _, _ = quote
    assert recompute_hash(env) == env["negotiation_hash"]


def test_signature_recovers_the_throwaway_signer(quote):
    env, key, _ = quote
    assert recover(env["negotiation_hash"], env["provider_sig"]) == key.address


def test_sdk_reference_verifier_accepts_the_quote(quote):
    env, key, _ = quote
    verdict = verify_quote_signature(envelope=env, provider=key.address, w3=FakeW3(int(time.time())),
                                     expected_verifying_contract=COMMERCE)
    assert verdict.valid and verdict.method == "eip191" and verdict.signer == key.address
    other = Account.create().address
    assert not verify_quote_signature(envelope=env, provider=other, w3=FakeW3(int(time.time()))).valid
    later = env["response"]["quote_expires_at"]
    assert verify_quote_signature(envelope=env, provider=key.address, w3=FakeW3(later)).reason == "quote has expired"


def test_quote_ttl_is_at_most_900_seconds(quote):
    env, _, _ = quote
    resp = env["response"]
    assert 0 < resp["quote_expires_at"] - resp["negotiated_at"] <= 900
    assert abs(resp["negotiated_at"] - time.time()) < 60


def test_config_caps_the_quote_ttl(monkeypatch):
    monkeypatch.setenv("VITALS_QUOTE_TTL", "5000")
    assert Config.from_env().quote_ttl == 900


def test_quote_is_recorded(quote):
    env, key, db = quote
    row = db.quote(env["negotiation_hash"].upper().replace("0X", "0x"))
    assert row is not None
    assert row["price"] == PRICE and row["address"] == HF1_ADDRESS and row["client"] == "10.0.0.1"
    assert row["expires_at"] == env["response"]["quote_expires_at"]
    assert db.quote_count() == 1


def test_job_description_round_trip(quote):
    env, key, _ = quote
    desc = build_job_description(env)
    jd = parse_job_description(desc)
    assert jd is not None and jd.version == 1
    assert jd.task == TASK
    assert jd.price == PRICE and jd.currency == U
    assert jd.negotiation_hash == env["negotiation_hash"] and jd.provider_sig == env["provider_sig"]
    assert jd.negotiated_at == env["response"]["negotiated_at"]
    assert jd.quote_expires_at == env["response"]["quote_expires_at"]
    assert jd.terms == {"deliverables": DEFAULT_TERMS["deliverables"],
                        "quality_standards": DEFAULT_TERMS["quality_standards"]}
    raw = json.loads(desc)
    assert raw["chain_id"] == 56 and raw["verifying_contract"] == COMMERCE
    # The description re-hashes to the signed hash (what a dispute voter or indexer checks) ...
    unsigned = {k: v for k, v in raw.items() if k not in ("negotiation_hash", "provider_sig")}
    assert "0x" + keccak(text=json.dumps(unsigned, sort_keys=True, separators=(",", ":"))).hex() == jd.negotiation_hash
    assert recover(jd.negotiation_hash, jd.provider_sig) == key.address
    # ... and the seller reads the buyer's task back out of it when the job is funded.
    assert task_text_from_description(desc) == TASK
    task = parse_task(task_text_from_description(desc))
    assert (task.address, task.block_number) == (HF1_ADDRESS, 124010796)


@pytest.mark.parametrize("path,value", [
    (("response", "terms", "price"), "1"),
    (("response", "terms", "price"), str(10**16 + 1)),
    (("response", "terms", "currency"), "0x55d398326f99059fF775485246999027B3197955"),
    (("response", "terms", "deliverables"), "something else"),
    (("response", "quote_expires_at"), 4_102_444_800),
    (("request", "task_description"), TASK.replace("2.5", "1.5")),
    (("request", "task_description"), "Account: 0x" + "12" * 20),
    (("chain_id",), 97),
    (("verifying_contract",), "0x" + "34" * 20),
])
def test_tampering_breaks_the_hash(quote, path, value):
    env, key, _ = quote
    bad = copy.deepcopy(env)
    node = bad
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value
    assert recompute_hash(bad) != env["negotiation_hash"]
    verdict = verify_quote_signature(envelope=bad, provider=key.address, w3=FakeW3(int(time.time())))
    assert not verdict.valid


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("data", [
    {"task_description": "please check my loan"},
    {"task_description": ""},
    {"task_description": "   "},
    {},
    {"task_description": {"blockNumber": 1}},
    {"task": "Account: 0x1234 health factor"},
])
def test_task_without_an_address_is_rejected_0x04(make_seller, key, data):
    seller, db = make_seller(key, with_handler=False)
    env = seller.negotiate({**data, "terms": dict(DEFAULT_TERMS)})
    resp = env["response"]
    assert resp["accepted"] is False
    assert resp["reason_code"] == "0x04"
    assert "address" in resp["reason"] and "0x" in resp["reason"]
    # An empty negotiation_hash marks the decline for Marque's quote reader; nothing is signed.
    assert env["negotiation_hash"] == "" and "provider_sig" not in env
    assert db.quote_count() == 0


def test_target_at_or_below_one_is_rejected_0x04(make_seller, key):
    seller, _ = make_seller(key, with_handler=False)
    env = seller.negotiate({"task_description": json.dumps({"address": HF1_ADDRESS, "targetHealthFactor": 1})})
    assert env["response"]["reason_code"] == "0x04" and "above 1" in env["response"]["reason"]


@pytest.mark.parametrize("field", ["task_description", "description", "task", "prompt"])
def test_task_aliases_are_accepted(make_seller, key, field):
    seller, _ = make_seller(key)
    env = seller.negotiate({field: TASK})
    assert env["response"]["accepted"] is True
    assert env["request"]["task_description"] == TASK


def test_structured_task_is_serialised_compactly(make_seller, key):
    seller, _ = make_seller(key)
    task = {"address": HF1_ADDRESS, "blockNumber": 124010796, "targetHealthFactor": 2.5}
    env = seller.negotiate({"task_description": task})
    assert env["response"]["accepted"] is True
    assert env["request"]["task_description"] == json.dumps(task, separators=(",", ":"))
    assert recompute_hash(env) == env["negotiation_hash"]


@pytest.mark.parametrize("terms", [None, {}, {"deliverables": "", "quality_standards": None}, "not a dict"])
def test_missing_terms_are_filled_with_defaults(make_seller, key, terms):
    seller, _ = make_seller(key)
    data = {"task_description": TASK}
    if terms is not None:
        data["terms"] = terms
    env = seller.negotiate(data)
    assert env["response"]["accepted"] is True
    for side in (env["request"]["terms"], env["response"]["terms"]):
        assert side["deliverables"] == DEFAULT_TERMS["deliverables"]
        assert side["quality_standards"] == DEFAULT_TERMS["quality_standards"]


def test_partial_terms_keep_the_buyers_wording(make_seller, key):
    seller, _ = make_seller(key)
    env = seller.negotiate({"task_description": TASK, "terms": {"deliverables": "HF report as JSON"}})
    terms = env["response"]["terms"]
    assert terms["deliverables"] == "HF report as JSON"
    assert terms["quality_standards"] == DEFAULT_TERMS["quality_standards"]


def test_signer_other_than_the_owner_is_refused_0x05(make_seller, key):
    seller, db = make_seller(key, owner=Account.create().address, with_handler=False)
    assert seller.can_sign and not seller.signer_matches_owner
    env = seller.negotiate({"task_description": TASK})
    assert env["response"]["accepted"] is False
    assert env["response"]["reason_code"] == "0x05"
    assert "owner" in env["response"]["reason"]
    assert db.quote_count() == 0


def test_no_key_is_refused_0x05(make_seller):
    seller, _ = make_seller(None, with_handler=False)
    assert not seller.can_sign
    env = seller.negotiate({"task_description": TASK})
    assert env["response"]["accepted"] is False and env["response"]["reason_code"] == "0x05"
    assert "signing key" in env["response"]["reason"]


def test_no_key_sdk_refuses_to_build(cfg):
    cfg.owner = Account.create().address
    seller = Seller(cfg, None, None, None, None)
    with pytest.raises(RuntimeError, match="no signing key"):
        Seller.sdk(seller)


def test_oversized_task_is_rejected_by_the_sdk(make_seller, key):
    seller, db = make_seller(key)
    env = seller.negotiate({"task_description": TASK + " " + "x" * 5000})
    assert env["response"]["accepted"] is False
    assert env["response"]["reason_code"] == "0x07"
    assert db.quote_count() == 0


def test_per_client_rate_limit(make_seller, key):
    seller, _ = make_seller(key)
    codes = [seller.negotiate({"task_description": TASK}, client="1.2.3.4")["response"].get("reason_code")
             for _ in range(31)]
    assert codes[:30] == [None] * 30
    assert codes[30] == "0x05"
    # Another client is not affected.
    assert seller.negotiate({"task_description": TASK}, client="5.6.7.8")["response"]["accepted"] is True


# ------------------------------------------- Mandate's checkSdkQuote, ported


def _js_string(s: str) -> str:
    """JSON.stringify(s) followed by Mandate's escape of every char in [\\u007f-\\uffff]."""
    short = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch in short:
            out.append(short[ch])
        elif o < 0x20:
            out.append(f"\\u{o:04x}")
        elif o < 0x7F:
            out.append(ch)
        elif o <= 0xFFFF:
            out.append(f"\\u{o:04x}")
        else:  # JS strings are UTF-16: each surrogate is escaped on its own
            o -= 0x10000
            out.append(f"\\u{0xD800 + (o >> 10):04x}\\u{0xDC00 + (o & 0x3FF):04x}")
    out.append('"')
    return "".join(out)


def py_json(v) -> str:
    """Port of Mandate's pyJson (src/lib/escrow/sdk.ts)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ",".join(py_json(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{py_json(k)}:{py_json(v[k])}" for k in sorted(v)) + "}"
    if isinstance(v, str):
        return _js_string(v)
    return json.dumps(v)


def sanitize(s) -> str:
    t = s if isinstance(s, str) else ("" if s is None else str(s))
    t = t.replace("[", "(").replace("]", ")")
    return "".join(ch for ch in t if ord(ch) >= 0x20 or ch in ("\t", "\n"))


def _coalesce(a, b):
    return a if a is not None else b


def signed_content(q: dict) -> dict:
    t = q["response"].get("terms") or {}
    if t.get("price") is None:
        raise ValueError("the quote has no price")
    if not t.get("currency"):
        raise ValueError("the quote has no currency")
    terms = {"deliverables": sanitize(_coalesce(t.get("deliverables"), "")),
             "quality_standards": sanitize(_coalesce(t.get("quality_standards"), ""))}
    if t.get("success_criteria"):
        terms["success_criteria"] = [sanitize(c) for c in t["success_criteria"]]
    content = {
        "version": 1,
        "negotiated_at": _coalesce(q.get("negotiated_at"), q["response"].get("negotiated_at")),
        "task": sanitize(_coalesce(q["request"].get("task_description"), "")),
        "terms": terms,
        "price": str(int(str(t["price"]))),
        "currency": t["currency"],
    }
    expires = _coalesce(q.get("quote_expires_at"), q["response"].get("quote_expires_at"))
    if expires is not None:
        content["quote_expires_at"] = expires
    if q.get("chain_id") is not None:
        content["chain_id"] = q["chain_id"]
    if q.get("verifying_contract"):
        content["verifying_contract"] = to_checksum_address(q["verifying_contract"])
    return content


def check_sdk_quote(q: dict, *, chain_id: int, commerce: str, token: str, signers: list[str],
                    now: int | None = None) -> dict:
    """Port of Mandate's checkSdkQuote: what a buyer there runs before funding."""
    if q["response"].get("accepted") is False:
        return {"refused": f"it declined: {_coalesce(q['response'].get('reason'), 'no reason given')}"}
    if not q.get("negotiation_hash") or not q.get("provider_sig"):
        return {"refused": "its quote is not signed"}
    try:
        content = signed_content(q)
    except ValueError as exc:
        return {"refused": str(exc)}
    h = "0x" + keccak(text=py_json(content)).hex()
    if h.lower() != q["negotiation_hash"].lower():
        return {"refused": "its quote's hash does not match its terms"}
    if q.get("chain_id") is not None and int(q["chain_id"]) != chain_id:
        return {"refused": f"it settles on chain {q['chain_id']}, not BNB Smart Chain"}
    vc = q.get("verifying_contract")
    if vc and (not is_address(vc) or to_checksum_address(vc) != to_checksum_address(commerce)):
        return {"refused": "its escrow is a different contract from the ERC-8183 kernel we use"}
    currency = str(content["currency"])
    if not is_address(currency) or to_checksum_address(currency) != to_checksum_address(token):
        return {"refused": "it wants a token other than $U"}
    expires_at = content.get("quote_expires_at") if isinstance(content.get("quote_expires_at"), int) else None
    if expires_at is not None and expires_at <= (now if now is not None else int(time.time())):
        return {"refused": "its quote has expired"}
    try:
        signer = recover(q["negotiation_hash"], q["provider_sig"])
    except Exception:
        return {"refused": "its quote's signature cannot be read"}
    if signers and not any(s.lower() == signer.lower() for s in signers):
        return {"refused": "its quote is signed by a wallet this agent's registration does not name"}
    eta = q["response"].get("estimated_completion_seconds")
    return {"ok": {"provider": signer, "price": int(content["price"]), "currency": to_checksum_address(currency),
                   "expiresAt": expires_at, "etaSeconds": eta if isinstance(eta, int) else None,
                   "task": content["task"]}}


def read_signed_description(description: str) -> dict | None:
    """Port of Mandate's readSignedDescription."""
    try:
        d = json.loads(description)
    except ValueError:
        return None
    if (not isinstance(d, dict) or d.get("version") != 1 or not isinstance(d.get("negotiation_hash"), str)
            or not isinstance(d.get("provider_sig"), str)):
        return None
    h, sig = d.pop("negotiation_hash"), d.pop("provider_sig")
    if "0x" + keccak(text=py_json(d)).hex() != h.lower():
        return {"refused": "its description does not hash to the quote it carries"}
    return {"signer": recover(h, sig), "price": int(d["price"]), "currency": d["currency"], "task": d.get("task", "")}


def mandate_want(key, **kw):
    return {"chain_id": 56, "commerce": COMMERCE, "token": U, "signers": [key.address], **kw}


@pytest.fixture
def marque_quote(make_seller, key):
    """A quote on Marque's prompt: non-ASCII (’ and —) and newlines exercise the escaping rules."""
    seller, _ = make_seller(key)
    env = seller.negotiate({"skill": "negotiate-erc8183-job", "task_description": hf_prompt(),
                            "terms": {"deliverables": "HF [report] as JSON\x07", "quality_standards": "on chain"}})
    assert env["response"]["accepted"] is True
    return env, key


def test_py_json_port_equals_python_json_dumps(marque_quote):
    env, _ = marque_quote
    content = signed_content(env)
    assert content == _build_description_content(env, chain_id=env["chain_id"],
                                                 verifying_contract=env["verifying_contract"])
    assert py_json(content) == json.dumps(content, sort_keys=True, separators=(",", ":"))
    assert "\\u2019" in py_json(content) and "\\u2014" in py_json(content)
    assert content["terms"]["deliverables"] == "HF (report) as JSON"  # brackets and control chars sanitised
    for sample in ["plain", "quote \" backslash \\ tab \t nl \n cr \r", "\x00\x1f\x7f", "é ’ — 中", "emoji 😀",
                   ["a", {"z": 1, "a": [True, False, None]}], {"b": 2, "a": "x"}]:
        assert py_json(sample) == json.dumps(sample, sort_keys=True, separators=(",", ":"))


def test_mandate_checker_accepts_our_quote(marque_quote):
    env, key = marque_quote
    res = check_sdk_quote(env, **mandate_want(key))
    assert "ok" in res, res
    ok = res["ok"]
    assert ok["provider"] == key.address
    assert ok["price"] == 10**16 and ok["currency"] == U
    assert ok["expiresAt"] == env["response"]["quote_expires_at"]
    assert ok["etaSeconds"] == env["response"]["estimated_completion_seconds"]
    assert ok["task"] == hf_prompt()
    assert parse_task(ok["task"]).address == HF1_ADDRESS


def test_mandate_checker_reads_our_job_description(marque_quote):
    env, key = marque_quote
    got = read_signed_description(build_job_description(env))
    assert got == {"signer": key.address, "price": 10**16, "currency": U, "task": hf_prompt()}


def test_mandate_checker_refusals(marque_quote):
    env, key = marque_quote
    tampered = copy.deepcopy(env)
    tampered["response"]["terms"]["price"] = "1"
    assert check_sdk_quote(tampered, **mandate_want(key)) == {"refused": "its quote's hash does not match its terms"}
    exp = env["response"]["quote_expires_at"]
    assert check_sdk_quote(env, **mandate_want(key, now=exp)) == {"refused": "its quote has expired"}
    assert check_sdk_quote(env, **mandate_want(key, now=exp - 1)).get("ok")
    assert check_sdk_quote(env, **mandate_want(key, signers=[Account.create().address]))["refused"].startswith(
        "its quote is signed by a wallet")
    assert check_sdk_quote(env, **mandate_want(key, commerce="0x" + "02" * 20))["refused"].startswith("its escrow")
    assert check_sdk_quote(env, **mandate_want(key, token="0x55d398326f99059fF775485246999027B3197955")) == {
        "refused": "it wants a token other than $U"}
    assert check_sdk_quote(env, **mandate_want(key, chain_id=97))["refused"].startswith("it settles on chain 56")
    unsigned = {k: v for k, v in env.items() if k != "provider_sig"}
    assert check_sdk_quote(unsigned, **mandate_want(key)) == {"refused": "its quote is not signed"}
    forged = copy.deepcopy(env)
    forged["provider_sig"] = "0x" + "00" * 65
    assert "refused" in check_sdk_quote(forged, **mandate_want(key))


def test_mandate_checker_reports_a_decline(make_seller, key):
    seller, _ = make_seller(key, with_handler=False)
    env = seller.negotiate({"task_description": "no account here"})
    res = check_sdk_quote(env, **mandate_want(key))
    assert res["refused"].startswith("it declined: no BNB Smart Chain address found")
