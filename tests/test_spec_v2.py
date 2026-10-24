"""Tests for AgentSpecV2 schema, mode derivation, specialists, budget, and validation."""

from datetime import datetime, timezone
import pytest

from genesis.spec import (
    AgentSpec,
    AgentSpecV2,
    Budget,
    Expect,
    InputField,
    SelfTest,
    SpecialistRef,
    Step,
    parse_spec,
    spec_hash,
)


def make_valid_spec_v2(**kwargs) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    base = {
        "spec_version": "2",
        "name": "orchestrator-spec",
        "title": "Crypto Specialist Orchestrator",
        "description": "Orchestrates specialists on Agentverse and delivers notifications.",
        "use_cases": ["Tracks token metrics", "Alerts on Telegram"],
        "limitations": ["Requires internet", "Subject to specialist availability"],
        "domain_tag": "defi-analytics",
        "inputs": [{"name": "symbol", "kind": "text", "label": "Token Symbol"}],
        "specialists": [
            {
                "alias": "crypto_data",
                "address": "agent1q0123456789012345678901234567890123456789",
                "name": "CryptoData Specialist",
                "readme_sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
                "vet_score": 0.92,
                "vetted_at": now,
                "price_cap_usd": 0.05,
                "send_fields": ["symbol"],
                "fallbacks": [],
            }
        ],
        "budget": {"max_usd_per_run": 0.15, "max_agent_calls": 2},
        "steps": [
            {
                "id": "step_call",
                "tool": "agent_call",
                "args": {
                    "specialist": "crypto_data",
                    "message": "Fetch price for {{input.symbol}}",
                    "expect": {"kind": "nonempty"},
                    "timeout_s": 25,
                    "on_fail": "fallback",
                },
            },
            {
                "id": "step_alert",
                "tool": "action",
                "args": {
                    "action": "telegram_send",
                    "body_template": "Report: {{steps.step_call.out}}",
                },
            },
        ],
        "output_template": "Summary: {{steps.step_call.out}}",
        "permissions": ["notify"],
        "selftest": {"inputs": {"symbol": "FET"}, "expect_nonempty": True},
    }
    base.update(kwargs)
    return base


def test_agentspec_v2_valid_managed():
    raw = make_valid_spec_v2()
    spec = parse_spec(raw)
    assert isinstance(spec, AgentSpecV2)
    assert spec.spec_version == "2"
    assert spec.mode == "managed"
    assert len(spec.specialists) == 1
    assert spec.specialists[0].alias == "crypto_data"
    assert spec.budget.max_usd_per_run == 0.15
    assert len(spec.steps) == 2


def test_agentspec_v2_local_mode_derived():
    raw = make_valid_spec_v2()
    # Replace steps with pure local/gateway steps
    raw["steps"] = [
        {
            "id": "step_search",
            "tool": "search",
            "args": {"query": "FET token price", "max_results": 3},
        },
        {
            "id": "step_fmt",
            "tool": "format",
            "args": {"template": "Results: {{steps.step_search.out}}"},
        },
    ]
    raw["output_template"] = "Output: {{steps.step_fmt.out}}"
    raw["permissions"] = ["network_read"]
    spec = parse_spec(raw)
    assert isinstance(spec, AgentSpecV2)
    assert spec.mode == "local"


def test_unknown_specialist_in_call_rejected():
    raw = make_valid_spec_v2()
    raw["steps"][0]["args"]["specialist"] = "unknown_alias"
    with pytest.raises(Exception, match="unknown specialist"):
        parse_spec(raw)


def test_actions_must_be_last():
    raw = make_valid_spec_v2()
    # Put action step before agent_call
    raw["steps"] = [
        {
            "id": "step_alert",
            "tool": "action",
            "args": {"action": "telegram_send", "body_template": "Alert early"},
        },
        {
            "id": "step_call",
            "tool": "agent_call",
            "args": {
                "specialist": "crypto_data",
                "message": "Too late",
                "expect": {"kind": "nonempty"},
            },
        },
    ]
    with pytest.raises(Exception, match="action steps must only appear after all non-action steps"):
        parse_spec(raw)


def test_duplicate_specialist_addresses_rejected():
    raw = make_valid_spec_v2()
    dup = dict(raw["specialists"][0])
    dup["alias"] = "crypto_data_2"
    raw["specialists"].append(dup)
    with pytest.raises(Exception, match="duplicate specialist address"):
        parse_spec(raw)


def test_backward_compatibility_v1_specs():
    raw_v1 = {
        "spec_version": "1",
        "name": "v1-agent",
        "title": "V1 Test Agent",
        "description": "Legacy v1 agent for web reading.",
        "use_cases": ["Reading", "Summarizing"],
        "limitations": ["No actions"],
        "domain_tag": "web-reader",
        "inputs": [{"name": "topic", "kind": "text", "label": "Topic"}],
        "allowed_hosts": ["example.com"],
        "permissions": ["network_read", "llm"],
        "steps": [
            {"id": "fetch1", "tool": "fetch", "args": {"url": "https://example.com"}},
            {"id": "llm1", "tool": "llm", "args": {"prompt": "summarize {{steps.fetch1.out}}"}},
        ],
        "output_template": "Summary: {{steps.llm1.out}}",
        "selftest": {"inputs": {"topic": "ai"}, "expect_nonempty": True},
    }
    spec = parse_spec(raw_v1)
    assert isinstance(spec, AgentSpec)
    assert not isinstance(spec, AgentSpecV2)
    assert spec.spec_version == "1"
    assert spec.mode == "local"
