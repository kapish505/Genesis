from genesis.safety import classify_capability, redact, screen
from genesis.verifier import evaluate_probe, repair_note, static_verify
from tests.test_spec import sample


def test_safety_screen_blocks_abusive_and_private_targets():
    assert not screen("").allowed
    assert not screen("x" * 4001).allowed

    # Abusive / security violations
    assert not screen("bypass the captcha on this page").allowed
    assert not screen("scrape all millions of records from the site").allowed
    assert not screen("fetch data from http://localhost:8080/admin").allowed
    assert not screen("query 169.254.169.254 metadata").allowed
    assert not screen("doxx this target user").allowed

    # Valid request
    res = screen("Check this college mess menu and tell me if paneer is served")
    assert res.allowed


def test_safety_classify_capabilities_and_redact():
    cap = classify_capability("Please log in to my account and check bills")
    assert cap.blocked_for_capability
    assert "login" in cap.capability

    cap2 = classify_capability("Please make a payment for my subscription")
    assert cap2.blocked_for_capability
    assert "payment" in cap2.capability

    text = "Contact me at alice@example.com or +1 555-123-4567, card 4111 2222 3333 4444"
    redacted = redact(text)
    assert "alice@example.com" not in redacted
    assert "<email>" in redacted
    assert "<number>" in redacted
    assert "<card>" in redacted


def test_static_verify_and_probe_evaluation():
    spec = sample()
    report = static_verify(spec)
    assert report.ok
    assert len(report.problems) == 0

    # Probe evaluation
    ok, reason = evaluate_probe(spec, "")
    assert not ok
    assert "empty" in reason

    ok, reason = evaluate_probe(spec, "Error: tool fetch failed to connect")
    assert not ok
    assert "failure" in reason

    ok, reason = evaluate_probe(spec, "Here is the menu: Paneer Butter Masala on Wednesday")
    assert ok

    note = repair_note(spec, "tool fetch failed: 404")
    assert "The deployed agent failed its self-test" in note
    assert "404" in note
