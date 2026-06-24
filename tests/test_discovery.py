"""L0/L1 tests for W2 DISCOVER (seed mode, deterministic)."""
from app.models import ProcurementRequest
from app.services import discovery

TRANSCRIPTION_SPEC = {
    "need": "transcription API for a podcast app",
    "title": "Transcription API",
    "category": "API",
    "budget_ceiling_usd": 300,
    "must_haves": ["english", "speaker diarization"],
}


# --------------------------------------------------------------------------- #
# Bucket matching
# --------------------------------------------------------------------------- #
def test_bucket_transcription():
    assert discovery.match_bucket(TRANSCRIPTION_SPEC) == "transcription"


def test_bucket_analytics():
    assert discovery.match_bucket({"need": "product analytics with funnels"}) == "analytics"


def test_bucket_default_fallback():
    assert discovery.match_bucket({"need": "a quantum teapot"}) == "default"


# --------------------------------------------------------------------------- #
# discover() — seed mode
# --------------------------------------------------------------------------- #
def test_discover_seed_returns_candidates(app):
    cands = discovery.discover(TRANSCRIPTION_SPEC, mode="seed")
    assert len(cands) >= 3
    first = cands[0]
    for key in ("vendor", "price", "url", "price_basis", "supports", "_source"):
        assert key in first
    assert first["_source"] == "seed"
    # the curated transcription bucket should surface Deepgram
    assert any(c["vendor"] == "Deepgram" for c in cands)


def test_discover_respects_limit(app):
    assert len(discovery.discover(TRANSCRIPTION_SPEC, mode="seed", limit=2)) == 2


def test_discover_default_mode_is_seed(app):
    # config fixture sets PROCUREMENT_DISCOVERY_MODE=seed
    cands = discovery.discover(TRANSCRIPTION_SPEC)
    assert cands and cands[0]["_source"] == "seed"


def test_live_mode_falls_back_to_seed_when_offline(app):
    # HERMES_API_URL is empty in the fixture -> live yields nothing -> seed
    cands = discovery.discover(TRANSCRIPTION_SPEC, mode="live")
    assert cands and cands[0]["_source"] == "seed"


# --------------------------------------------------------------------------- #
# persist_candidates + discover_for_request
# --------------------------------------------------------------------------- #
def _make_request(db, **kw):
    req = ProcurementRequest(title=kw.pop("title", "Transcription API"), **kw)
    req.requirement_spec = TRANSCRIPTION_SPEC
    db.session.add(req)
    db.session.commit()
    return req


def test_persist_creates_vendor_rows(app, db):
    req = _make_request(db)
    cands = discovery.discover(TRANSCRIPTION_SPEC, mode="seed")
    created = discovery.persist_candidates(req, cands)
    db.session.commit()
    assert len(created) == len(cands)
    assert all(v.source == "seed" for v in created)
    assert all(v.id is not None for v in created)
    # supports captured into notes for W3
    assert any("Supports:" in v.notes for v in created)


def test_persist_dedupes_by_name(app, db):
    req = _make_request(db)
    cands = discovery.discover(TRANSCRIPTION_SPEC, mode="seed")
    discovery.persist_candidates(req, cands)
    db.session.commit()
    before = len(req.vendors)
    again = discovery.persist_candidates(req, cands)  # same set again
    db.session.commit()
    assert again == []
    assert len(req.vendors) == before


def test_discover_for_request_uses_stored_spec(app, db):
    req = _make_request(db)
    created, resolved = discovery.discover_for_request(req)
    assert resolved == "seed"
    assert len(created) >= 3
    assert any(v.name == "Deepgram" for v in req.vendors)


def test_discover_for_request_without_spec_uses_fields(app, db):
    # manual request: no requirement_spec, but fields imply transcription
    req = ProcurementRequest(title="Speech-to-text API", description="podcast transcription",
                             must_haves="english, speaker diarization")
    db.session.add(req)
    db.session.commit()
    created, resolved = discovery.discover_for_request(req)
    assert created
    assert any(v.name == "Deepgram" for v in created)
