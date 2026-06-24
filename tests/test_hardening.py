"""Regression tests for the Phase 0 (safety) + Phase 1 (correctness) hardening.

These cover the write/decision paths the original suite didn't: the approval
idempotency/force-post guard, the reconciliation fix for live-approved spends,
numeric-input coercion, the chatbot identity scrub, the guardrail dev-toggle, and
the same-origin CSRF guard.
"""
import os

import pytest

from app import create_app
from app.extensions import db as _db
from app.models import (ApprovalRequest, LedgerEntry, ProcurementRequest,
                        VendorOption)
from app.routes.procurement import parse_int, parse_money
from app.services import agent_guardrail, audit_engine, guardrail_service
from app.services import hermes_service


# --------------------------------------------------------------------------- #
# 1.3 — numeric input coercion never 500s
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("$30,000", 30000.0), ("3.5k", 3500.0), ("2m", 2_000_000.0),
    ("1,234.50", 1234.5), ("", 0.0), (None, 0.0), ("not-a-number", 0.0),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_parse_int():
    assert parse_int("5") == 5
    assert parse_int("3.9") == 4
    assert parse_int("junk", 3) == 3


# --------------------------------------------------------------------------- #
# 1.4 — chatbot never leaks its platform/model identity
# --------------------------------------------------------------------------- #
def test_identity_question_canned_reply(app):
    with app.app_context():
        for q in ("who are you?", "what can you do", "which model are you?"):
            assert hermes_service._identity_reply(q)
        assert hermes_service._identity_reply("what is our AWS spend?") is None


def test_scrub_identity_removes_platform_terms_keeps_vendors():
    leaky = ("I'm a large language model (Nemotron) in a NemoClaw sandbox at "
             "127.0.0.1:8642 using SKILL.md skills. We spend $8,600 on AWS and OpenAI.")
    out = agent_guardrail.scrub_identity(leaky)
    for term in ("Nemotron", "NemoClaw", "8642", "SKILL.md", "language model"):
        assert term.lower() not in out.lower()
    # Vendors are finance, not identity — they stay.
    assert "AWS" in out and "OpenAI" in out


# --------------------------------------------------------------------------- #
# 0.4 — guardrail dev-toggle: ALLOW everything when disabled, deny otherwise
# --------------------------------------------------------------------------- #
def test_guardrail_dev_toggle(app):
    with app.app_context():
        app.config["GUARDRAILS_DISABLED"] = False
        assert guardrail_service.evaluate_policy(60000, payee="x")["decision"] == "BLOCK"
        app.config["GUARDRAILS_DISABLED"] = True
        d = guardrail_service.evaluate_policy(99_000_000, payee="sanctioned ltd")
        assert d["decision"] == "ALLOW" and d["rule"] == "dev_bypass"
        app.config["GUARDRAILS_DISABLED"] = False


# --------------------------------------------------------------------------- #
# 1.1 — approval idempotency + BLOCKED can never be force-posted
# --------------------------------------------------------------------------- #
def _make_request(amount, payee):
    r = ProcurementRequest(title=f"buy from {payee}")
    _db.session.add(r)
    _db.session.commit()
    v = VendorOption(request_id=r.id, name=payee, price=amount)
    _db.session.add(v)
    _db.session.commit()
    r.recommended_vendor_id = v.id
    _db.session.commit()
    return r


def test_approval_is_idempotent(app):
    with app.app_context():
        r = _make_request(12000, "NimbusCloud")           # NEEDS_APPROVAL
        appr = guardrail_service.send_to_guardrail(r)
        assert appr.status == "NEEDS_APPROVAL"
        guardrail_service.decide_approval(appr, "approve")
        guardrail_service.decide_approval(appr, "approve")  # double submit
        posted = LedgerEntry.query.filter_by(request_id=r.id).count()
        assert posted == 1


def test_blocked_cannot_be_force_posted(app):
    with app.app_context():
        r = _make_request(999, "Sanctioned Ltd")           # blocklisted -> BLOCK
        appr = guardrail_service.send_to_guardrail(r)
        assert appr.status == "BLOCKED"
        before = LedgerEntry.query.count()
        guardrail_service.decide_approval(appr, "approve")  # attacker force-post
        assert LedgerEntry.query.count() == before          # nothing posted
        assert appr.status == "BLOCKED"


# --------------------------------------------------------------------------- #
# 1.2 — a live-approved spend reconciles (no false ledger_only)
# --------------------------------------------------------------------------- #
def test_approved_spend_reconciles(app):
    with app.app_context():
        r = _make_request(12000, "NimbusCloud")
        appr = guardrail_service.send_to_guardrail(r)
        guardrail_service.decide_approval(appr, "approve")
        recon = audit_engine.reconcile(30)
        ledger_only_txns = {x["transaction_id"] for x in recon["ledger_only"]}
        assert not any(t.startswith("ch_aegis_") for t in ledger_only_txns)
        assert recon["matched_count"] >= 1


# --------------------------------------------------------------------------- #
# 1.6 — same-origin CSRF guard (needs a non-TESTING app)
# --------------------------------------------------------------------------- #
def test_csrf_same_origin_guard():
    os.environ["HERMES_API_URL"] = ""
    app = create_app()
    app.testing = False  # CSRF is intentionally skipped under TESTING
    with app.app_context():
        _db.create_all()
    c = app.test_client()
    ok = c.post("/agent/chat", json={"message": "hi"},
                headers={"Origin": "http://localhost"}, base_url="http://localhost")
    bad = c.post("/agent/chat", json={"message": "hi"},
                 headers={"Origin": "http://evil.com"}, base_url="http://localhost")
    assert ok.status_code == 200
    assert bad.status_code == 403
