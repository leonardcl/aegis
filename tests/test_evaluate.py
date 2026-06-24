"""L1 tests for W4 EVALUATE (comparative recommendation narrative)."""
from app.models import ProcurementRequest
from app.services import discovery, enrich, evaluate, procurement_service

SPEC = {
    "need": "transcription API for a podcast app",
    "title": "Transcription API",
    "category": "API",
    "budget_ceiling_usd": 300,
    "must_haves": ["english", "indonesian", "speaker diarization"],
    "nice_to_haves": ["word-level timestamps"],
    "priority": {"price": 4, "time": 3, "risk": 4, "quality": 4, "terms": 2},
}


def _ready_request(db):
    req = ProcurementRequest(title="Transcription API", budget_ceiling=300,
                             priority_price=4, priority_risk=4, priority_quality=4)
    req.requirement_spec = SPEC
    db.session.add(req)
    db.session.commit()
    discovery.persist_candidates(req, discovery.discover(SPEC, mode="seed", limit=4))
    db.session.commit()
    enrich.enrich_request(req)
    return req


def test_narrative_names_winner_and_runner_and_excluded(app, db):
    req = _ready_request(db)
    best = procurement_service.generate_recommendation(req)
    text = req.agent_recommendation
    assert best.name == "Deepgram"
    assert "Deepgram" in text                       # winner named
    assert "Google Cloud Speech-to-Text" in text    # runner-up named
    # at least one disqualified vendor is explained
    assert "AssemblyAI" in text or "Whisper" in text
    assert "must-have" in text.lower()


def test_narrative_mentions_priorities(app, db):
    req = _ready_request(db)
    procurement_service.generate_recommendation(req)
    assert "priorit" in req.agent_recommendation.lower()


def test_narrate_engine_is_deterministic_when_offline(app, db):
    req = _ready_request(db)
    best = procurement_service.generate_recommendation(req)
    _text, engine = evaluate.narrate(req, best, use_hermes=True)  # offline -> falls back
    assert engine == "deterministic"


def test_build_narrative_is_pure(app, db):
    # build_narrative must not require Hermes and must be stable
    req = _ready_request(db)
    best = max((v for v in req.vendors if not v.disqualified),
               key=lambda v: v.total_score)
    a = evaluate.build_narrative(req, best)
    b = evaluate.build_narrative(req, best)
    assert a == b and best.name in a


def test_top_strengths_orders_by_score(app, db):
    req = _ready_request(db)
    best = max((v for v in req.vendors if not v.disqualified),
               key=lambda v: v.total_score)
    s = evaluate._top_strengths(best, n=2)
    assert "(" in s and ")" in s  # "label (score) and label (score)"
