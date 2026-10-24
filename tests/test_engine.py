from genesis.planner import AgentSpec
from runtime.engine import run_spec


class FakeGateway:
    def call(self, tool, args):
        return {"tool": tool, "args": args, "text": "paneer appears"}


def test_engine_runs_gateway_and_local_steps():
    spec = AgentSpec(
        name="menu-checker",
        title="Menu Checker",
        description="Checks a college mess menu PDF and reports paneer dishes.",
        use_cases=["Check college menus", "Dietary preference checks"],
        limitations=["Public menus only"],
        domain_tag="food",
        inputs=[{"name": "url", "kind": "url", "label": "Menu URL"}],
        steps=[
            {"id": "fetch", "tool": "fetch", "args": {"url": "{{input.url}}"}},
            {
                "id": "fmt",
                "tool": "format",
                "args": {"value": "{{steps.fetch.out}}", "template": "Answer: {{value}}"},
            },
        ],
        output_template="{{steps.fmt.out}}",
        allowed_hosts=["example.com"],
        permissions=["network_read"],
        selftest={"inputs": {"url": "https://example.com"}},
    )
    result = run_spec(spec.model_dump(), {"url": "https://example.com"}, FakeGateway().call)
    assert "Answer" in result["output"]
    assert "fetch" in result["steps"]
