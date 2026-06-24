"""L0 unit tests for W1 INTAKE (deterministic parser).

Run:  ~/miniconda3/envs/aegis/bin/python -m pytest tests/test_intake.py -q
"""
from datetime import date, timedelta

from app.services import intake

FIXED_TODAY = date(2026, 6, 22)


def spec(text):
    return intake.parse_need_deterministic(text, today=FIXED_TODAY)


# --------------------------------------------------------------------------- #
# The canonical demo sentence
# --------------------------------------------------------------------------- #
def test_demo_sentence_full_spec():
    s = spec("I need a transcription API under $300/mo with English and "
             "Indonesian and speaker diarization, live in 2 weeks, "
             "nice to have word-level timestamps")
    assert s["budget_ceiling_usd"] == 300
    assert s["category"] == "API"
    assert s["deadline"] == (FIXED_TODAY + timedelta(days=14)).isoformat()
    assert "English" in s["must_haves"]
    assert "Indonesian" in s["must_haves"]
    assert "speaker diarization" in s["must_haves"]
    assert "word-level timestamps" in s["nice_to_haves"]
    # priority weights are all within range
    assert all(0 <= v <= 5 for v in s["priority"].values())


# --------------------------------------------------------------------------- #
# Budget extraction
# --------------------------------------------------------------------------- #
def test_budget_under():
    assert spec("API under $300/mo")["budget_ceiling_usd"] == 300


def test_budget_thousands_suffix():
    assert spec("laptops, budget $45k")["budget_ceiling_usd"] == 45000


def test_budget_comma():
    assert spec("audit engagement up to $60,000")["budget_ceiling_usd"] == 60000


def test_budget_absent():
    assert spec("I need a CRM")["budget_ceiling_usd"] == 0.0


# --------------------------------------------------------------------------- #
# Deadline extraction
# --------------------------------------------------------------------------- #
def test_deadline_weeks():
    assert spec("live in 3 weeks")["deadline"] == (FIXED_TODAY + timedelta(days=21)).isoformat()


def test_deadline_asap():
    s = spec("I need a fraud API ASAP")
    assert s["deadline"] == (FIXED_TODAY + timedelta(days=7)).isoformat()
    assert s["deadline_raw"] == "ASAP"


def test_deadline_absent():
    assert spec("I need a CRM tool")["deadline"] is None


# --------------------------------------------------------------------------- #
# Priority derivation
# --------------------------------------------------------------------------- #
def test_priority_urgent_bumps_time():
    assert spec("urgent transcription API")["priority"]["time"] == 5


def test_priority_cheap_bumps_price():
    assert spec("cheapest possible storage")["priority"]["price"] == 5


def test_priority_critical_bumps_risk_quality():
    p = spec("mission-critical secure payments API")["priority"]
    assert p["risk"] >= 4 and p["quality"] >= 4


def test_priority_default_neutral():
    assert spec("a notes app")["priority"] == intake.DEFAULT_PRIORITY


# --------------------------------------------------------------------------- #
# Category + quantity
# --------------------------------------------------------------------------- #
def test_category_api():
    assert spec("speech-to-text API")["category"] == "API"


def test_category_hardware():
    assert spec("15 laptops for the team")["category"] == "Hardware"


def test_quantity_minutes():
    assert "50,000" in spec("transcription, ~50,000 min/month")["quantity"]


def test_quantity_does_not_leak_into_musthaves():
    # regression: inline usage figure must not become a must-have token
    s = spec("transcription API with English and Indonesian and speaker "
             "diarization, ~50,000 min/month, live in 2 weeks")
    assert s["must_haves"] == ["English", "Indonesian", "speaker diarization"]
    assert "50,000 min/month" in s["quantity"] or "50,000" in s["quantity"]
    # no token containing a stray digit or bare unit word
    assert not any(any(ch.isdigit() for ch in m) for m in s["must_haves"])


# --------------------------------------------------------------------------- #
# parse_need wrapper (deterministic path) + apply_spec_to_request
# --------------------------------------------------------------------------- #
def test_parse_need_marks_engine():
    s = intake.parse_need("I need a CRM under $100/mo", use_hermes=False, today=FIXED_TODAY)
    assert s["_engine"] == "deterministic"


def test_apply_spec_to_request_maps_fields():
    from app.models import ProcurementRequest

    s = spec("I need a transcription API under $300/mo with English diarization, "
             "live in 2 weeks")
    req = ProcurementRequest()
    intake.apply_spec_to_request(req, s, raw_text="orig text")
    assert req.budget_ceiling == 300
    assert req.intake_raw == "orig text"
    assert req.category == "API"
    assert req.status == "analyzing"
    # the spec round-trips through the JSON property
    assert req.requirement_spec["budget_ceiling_usd"] == 300
    assert "_engine" not in req.requirement_spec  # transient marker stripped
