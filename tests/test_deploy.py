"""Phase 5 — deployability + maturity: health probe, external policy file,
and the Stripe live-mode fallback (default-off must never break the mock path)."""
import os

from app.services import guardrail_service, stripe_source


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok" and body["db"] is True
    assert "hermes_live" in body


def test_policy_file_is_loaded():
    # The out-of-process policy file is parsed and feeds the live constants.
    assert guardrail_service._FILE, "agent-cfo.policy.yaml should be loaded"
    assert guardrail_service._SPEND.get("auto_approve_limit") == 5000
    assert "unverified vendor" in guardrail_service.BLOCKED_PAYEES


def test_stripe_defaults_to_mock():
    # STRIPE_LIVE unset -> deterministic mock with the seeded rogue charge.
    os.environ.pop("STRIPE_LIVE", None)
    txns = {c["transaction_id"] for c in stripe_source.get_charges(30)}
    assert "txn_str_555" in txns


def test_stripe_live_without_key_falls_back_to_mock():
    os.environ["STRIPE_LIVE"] = "1"
    os.environ.pop("STRIPE_SECRET_KEY", None)
    try:
        assert stripe_source.get_charges_live(30) is None  # no key -> None
        # get_charges must still return the mock, never crash.
        txns = {c["transaction_id"] for c in stripe_source.get_charges(30)}
        assert "txn_str_555" in txns
    finally:
        os.environ.pop("STRIPE_LIVE", None)
