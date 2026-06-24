"""W2 DISCOVER — find candidate vendors for a requirement spec.

Stage 2 of the on-demand procurement flow. Given a requirement spec (from W1
INTAKE), produce a candidate vendor list and persist them as ``VendorOption``
rows for the EVALUATE stage.

Two modes (mirrors the audit flow's "always demoable" stance):

* ``seed`` (default) — a curated catalogue of real, Stripe-payable SaaS/API
  vendors, chosen by matching the need against topic buckets. Deterministic,
  instant, identical every take — the safe path for filming.
* ``live`` — ask the real agent to suggest vendors (model knowledge, returned as
  JSON). Any failure (agent down, slow, non-JSON, empty) falls back to ``seed``.
  NOTE: this uses the model's knowledge, not a live browser. True web/browser
  discovery is the sandbox (Option B) path, deferred to W10.

Capabilities for each candidate are carried in ``supports`` and persisted into
the vendor ``notes`` ("Supports: ...") so W3 ENRICH can disqualify candidates
that miss a must-have.
"""
import json

from flask import current_app

from ..extensions import db
from ..models import VendorOption
from . import hermes_client


# --------------------------------------------------------------------------- #
# Seed catalogue — real, Stripe-payable SaaS/API vendors by topic bucket.
# Prices are plausible monthly estimates; price_basis explains the assumption.
# --------------------------------------------------------------------------- #
SEED_CATALOG = {
    "transcription": [
        {"vendor": "Deepgram", "product": "Nova-2 Speech-to-Text API",
         "url": "https://deepgram.com", "price": 215.0,
         "price_basis": "$0.0043/min @ ~50k min/mo", "lead_time_days": 1,
         "supports": ["english", "indonesian", "speaker diarization",
                      "word-level timestamps"],
         "reliability": 88, "flexibility": 90,
         "notes": "Usage-based, fast integration, strong diarization."},
        {"vendor": "AssemblyAI", "product": "Universal-2 Speech API",
         "url": "https://assemblyai.com", "price": 185.0,
         "price_basis": "$0.0037/min @ ~50k min/mo", "lead_time_days": 1,
         "supports": ["english", "speaker diarization", "word-level timestamps"],
         "reliability": 85, "flexibility": 88,
         "notes": "Great English accuracy; limited Indonesian coverage."},
        {"vendor": "Google Cloud Speech-to-Text", "product": "STT v2",
         "url": "https://cloud.google.com/speech-to-text", "price": 240.0,
         "price_basis": "~$0.0048/min w/ commit @ ~50k min/mo", "lead_time_days": 2,
         "supports": ["english", "indonesian", "speaker diarization",
                      "word-level timestamps"],
         "reliability": 92, "flexibility": 70,
         "notes": "Broad language coverage incl. Indonesian; GCP setup overhead."},
        {"vendor": "OpenAI Whisper API", "product": "whisper-1",
         "url": "https://platform.openai.com", "price": 300.0,
         "price_basis": "$0.006/min @ ~50k min/mo", "lead_time_days": 1,
         "supports": ["english", "indonesian"],
         "reliability": 80, "flexibility": 92,
         "notes": "Cheap & multilingual, but no built-in speaker diarization."},
        {"vendor": "Rev.ai", "product": "Asynchronous Speech-to-Text",
         "url": "https://rev.ai", "price": 1000.0,
         "price_basis": "$0.02/min @ ~50k min/mo", "lead_time_days": 1,
         "supports": ["english", "speaker diarization", "word-level timestamps"],
         "reliability": 90, "flexibility": 85,
         "notes": "Premium accuracy; well over a $300 budget at this volume."},
    ],
    "analytics": [
        {"vendor": "PostHog", "product": "Product Analytics",
         "url": "https://posthog.com", "price": 0.0,
         "price_basis": "Free tier to 1M events, then usage", "lead_time_days": 1,
         "supports": ["events", "funnels", "session replay", "self-host"],
         "notes": "Generous free tier; open-source option."},
        {"vendor": "Mixpanel", "product": "Growth Analytics",
         "url": "https://mixpanel.com", "price": 140.0,
         "price_basis": "Growth plan @ ~1M events/mo", "lead_time_days": 1,
         "supports": ["events", "funnels", "retention"],
         "notes": "Strong funnels/retention reporting."},
        {"vendor": "Amplitude", "product": "Analytics Plus",
         "url": "https://amplitude.com", "price": 250.0,
         "price_basis": "Plus plan @ ~1M events/mo", "lead_time_days": 2,
         "supports": ["events", "funnels", "retention", "experimentation"],
         "notes": "Behavioural cohorts + experimentation."},
        {"vendor": "Heap", "product": "Digital Insights",
         "url": "https://heap.io", "price": 280.0,
         "price_basis": "Growth tier estimate", "lead_time_days": 3,
         "supports": ["events", "autocapture", "funnels"],
         "notes": "Autocapture reduces instrumentation effort."},
    ],
    "crm": [
        {"vendor": "HubSpot", "product": "Sales Hub Starter",
         "url": "https://hubspot.com", "price": 90.0,
         "price_basis": "Starter, ~2 seats", "lead_time_days": 1,
         "supports": ["contacts", "pipeline", "email", "automation"],
         "notes": "Easy onboarding; scales into marketing suite."},
        {"vendor": "Pipedrive", "product": "Advanced",
         "url": "https://pipedrive.com", "price": 100.0,
         "price_basis": "Advanced @ ~4 seats", "lead_time_days": 1,
         "supports": ["contacts", "pipeline", "automation"],
         "notes": "Lightweight, sales-focused."},
        {"vendor": "Salesforce", "product": "Sales Cloud Pro",
         "url": "https://salesforce.com", "price": 300.0,
         "price_basis": "Pro @ ~4 seats", "lead_time_days": 5,
         "supports": ["contacts", "pipeline", "email", "automation", "reports"],
         "notes": "Most powerful; heavier setup and lock-in."},
    ],
    "default": [
        {"vendor": "Vercel", "product": "Pro",
         "url": "https://vercel.com", "price": 20.0,
         "price_basis": "Pro per-seat", "lead_time_days": 1,
         "supports": ["hosting", "ci", "edge"],
         "notes": "Frontend hosting / preview deploys."},
        {"vendor": "Notion", "product": "Business",
         "url": "https://notion.so", "price": 120.0,
         "price_basis": "Business @ ~8 seats", "lead_time_days": 1,
         "supports": ["docs", "wiki", "tasks"],
         "notes": "Docs & knowledge base."},
        {"vendor": "Linear", "product": "Standard",
         "url": "https://linear.app", "price": 64.0,
         "price_basis": "Standard @ ~8 seats", "lead_time_days": 1,
         "supports": ["issues", "projects", "roadmap"],
         "notes": "Issue tracking for engineering."},
    ],
}

# Keyword -> bucket. First match wins (scanned in this order).
_BUCKET_KEYWORDS = [
    ("transcription", ("transcription", "transcribe", "speech-to-text",
                       "speech to text", "stt", "voice", "diariz", "podcast",
                       "caption", "subtitle")),
    ("analytics", ("analytics", "product analytics", "events", "funnel",
                   "telemetry", "tracking", "mixpanel", "amplitude")),
    ("crm", ("crm", "customer relationship", "sales pipeline", "leads", "deals")),
]


def _cfg(key, default=None):
    try:
        return current_app.config.get(key, default)
    except RuntimeError:  # outside app context
        return default


def _spec_text(spec):
    """Flatten a spec into a lowercase search string."""
    parts = [str(spec.get(k, "")) for k in ("need", "title", "category")]
    parts += [str(x) for x in spec.get("must_haves", [])]
    return " ".join(parts).lower()


def match_bucket(spec):
    """Return the catalogue bucket name best matching the spec."""
    text = _spec_text(spec)
    for bucket, keywords in _BUCKET_KEYWORDS:
        if any(k in text for k in keywords):
            return bucket
    return "default"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _annotate(candidates, source):
    out = []
    for c in candidates:
        d = dict(c)
        d["_source"] = source
        out.append(d)
    return out


def _discover_seed(spec, limit):
    bucket = match_bucket(spec)
    return _annotate(SEED_CATALOG.get(bucket, SEED_CATALOG["default"])[:limit], "seed")


_LIVE_SYSTEM = (
    "You are a procurement researcher. Suggest {n} distinct, real vendors or "
    "products that genuinely fit the need — these may be SaaS/API/digital "
    "services OR physical goods/hardware, whichever actually matches the request "
    "and its category. Honour the stated category: do not force a digital "
    "subscription when the need is for goods, or vice versa. Output ONLY a JSON "
    "array (no prose, no fence) of {n} objects with keys: vendor (string), "
    "product (string), url (string), price (number, estimated USD — the "
    "recurring monthly cost for subscriptions, or the one-time/per-unit cost for "
    "goods), pricing_model (string, one of 'subscription_monthly', 'one_time', "
    "'per_unit', 'usage'), price_basis (string explaining the price and unit, "
    "e.g. '$1,200/unit x 15 units' or '$0.004/min @ ~50k min/mo'), "
    "lead_time_days (integer — shipping/delivery time for goods or onboarding "
    "time for services), supports (array of strings — capabilities for services, "
    "or key specs/features for goods). Prefer well-known, reputable vendors and "
    "cover a range of price points."
)

# Human-readable suffix per pricing model, so the scorecard's price_basis does
# not imply a recurring monthly charge for a one-off purchase.
_PRICING_LABELS = {
    "one_time": "one-time",
    "per_unit": "per-unit",
    "usage": "usage-based",
}


def _discover_live(spec, limit):
    """Ask the agent for candidates as JSON; return list or None on any failure."""
    if not hermes_client.is_live():
        return None
    try:
        payload = {k: spec.get(k) for k in
                   ("need", "category", "budget_ceiling_usd", "quantity", "must_haves")}
        result = hermes_client.chat(
            [{"role": "system", "content": _LIVE_SYSTEM.format(n=limit)},
             {"role": "user", "content": json.dumps(payload)}],
            use_tools=False, label="discover")
        if result.get("engine") != "hermes":
            return None
        content = result.get("content", "")
        start, end = content.find("["), content.rfind("]")
        if start == -1 or end <= start:
            return None
        arr = json.loads(content[start:end + 1])
        clean = []
        for c in arr[:limit]:
            if not isinstance(c, dict) or not c.get("vendor"):
                continue
            pricing_model = str(c.get("pricing_model", "")).strip().lower()
            basis = str(c.get("price_basis", "")).strip()
            label = _PRICING_LABELS.get(pricing_model)
            if label and label not in basis.lower():
                basis = f"{basis} · {label}".strip(" ·")
            clean.append({
                "vendor": str(c.get("vendor", "")).strip(),
                "product": str(c.get("product", "")).strip(),
                "url": str(c.get("url", "")).strip(),
                "price": float(c.get("price") or 0),
                "pricing_model": pricing_model,
                "price_basis": basis,
                "lead_time_days": int(c.get("lead_time_days") or 0),
                "supports": [str(x).strip() for x in (c.get("supports") or [])
                             if str(x).strip()],
                "notes": str(c.get("notes", "")).strip(),
            })
        return clean or None
    except Exception:  # never let discovery fail because of the model
        return None


def discover(spec, mode=None, limit=None):
    """Return a candidate list for ``spec``.

    mode: "seed" | "live" (default from config PROCUREMENT_DISCOVERY_MODE).
    Live mode falls back to seed when it yields nothing.
    """
    mode = (mode or _cfg("PROCUREMENT_DISCOVERY_MODE", "seed") or "seed").lower()
    limit = limit or int(_cfg("PROCUREMENT_DISCOVERY_LIMIT", 4) or 4)
    if mode == "live":
        live = _discover_live(spec, limit)
        if live:
            return _annotate(live, "live")
    return _discover_seed(spec, limit)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _notes_with_supports(candidate):
    bits = []
    if candidate.get("product"):
        bits.append(candidate["product"])
    if candidate.get("notes"):
        bits.append(candidate["notes"])
    if candidate.get("supports"):
        bits.append("Supports: " + ", ".join(candidate["supports"]))
    return " · ".join(bits)


def persist_candidates(req, candidates, default_source="seed"):
    """Create VendorOption rows for ``candidates`` on ``req``.

    Skips vendors already present (case-insensitive name match). Returns the
    list of newly created VendorOption rows. Caller commits.
    """
    existing = {(v.name or "").strip().lower() for v in req.vendors}
    created = []
    for c in candidates:
        name = (c.get("vendor") or "").strip()
        if not name or name.lower() in existing:
            continue
        vendor = VendorOption(
            request_id=req.id,
            name=name,
            price=float(c.get("price") or 0),
            price_basis=c.get("price_basis", ""),
            lead_time_days=int(c.get("lead_time_days") or 0),
            url=c.get("url", ""),
            source=c.get("_source", default_source),
            notes=_notes_with_supports(c),
        )
        # ENRICH inputs for W3 (defaults when discovery didn't supply them).
        vendor.enrichment = {
            "capabilities": [s.strip().lower() for s in (c.get("supports") or [])
                             if str(s).strip()],
            "reliability": int(c.get("reliability", 75) or 75),
            "flexibility": int(c.get("flexibility", 70) or 70),
        }
        db.session.add(vendor)
        created.append(vendor)
        existing.add(name.lower())
    return created


def _spec_from_request(req):
    """Build a usable spec from a request that has no stored requirement_spec
    (e.g. created via the manual form rather than NL intake)."""
    return {
        "need": " ".join(filter(None, [req.title, req.description])),
        "title": req.title,
        "category": req.category,
        "budget_ceiling_usd": req.budget_ceiling,
        "quantity": req.quantity_or_usage,
        "must_haves": [m.strip() for m in (req.must_haves or "").split(",") if m.strip()],
    }


def discover_for_request(req, mode=None):
    """Discover candidates for a request and persist them. Commits.

    Returns (created_rows, resolved_mode).
    """
    spec = req.requirement_spec or _spec_from_request(req)
    resolved = (mode or _cfg("PROCUREMENT_DISCOVERY_MODE", "seed") or "seed").lower()
    candidates = discover(spec, mode=mode)
    # candidates carry their true _source (live may have fallen back to seed)
    if candidates:
        resolved = candidates[0].get("_source", resolved)
    created = persist_candidates(req, candidates)
    db.session.commit()
    return created, resolved
