"""Genesis Demo Runner — Rehearses and executes the Section 12 showcase scenarios.

Scenarios:
1. 'mess': College mess menu PDF checker (fetch -> extract -> llm -> format)
2. 'scheme': Government scheme diff tracker (fetch -> extract -> diff_state -> llm -> format)
3. 'mandi': Mandi tomato price threshold filter (fetch -> extract -> filter -> format)

Usage:
  python scripts/demo_runner.py --demo all
  python scripts/demo_runner.py --demo mess
  python scripts/demo_runner.py --demo scheme
  python scripts/demo_runner.py --demo mandi
  python scripts/demo_runner.py --demo all --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from genesis.demo_specs import GOVT_SCHEME_SPEC, MANDI_PRICE_SPEC, MESS_MENU_SPEC
from genesis.spec import AgentSpec
from runtime.engine import run_spec


def run_mess_demo() -> dict[str, Any]:
    spec: AgentSpec = MESS_MENU_SPEC
    inputs = {"menu_url": "https://college.edu/menu.pdf"}

    def mock_gateway(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "fetch":
            return {"content": "RAW_PDF_STREAM_BYTES"}
        if tool == "extract":
            return {
                "text": (
                    "COLLEGE HOSTEL MESS MENU - WEEK 42\n"
                    "Monday: Rajma Chawal\n"
                    "Tuesday: Dal Tadka & Jeera Rice\n"
                    "Wednesday: Paneer Butter Masala, Roti, Kheer (Special Dinner)\n"
                    "Thursday: Chana Masala\n"
                    "Friday: Mix Veg Curry\n"
                    "Saturday: Kadai Mushroom\n"
                    "Sunday: Matar Paneer (Lunch), Veg Pulao\n"
                )
            }
        if tool == "llm":
            return {
                "text": (
                    "Paneer is served twice this week:\n"
                    "1. Wednesday Dinner: Paneer Butter Masala (Special Dinner)\n"
                    "2. Sunday Lunch: Matar Paneer"
                )
            }
        return {}

    start = time.perf_counter()
    result = run_spec(spec.model_dump(), inputs, mock_gateway)
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    return {
        "scenario": "College Mess Menu PDF Checker",
        "spec_name": spec.name,
        "tools_used": [s.tool for s in spec.steps],
        "inputs": inputs,
        "elapsed_ms": elapsed,
        "output": result.get("output", ""),
        "success": bool("Paneer" in result.get("output", "")),
    }


def run_scheme_demo() -> dict[str, Any]:
    spec: AgentSpec = GOVT_SCHEME_SPEC
    inputs = {"scheme_url": "https://example.gov/scheme"}
    state: dict[str, Any] = {
        "scheme_text": (
            "National Agriculture Solar Pump Scheme (PM-KUSUM)\n"
            "Subsidy: 30% financial assistance from Central Government.\n"
            "Eligibility: Small and marginal farmers with borewell connection.\n"
        )
    }

    def mock_gateway(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "fetch":
            return {"content": "HTML_PAGE_MARKUP"}
        if tool == "extract":
            return {
                "text": (
                    "National Agriculture Solar Pump Scheme (PM-KUSUM) - Revised Guidelines\n"
                    "Subsidy: 40% financial assistance from Central Government (Updated Jan 2026).\n"
                    "Eligibility: All farmer categories with verifiable land title or lease.\n"
                    "Solar panel capacity cap raised from 7.5 HP to 10 HP."
                )
            }
        if tool == "llm":
            return {
                "text": (
                    "- Central subsidy increased from 30% to 40%.\n"
                    "- Eligibility expanded beyond small/marginal farmers to all verified landholders.\n"
                    "- Pump capacity ceiling increased to 10 HP (from 7.5 HP)."
                )
            }
        return {}

    start = time.perf_counter()
    result = run_spec(spec.model_dump(), inputs, mock_gateway, state=state)
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    return {
        "scenario": "Government Scheme Policy Diff Tracker",
        "spec_name": spec.name,
        "tools_used": [s.tool for s in spec.steps],
        "inputs": inputs,
        "elapsed_ms": elapsed,
        "output": result.get("output", ""),
        "persisted_state_keys": list(state.keys()),
        "success": bool("subsidy" in result.get("output", "").lower()),
    }


def run_mandi_demo() -> dict[str, Any]:
    spec: AgentSpec = MANDI_PRICE_SPEC
    inputs = {
        "mandi_url": "https://mandi.gov.in/rates",
        "district": "Nashik",
        "threshold": "30",
    }

    def mock_gateway(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "fetch":
            return {"content": "MANDI_PORTAL_HTML"}
        if tool == "extract":
            return {
                "text": (
                    "Daily Mandi Commodity Prices (Tomato / Hybrid):\n"
                    "- Pune (Gultekdi): Min ₹22/kg, Max ₹28/kg, Modal ₹25/kg\n"
                    "- Nashik (Pimpalgaon): Min ₹32/kg, Max ₹42/kg, Modal ₹38/kg [EXCEEDS THRESHOLD]\n"
                    "- Nagpur (Kalamna): Min ₹20/kg, Max ₹26/kg, Modal ₹24/kg\n"
                    "- Ahmednagar (Rahata): Min ₹28/kg, Max ₹34/kg, Modal ₹31/kg"
                )
            }
        return {}

    start = time.perf_counter()
    result = run_spec(spec.model_dump(), inputs, mock_gateway)
    elapsed = round((time.perf_counter() - start) * 1000, 2)
    return {
        "scenario": "Mandi Tomato Price Threshold Checker",
        "spec_name": spec.name,
        "tools_used": [s.tool for s in spec.steps],
        "inputs": inputs,
        "elapsed_ms": elapsed,
        "output": result.get("output", ""),
        "success": bool("Nashik" in result.get("output", "")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Genesis Showcase Demo Runner")
    parser.add_argument(
        "--demo",
        choices=["mess", "scheme", "mandi", "all"],
        default="all",
        help="Select demo to execute (default: all)",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON output")
    args = parser.parse_args()

    runners = {
        "mess": run_mess_demo,
        "scheme": run_scheme_demo,
        "mandi": run_mandi_demo,
    }

    selected = ["mess", "scheme", "mandi"] if args.demo == "all" else [args.demo]
    results = [runners[name]() for name in selected]

    all_passed = all(r["success"] for r in results)

    if args.json:
        print(
            json.dumps(
                {
                    "overall_success": all_passed,
                    "scenarios_executed": len(results),
                    "results": results,
                },
                indent=2,
            )
        )
    else:
        print("=" * 80)
        print("GENESIS — Showcase Scenarios Execution")
        print("=" * 80)
        for r in results:
            print(f"Scenario : {r['scenario']}")
            print(f"Spec     : {r['spec_name']}")
            print(f"Tools    : {' -> '.join(r['tools_used'])}")
            print(f"Latency  : {r['elapsed_ms']} ms")
            print("Output   :")
            for line in r["output"].strip().split("\n"):
                print(f"  {line}")
            print("-" * 80)
        print(f"Execution Status: {'ALL DEMOS PASSED' if all_passed else 'FAILURES DETECTED'}")

    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
