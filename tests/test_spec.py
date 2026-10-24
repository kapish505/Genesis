import pytest

from genesis.planner import AgentSpec, canonical_json, parse_spec, spec_hash


def sample() -> AgentSpec:
    return AgentSpec(
        name="menu-checker",
        title="Menu checker",
        description="Checks a public menu page and reports whether a dish appears.",
        use_cases=["Check menus", "Find dishes"],
        limitations=["Public pages only"],
        domain_tag="food",
        inputs=[{"name": "url", "kind": "url", "label": "Menu URL"}],
        steps=[
            {"id": "fetch_menu", "tool": "fetch", "args": {"url": "{{input.url}}"}},
            {
                "id": "format_result",
                "tool": "format",
                "args": {"value": "{{steps.fetch_menu.out}}", "template": "{{value}}"},
            },
        ],
        output_template="{{steps.format_result.out}}",
        allowed_hosts=["example.com"],
        permissions=["network_read"],
        selftest={"inputs": {"url": "https://example.com"}},
    )


def test_spec_hash_is_canonical():
    spec = sample()
    assert spec_hash(spec) == spec_hash(parse_spec(canonical_json(spec)))


def test_private_and_wildcard_hosts_rejected():
    with pytest.raises(ValueError):
        data = sample().model_dump()
        data["allowed_hosts"] = ["*.example.com"]
        AgentSpec.model_validate(data)


def test_fetch_requires_network_permission():
    with pytest.raises(ValueError):
        data = sample().model_dump()
        data["permissions"] = []
        AgentSpec.model_validate(data)
