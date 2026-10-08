"""A2A and MCP faces (in Marque's exact wire format), the agent card, the shared
HFService, and the HTTP server, all with the chain replaced by fakes."""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal

import pytest
from eth_utils import keccak

import vitals.service as service_mod
from conftest import ground_truth, hf1_account, reference, synthetic_hf_answer
from marque_format import (HF1_ADDRESS, MCP_HEADERS, a2a_body, extract_json_payload, grade_hf, hf_prompt,
                           mcp_arguments_for, mcp_initialize)
from vitals.a2a import A2AHandler, split_message
from vitals.card import EXAMPLE_ADDRESS, agent_card, registration_file
from vitals.mcp import HF_TOOL, LATEST, MCPHandler
from vitals.parse import parse_task
from vitals.rpc import RpcUnavailable
from vitals.service import HFService, refusal
from vitals.venus import EngineError, build_report, compute

GRADED = ("healthFactor", "primaryCollateralSymbol", "primaryCollateralFactor", "primaryLiquidationPriceUsd",
          "repayUsdToReachTarget")
WANTED = re.compile(r"position|liquid|health|factor|yield|apr|apy|grid|rebalanc|venus|pancake", re.I)
CANNED = {"healthFactor": 1.221, "primaryCollateralSymbol": "BTCB", "primaryCollateralFactor": 0.8,
          "primaryLiquidationPriceUsd": 68750.79421127381, "repayUsdToReachTarget": 5627.9437904761135,
          "targetHealthFactor": 2.5, "blockNumber": 124010796, "account": HF1_ADDRESS, "status": "watch",
          "markets": [{"underlyingSymbol": "BTCB", "collateralFactor": 0.8}]}
BLOCKS = sorted(reference()["cases"])


class Answer:
    """A fake hf_answer that records its inputs."""

    def __init__(self, ok=True, payload=None, exc=None):
        self.ok, self.payload, self.exc = ok, CANNED if payload is None else payload, exc
        self.calls = []

    def __call__(self, text, data):
        self.calls.append((text, data))
        if self.exc:
            raise self.exc
        return self.ok, self.payload


class FakeSeller:
    def __init__(self):
        self.calls = []

    def negotiate(self, data, client="unknown"):
        self.calls.append(("negotiate", data, client))
        return {"request": {"task_description": data.get("task_description")}, "response": {"accepted": True},
                "negotiation_hash": "0x" + "ab" * 32, "provider_sig": "0x" + "cd" * 65}

    def notify_funded(self, data):
        self.calls.append(("notify_funded", data))
        return {"status": "accepted", "job_id": int(data["job_id"])}

    def job_status(self, job_id):
        self.calls.append(("job_status", job_id))
        return {"job_id": job_id, "status": "FUNDED"}


def data_message(data, **extra):
    return {"jsonrpc": "2.0", "id": "q1", "method": "message/send",
            "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "data", "data": data}],
                                   **extra}}}


def text_message(text, **extra):
    return {"jsonrpc": "2.0", "id": "q2", "method": "message/send",
            "params": {"message": {"role": "user", "messageId": "m2", "parts": [{"kind": "text", "text": text}],
                                   **extra}}}


# ======================================================================= A2A


def test_a2a_marque_body_returns_a_task_with_json_text_first():
    answer = Answer()
    h = A2AHandler(answer)
    prompt = hf_prompt()
    resp = h.handle(a2a_body(prompt))
    resp = json.loads(json.dumps(resp, allow_nan=False))  # what goes over the wire
    assert resp["jsonrpc"] == "2.0" and resp["id"] == 1
    task = resp["result"]
    assert task["kind"] == "task" and task["status"]["state"] == "completed"
    first, second = task["artifacts"][0]["parts"]
    assert first["kind"] == "text" and json.loads(first["text"]) == CANNED
    assert second == {"kind": "data", "data": CANNED}
    assert extract_json_payload(resp) == CANNED
    assert answer.calls == [(prompt, None)]


@pytest.mark.parametrize("block", BLOCKS)
def test_a2a_end_to_end_grades_against_the_fixture(block):
    resp = A2AHandler(synthetic_hf_answer).handle(a2a_body(hf_prompt(block=int(block))))
    payload = extract_json_payload(json.loads(json.dumps(resp)))
    assert payload["blockNumber"] == int(block) and payload["account"] == HF1_ADDRESS
    diffs = grade_hf(ground_truth(block), payload)
    assert all(d["pass"] for d in diffs), diffs


def test_a2a_refusal_is_a_rejected_task_with_the_reason():
    answer = Answer(ok=False, payload=refusal("no BNB Smart Chain address found", "a 0x address"))
    h = A2AHandler(answer)
    resp = h.handle(text_message("what is my health factor?"))
    task = resp["result"]
    assert task["status"]["state"] == "rejected"
    assert task["artifacts"][0]["name"] == "refusal"
    assert json.loads(task["status"]["message"]["parts"][0]["text"])["reason"].startswith("no BNB")
    payload = extract_json_payload(resp)
    assert payload["refused"] is True and payload["healthFactor"] is None and payload["needs"] == "a 0x address"


def test_a2a_retryable_failure_is_a_failed_task():
    answer = Answer(ok=False, payload=refusal("RPC unavailable", retryable=True))
    task = A2AHandler(answer).handle(text_message(f"Account: {HF1_ADDRESS}"))["result"]
    assert task["status"]["state"] == "failed"
    assert extract_json_payload(task)["retryable"] is True


def test_a2a_synthetic_refusal_without_an_address():
    resp = A2AHandler(synthetic_hf_answer).handle(text_message("health factor please"))
    payload = extract_json_payload(resp)
    assert resp["result"]["status"]["state"] == "rejected"
    assert "0x" in payload["reason"] and payload["error"] == "refused"


def test_a2a_negotiate_data_part_routes_to_the_seller():
    seller = FakeSeller()
    answer = Answer()
    h = A2AHandler(answer, seller)
    data = {"skill": "negotiate-erc8183-job", "task_description": f"Account: {HF1_ADDRESS}",
            "terms": {"deliverables": "d", "quality_standards": "q"}}
    resp = h.handle(data_message(data, contextId="ctx-1"), client="9.9.9.9")
    msg = resp["result"]
    assert msg["kind"] == "message" and msg["role"] == "agent" and msg["contextId"] == "ctx-1"
    assert msg["parts"] == [{"kind": "data", "data": seller.negotiate(data)}]
    assert seller.calls[0] == ("negotiate", data, "9.9.9.9")
    assert answer.calls == []  # no free report


def test_a2a_negotiate_text_json_part_routes_to_the_seller():
    seller = FakeSeller()
    h = A2AHandler(Answer(), seller)
    body = {"skill": "negotiate", "task_description": f"Account: {HF1_ADDRESS}", "terms": {"deliverables": "d"}}
    resp = h.handle(text_message(json.dumps(body)))
    assert seller.calls[0][0] == "negotiate" and seller.calls[0][1] == body
    assert resp["result"]["parts"][0]["data"]["negotiation_hash"].startswith("0x")


def test_a2a_negotiate_by_message_metadata():
    seller = FakeSeller()
    h = A2AHandler(Answer(), seller)
    h.handle(data_message({"task_description": f"Account: {HF1_ADDRESS}"}, metadata={"skillId": "negotiate"}))
    assert seller.calls and seller.calls[0][0] == "negotiate"


def test_a2a_notify_funded_and_status_route_to_the_seller():
    seller = FakeSeller()
    h = A2AHandler(Answer(), seller)
    resp = h.handle(data_message({"skill": "notify_funded", "job_id": 12}))
    assert resp["result"]["parts"][0]["data"] == {"status": "accepted", "job_id": 12}
    assert seller.calls[-1] == ("notify_funded", {"skill": "notify_funded", "job_id": 12})
    resp = h.handle(data_message({"skill": "erc8183-job-status", "job_id": 12}))
    assert resp["result"]["parts"][0]["data"] == {"job_id": 12, "status": "FUNDED"}


def test_a2a_commerce_disabled_without_a_seller():
    h = A2AHandler(Answer())
    assert h.handle(data_message({"skill": "negotiate"}))["result"]["parts"][0]["data"]["accepted"] is False
    assert h.handle(data_message({"skill": "notify_funded", "job_id": 1}))["result"]["parts"][0]["data"][
        "status"] == "rejected"
    assert "error" in h.handle(data_message({"skill": "job_status", "job_id": 1}))["result"]["parts"][0]["data"]


def test_a2a_tasks_get_and_cancel():
    h = A2AHandler(Answer())
    task = h.handle(a2a_body(hf_prompt()))["result"]
    got = h.handle({"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}})
    assert got["result"] == task and got["id"] == 2
    missing = h.handle({"jsonrpc": "2.0", "id": 3, "method": "tasks/get", "params": {"id": "nope"}})
    assert missing["error"]["code"] == -32001
    cancel = h.handle({"jsonrpc": "2.0", "id": 4, "method": "tasks/cancel", "params": {"id": task["id"]}})
    assert cancel["error"]["code"] == -32002
    assert h.handle({"jsonrpc": "2.0", "id": 5, "method": "tasks/cancel", "params": {"id": "x"}})["error"][
        "code"] == -32001


def test_a2a_task_store_is_bounded():
    h = A2AHandler(Answer(), max_tasks=2)
    ids = [h.handle(a2a_body(hf_prompt()))["result"]["id"] for _ in range(3)]
    assert list(h.tasks) == ids[1:]


@pytest.mark.parametrize("body,code", [
    ({"jsonrpc": "2.0", "id": 1, "method": "tasks/resubscribeForever"}, -32601),
    ({"jsonrpc": "2.0", "id": 1, "method": "agent/getAuthenticatedExtendedCard"}, -32004),
    ({"jsonrpc": "2.0", "id": 1, "method": "tasks/pushNotificationConfig/set"}, -32003),
    ({"jsonrpc": "1.0", "id": 1, "method": "message/send"}, -32600),
    ({"jsonrpc": "2.0", "id": 1}, -32600),
    ({"jsonrpc": "2.0", "id": 1, "method": 7}, -32600),
    ("message/send", -32600),
    (42, -32600),
    ({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": "hi"}}, -32602),
])
def test_a2a_json_rpc_errors(body, code):
    resp = A2AHandler(Answer()).handle(body)
    assert resp["jsonrpc"] == "2.0" and resp["error"]["code"] == code
    if isinstance(body, dict) and "id" in body:
        assert resp["id"] == body["id"]


def test_a2a_internal_errors_do_not_leak():
    resp = A2AHandler(Answer(exc=RuntimeError("secret detail"))).handle(a2a_body(hf_prompt()))
    assert resp["error"] == {"code": -32603, "message": "Internal error: RuntimeError"}


def test_a2a_batch():
    out = A2AHandler(Answer()).handle([a2a_body(hf_prompt()), {"jsonrpc": "2.0", "id": 9, "method": "nope"}])
    assert out[0]["result"]["kind"] == "task" and out[1]["error"]["code"] == -32601


def test_split_message():
    msg = {"parts": [{"kind": "text", "text": "hello"}, {"type": "text", "text": "world"},
                     {"kind": "data", "data": {"a": 1}}, {"kind": "data", "data": {"b": 2, "skill": "negotiate"}},
                     {"text": "loose"}, "junk", {"kind": "file", "file": {}}]}
    text, data, skill = split_message(msg)
    assert text == "hello\nworld\nloose"
    assert data == {"a": 1, "b": 2, "skill": "negotiate"}
    assert skill == "negotiate"
    assert split_message({"parts": [{"kind": "text", "text": '{"skill": "notify_funded", "job_id": 3}'}]}) == (
        '{"skill": "notify_funded", "job_id": 3}', {"skill": "notify_funded", "job_id": 3}, "notify_funded")
    assert split_message({"parts": [{"kind": "text", "text": "{broken"}]}) == ("{broken", None, None)
    assert split_message({"parts": [], "metadata": {"skill_id": "x"}}) == ("", None, "x")
    assert split_message({}) == ("", None, None)


# ======================================================================= MCP


def mcp(answer=None, info=None):
    return MCPHandler(answer or Answer(), info or (lambda: {"name": "Vitals", "price": "0.01"}))


def test_mcp_initialize_echoes_the_version_and_opens_a_session():
    resp, headers = mcp().handle(mcp_initialize())
    assert resp["id"] == 1 and resp["result"]["protocolVersion"] == "2024-11-05"
    assert re.fullmatch(r"[0-9a-f]{32}", headers["Mcp-Session-Id"])
    assert resp["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert resp["result"]["serverInfo"]["name"] == "vitals"
    other, _ = mcp().handle({**mcp_initialize(), "params": {"protocolVersion": "1999-01-01"}})
    assert other["result"]["protocolVersion"] == LATEST


def test_mcp_notifications_get_no_response():
    assert mcp().handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) == (None, {})
    assert mcp().handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) == (None, {})
    assert mcp().handle({"jsonrpc": "2.0", "method": "ping"})[0] is None  # any request without an id
    assert mcp().handle({"jsonrpc": "2.0", "method": "unknown/thing"})[0] is None


def test_mcp_tools_list_is_what_marque_picks():
    resp, _ = mcp().handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = resp["result"]["tools"]
    first = tools[0]
    assert first["name"] == "venus_health_factor"
    assert WANTED.search(f"{first['name']} {first['description']}")
    assert mcp_arguments_for(first, hf_prompt(), 124010796) is not None
    # The adapter's choice: the first tool whose name/description match AND whose arguments can be built.
    picked = next(t for t in tools if WANTED.search(f"{t['name']} {t.get('description', '')}")
                  and mcp_arguments_for(t, hf_prompt(), 124010796) is not None)
    assert picked is first


def test_mcp_tools_call_puts_graded_fields_at_the_top_level():
    answer = Answer()
    prompt = hf_prompt()
    args = mcp_arguments_for(HF_TOOL, prompt, 124010796)
    resp, _ = mcp(answer).handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "venus_health_factor", "arguments": args}})
    resp = json.loads(json.dumps(resp, allow_nan=False))
    result = resp["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == CANNED and result["content"][0]["type"] == "text"
    assert result["structuredContent"] == CANNED
    for k in GRADED + ("targetHealthFactor", "blockNumber", "account"):
        assert result[k] == CANNED[k]
    assert "markets" not in result and "status" not in result  # only the graded fields are lifted
    payload = extract_json_payload(resp)
    assert {k: payload[k] for k in GRADED} == {k: CANNED[k] for k in GRADED}
    assert answer.calls == [(prompt, args)]


@pytest.mark.parametrize("block", BLOCKS)
def test_mcp_end_to_end_grades_against_the_fixture(block):
    h = mcp(synthetic_hf_answer)
    tools = h.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0]["result"]["tools"]
    args = mcp_arguments_for(tools[0], hf_prompt(block=int(block)), int(block))
    resp, _ = h.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": tools[0]["name"], "arguments": args}})
    payload = extract_json_payload(json.loads(json.dumps(resp)))
    diffs = grade_hf(ground_truth(block), payload)
    assert all(d["pass"] for d in diffs), diffs


def test_mcp_refusal_is_an_error_result():
    answer = Answer(ok=False, payload=refusal("no BNB Smart Chain address found"))
    resp, _ = mcp(answer).handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "venus_health_factor", "arguments": {"prompt": "hi"}}})
    result = resp["result"]
    assert result["isError"] is True and result["reason"].startswith("no BNB")
    assert result["healthFactor"] is None


def test_mcp_info_tool_and_unknown_tool():
    resp, _ = mcp().handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "vitals_agent_info", "arguments": {}}})
    assert resp["result"]["structuredContent"] == {"name": "Vitals", "price": "0.01"}
    bad, _ = mcp().handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"}})
    assert bad["error"]["code"] == -32602


@pytest.mark.parametrize("body,code", [
    ({"jsonrpc": "2.0", "id": 7, "method": "sampling/createMessage"}, -32601),
    ({"jsonrpc": "1.0", "id": 7, "method": "tools/list"}, -32600),
    ({"jsonrpc": "2.0", "id": 7}, -32600),
    ("tools/list", -32600),
])
def test_mcp_json_rpc_errors(body, code):
    resp, _ = mcp().handle(body)
    assert resp["error"]["code"] == code


def test_mcp_misc_methods_and_batch():
    h = mcp()
    assert h.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})[0]["result"] == {}
    assert h.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})[0]["result"] == {"resources": []}
    assert h.handle({"jsonrpc": "2.0", "id": 1, "method": "prompts/list"})[0]["result"] == {"prompts": []}
    out, headers = h.handle([mcp_initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    assert [r["id"] for r in out] == [1, 2] and "Mcp-Session-Id" in headers
    assert h.handle([{"jsonrpc": "2.0", "method": "notifications/initialized"}]) == (None, {})


def test_mcp_internal_errors_do_not_leak():
    resp, _ = mcp(Answer(exc=ValueError("secret detail"))).handle(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "venus_health_factor"}})
    assert resp["error"] == {"code": -32603, "message": "Internal error: ValueError"}


# ====================================================================== card


def test_agent_card(cfg):
    card = agent_card(cfg, provider_address=HF1_ADDRESS)
    ids = [s["id"] for s in card["skills"]]
    assert ids[0] == "venus-health-factor"
    assert {"negotiate-erc8183-job", "negotiate", "notify_funded", "erc8183-job-status"} <= set(ids)
    assert card["url"] == cfg.a2a_url and card["protocolVersion"] == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC" and card["capabilities"]["streaming"] is False
    assert card["pricing"]["atomic"] == "10000000000000000" and card["erc8183"]["provider"] == HF1_ADDRESS
    assert card["erc8183"]["quoteTtlSeconds"] <= 900
    assert card["category"] == "health_factor"
    # Marketplaces use a skill's first example as a task: it must name an account we can parse.
    hf_skill = card["skills"][0]
    assert parse_task(hf_skill["examples"][0]).address == EXAMPLE_ADDRESS
    assert parse_task(hf_skill["examples"][1]).target_health_factor == Decimal("2.5")
    neg = next(s for s in card["skills"] if s["id"] == "negotiate-erc8183-job")
    example = json.loads(neg["examples"][0])
    assert example["skill"] == "negotiate-erc8183-job"
    assert parse_task(example["task_description"]).address == EXAMPLE_ADDRESS
    json.dumps(card)


def test_registration_file(cfg):
    cfg.agent_id = 4242
    reg = registration_file(cfg)
    assert reg["type"].endswith("#registration-v1")
    assert {s["name"] for s in reg["services"]} == {"A2A", "agent-card", "MCP", "web"}
    assert reg["registrations"] == [{"agentId": 4242,
                                     "agentRegistry": f"eip155:56:{cfg.identity_registry}"}]
    cfg.agent_id = None
    assert registration_file(cfg)["registrations"] == []


# =========================================================== shared service


@pytest.fixture
def svc(cfg, monkeypatch):
    calls = []

    def fake_health_report(pool, cfg_, account, block_number=None, target=Decimal("2.0"), inputs=None):
        calls.append((account, block_number, str(target)))
        raw = hf1_account(block_number or BLOCKS[0], account)
        return build_report(compute(raw), Decimal(str(target)), inputs=inputs)

    monkeypatch.setattr(service_mod, "health_report", fake_health_report)
    s = HFService(cfg, pool=None)
    s.test_calls = calls
    return s


def test_service_answer_and_cache(svc):
    ok, first = svc.answer(hf_prompt())
    assert ok and first["healthFactor"] == 1.221 and first["assumptions"] == [] and "latencyMs" in first
    assert first["input"]["blockNumber"] == 124010796
    ok, second = svc.answer(hf_prompt())
    assert ok and second["cached"] is True and svc.test_calls == [(HF1_ADDRESS, 124010796, "2.5")]
    assert {k: second[k] for k in GRADED} == {k: first[k] for k in GRADED}
    svc.answer(f"Account: {HF1_ADDRESS}")  # latest block: never cached
    svc.answer(f"Account: {HF1_ADDRESS}")
    assert len(svc.test_calls) == 3
    ok, warned = svc.answer(f"Account: {HF1_ADDRESS}")
    assert any("no target" in w for w in warned["assumptions"])


def test_service_cache_is_not_corrupted_by_callers(svc):
    # The seller adds job details to the report it delivers; a later free answer must not carry them.
    task = parse_task(hf_prompt())
    delivered = svc.report_for(task)
    delivered["job"] = {"jobId": 1, "client": "0x" + "11" * 20}
    again = svc.report_for(task)
    assert again.get("cached") is True
    assert "job" not in again
    ok, free = svc.answer(hf_prompt())
    assert "job" not in free


def test_service_refusals(svc, monkeypatch):
    ok, payload = svc.answer("no address here")
    assert not ok and payload["refused"] is True and payload["needs"].startswith("a 0x address")

    def engine_fails(*a, **k):
        raise EngineError("the Venus Comptroller could not be read at this block")

    monkeypatch.setattr(service_mod, "health_report", engine_fails)
    ok, payload = svc.answer(f"Account: {HF1_ADDRESS}")
    assert not ok and payload["reason"].startswith("could not read Venus state") and payload["input"]["address"]

    def rpc_down(*a, **k):
        raise RpcUnavailable("all endpoints failed")

    monkeypatch.setattr(service_mod, "health_report", rpc_down)
    ok, payload = svc.answer(f"Account: {HF1_ADDRESS}")
    assert not ok and payload["retryable"] is True and payload["error"] == "unavailable"


# ==================================================================== server


class NoNetwork:
    """Stands in for every RPC pool: any use fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"the server tried to use the RPC pool ({name})")


def test_http_server_end_to_end(cfg):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from vitals.server import build_app

    async def scenario():
        app = build_app(cfg, pool=NoNetwork(), write_pool=NoNetwork())
        core = app["core"]
        core.a2a.hf_answer = synthetic_hf_answer
        core.mcp.hf_answer = synthetic_hf_answer
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/.well-known/agent-card.json")
            assert r.status == 200
            card = await r.json()
            assert card["skills"][0]["id"] == "venus-health-factor"

            # A2A, Marque's exact request.
            r = await client.post("/a2a", data=json.dumps(a2a_body(hf_prompt())),
                                  headers={"content-type": "application/json"})
            assert r.status == 200 and r.headers["X-Agent"].startswith("Vitals/")
            assert all(d["pass"] for d in grade_hf(ground_truth("124010796"), extract_json_payload(await r.json())))
            r = await client.post("/a2a", data="{not json")
            assert r.status == 400 and (await r.json())["error"]["code"] == -32700

            # MCP, Marque's exact sequence.
            r = await client.post("/mcp", data=json.dumps(mcp_initialize()), headers=MCP_HEADERS)
            assert r.status == 200 and (await r.json())["result"]["protocolVersion"] == "2024-11-05"
            sid = r.headers["Mcp-Session-Id"]
            h = {**MCP_HEADERS, "mcp-session-id": sid}
            r = await client.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                                  headers=h)
            assert r.status == 202
            r = await client.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                                  headers=h)
            tool = (await r.json())["result"]["tools"][0]
            args = mcp_arguments_for(tool, hf_prompt(), 124010796)
            r = await client.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                           "params": {"name": tool["name"], "arguments": args}}),
                                  headers=h)
            assert all(d["pass"] for d in grade_hf(ground_truth("124010796"), extract_json_payload(await r.json())))
            r = await client.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}),
                                  headers={"accept": "text/event-stream"})
            assert r.content_type == "text/event-stream"
            assert (await r.text()).startswith("event: message\ndata: ")

            # REST refusal without an address never reaches the chain.
            r = await client.post("/api/hf", data=json.dumps({"blockNumber": 1}))
            assert r.status == 400 and (await r.json())["refused"] is True

            # Quotes without a signing key are refused, not crashed.
            r = await client.post("/api/negotiate", data=json.dumps({"task_description": f"Account: {HF1_ADDRESS}"}))
            assert (await r.json())["response"]["reason_code"] == "0x05"

            # Content-addressed deliverables.
            manifest = {"version": 1, "job_id": 1, "chain_id": 56, "contracts": {}, "response": {"content": "{}"}}
            digest, _ = core.seller.store.put(manifest)
            r = await client.get(f"/deliverables/{digest}.json")
            body = await r.read()
            assert r.status == 200 and "0x" + keccak(body).hex() == digest
            r = await client.get(f"/deliverables/{digest[:-1]}0.json" if not digest.endswith("0")
                                 else f"/deliverables/{digest[:-1]}1.json")
            assert r.status == 404
            r = await client.get("/deliverables/not-a-hash.json")
            assert r.status == 400

            r = await client.get("/health")
            assert r.status == 200 and (await r.json())["signer"] is False
            r = await client.get("/api/jobs")
            assert (await r.json())["jobs"] == []
            r = await client.get("/no/such/route")
            assert r.status == 404 and (await r.json())["error"] == "not found"

    try:
        asyncio.run(scenario())
    except PermissionError as exc:  # pragma: no cover - sandbox without loopback sockets
        pytest.skip(f"cannot bind a loopback socket: {exc}")
