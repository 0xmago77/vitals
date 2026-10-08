"""Delivery-side handling of job descriptions written by different marketplaces."""

from __future__ import annotations

import json

import pytest
from eth_account import Account

from marque_format import HF1_ADDRESS
from test_quotes import key, make_seller, quote  # noqa: F401  (fixtures)
from vitals.seller import classify_description, task_text_from_description

CLIENT = "0x00000000000000000000000000000000000Ab1e5"


def test_sdk_schema_description(quote):  # noqa: F811
    from bnbagent.erc8183.negotiation import build_job_description

    env = quote[0] if isinstance(quote, tuple) else quote
    desc = build_job_description(env)
    c = classify_description(desc)
    assert c["kind"] == "sdk"
    assert HF1_ADDRESS.lower() in c["task"].lower()


def test_dolphin_negotiation_result_envelope(quote):  # noqa: F811
    env = quote[0] if isinstance(quote, tuple) else quote
    c = classify_description(json.dumps(env))
    assert c["kind"] == "envelope"
    assert c["task"] == env["request"]["task_description"]


@pytest.mark.parametrize("desc,needle", [
    ('{"protocol":"pokter-job","version":1,"task":"Analyse my Venus position ' + HF1_ADDRESS + '"}', HF1_ADDRESS),
    ('{"marketplace":"BNB Agent Studio","input":{"prompt":"check ' + HF1_ADDRESS + '"}}', HF1_ADDRESS),
    ("via mandatemarkets.com: health factor of " + HF1_ADDRESS, HF1_ADDRESS),
    ('{"schema":"x.v1","account":"' + HF1_ADDRESS + '"}', HF1_ADDRESS),
])
def test_other_formats_keep_the_address_readable(desc, needle):
    c = classify_description(desc)
    assert c["kind"] == "other"
    assert needle.lower() in task_text_from_description(desc).lower()


def test_task_without_address_falls_back_to_job_client(make_seller, key):  # noqa: F811
    seller, _ = make_seller(key, with_handler=False)
    task = seller.task_for("Holon marketplace hire: health check", {"client": CLIENT})
    assert task.address.lower() == CLIENT.lower()
    assert task.sources["address"] == "job.client"
    assert any("client" in w for w in task.warnings)


def test_task_with_address_but_bad_target_still_reads_address(make_seller, key):  # noqa: F811
    seller, _ = make_seller(key, with_handler=False)
    task = seller.task_for(f"Account {HF1_ADDRESS}, target HF: 0.5", {"client": CLIENT})
    assert task.address.lower() == HF1_ADDRESS.lower()
    assert float(task.target_health_factor) == 2.0


def test_own_quote_check_recognises_our_signature(make_seller, key):  # noqa: F811
    seller, db = make_seller(key)
    env = seller.negotiate({"task_description": json.dumps({"address": HF1_ADDRESS}),
                            "terms": {"deliverables": "report", "quality_standards": "on chain"}})
    for desc in (json.dumps(env),):
        res = seller.check_own_quote(classify_description(desc))
        assert res["present"] and res["hashMatches"] and res["signedByUs"] and res["issuedHere"]
    from bnbagent.erc8183.negotiation import build_job_description
    res = seller.check_own_quote(classify_description(build_job_description(env)))
    assert res["hashMatches"] and res["signedByUs"]
    # Someone else's signature is reported, not trusted.
    other = Account.create()
    forged = dict(env)
    forged["provider_sig"] = "0x" + other.sign_message(
        __import__("eth_account.messages", fromlist=["encode_defunct"]).encode_defunct(text=env["negotiation_hash"])
    ).signature.hex().removeprefix("0x")
    res = seller.check_own_quote(classify_description(json.dumps(forged)))
    assert res["hashMatches"] and not res["signedByUs"]
