"""Pre-warmed demo specifications.

These 3 specs match Section 12 of the Master Build Prompt:
1. College mess menu PDF (fetch -> extract -> llm -> format)
2. Government scheme diff_state (fetch -> extract -> diff_state -> llm -> format)
3. Mandi tomato price threshold (fetch -> extract -> filter -> format)
"""

from __future__ import annotations

from genesis.spec import AgentSpec, InputField, SelfTest, Step

MESS_MENU_SPEC = AgentSpec(
    spec_version="1",
    name="college-mess-menu-checker",
    title="College Mess Menu Paneer Checker",
    description="Checks a college mess menu PDF to verify if paneer dishes are scheduled this week.",
    use_cases=[
        "Check weekly college mess menus for special dietary items",
        "Verify vegetarian paneer dish availability for students",
    ],
    limitations=[
        "Requires a reachable public PDF menu URL",
        "Works with plain text and tabular PDF documents only",
    ],
    domain_tag="food-dining",
    inputs=[
        InputField(name="menu_url", kind="url", label="Mess Menu PDF URL"),
    ],
    steps=[
        Step(id="fetch", tool="fetch", args={"url": "{{input.menu_url}}"}),
        Step(
            id="extract", tool="extract", args={"content": "{{steps.fetch.out}}", "format": "pdf"}
        ),
        Step(
            id="llm",
            tool="llm",
            args={
                "prompt": (
                    "Analyze the mess menu text below and identify if paneer is served this week, "
                    "specifying meal and day: {{steps.extract.out}}"
                )
            },
        ),
        Step(id="fmt", tool="format", args={"template": "Menu Analysis:\n{{steps.llm.out}}"}),
    ],
    output_template="{{steps.fmt.out}}",
    trigger="on_demand",
    notify="chat",
    allowed_hosts=["example.edu", "college.edu", "mess.org"],
    permissions=["network_read", "llm"],
    selftest=SelfTest(
        inputs={"menu_url": "https://college.edu/menu.pdf"},
        expect_nonempty=True,
        expect_contains=["Menu Analysis"],
    ),
)


GOVT_SCHEME_SPEC = AgentSpec(
    spec_version="1",
    name="govt-scheme-diff-tracker",
    title="Government Scheme Page Change Tracker",
    description="Monitors government scheme webpages and reports new updates or policy changes since the previous check.",
    use_cases=[
        "Track policy updates on citizen welfare scheme pages",
        "Stay informed on eligibility criteria revisions",
    ],
    limitations=[
        "Requires public HTML webpages",
        "Diff tracking is based on extracted document text",
    ],
    domain_tag="public-policy",
    inputs=[
        InputField(name="scheme_url", kind="url", label="Government Scheme Webpage URL"),
    ],
    steps=[
        Step(id="fetch", tool="fetch", args={"url": "{{input.scheme_url}}"}),
        Step(
            id="extract", tool="extract", args={"content": "{{steps.fetch.out}}", "format": "html"}
        ),
        Step(
            id="diff",
            tool="diff_state",
            args={"key": "scheme_text", "value": "{{steps.extract.out}}"},
        ),
        Step(
            id="llm",
            tool="llm",
            args={"prompt": ("Summarize policy changes based on diff: {{steps.diff.out}}")},
        ),
        Step(
            id="fmt", tool="format", args={"template": "Scheme Update Summary:\n{{steps.llm.out}}"}
        ),
    ],
    output_template="{{steps.fmt.out}}",
    trigger="on_demand",
    notify="chat",
    allowed_hosts=["example.gov", "india.gov.in", "schemes.gov.in"],
    permissions=["network_read", "llm", "storage"],
    selftest=SelfTest(
        inputs={"scheme_url": "https://example.gov/scheme"},
        expect_nonempty=True,
        expect_contains=["Scheme Update Summary"],
    ),
)


MANDI_PRICE_SPEC = AgentSpec(
    spec_version="1",
    name="mandi-tomato-price-filter",
    title="Mandi Tomato Price Threshold Checker",
    description="Fetches mandi commodity rates from an agricultural portal and checks if tomato prices exceed a threshold.",
    use_cases=[
        "Check daily agricultural mandi rates for tomatoes",
        "Filter mandi district prices against target price thresholds",
    ],
    limitations=[
        "Requires structured or text mandi reports",
        "Price data must be present on the target page",
    ],
    domain_tag="agriculture",
    inputs=[
        InputField(name="mandi_url", kind="url", label="Mandi Portal URL"),
        InputField(name="district", kind="text", label="Target District Name"),
        InputField(name="threshold", kind="number", label="Price Threshold in Rupees"),
    ],
    steps=[
        Step(id="fetch", tool="fetch", args={"url": "{{input.mandi_url}}"}),
        Step(
            id="extract", tool="extract", args={"content": "{{steps.fetch.out}}", "format": "html"}
        ),
        Step(
            id="filter",
            tool="filter",
            args={"value": "{{steps.extract.out}}", "contains": "{{input.district}}"},
        ),
        Step(
            id="fmt",
            tool="format",
            args={"template": "Tomato Mandi Report for {{input.district}}:\n{{steps.filter.out}}"},
        ),
    ],
    output_template="{{steps.fmt.out}}",
    trigger="on_demand",
    notify="chat",
    allowed_hosts=["example.agri.gov", "mandi.gov.in", "agmarknet.gov.in"],
    permissions=["network_read"],
    selftest=SelfTest(
        inputs={"mandi_url": "https://mandi.gov.in/rates", "district": "Nashik", "threshold": "30"},
        expect_nonempty=True,
        expect_contains=["Tomato Mandi Report"],
    ),
)


DEMO_SPECS: dict[str, AgentSpec] = {
    MESS_MENU_SPEC.name: MESS_MENU_SPEC,
    GOVT_SCHEME_SPEC.name: GOVT_SCHEME_SPEC,
    MANDI_PRICE_SPEC.name: MANDI_PRICE_SPEC,
}
