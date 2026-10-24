"""Interactive card construction and selection parsing.

Cards are advisory UI. Every card is accompanied by plain text that states the
typed fallback, because an unsupported or invalid card degrades silently to its
text. Selections always carry the session id and an action, and the parser
accepts all documented arrival shapes:

1. raw JSON (direct @mention),
2. JSON with extra keys (planner-mediated),
3. prose containing ``sid=...``/``action=...`` tokens,
4. prose naming an action in words ("approve", "cancel", "yes"),
5. an LLM classification of ambiguous free text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

MAX_CARD_BYTES = 64 * 1024
MAX_TREE_DEPTH = 8
CARD_KINDS = frozenset({"carousel", "detail", "form", "review", "custom"})

APPROVE_WORDS = (
    "approve",
    "approved",
    "accept",
    "yes",
    "yep",
    "go ahead",
    "build it",
    "do it",
    "proceed",
    "ok",
)
CANCEL_WORDS = ("cancel", "no", "stop", "abort", "never mind", "nevermind", "don't", "dont")
EDIT_WORDS = ("edit", "change", "modify", "instead")
DELETE_WORDS = ("delete", "remove agent", "purge")
RERUN_WORDS = ("run again", "rerun", "again", "repeat")
BUILD_WORDS = ("build", "create", "new agent", "make one")
USE_WORDS = ("use it", "use existing", "existing", "that one")

Fallback = Callable[[str, str, list[str]], Awaitable[dict[str, Any] | None]]


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_TREE_DEPTH:
        return level
    if isinstance(value, Mapping):
        return max((_depth(item, level + 1) for item in value.values()), default=level)
    if isinstance(value, (list, tuple)):
        return max((_depth(item, level + 1) for item in value), default=level)
    return level


def card_metadata(kind: str, payload: Mapping[str, Any]) -> dict[str, str]:
    if kind not in CARD_KINDS:
        raise ValueError(f"unsupported card kind: {kind}")
    if _depth(payload) >= MAX_TREE_DEPTH:
        raise ValueError("card payload tree is too deep")
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(payload_json.encode()) > MAX_CARD_BYTES:
        raise ValueError("card payload exceeds 64 KB")
    return {
        "card_protocol_version": "1",
        "requires_card_interaction": "true",
        "card_kind": kind,
        "card_payload": payload_json,
    }


def selection(sid: str, action: str, **extra: Any) -> dict[str, str]:
    return {"sid": sid, "action": action, **{key: str(value) for key, value in extra.items()}}


# --------------------------------------------------------------------- cards


def form_card(sid: str, fields: list[dict[str, Any]], intro: str = "") -> dict[str, str]:
    payload = {
        "title": "A little more information",
        "description": intro or "I need these details before I can continue.",
        "fields": fields,
        "submit": {"label": "Continue", "selection": selection(sid, "submit")},
        "actions": [{"label": "Cancel", "selection": selection(sid, "cancel")}],
    }
    return card_metadata("form", payload)


def carousel_card(sid: str, agents: list[dict[str, Any]], intro: str = "") -> dict[str, str]:
    items: list[dict[str, Any]] = []
    for agent in agents:
        item: dict[str, Any] = {
            "title": str(agent.get("name") or "Agent"),
            "subtitle": str(agent.get("reason") or ""),
            "body": str(agent.get("readme", ""))[:280],
            "selection": selection(
                sid, "use_existing", agent_address=str(agent.get("address", ""))
            ),
        }
        if agent.get("handle"):
            item["handle"] = str(agent["handle"])
        items.append(item)
    items.append(
        {
            "title": "Build a new agent",
            "subtitle": "Genesis creates and deploys a new agent for this task",
            "body": "Choose this when no existing agent fits, or when you want a purpose-built one.",
            "selection": selection(sid, "build_new"),
        }
    )
    payload = {
        "title": "Choose a path",
        "description": intro or "I found agents that are close, but none is an exact fit.",
        "items": items,
        "actions": [{"label": "Cancel", "selection": selection(sid, "cancel")}],
    }
    return card_metadata("carousel", payload)


def review_card(sid: str, spec: Mapping[str, Any], *, cost: str = "Free") -> dict[str, str]:
    schedule = str(spec.get("trigger") or "on_demand")
    if schedule == "interval":
        seconds = spec.get("interval_seconds")
        schedule = f"every {seconds}s" if seconds else "interval"
    rows = [
        {"label": "Name", "value": str(spec.get("title", ""))},
        {"label": "What it does", "value": str(spec.get("description", ""))},
        {
            "label": "Tools",
            "value": ", ".join(step.get("tool", "") for step in spec.get("steps", [])),
        },
        {
            "label": "Hosts",
            "value": ", ".join(str(host) for host in spec.get("allowed_hosts", [])) or "None",
        },
        {"label": "Schedule", "value": schedule},
        {"label": "Cost", "value": cost},
    ]
    payload = {
        "title": "Review new agent",
        "description": "Approve to deploy this agent on Agentverse, or edit the request.",
        "rows": rows,
        "actions": [
            {"label": "Approve", "style": "primary", "selection": selection(sid, "approve")},
            {"label": "Edit request", "selection": selection(sid, "edit")},
            {"label": "Cancel", "selection": selection(sid, "cancel")},
        ],
    }
    if spec.get("specialists"):
        specs_list = spec["specialists"]
        names = [s.get("name", s.get("alias", "Specialist")) for s in specs_list]
        payload["rows"].append({"label": "Specialists", "value": ", ".join(names)})
        send_fields = []
        for s in specs_list:
            send_fields.extend(s.get("send_fields", []))
        if send_fields:
            payload["rows"].append({"label": "Shared Data Fields", "value": ", ".join(sorted(set(send_fields)))})
    if spec.get("budget"):
        b = spec["budget"]
        payload["rows"].append({"label": "Budget Cap", "value": f"${b.get('max_usd_per_run', 0.10):.2f} / {b.get('max_agent_calls', 3)} calls"})

    return card_metadata("review", payload)


def specialist_carousel_card(sid: str, specialists: list[dict[str, Any]], intro: str = "") -> dict[str, str]:
    """Carousel card displaying vetted specialist candidates with live scores, latency, and price."""
    items: list[dict[str, Any]] = []
    for spec in specialists:
        score = spec.get("vet_score", 0.0)
        latency = spec.get("latency", 0.0)
        price = spec.get("price_cap_usd", 0.0)
        items.append({
            "title": str(spec.get("name") or "Specialist"),
            "subtitle": f"Vet Score: {score:.0%} | Latency: {latency:.1f}s | Price: ${price:.3f}",
            "body": str(spec.get("reason") or spec.get("readme", ""))[:280],
            "selection": selection(sid, "use_specialist", specialist_alias=str(spec.get("alias", ""))),
        })
    items.append({
        "title": "Build missing glue only",
        "subtitle": "Genesis orchestrates vetted specialists and builds only the gap",
        "body": "Proceed with recommended specialists and plan.",
        "selection": selection(sid, "proceed_plan"),
    })
    payload = {
        "title": "Vetted Specialists",
        "description": intro or "Genesis probed live agents on Agentverse against your intent.",
        "items": items,
        "actions": [{"label": "Cancel", "selection": selection(sid, "cancel")}],
    }
    return card_metadata("carousel", payload)


def action_approval_card(sid: str, approval_id: str, action: str, recipient: str, body: str) -> dict[str, str]:
    """Confirmation card for real side-effects (Telegram / Email) requiring explicit approval."""
    payload = {
        "title": f"Confirm Action: {action}",
        "description": "Genesis requires your approval before executing this real side-effect.",
        "rows": [
            {"label": "Action", "value": action},
            {"label": "Recipient", "value": recipient},
            {"label": "Message Content", "value": body},
        ],
        "actions": [
            {"label": "Confirm & Send", "style": "primary", "selection": selection(sid, "approve_action", approval_id=approval_id)},
            {"label": "Reject", "selection": selection(sid, "reject_action", approval_id=approval_id)},
        ],
    }
    return card_metadata("review", payload)


def result_card(
    sid: str,
    output: str,
    *,
    spec_hash: str = "",
    can_promote: bool = True,
) -> dict[str, str]:
    """Result card displaying final answer with optional 'Keep this agent' promotion button."""
    actions = [{"label": "Run again", "selection": selection(sid, "run_again")}]
    if can_promote:
        actions.append({"label": "Keep this agent", "selection": selection(sid, "promote_agent", spec_hash=spec_hash)})

    payload = {
        "title": "Execution Complete",
        "body": output[:600],
        "actions": actions,
    }
    return card_metadata("detail", payload)


def detail_card(
    sid: str,
    title: str,
    body: str,
    *,
    address: str = "",
    handle: str = "",
    rows: list[dict[str, str]] | None = None,
    deletable: bool = True,
) -> dict[str, str]:
    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "rows": list(rows or []),
        "actions": [],
    }
    if address:
        payload["rows"].append({"label": "Address", "value": address})
    if handle:
        payload["rows"].append({"label": "Handle", "value": f"@{handle}"})
    actions = payload["actions"]
    assert isinstance(actions, list)
    actions.append({"label": "Run again", "selection": selection(sid, "run_again")})
    if deletable and address:
        actions.append(
            {
                "label": "Delete agent",
                "selection": selection(sid, "delete_agent", agent_address=address),
            }
        )
    return card_metadata("detail", payload)


def fallback_text(kind: str, sid: str, *, extra: str = "") -> str:
    """The typed fallback that must accompany every card."""
    prompts = {
        "form": "Reply with the requested details, or CANCEL.",
        "carousel": "Reply with the number of an option, or BUILD to create a new agent, or CANCEL.",
        "review": "Reply APPROVE to deploy, EDIT to change the request, or CANCEL.",
        "detail": "Reply RUN AGAIN or DELETE AGENT, or send a new request.",
    }
    base = prompts.get(kind, "Reply with your choice.")
    return f"{base} (If the card does not open, send: sid={sid} action=<choice>.)" + (
        f"\n\n{extra}" if extra else ""
    )


# ------------------------------------------------------------------- parsing


def _normalise_action(token: str) -> str:
    token = token.strip().casefold().replace("-", "_").replace(" ", "_")
    synonyms = {
        "approve": "approve",
        "approved": "approve",
        "yes": "approve",
        "accept": "approve",
        "ok": "approve",
        "proceed": "approve",
        "go": "approve",
        "cancel": "cancel",
        "no": "cancel",
        "stop": "cancel",
        "abort": "cancel",
        "edit": "edit",
        "change": "edit",
        "submit": "submit",
        "build": "build_new",
        "build_new": "build_new",
        "new": "build_new",
        "create": "build_new",
        "use_existing": "use_existing",
        "existing": "use_existing",
        "use": "use_existing",
        "run_again": "run_again",
        "rerun": "run_again",
        "again": "run_again",
        "delete": "delete_agent",
        "delete_agent": "delete_agent",
        "remove": "delete_agent",
    }
    return synonyms.get(token, token)


def parse_selection_offline(
    text: str, sid: str, allowed: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """Deterministic parse: JSON, then explicit tokens, then action words.

    ``allowed`` maps action -> card kind; it prevents "yes" from approving a
    card that has no approve action.
    """
    raw = text.strip()
    if not raw:
        return None

    if raw.startswith("{"):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            candidate_sid = value.get("sid") or value.get("session_id")
            if candidate_sid is not None and str(candidate_sid) != sid:
                return None
            action = value.get("action") or value.get("choice")
            if isinstance(action, str) and action.strip():
                parsed: dict[str, Any] = {"sid": sid, "action": _normalise_action(action)}
                for key, item in value.items():
                    if key not in {"sid", "action", "session_id", "choice"}:
                        parsed[key] = item
                if "fields" in value and isinstance(value["fields"], dict):
                    parsed.update(value["fields"])
                return parsed

    sid_match = re.search(r"\bsid\s*[=:]\s*([\w-]+)", raw, flags=re.I)
    if sid_match and sid_match.group(1) != sid:
        return None
    action_match = re.search(r"\baction\s*[=:]\s*([\w-]+)", raw, flags=re.I)
    if not action_match:
        action_match = re.search(
            r"\b(?:choose|picked|select(?:ed)?)\s*[=:]?\s*(?!sid\b)([\w-]+)",
            raw,
            flags=re.I,
        )
    if action_match:
        token = action_match.group(1) or ""
        return {"sid": sid, "action": _normalise_action(token)}

    lowered = raw.casefold()
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("delete_agent", DELETE_WORDS),
        ("run_again", RERUN_WORDS),
        ("build_new", BUILD_WORDS),
        ("use_existing", USE_WORDS),
        ("approve", APPROVE_WORDS),
        ("edit", EDIT_WORDS),
        ("cancel", CANCEL_WORDS),
    ]
    for action, words in rules:
        if allowed is not None and action not in allowed:
            continue
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
            return {"sid": sid, "action": action}
    return None


async def parse_selection(
    text: str,
    sid: str,
    allowed: Mapping[str, str] | None = None,
    classifier: Fallback | None = None,
) -> dict[str, Any] | None:
    """Full parse pipeline, including an optional LLM classifier for ambiguous text."""
    parsed = parse_selection_offline(text, sid, allowed)
    if parsed is not None:
        return parsed
    if classifier is None or allowed is None:
        return None
    try:
        result = await classifier(text, sid, sorted(allowed))
    except Exception:  # noqa: BLE001 - classification is best-effort by design
        return None
    if not isinstance(result, dict):
        return None
    action = _normalise_action(str(result.get("action", "")))
    if action not in allowed:
        return None
    parsed = {"sid": sid, "action": action}
    fields = result.get("fields")
    if isinstance(fields, dict):
        parsed.update({str(key): value for key, value in fields.items()})
    return parsed


def is_structured_selection(text: str) -> bool:
    """True when the text is machine-generated card JSON rather than human prose."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict) and ("action" in value or "choice" in value)
