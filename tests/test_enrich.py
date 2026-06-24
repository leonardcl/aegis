"""L1 tests for W3 ENRICH (scoring + must-have disqualification)."""
from datetime import date, timedelta

from app.models import ProcurementRequest
from app.services import discovery, enrich, procurement_service

SPEC = {
    "need": "transcription API for a podcast app",
    "title": "Transcription API",
    "category": "API",
    "budget_ceiling_usd": 300,
    "must_haves": ["english", "indonesian", "speaker diarization"],
    "nice_to_haves": ["word-level timestamps"],
    "priority": {"price": 4, "time": 3, "risk": 4, "quality": 4, "terms": 2},
}


def _seeded_request(db):
    req = ProcurementRequest(title="Transcription API", budget_ceiling=300)
    req.requirement_spec = SPEC
    db.session.add(req)
    db.session.commit()
    cands = discovery.discover(SPEC, mode="seed", limit=4)
    discovery.persist_candidates(req, cands)
    db.session.commit()
    return req


# --------------------------------------------------------------------------- #
# Disqualification
# --------------------------------------------------------------------------- #
def test_missing_musthave_disqualifies(app, db):
    req = _seeded_request(db)
    enrich.enrich_request(req)
    by = {v.name: v for v in req.vendors}
    # Whisper has no diarization; AssemblyAI has no Indonesian -> both disqualified
    assert by["OpenAI Whisper API"].disqualified is True
    assert "diariz" in by["OpenAI Whisper API"].disqualify_reason.lower()
    assert by["AssemblyAI"].disqualified is True
    # Deepgram + Google cover all must-haves -> qualified
    assert by["Deepgram"].disqualified is False
    assert by["Google Cloud Speech-to-Text"].disqualified is False


def test_enrich_summary_counts(app, db):
    req = _seeded_request(db)
    summary = enrich.enrich_request(req)
    assert summary["enriched"] == 4
    assert summary["disqualified"] == 2
    assert summary["qualified"] == 2


def test_unverified_capabilities_not_disqualified(app, db):
    # manual vendor with no enrichment data must not be wrongly killed
    from app.models import VendorOption
    req = ProcurementRequest(title="X", budget_ceiling=300, must_haves="english")
    db.session.add(req)
    db.session.commit()
    v = VendorOption(request_id=req.id, name="Manual Co", price=100)
    db.session.add(v)
    db.session.commit()
    enrich.enrich_request(req)
    assert v.disqualified is False
    assert v.disqualify_reason == "Capabilities unverified"


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def test_scores_populated_in_range(app, db):
    req = _seeded_request(db)
    enrich.enrich_request(req)
    for v in req.vendors:
        for attr in ("score_price", "score_time", "score_risk",
                     "score_quality", "score_terms"):
            assert 0 <= getattr(v, attr) <= 100


def test_cheaper_scores_higher_on_price(app, db):
    req = _seeded_request(db)
    enrich.enrich_request(req)
    by = {v.name: v for v in req.vendors}
    # AssemblyAI ($185) cheaper than Google ($240) -> higher price score
    assert by["AssemblyAI"].score_price > by["Google Cloud Speech-to-Text"].score_price


def test_recommendation_excludes_disqualified(app, db):
    req = _seeded_request(db)
    enrich.enrich_request(req)
    best = procurement_service.generate_recommendation(req)
    assert best is not None
    assert best.disqualified is False
    # qualifying set is Deepgram / Google; cheaper+faster Deepgram should win
    assert best.name == "Deepgram"


def test_over_budget_penalised(app, db):
    # Rev.ai ($1000) is well over the $300 budget -> low price score
    from app.models import VendorOption
    req = ProcurementRequest(title="X", budget_ceiling=300)
    req.requirement_spec = SPEC
    db.session.add(req)
    db.session.commit()
    cheap = VendorOption(request_id=req.id, name="Cheap", price=200)
    cheap.enrichment = {"capabilities": ["english", "indonesian", "speaker diarization"],
                        "reliability": 80, "flexibility": 80}
    pricey = VendorOption(request_id=req.id, name="Pricey", price=1000)
    pricey.enrichment = {"capabilities": ["english", "indonesian", "speaker diarization"],
                         "reliability": 80, "flexibility": 80}
    db.session.add_all([cheap, pricey])
    db.session.commit()
    enrich.enrich_request(req)
    assert pricey.score_price <= 30
    assert cheap.score_price > pricey.score_price


def test_deadline_miss_penalises_time(app, db):
    from app.models import VendorOption
    req = ProcurementRequest(title="X", budget_ceiling=300,
                             deadline=date.today() + timedelta(days=3))
    db.session.add(req)
    db.session.commit()
    fast = VendorOption(request_id=req.id, name="Fast", price=100, lead_time_days=1)
    slow = VendorOption(request_id=req.id, name="Slow", price=100, lead_time_days=30)
    db.session.add_all([fast, slow])
    db.session.commit()
    enrich.enrich_request(req)
    assert slow.score_time <= 35  # cannot make a 3-day deadline
    assert fast.score_time > slow.score_time
