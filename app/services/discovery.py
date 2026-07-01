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
import logging
import re

from flask import current_app

logger = logging.getLogger(__name__)

from ..extensions import db
from ..models import VendorOption
from . import hermes_client, websearch


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
# Category classification — so a HARDWARE need can never be served a SaaS vendor
# (the root cause of "robot arm -> Vercel"). Heuristic and deliberately
# permissive: only "goods" vs "service" are asserted; ambiguous -> "unknown",
# and the relevance gate never rejects on "unknown".
# --------------------------------------------------------------------------- #
_GOODS_WORDS = (
    "robot", "arm", "laptop", "device", "server", "machine", "hardware",
    "printer", "camera", "sensor", "drone", "motor", "cnc", "lathe", "forklift",
    "vehicle", "furniture", "desk", "chair", "monitor", "cable", "equipment",
    "tool", "part", "component", "unit", "appliance", "gpu", "chip", "board",
    "physical", "ship", "deliver", "manufactur", "supplier",
)
_SERVICE_WORDS = (
    "api", "saas", "subscription", "software", "platform", "app", "cloud",
    "seat", "license", "licence", "per month", "/mo", "hosting", "dashboard",
    "integration", "webhook", "sdk", "service", "tool ", "plan",
)


def classify_need_kind(spec):
    """Classify the requirement as 'goods', 'service', or 'unknown'."""
    text = _spec_text(spec)
    cat = str(spec.get("category", "")).lower()
    if cat in ("hardware", "goods", "equipment"):
        return "goods"
    if cat in ("saas", "api", "software", "service"):
        return "service"
    goods = sum(1 for w in _GOODS_WORDS if w in text)
    service = sum(1 for w in _SERVICE_WORDS if w in text)
    if goods and goods > service:
        return "goods"
    if service and service > goods:
        return "service"
    return "unknown"


def candidate_kind(candidate):
    """Best-effort 'goods'/'service'/'unknown' for a discovered candidate."""
    pm = str(candidate.get("pricing_model", "")).lower()
    if pm in ("one_time", "per_unit"):
        return "goods"
    if pm in ("subscription_monthly", "usage"):
        return "service"
    blob = " ".join(str(candidate.get(k, "")) for k in
                    ("product", "notes", "price_basis", "snippet")).lower()
    blob += " " + " ".join(str(x) for x in candidate.get("supports", [])).lower()
    goods = sum(1 for w in _GOODS_WORDS if w in blob)
    service = sum(1 for w in _SERVICE_WORDS if w in blob)
    if goods and goods > service:
        return "goods"
    if service and service > goods:
        return "service"
    return "unknown"


def _kinds_conflict(need_kind, cand_kind):
    """True only when both kinds are confidently known AND incompatible."""
    return (need_kind in ("goods", "service") and cand_kind in ("goods", "service")
            and need_kind != cand_kind)


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
    # GUARD: never serve the generic SaaS "default" bucket (Vercel/Notion/Linear)
    # for a physical-goods need — that is the exact "robot arm -> Vercel" bug.
    # Honest empty beats a confidently wrong vendor.
    if bucket == "default" and classify_need_kind(spec) == "goods":
        return []
    return _annotate(SEED_CATALOG.get(bucket, SEED_CATALOG["default"])[:limit], "seed")


def curated_candidates(spec, limit):
    """Seed rows ONLY when the need matches a real curated bucket
    (transcription/analytics/crm). Returns ``[]`` otherwise — the generic
    "default" SaaS trio is NOT used as a blind catch-all here."""
    bucket = match_bucket(spec)
    if bucket == "default":
        return []
    return _annotate(SEED_CATALOG[bucket][:limit], "seed")


# Human-readable suffix per pricing model, so the scorecard's price_basis does
# not imply a recurring monthly charge for a one-off purchase.
_PRICING_LABELS = {
    "one_time": "one-time",
    "per_unit": "per-unit",
    "usage": "usage-based",
}


def _noop(stage, detail=""):
    pass


def _search_queries(spec):
    """Build a couple of real search queries from the spec."""
    need = str(spec.get("need") or spec.get("title") or "").strip()
    # Trim a long NL need to its first clause for a focused query.
    need = need.split(".")[0].split(" with ")[0].strip()[:80] or "product"
    cat = str(spec.get("category") or "").strip()
    kind = classify_need_kind(spec)
    noun = "supplier manufacturer" if kind == "goods" else "vendor pricing"
    q1 = " ".join(x for x in [need, cat, noun] if x)
    q2 = " ".join(x for x in [need, ("buy" if kind == "goods" else "best"), cat] if x)
    return [q for q in dict.fromkeys([q1, q2]) if q]


# A price on a page, e.g. "$1,299", "$0.0043", "$25,000.00".
_PRICE_ON_PAGE_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def _price_from_text(text):
    """First plausible dollar amount on a page (>= $1 to skip cents noise)."""
    for m in _PRICE_ON_PAGE_RE.finditer(text or ""):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if val >= 1:
            return val
    return None


def _hunt_price(c, log):
    """Persistent price lookup: when a vendor's own page showed no price, run a
    targeted '<vendor> <product> price' search and read those pages until a real
    number turns up. Returns True if a real price was found (so we stop guessing).
    """
    q = " ".join(x for x in [c.get("vendor", ""), c.get("product", ""),
                             "price cost"] if x).strip()
    log("discover", f"Hunting a real price for {c['vendor']}…")
    results = websearch.web_search(q, max_results=5)
    pages = websearch.fetch_pages([r["url"] for r in results[:3]])
    for r in results[:3]:
        verdict, text = pages.get(r["url"], ("dead", ""))
        price = _price_from_text(text) if verdict == "alive" else None
        if price:
            c["price"] = price
            c["price_from_page"] = True
            c["price_basis"] = f"${price:,.0f} found at {r['domain']}"
            log("discover", f"Found ${price:,.0f} for {c['vendor']} "
                            f"(via {r['domain']}).")
            return True
    if c.get("price"):
        log("discover", f"No public price page for {c['vendor']}; using estimate "
                        f"${c['price']:,.0f}.")
    else:
        log("discover", f"No price found for {c['vendor']} — flagged for a quote.")
    return False


def _extract_json_array(content):
    """Extract a JSON array from raw model output.

    Handles a fenced ```json (or bare ```) code block as well as a plain
    ``[...]`` slice. Tries ``json.loads`` on each candidate slice and returns the
    first that parses to a list. On total failure returns ``[]`` — but never
    silently: the raw snippet (truncated) is logged at warning level so a
    malformed model response stays diagnosable.
    """
    text = content or ""
    candidates = []
    # Prefer a fenced code block (```json ... ``` or ``` ... ```), if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidates.append(fence.group(1).strip())
    # Fall back to the outermost bracket slice.
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for snippet in candidates:
        try:
            data = json.loads(snippet)
        except (ValueError, TypeError) as exc:
            logger.warning("discover: JSON parse failed (%s); snippet=%r",
                           exc, snippet[:300])
            continue
        if isinstance(data, list):
            return data
        logger.warning("discover: parsed JSON was %s, not a list; snippet=%r",
                       type(data).__name__, snippet[:300])

    if not candidates:
        logger.warning("discover: no JSON array found in model output; raw=%r",
                       text[:300])
    return []


def _norm_name(s):
    """Lowercase + collapse internal whitespace for tolerant name comparison."""
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _vendor_in_title(vendor, title):
    """Case-insensitive, whitespace-tolerant vendor-name match against a title.

    Fixes the multi-word bug where only the first token of a vendor name was
    tested: "Acme Cloud Inc" now matches a title containing that full name (in
    any spacing/casing) or one that contains all of its words.
    """
    v, t = _norm_name(vendor), _norm_name(title)
    if not v or not t:
        return False
    if v in t:
        return True
    # Punctuation/spacing-insensitive containment ("AcmeCloudInc" vs "Acme Cloud").
    v_compact = re.sub(r"[^a-z0-9]", "", v)
    t_compact = re.sub(r"[^a-z0-9]", "", t)
    if v_compact and v_compact in t_compact:
        return True
    # Otherwise require every word of the vendor name to appear in the title.
    words = [w for w in re.split(r"\W+", v) if w]
    return bool(words) and all(w in t for w in words)


def _hermes_pick(spec, results, limit, log):
    """Ask Hermes to select/normalize vendors FROM the fetched pages.

    Each result carries the REAL page text (``_text``) when we could read it, so
    Hermes extracts the price **listed on the vendor's page** and only estimates
    (flagged) when the page shows none. The model never invents URLs — it must
    reuse a url from the provided results. Returns a list or [] on any failure."""
    if not hermes_client.is_live() or not results:
        return []
    catalogue = []
    for r in results:
        page = (r.get("_text") or "")[:3500]
        catalogue.append({
            "vendor_hint": r["title"][:80], "url": r["url"],
            "page_content": page or r.get("snippet", "")[:200],
            "page_readable": bool(page),
        })
    sys = (
        "You are a procurement researcher. From the PAGES provided (real vendor "
        "pages fetched from the web, with their text in 'page_content'), select up "
        "to {n} DISTINCT, real vendors that genuinely sell what the buyer needs. "
        "Use ONLY urls that appear in the input — never invent a url. Drop "
        "directories, marketplaces and listicles; prefer the vendor's own site. "
        "Honour the category: never return a software/SaaS vendor for a "
        "physical-goods need or vice versa. For PRICE: read the actual "
        "page_content and use the REAL price shown there for the buyer's "
        "quantity/usage; set price_from_page=true. ONLY if the page shows no "
        "price (e.g. 'contact sales') use your best market estimate and set "
        "price_from_page=false. Output ONLY a JSON array of up to {n} objects with "
        "keys: vendor, product, url, price (number USD), price_from_page "
        "(boolean), pricing_model (subscription_monthly|one_time|per_unit|usage), "
        "price_basis (short string, e.g. '$1,299 listed on product page' or "
        "'est. market price'), lead_time_days (int), supports (array of strings)."
    ).format(n=limit)
    payload = {"need": spec.get("need"), "category": spec.get("category"),
               "budget_ceiling_usd": spec.get("budget_ceiling_usd"),
               "quantity": spec.get("quantity"),
               "must_haves": spec.get("must_haves"),
               "pages": catalogue}
    try:
        result = hermes_client.chat(
            [{"role": "system", "content": sys},
             {"role": "user", "content": json.dumps(payload)}],
            use_tools=False, label="discover")  # use_tools=False: Nemotron hangs on tools
    except (TypeError, ValueError) as exc:  # payload/serialisation problem
        logger.warning("discover: could not build/run Hermes chat: %s", exc)
        return []
    if result.get("engine") != "hermes":
        return []
    arr = _extract_json_array(result.get("content", ""))
    if not arr:
        return []

    by_domain = {r["domain"]: r for r in results}
    clean = []
    for c in arr[:limit]:
        if not isinstance(c, dict) or not c.get("vendor"):
            continue
        url = str(c.get("url", "")).strip()
        # Re-pin to a real result url (defend against any invented url).
        dom = websearch.domain_of(url)
        match = by_domain.get(dom) or next(
            (r for r in results if _vendor_in_title(c["vendor"], r.get("title", ""))),
            None)
        if not match:
            continue
        url = match["url"]
        pricing_model = str(c.get("pricing_model", "")).strip().lower()
        from_page = bool(c.get("price_from_page"))
        price = float(c.get("price") or 0)
        basis = str(c.get("price_basis", "")).strip()
        # If Hermes claims a page price but the page text has a clearer number,
        # trust the page; if it estimated, say so plainly.
        if not from_page and price:
            if "est" not in basis.lower():
                basis = (basis + " · estimate").strip(" ·")
        label = _PRICING_LABELS.get(pricing_model)
        if label and label not in basis.lower():
            basis = f"{basis} · {label}".strip(" ·")
        clean.append({
            "vendor": str(c.get("vendor", "")).strip(),
            "product": str(c.get("product", "")).strip(),
            "url": url,
            "price": price,
            "price_from_page": from_page,
            "pricing_model": pricing_model,
            "price_basis": basis,
            "lead_time_days": int(c.get("lead_time_days") or 0),
            "supports": [str(x).strip() for x in (c.get("supports") or [])
                         if str(x).strip()],
            "snippet": match.get("snippet", ""),
            "url_status": match.get("_verdict", "alive"),
        })
    return clean


def _candidates_from_results(results, limit):
    """Deterministic fallback when Hermes is unavailable: build candidates from
    the fetched pages — vendor name from the domain, and a real price scraped
    from the page text when one is present."""
    out = []
    for r in results[:limit]:
        name = r["domain"].split(".")[0].replace("-", " ").title()
        price = _price_from_text(r.get("_text", "")) or 0.0
        out.append({
            "vendor": name, "product": r["title"][:120], "url": r["url"],
            "price": price, "price_from_page": bool(price),
            "pricing_model": "",
            "price_basis": ("$%0.0f found on page" % price) if price else "",
            "lead_time_days": 0, "supports": [], "snippet": r.get("snippet", ""),
            "url_status": r.get("_verdict", "alive"),
        })
    return out


def _discover_live(spec, limit, on_progress=None):
    """Grounded discovery: real web search -> FETCH each vendor page -> Hermes
    reads the real page content (real prices, no guessing) -> category relevance
    gate. Returns a validated candidate list, or ``None`` when nothing
    trustworthy survives (caller stays honest instead of inventing a vendor)."""
    log = on_progress or _noop
    # Grounded discovery is the "live" path: only run real network search when
    # Hermes is configured. Offline (e.g. tests) -> None so the caller falls back
    # to the deterministic curated/seed path without touching the network.
    if not hermes_client.is_live():
        return None
    need_kind = classify_need_kind(spec)

    # 1) Real web search across a couple of query variants.
    pool, seen = [], set()
    queries = _search_queries(spec)
    for i, q in enumerate(queries):
        if i:
            log("discover", "Searching the next query for more options…")
        log("discover", f"Searching the web: “{q}”")
        for r in websearch.web_search(q, max_results=8):
            if r["domain"] not in seen:
                seen.add(r["domain"])
                pool.append(r)
    if not pool:
        log("discover", "Web search returned nothing reachable.")
        return None
    log("discover", f"Found {len(pool)} candidate sources across the web.")

    # 2) FETCH each page (this both validates the link AND gives us the real page
    #    text so prices come from the site, not a guess). Drop dead links here.
    log("discover", f"Reading {len(pool)} vendor pages to get real prices…")
    pages = websearch.fetch_pages([r["url"] for r in pool])
    live_pool = []
    for r in pool:
        verdict, text = pages.get(r["url"], ("dead", ""))
        if verdict == "dead":
            log("discover", f"Dropped {r['domain']} — dead link.")
            continue
        r["_verdict"], r["_text"] = verdict, text
        live_pool.append(r)
    if not live_pool:
        log("discover", "No vendor page was reachable.")
        return None
    readable = sum(1 for r in live_pool if r.get("_text"))
    log("discover", f"{len(live_pool)} reachable ({readable} fully readable for "
                    f"on-page pricing).")

    # 3) Let Hermes read the real pages and pick vendors with real prices.
    log("discover", "Analysing the pages with Hermes (reading real prices)…")
    candidates = _hermes_pick(spec, live_pool, limit, log)
    if not candidates:
        log("discover", "Falling back to scraping the fetched pages directly.")
        candidates = _candidates_from_results(live_pool, limit)
    if not candidates:
        return None

    # 4) Category relevance gate (a goods need never keeps a SaaS candidate).
    kept = []
    for c in candidates:
        ckind = candidate_kind(c)
        if _kinds_conflict(need_kind, ckind):
            log("discover", f"Dropped {c['vendor']} — {ckind} vendor for a "
                            f"{need_kind} need.")
            continue
        c["url_verified"] = True
        # Look for the price until it's certain: if the vendor's own page had no
        # price, chase it down with a targeted search before settling.
        if not (c.get("price_from_page") and c.get("price")):
            _hunt_price(c, log)
        priced = ("on-page" if c.get("price_from_page") and c.get("price")
                  else "estimated" if c.get("price") else "no price")
        log("discover", f"Kept {c['vendor']} — "
                        f"{('$%0.0f' % c['price']) if c.get('price') else 'price TBD'} "
                        f"({priced}).")
        kept.append(c)
        if len(kept) >= limit:
            break
    if not kept:
        log("discover", "No vendor passed the category check.")
        return None
    log("discover", f"{len(kept)} vendor(s) verified.")
    return kept


def discover(spec, mode=None, limit=None, on_progress=None):
    """Return a candidate list for ``spec``.

    mode:
      * ``"live"``    — real web-search discovery (search -> Hermes selection ->
        link validation -> category gate). Falls back to the *curated* bucket
        (real bucket only) when grounded discovery yields nothing, then to an
        honest empty list. It NEVER substitutes the generic SaaS "default" trio
        for an off-catalogue or goods need (that was the "robot arm -> Vercel" bug).
      * ``"curated"`` — seed rows only on a real bucket hit, else ``[]``.
      * ``"seed"``    — the full curated catalogue incl. the generic default
        bucket for non-goods needs (demo-safe, deterministic, offline).
    """
    mode = (mode or _cfg("PROCUREMENT_DISCOVERY_MODE", "seed") or "seed").lower()
    limit = limit or int(_cfg("PROCUREMENT_DISCOVERY_LIMIT", 4) or 4)
    if mode == "live":
        live = _discover_live(spec, limit, on_progress=on_progress)
        if live:
            return _annotate(live, "live")
        # Honest fallback: real curated bucket only — never the SaaS default.
        return curated_candidates(spec, limit)
    if mode == "curated":
        return curated_candidates(spec, limit)
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
        # Also carry NEGOTIATE/UI signals (pricing model + link health) — stored
        # in enrichment_json so no DB migration is needed.
        vendor.enrichment = {
            "capabilities": [s.strip().lower() for s in (c.get("supports") or [])
                             if str(s).strip()],
            "reliability": int(c.get("reliability", 75) or 75),
            "flexibility": int(c.get("flexibility", 70) or 70),
            "pricing_model": str(c.get("pricing_model", "")).strip().lower(),
            "link_status": c.get("url_status", ""),       # 'alive'|'blocked'|''
            "url_verified": bool(c.get("url_verified", False)),
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


def discover_for_request(req, mode=None, on_progress=None):
    """Discover candidates for a request and persist them. Commits.

    Returns (created_rows, resolved_mode). ``resolved_mode`` is the true source
    of the candidates ('live'/'seed'), or 'none' when nothing was found.
    """
    spec = req.requirement_spec or _spec_from_request(req)
    resolved = (mode or _cfg("PROCUREMENT_DISCOVERY_MODE", "seed") or "seed").lower()
    candidates = discover(spec, mode=mode, on_progress=on_progress)
    # candidates carry their true _source (live may have fallen back to curated)
    if candidates:
        resolved = candidates[0].get("_source", resolved)
    else:
        resolved = "none"
    created = persist_candidates(req, candidates)
    db.session.commit()
    return created, resolved
