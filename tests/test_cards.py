import json

import pytest

from genesis.cards import MAX_CARD_BYTES, card_metadata, parse_selection, review_card


def test_review_card_is_string_metadata_and_has_typed_fallback():
    card = review_card(
        "sid-1",
        {
            "title": "Demo",
            "description": "Does it",
            "steps": [{"tool": "format"}],
            "allowed_hosts": [],
            "trigger": "on_demand",
        },
    )
    assert card["requires_card_interaction"] == "true"
    assert json.loads(card["card_payload"])["actions"][0]["selection"]["sid"] == "sid-1"


@pytest.mark.asyncio
async def test_selection_supports_direct_json_and_prose_tokens():
    assert (await parse_selection('{"sid":"s1","action":"approve"}', "s1"))["action"] == "approve"
    assert (await parse_selection("sid=s1 action=cancel", "s1"))["action"] == "cancel"
    assert (await parse_selection("sid=s2 action=cancel", "s1")) is None


def test_card_limit():
    with pytest.raises(ValueError):
        card_metadata("detail", {"body": "x" * (MAX_CARD_BYTES + 1)})
