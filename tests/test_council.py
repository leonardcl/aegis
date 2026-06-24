"""Tests for the audit council — one-shot strategy, parsing, and fallback.

These run offline (conftest forces HERMES_API_URL=""), so the council uses the
deterministic narrator. They lock the *contract* the live one-shot path relies
on: a complete five-voice transcript, real engine numbers, and a result dict the
existing persistence/UI consume unchanged.
"""
from datetime import datetime

from app.models import LedgerEntry
from app.services import audit_service, hermes_council

ORDER = ["reconciler", "compliance", "period", "advisor", "lead"]


def _seed_ledger(db):
    """Seed posted spends that match / mismatch the mock Stripe source."""
    rows = [
        # matches txn_atl_014 ($3,400) exactly -> matched
        LedgerEntry(action="pay_invoice", payee="Atlassian", amount=3400,
                    outcome="posted", policy_decision="ALLOW",
                    transaction_id="txn_atl_014", timestamp=datetime.utcnow()),
        # ledger $1,200 vs Stripe $1,500 on txn_cf_001 -> amount_mismatch
        LedgerEntry(action="pay_invoice", payee="CloudFlare Inc", amount=1200,
                    outcome="posted", policy_decision="ALLOW",
                    transaction_id="txn_cf_001", timestamp=datetime.utcnow()),
        # txn_str_555 ("unknown-saas.io") is never seeded -> stripe_only rogue
    ]
    db.session.add_all(rows)
    db.session.commit()


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #
def test_parse_sections_splits_all_five():
    reply = (
        "@@RECONCILER@@\nMatched 1, one rogue charge txn_str_555.\n"
        "@@COMPLIANCE@@\nPASS vs policy 2026.06-1.\n"
        "@@PERIOD@@\nNo spikes.\n"
        "@@ADVISOR@@\nKeep Atlassian.\n"
        "@@LEAD@@\nTotal spend $4,600; one item escalated.\n"
    )
    out = hermes_council._parse_sections(reply)
    assert set(out) == set(ORDER)
    assert "txn_str_555" in out["reconciler"]
    assert out["lead"].startswith("Total spend")


def test_parse_sections_tolerates_noise_and_missing():
    reply = ("Sure, here is the deliberation.\n"
             "@@RECONCILER@@\nOne rogue charge.\n"
             "@@ LEAD @@\nEscalating one item.\n")  # spaced sentinel, 2 of 5
    out = hermes_council._parse_sections(reply)
    assert set(out) == {"reconciler", "lead"}
    assert "rogue" in out["reconciler"]


def test_parse_sections_empty():
    assert hermes_council._parse_sections("") == {}
    assert hermes_council._parse_sections("no sentinels here") == {}


# --------------------------------------------------------------------------- #
# One-shot end-to-end (offline -> deterministic narrator)
# --------------------------------------------------------------------------- #
def test_oneshot_offline_produces_complete_transcript(app, db):
    _seed_ledger(db)
    result = hermes_council.run_council(period_days=30, strategy="oneshot")

    # Shape the rest of the system depends on.
    assert result["rounds"] == 1
    assert result["engine"] == "local"          # offline -> all sections narrated
    assert [t["persona"] for t in result["transcript"]] == ORDER

    for turn in result["transcript"]:
        assert turn["content"].strip()           # never an empty voice
        assert turn["round"] == 1
        assert turn["title"]
        assert isinstance(turn["tool_calls"], list)

    # Numbers come from the engine, not invented.
    audit = result["audit"]
    assert audit["headline"]["total_spend"] >= 0
    # The unseeded Stripe charge must surface as a rogue (stripe_only) escalation.
    types = {e["exception_type"] for e in audit["escalations"]}
    assert "stripe_only_charge" in types
    recon_text = result["transcript"][0]["content"]
    assert "txn_str_555" in recon_text or "unknown-saas.io" in recon_text


def test_oneshot_result_persists_and_renders(app, db):
    _seed_ledger(db)
    result = hermes_council.run_council(period_days=30, strategy="oneshot")
    # Persistence + notes flattening must accept the one-shot transcript verbatim.
    report = audit_service.persist_council_result(result)
    assert report.id is not None
    assert report.notes and "Reconciler" in report.notes
    assert report.exceptions  # escalations mapped onto AuditException rows


def test_oneshot_computes_audit_once(app, db, monkeypatch):
    """The one-shot path must call full_audit exactly once (no per-persona reruns)."""
    _seed_ledger(db)
    calls = {"n": 0}
    real_full_audit = hermes_council.audit_engine.full_audit

    def _counting(*a, **k):
        calls["n"] += 1
        return real_full_audit(*a, **k)

    monkeypatch.setattr(hermes_council.audit_engine, "full_audit", _counting)
    hermes_council.run_council(period_days=30, strategy="oneshot")
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# Strategy dispatch
# --------------------------------------------------------------------------- #
def test_auto_is_sequential_when_offline(app, db):
    _seed_ledger(db)
    result = hermes_council.run_council(period_days=30, strategy="auto")
    # Offline auto -> sequential -> local reasoner keeps two rounds.
    assert result["rounds"] >= 1
    assert [t["persona"] for t in result["transcript"][:5]] == ORDER


def test_sequential_still_works(app, db):
    _seed_ledger(db)
    result = hermes_council.run_council(period_days=30, strategy="sequential")
    assert result["transcript"]
    assert result["audit"]["headline"]["total_spend"] >= 0


def test_hybrid_offline_full_transcript(app, db):
    _seed_ledger(db)
    result = hermes_council.run_council(period_days=30, strategy="hybrid")
    assert [t["persona"] for t in result["transcript"]] == ORDER
    for turn in result["transcript"]:
        assert turn["content"].strip()
    # Offline -> lead also narrated locally; persistence must still accept it.
    report = audit_service.persist_council_result(result)
    assert report.id is not None and report.notes
