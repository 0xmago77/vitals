"""Task parsing: Marque's exact prompt, structured data, nesting, refusals."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from eth_utils import to_checksum_address

from marque_format import HF1_ADDRESS, HF1_POLICY, hf_prompt, mcp_arguments_for
from vitals.mcp import HF_TOOL
from vitals.parse import DEFAULT_TARGET, TaskError, parse_task

LOWER = HF1_ADDRESS.lower()
D = Decimal


def bad_checksum(address: str) -> str:
    """Flip the case of one letter so the mixed-case checksum no longer verifies."""
    for i, ch in enumerate(address[2:], start=2):
        if ch.isalpha():
            return address[:i] + ch.swapcase() + address[i + 1 :]
    raise AssertionError("address has no letters")


# --------------------------------------------------------------- Marque prompt


def test_marque_prompt_at_block_124010796():
    t = parse_task(hf_prompt(HF1_ADDRESS, 124010796, 2.5))
    assert t.address == HF1_ADDRESS == to_checksum_address(LOWER)
    assert t.block_number == 124010796
    assert t.target_health_factor == D("2.5")
    assert t.chain_id == 56
    assert t.sources == {"address": "text", "blockNumber": "text", "targetHealthFactor": "text"}
    assert t.warnings == []


def test_reaches_hf_one_in_the_prompt_is_not_the_target():
    prompt = hf_prompt()
    assert "reaches HF 1.0" in prompt
    assert parse_task(prompt).target_health_factor == D("2.5")
    # With no target in the policy statement, the "reach HF N" field hint is used, never the 1.0.
    t = parse_task(hf_prompt(target=3.0, statement="report the account's current health factor"))
    assert t.target_health_factor == D(3)


@pytest.mark.parametrize("block", [122829508, 124010796, 1])
def test_marque_prompt_block_numbers(block):
    assert parse_task(hf_prompt(block=block)).block_number == block


def test_marque_prompt_with_lowercase_address():
    t = parse_task(hf_prompt(address=LOWER))
    assert t.address == HF1_ADDRESS and t.warnings == []


# ------------------------------------------------------------ structured data


@pytest.mark.parametrize("block,want", [
    (124010796, 124010796),
    ("124010796", 124010796),
    (hex(124010796), 124010796),
    ("0X" + format(124010796, "X"), 124010796),
    ("latest", None),
    ("LATEST", None),
])
def test_structured_block_number_forms(block, want):
    t = parse_task(data={"address": HF1_ADDRESS, "blockNumber": block, "targetHealthFactor": 2.5})
    assert t.address == HF1_ADDRESS
    assert t.block_number == want
    assert t.target_health_factor == D("2.5")
    assert t.sources == {"address": "data.address", "blockNumber": "data.blockNumber",
                         "targetHealthFactor": "data.targetHealthFactor"}


@pytest.mark.parametrize("key", ["address", "account", "wallet", "borrower", "user", "owner"])
def test_structured_address_aliases(key):
    assert parse_task(data={key: LOWER}).address == HF1_ADDRESS


@pytest.mark.parametrize("key", ["block", "block_number", "blockHeight", "atBlock"])
def test_structured_block_aliases(key):
    assert parse_task(data={"address": LOWER, key: "5"}).block_number == 5


@pytest.mark.parametrize("key,value", [("target", "1.9"), ("target_hf", 1.9), ("targetHf", "1.9"),
                                       ("target_health_factor", 1.9)])
def test_structured_target_aliases(key, value):
    assert parse_task(data={"address": LOWER, key: value}).target_health_factor == D("1.9")


def test_structured_fields_win_over_text():
    t = parse_task("Account: 0x" + "1" * 40 + " Block: 7 restore it to 3",
                   {"address": HF1_ADDRESS, "blockNumber": 9, "targetHealthFactor": 2.2})
    assert t.address == HF1_ADDRESS and t.block_number == 9 and t.target_health_factor == D("2.2")


@pytest.mark.parametrize("bad", [{"blockNumber": "soon"}, {"blockNumber": 1.5}, {"targetHealthFactor": "high"},
                                 {"targetHealthFactor": True}])
def test_structured_bad_values_are_refused(bad):
    with pytest.raises(TaskError):
        parse_task(data={"address": HF1_ADDRESS, **bad})


def test_nested_task_shape_from_marque_mcp_arguments():
    tool = {"name": "venus_position", "description": "health factor",
            "inputSchema": {"type": "object", "properties": {"task": {"type": "object"}}, "required": ["task"]}}
    args = mcp_arguments_for(tool, hf_prompt(), 124010796)
    assert args is not None and set(args) == {"task"}
    assert args["task"]["subject"] == {"address": HF1_ADDRESS} and args["task"]["blockNumber"] == "124010796"
    t = parse_task(data=args)
    assert t.address == HF1_ADDRESS
    assert t.block_number == 124010796
    assert t.target_health_factor == D("2.5")
    assert t.sources["targetHealthFactor"] == "data.targetHealthFactor"


def test_nested_task_shape_written_out():
    data = {"task": {"subject": {"address": LOWER}, "policy": {"targetHealthFactor": 2.5}, "blockNumber": 124010796}}
    t = parse_task(data=data)
    assert (t.address, t.block_number, t.target_health_factor) == (HF1_ADDRESS, 124010796, D("2.5"))


def test_marque_mcp_arguments_for_our_tool():
    prompt = hf_prompt()
    args = mcp_arguments_for(HF_TOOL, prompt, 124010796)
    assert args == {"address": HF1_ADDRESS, "blockNumber": "124010796", "policy": HF1_POLICY, "prompt": prompt}
    t = parse_task(args["prompt"], args)  # what MCPHandler passes to hf_answer
    assert (t.address, t.block_number, t.target_health_factor) == (HF1_ADDRESS, 124010796, D("2.5"))


def test_task_description_json_string():
    inner = json.dumps({"address": LOWER, "blockNumber": "123", "targetHealthFactor": 3})
    t = parse_task(data={"task_description": inner})
    assert (t.address, t.block_number, t.target_health_factor) == (HF1_ADDRESS, 123, D(3))
    # The same JSON as plain text.
    t2 = parse_task(inner)
    assert (t2.address, t2.block_number, t2.target_health_factor) == (HF1_ADDRESS, 123, D(3))


def test_task_description_text():
    t = parse_task(data={"task_description": f"Account: {HF1_ADDRESS}. Block: 124010796. Restore it to 2.5."})
    assert (t.address, t.block_number, t.target_health_factor) == (HF1_ADDRESS, 124010796, D("2.5"))


# ----------------------------------------------------------------- addresses


def test_lowercase_address_is_checksummed_without_warning():
    t = parse_task(f"health factor of {LOWER}")
    assert t.address == HF1_ADDRESS
    assert not any("checksum" in w for w in t.warnings)
    assert t.target_health_factor == DEFAULT_TARGET  # the "0" of "0x..." is not a target


def test_uppercase_hex_address_is_accepted():
    upper = "0x" + HF1_ADDRESS[2:].upper()
    t = parse_task(f"wallet {upper}")
    assert t.address == HF1_ADDRESS
    assert not any("checksum" in w for w in t.warnings)


@pytest.mark.parametrize("via", ["text", "data"])
def test_bad_mixed_case_checksum_warns_but_parses(via):
    bad = bad_checksum(HF1_ADDRESS)
    assert bad.lower() == LOWER and bad != HF1_ADDRESS
    t = parse_task(f"Account: {bad}") if via == "text" else parse_task(data={"address": bad})
    assert t.address == HF1_ADDRESS
    assert any("checksum" in w and bad in w for w in t.warnings)


def test_several_addresses_reads_the_first_and_warns():
    other = "0x" + "ab" * 20
    t = parse_task(f"compare {LOWER} with {other}")
    assert t.address == HF1_ADDRESS
    assert any("several addresses" in w for w in t.warnings)


def test_labeled_address_wins_over_an_earlier_unlabeled_one():
    other = "0x" + "ab" * 20
    t = parse_task(f"via router {other}: Account: {LOWER}")
    assert t.address == HF1_ADDRESS


def test_too_long_hex_is_not_an_address():
    with pytest.raises(TaskError):
        parse_task("Account: 0x" + "a" * 41)


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize("text,data", [
    ("what is my health factor?", None),
    ("", {"blockNumber": 1}),
    (None, {"address": "0x1234"}),
    (None, None),
])
def test_no_address_is_an_actionable_error(text, data):
    with pytest.raises(TaskError) as exc:
        parse_task(text, data)
    msg = str(exc.value)
    assert "address" in msg and "0x" in msg
    assert "blockNumber" in msg and "targetHealthFactor" in msg  # says what to send instead


@pytest.mark.parametrize("target", [1, "1.0", 0.95, 0, -2])
def test_target_at_or_below_one_is_refused(target):
    with pytest.raises(TaskError) as exc:
        parse_task(data={"address": HF1_ADDRESS, "targetHealthFactor": target})
    assert "above 1" in str(exc.value)


def test_explicit_text_target_of_one_is_refused():
    with pytest.raises(TaskError):
        parse_task(f"Account: {HF1_ADDRESS}. target HF: 1")


def test_target_above_100_is_refused():
    with pytest.raises(TaskError):
        parse_task(data={"address": HF1_ADDRESS, "targetHealthFactor": 101})


@pytest.mark.parametrize("chain", [97, "1", "0x1", "eip155:97", "ethereum"])
def test_other_chains_are_refused(chain):
    with pytest.raises(TaskError) as exc:
        parse_task(data={"address": HF1_ADDRESS, "chainId": chain})
    assert "56" in str(exc.value)


@pytest.mark.parametrize("chain", [56, "56", "0x38", "bsc", "eip155:56"])
def test_bsc_chain_ids_are_accepted(chain):
    assert parse_task(data={"address": HF1_ADDRESS, "chain_id": chain}).chain_id == 56


def test_non_positive_block_is_refused():
    with pytest.raises(TaskError):
        parse_task(data={"address": HF1_ADDRESS, "blockNumber": 0})


# ------------------------------------------------------------------- defaults


def test_default_target_with_a_warning():
    t = parse_task(f"Account: {HF1_ADDRESS}")
    assert t.target_health_factor == DEFAULT_TARGET == D("2.0")
    assert t.sources["targetHealthFactor"] == "default"
    assert t.sources["blockNumber"] == "default (latest)"
    assert t.block_number is None
    assert any("no target health factor" in w for w in t.warnings)


def test_latest_block_in_text():
    t = parse_task(f"Account: {HF1_ADDRESS} at the latest block, target HF 2")
    assert t.block_number is None and t.sources["blockNumber"] == "text (latest)"


# ------------------------------------------------------------- target phrases


@pytest.mark.parametrize("phrase,want", [
    ("restore it to 1.8", "1.8"),
    ("target HF: 3", "3"),
    ("bring the account back to a health factor of 2.2", "2.2"),
    ("restore a health factor of 2.5", "2.5"),
    ("restoring the position to HF 1.6", "1.6"),
    ("targetHealthFactor = 2.75", "2.75"),
    ("target health factor of 1.95", "1.95"),
    ("raise it to 2.4", "2.4"),
    ("keep the health factor above 1.7", "1.7"),
    ("I want HF >= 2.1", "2.1"),
])
def test_target_phrases(phrase, want):
    t = parse_task(f"Account: {HF1_ADDRESS}. Block: 100. {phrase}.")
    assert t.target_health_factor == D(want)
    assert t.sources["targetHealthFactor"] == "text"
    assert t.block_number == 100


def test_liquidation_mentions_do_not_set_the_target():
    t = parse_task(f"Account: {HF1_ADDRESS}: when does it reach HF 1.0? health factor 1 means liquidation.")
    assert t.target_health_factor == DEFAULT_TARGET
    assert t.sources["targetHealthFactor"] == "default"


def test_echo_is_json_ready():
    t = parse_task(hf_prompt())
    e = t.echo()
    json.dumps(e)
    assert e["targetHealthFactor"] == 2.5 and e["blockNumber"] == 124010796
    assert e["chainId"] == 56 and e["pool"] == "Venus Core Pool"
    assert parse_task(f"Account: {HF1_ADDRESS}").echo()["blockNumber"] == "latest"
