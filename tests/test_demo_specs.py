import py_compile
from pathlib import Path

from genesis.builder import Builder
from genesis.demo_specs import DEMO_SPECS, GOVT_SCHEME_SPEC, MANDI_PRICE_SPEC, MESS_MENU_SPEC
from genesis.spec import spec_hash
from runtime.engine import run_spec


def test_demo_specs_validation_and_hashing():
    for name, spec in DEMO_SPECS.items():
        assert spec.name == name
        assert len(spec.description) >= 20
        assert len(spec.use_cases) in {2, 3}
        assert len(spec.limitations) in {1, 2, 3, 4}
        assert len(spec.steps) <= 8
        digest = spec_hash(spec)
        assert len(digest) == 64


def test_demo_specs_build_compiles(tmp_path: Path):
    builder = Builder()
    for spec in DEMO_SPECS.values():
        code = builder.render(spec, gateway_url="http://localhost:8080", gateway_token="token-123")
        target = tmp_path / f"{spec.name}.py"
        target.write_text(code)
        py_compile.compile(str(target), doraise=True)


def test_mess_menu_spec_execution():
    def mock_gateway(tool: str, args: dict):
        if tool == "fetch":
            return {"content": "PDF data"}
        if tool == "extract":
            return {"text": "Weekly Mess Menu: Wednesday Dinner: Paneer Makhani with Naan."}
        if tool == "llm":
            return {"text": "Yes, paneer is served on Wednesday for dinner (Paneer Makhani)."}
        return {}

    spec = MESS_MENU_SPEC
    inputs = {"menu_url": "https://college.edu/menu.pdf"}
    result = run_spec(spec.model_dump(), inputs, mock_gateway)
    assert "Menu Analysis:" in result["output"]
    assert "Wednesday" in result["output"]
    assert "Paneer Makhani" in result["output"]


def test_govt_scheme_diff_execution():
    def mock_gateway(tool: str, args: dict):
        if tool == "fetch":
            return {"content": "HTML data"}
        if tool == "extract":
            return {"text": "Scheme Guidelines Version 2.0: Subsidy increased to 40%."}
        if tool == "llm":
            return {"text": "Key update: Subsidy increased from 30% to 40% in Version 2.0."}
        return {}

    spec = GOVT_SCHEME_SPEC
    inputs = {"scheme_url": "https://example.gov/scheme"}
    state = {}
    result = run_spec(spec.model_dump(), inputs, mock_gateway, state=state)
    assert "Scheme Update Summary:" in result["output"]
    assert "Subsidy increased" in result["output"]
    assert "scheme_text" in state


def test_mandi_price_filter_execution():
    def mock_gateway(tool: str, args: dict):
        if tool == "fetch":
            return {"content": "Mandi Rates Table"}
        if tool == "extract":
            return {
                "text": "District: Nashik | Commodity: Tomato | Modal Price: 35 INR/kg\n"
                "District: Pune | Commodity: Tomato | Modal Price: 28 INR/kg"
            }
        return {}

    spec = MANDI_PRICE_SPEC
    inputs = {
        "mandi_url": "https://mandi.gov.in/rates",
        "district": "Nashik",
        "threshold": "30",
    }
    result = run_spec(spec.model_dump(), inputs, mock_gateway)
    assert "Tomato Mandi Report for Nashik:" in result["output"]
    assert "Nashik" in result["output"]
