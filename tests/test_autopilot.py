"""Tests for the grounded-discovery + negotiability + autopilot fix.

The reported bug was "ask for a robot arm, get Vercel" (the seed 'default' SaaS
bucket served for an off-catalogue/goods need) plus dead/hallucinated links and a
manual flow. These tests lock the cure in:

* a goods need NEVER returns the SaaS default trio,
* grounded discovery only keeps real, validated, in-category vendor URLs,
* negotiability is assessed before negotiating, with quote-correct wording,
* the autopilot job streams staged progress and stops honestly when nothing is
  verified.

No test hits the live internet: ``conftest`` forces ``HERMES_API_URL=""`` (so the
network grounded path is gated off) and the grounded-path tests monkeypatch
``websearch``/``hermes_client``.
"""
import json
import time

from app.models import ProcurementRequest, VendorOption
from app.services import discovery, negotiability, negotiation, jobs


ROBOT = {"need": "industrial 6-axis robot arm", "title": "Robot arm",
         "category": "Hardware", "must_haves": []}
TRANSCRIPTION = {"need": "transcription API with english and indonesian and "
                         "speaker diarization", "title": "Transcription API",
                 "category": "API", "budget_ceiling_usd": 300,
                 "must_haves": ["english", "indonesian", "speaker diarization"]}


# --------------------------------------------------------------------------- #
# Category classification + the goods guard (the core regression)
# --------------------------------------------------------------------------- #
def test_classify_need_kind():
    assert discovery.classify_need_kind(ROBOT) == "goods"
    assert discovery.classify_need_kind({"need": "15 laptops for the team"}) == "goods"
    assert discovery.classify_need_kind(TRANSCRIPTION) == "service"
    assert discovery.classify_need_kind({"need": "a CRM subscription"}) == "service"


def test_candidate_kind():
    assert discovery.candidate_kind({"pricing_model": "one_time"}) == "goods"
    assert discovery.candidate_kind({"pricing_model": "subscription_monthly"}) == "service"


def test_curated_blocks_default_bucket(app):
    # transcription is a real bucket -> Deepgram; robot arm is 'default' -> []
    assert any(c["vendor"] == "Deepgram"
               for c in discovery.curated_candidates(TRANSCRIPTION, 4))
    assert discovery.curated_candidates(ROBOT, 4) == []


def test_seed_goods_guard_never_returns_vercel(app):
    # The exact reported bug: a goods need must NOT get the SaaS default trio.
    cands = discovery._discover_seed(ROBOT, 4)
    assert cands == []
    assert not any(c["vendor"] in ("Vercel", "Notion", "Linear") for c in cands)


def test_live_offline_falls_back_to_curated_not_default(app):
    # Offline (is_live False) live mode -> curated (real bucket) or honest empty,
    # NEVER the SaaS default for a goods need.
    t = discovery.discover(TRANSCRIPTION, mode="live")
    assert t and t[0]["_source"] == "seed" and any(c["vendor"] == "Deepgram" for c in t)
    g = discovery.discover(ROBOT, mode="live")
    assert all(c["vendor"] not in ("Vercel", "Notion", "Linear") for c in g)
    assert g == []


# --------------------------------------------------------------------------- #
# Grounded discovery: real-URL-only, link validation, category gate
# --------------------------------------------------------------------------- #
def _ground(monkeypatch, results, chat_content, pages=None):
    """Stub the network: web_search -> results, fetch_pages -> (verdict, text)
    per url (default alive with sample text), Hermes chat -> chat_content."""
    pages = pages or {}
    monkeypatch.setattr(discovery.hermes_client, "is_live", lambda: True)
    monkeypatch.setattr(discovery.websearch, "web_search",
                        lambda q, max_results=8: list(results))
    monkeypatch.setattr(discovery.hermes_client, "chat",
                        lambda *a, **k: {"engine": "hermes", "content": chat_content})
    monkeypatch.setattr(discovery.websearch, "fetch_pages",
                        lambda urls, **k: {u: pages.get(u, ("alive", "sample page $100"))
                                           for u in urls})


def test_grounded_keeps_only_result_urls(app, monkeypatch):
    results = [
        {"title": "ABB Robotics", "url": "https://abb.com/robotics",
         "snippet": "6-axis arms", "domain": "abb.com"},
        {"title": "DENSO Robotics", "url": "https://densorobotics.com/",
         "snippet": "industrial arms", "domain": "densorobotics.com"},
    ]
    chat = json.dumps([
        {"vendor": "ABB", "product": "IRB 1300", "url": "https://abb.com/robotics",
         "price": 22000, "pricing_model": "one_time", "supports": ["6-axis"]},
        # hallucinated url not present in the search results -> must be dropped
        {"vendor": "FakeCo", "product": "x", "url": "https://hallucinated-x.com",
         "price": 1, "pricing_model": "one_time", "supports": []},
    ])
    _ground(monkeypatch, results, chat, {})
    out = discovery._discover_live(ROBOT, 4)
    names = [c["vendor"] for c in out]
    assert "ABB" in names
    assert "FakeCo" not in names
    assert all(c["url"].startswith("https://abb.com") for c in out if c["vendor"] == "ABB")


def test_grounded_drops_dead_keeps_blocked(app, monkeypatch):
    results = [
        {"title": "ABB", "url": "https://abb.com/", "snippet": "", "domain": "abb.com"},
        {"title": "DENSO", "url": "https://densorobotics.com/", "snippet": "",
         "domain": "densorobotics.com"},
    ]
    chat = json.dumps([
        {"vendor": "ABB", "url": "https://abb.com/", "price": 20000,
         "pricing_model": "one_time", "supports": []},
        {"vendor": "Denso", "url": "https://densorobotics.com/", "price": 18000,
         "pricing_model": "one_time", "supports": []},
    ])
    pages = {"https://abb.com/": ("dead", ""),
             "https://densorobotics.com/": ("blocked", "")}
    _ground(monkeypatch, results, chat, pages)
    out = discovery._discover_live(ROBOT, 4)
    names = [c["vendor"] for c in out]
    assert "ABB" not in names           # dead link dropped
    assert "Denso" in names             # blocked == real site, kept
    denso = next(c for c in out if c["vendor"] == "Denso")
    assert denso["url_status"] == "blocked" and denso["url_verified"] is True


def test_grounded_category_gate_drops_saas_for_goods(app, monkeypatch):
    results = [{"title": "Vercel", "url": "https://vercel.com/", "snippet": "",
               "domain": "vercel.com"}]
    chat = json.dumps([{"vendor": "Vercel", "url": "https://vercel.com/",
                        "price": 20, "pricing_model": "subscription_monthly",
                        "supports": ["hosting"]}])
    _ground(monkeypatch, results, chat, {})
    # robot arm is goods; a subscription SaaS candidate must be gated out
    assert discovery._discover_live(ROBOT, 4) is None


def test_grounded_uses_no_tools_param(app, monkeypatch):
    captured = {}

    def fake_chat(messages, use_tools=True, label=None, **kw):
        captured["use_tools"] = use_tools
        captured["kw"] = kw
        return {"engine": "hermes", "content": "[]"}

    monkeypatch.setattr(discovery.hermes_client, "is_live", lambda: True)
    monkeypatch.setattr(discovery.websearch, "web_search",
                        lambda q, max_results=8: [{"title": "x", "url": "https://x.com",
                                                   "snippet": "", "domain": "x.com"}])
    monkeypatch.setattr(discovery.websearch, "fetch_pages",
                        lambda urls, **k: {u: ("alive", "page text") for u in urls})
    monkeypatch.setattr(discovery.hermes_client, "chat", fake_chat)
    discovery._discover_live(ROBOT, 4)
    assert captured.get("use_tools") is False          # never send the tools param
    assert "tools" not in captured.get("kw", {})


def test_pricing_model_and_link_persisted(app, db, monkeypatch):
    req = ProcurementRequest(title="Robot arm")
    req.requirement_spec = ROBOT
    db.session.add(req)
    db.session.commit()
    results = [{"title": "DENSO", "url": "https://densorobotics.com/", "snippet": "",
                "domain": "densorobotics.com"}]
    chat = json.dumps([{"vendor": "Denso", "url": "https://densorobotics.com/",
                        "price": 18000, "pricing_model": "one_time",
                        "supports": ["6-axis"]}])
    _ground(monkeypatch, results, chat, {"https://densorobotics.com/": ("blocked", "")})
    created, resolved = discovery.discover_for_request(req, mode="live")
    assert resolved == "live"
    v = next(v for v in req.vendors if v.name == "Denso")
    assert v.enrichment.get("pricing_model") == "one_time"
    assert v.enrichment.get("link_status") == "blocked"
    assert v.enrichment.get("url_verified") is True


# --------------------------------------------------------------------------- #
# Negotiability assessment + quote-correct negotiation
# --------------------------------------------------------------------------- #
def _vendor(price, pricing_model="", basis=""):
    v = VendorOption(name="X", price=price, price_basis=basis)
    if pricing_model:
        v.enrichment = {"pricing_model": pricing_model}
    return v


def test_is_negotiable_cases(app):
    assert negotiability.is_negotiable(
        _vendor(215, "subscription_monthly"), {"category": "API"})["negotiable"] is True
    assert negotiability.is_negotiable(_vendor(0), {})["negotiable"] is False
    assert negotiability.is_negotiable(
        _vendor(20, "subscription_monthly"), {})["negotiable"] is False
    big = negotiability.is_negotiable(
        _vendor(18000, "one_time"), {"category": "Hardware"})
    assert big["negotiable"] is True and "quote" in (big["tactic"] or "").lower()
    assert negotiability.is_negotiable(_vendor(200, "usage"), {})["negotiable"] is False
    assert negotiability.is_negotiable(_vendor(1500, "usage"), {})["negotiable"] is True


def test_negotiate_quote_has_no_period_wording(app):
    res = negotiation.negotiate("ABB", 20000, live=False, kind="quote",
                                pricing_model="one_time", floor_pct=0.85)
    assert res["kind"] == "quote"
    joined = " ".join(t["message"] for t in res["transcript"])
    assert "/period" not in joined
    assert "quote" in joined.lower()
    assert res["agreed_amount"] <= 20000


def test_negotiate_spend_keeps_period_wording(app):
    res = negotiation.negotiate("DataVault", 10000, live=False)  # default kind='spend'
    joined = " ".join(t["message"] for t in res["transcript"])
    assert "/period" in joined or "per period" in joined


# --------------------------------------------------------------------------- #
# Autopilot orchestration
# --------------------------------------------------------------------------- #
def _wait(job_id, timeout=15):
    for _ in range(int(timeout * 20)):
        j = jobs.get_job(job_id)
        if j.get("status") in ("done", "error"):
            return j
        time.sleep(0.05)
    return jobs.get_job(job_id)


def test_autopilot_robot_arm_offline_needs_input(app, db):
    req = ProcurementRequest(title="Robot arm")
    req.requirement_spec = ROBOT
    db.session.add(req)
    db.session.commit()
    job_id = jobs.start_autopilot_job(app, req.id, want_hermes=False)
    j = _wait(job_id)
    assert j["status"] == "done"
    db.session.expire_all()
    r = db.session.get(ProcurementRequest, req.id)
    assert r.status == "needs_input"
    assert not any(v.name in ("Vercel", "Notion", "Linear") for v in r.vendors)
    stages = [e["stage"] for e in j["events"]]
    assert "discover" in stages and "done" in stages


def test_autopilot_transcription_offline_recommends_and_skips_nonneg(app, db):
    req = ProcurementRequest(title="Transcription API")
    req.requirement_spec = TRANSCRIPTION
    db.session.add(req)
    db.session.commit()
    job_id = jobs.start_autopilot_job(app, req.id, want_hermes=False)
    j = _wait(job_id)
    assert j["status"] == "done"
    assert j["recommended_vendor_id"] is not None
    db.session.expire_all()
    r = db.session.get(ProcurementRequest, req.id)
    assert r.recommended_vendor.name == "Deepgram"
    # Deepgram is usage-metered at $215/mo -> below threshold -> not negotiated.
    assert j["negotiated"] is False
    assert any("negotiab" in e["stage"] for e in j["events"])


def test_autopilot_status_route_unknown(app, client):
    r = client.get("/procurement/autopilot-status/nope")
    assert r.status_code == 200 and r.get_json()["status"] == "unknown"


def test_autopilot_double_submit_returns_same_job(app, db, monkeypatch):
    req = ProcurementRequest(title="X")
    req.requirement_spec = TRANSCRIPTION
    db.session.add(req)
    db.session.commit()
    monkeypatch.setattr("app.services.autopilot.run_autopilot",
                        lambda a, r, w, log: time.sleep(0.4) or {"status": "done"})
    j1 = jobs.start_autopilot_job(app, req.id, want_hermes=False)
    j2 = jobs.start_autopilot_job(app, req.id, want_hermes=False)
    assert j1 == j2
    _wait(j1)


def test_autopilot_start_route_creates_request(app, client, db):
    resp = client.post("/procurement/autopilot",
                       data={"need": "I need 10 laptops under $15000"})
    assert resp.status_code in (302, 303)
    assert "autopilot=" in resp.headers["Location"]
