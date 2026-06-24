"""W1 INTAKE — turn a natural-language need into a requirement spec.

Stage 1 of the on-demand procurement flow (see ``_TARGET_TODO/PROCUREMENT-FLOW.md``):

    "I want to buy A"  ->  structured requirement spec
    {need, quantity, deadline, budget_ceiling_usd, must_haves, nice_to_haves,
     priority{price,time,risk,quality,terms}}

Design (mirrors the audit flow's deterministic-spine + LLM-narration pattern):

* ``parse_need`` ALWAYS computes a deterministic spec from the text. This is the
  reliable path — instant, offline, unit-testable, demoable.
* If ``use_hermes`` is on and the real agent is reachable, we additionally ask
  Hermes to parse the sentence and merge its (validated) fields over the
  deterministic ones. Any failure — Hermes down, slow, or non-JSON — silently
  falls back to the deterministic spec. We never route through the audit-specific
  local reasoner.

The deterministic route is the default so the web request never blocks on a
~30-40s model call (the Nemotron server is single-threaded).
"""
import json
import re
from datetime import date, datetime, timedelta

from . import hermes_client

# Priority weights are 0-5 ints (matches ProcurementRequest.priority_*).
DEFAULT_PRIORITY = {"price": 3, "time": 3, "risk": 3, "quality": 3, "terms": 3}
SPEC_KEYS = ("need", "title", "category", "quantity", "deadline", "deadline_raw",
             "budget_ceiling_usd", "must_haves", "nice_to_haves", "priority")

# Phrases that introduce the need; stripped to get a clean title.
_LEAD_INS = re.compile(
    r"^\s*(?:i\s+want\s+to\s+(?:buy|get|procure|purchase)|i\s+need|i'?d\s+like|"
    r"we\s+need|we\s+want|please\s+(?:buy|get|find)|buy|get|find|procure|"
    r"purchase|source)\s+", re.IGNORECASE)

# Quantity / usage phrase, e.g. "~50,000 min/month", "15 laptops", "50 seats".
_QTY_RE = re.compile(
    r"~?\s*[\d,]+(?:\.\d+)?\s*(?:k|K)?\s*"
    r"(?:min(?:ute)?s?|hours?|hrs?|seats?|users?|licen[cs]es?|requests?|"
    r"calls?|messages?|GB|TB|units?|laptops?|devices?)"
    r"(?:\s*(?:/|per)\s*(?:mo(?:nth)?|day|year|yr|week))?",
    re.IGNORECASE)

# Bare unit words that should never count as a must-have on their own.
_UNIT_STOPWORDS = {"min", "mins", "minute", "minutes", "month", "mo", "year",
                   "yr", "day", "days", "week", "hr", "hrs", "hour", "hours"}


# --------------------------------------------------------------------------- #
# Small extraction helpers
# --------------------------------------------------------------------------- #
def _clean_items(blob):
    """Split a 'a, b and c + d' blob into a clean, deduped token list."""
    if not blob:
        return []
    parts = re.split(r",|\band\b|\+|/|;", blob)
    out = []
    for p in parts:
        t = p.strip(" .;-").strip()
        # drop trailing filler clauses
        t = re.sub(r"\b(by|within|live|budget|under|nice to have).*$", "", t,
                   flags=re.IGNORECASE).strip()
        if not t or len(t) > 60:
            continue
        if t.lower() in _UNIT_STOPWORDS:
            continue
        if t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def _extract_budget(text):
    """Return a USD ceiling (float) or 0.0. Prefers amounts near budget words."""
    near = re.search(
        r"(?:under|below|max(?:imum)?|up\s*to|budget(?:\s*of)?|less\s*than|<|cap(?:\s*of)?)"
        r"\s*\$?\s*([\d,]+(?:\.\d+)?)\s*(k)?",
        text, re.IGNORECASE)
    m = near or re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(k)?", text, re.IGNORECASE)
    if not m:
        return 0.0
    amount = float(m.group(1).replace(",", ""))
    if m.group(2):  # "300k"
        amount *= 1000
    return amount


def _extract_quantity(text):
    m = _QTY_RE.search(text)
    return m.group(0).strip() if m else ""


def _extract_deadline(text, today=None):
    """Return (date|None, raw_phrase). Handles 'in N days/weeks/months' and ASAP."""
    today = today or date.today()
    m = re.search(r"(?:live|ready|delivered|done|by|within|in)\s+"
                  r"(\d+)\s+(day|week|month)s?", text, re.IGNORECASE)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"day": 1, "week": 7, "month": 30}[unit] * n
        return today + timedelta(days=days), m.group(0)
    if re.search(r"\b(asap|urgent(?:ly)?|immediately|right\s*away)\b", text, re.IGNORECASE):
        return today + timedelta(days=7), "ASAP"
    return None, ""


def _infer_category(text):
    t = text.lower()
    if re.search(r"\bapi\b", t):
        return "API"
    if any(k in t for k in ("saas", "subscription", "software", "tool", "platform", "app")):
        return "SaaS"
    if any(k in t for k in ("laptop", "hardware", "device", "server")):
        return "Hardware"
    return ""


def _derive_priority(text):
    """Bump default weights from intent keywords. Clamped to 0-5."""
    p = dict(DEFAULT_PRIORITY)
    t = text.lower()
    if re.search(r"\b(urgent|asap|immediately|right\s*away|tight\s*deadline|fast)\b", t):
        p["time"] += 2
    if re.search(r"\b(cheap|cheapest|budget|affordable|low\s*cost|save|inexpensive)\b", t):
        p["price"] += 2
    if re.search(r"\b(reliable|mission[- ]critical|critical|secure|security|"
                 r"compliance|compliant|sla|uptime|stable)\b", t):
        p["risk"] += 1
        p["quality"] += 1
    if re.search(r"\b(best|high[- ]quality|accuracy|accurate|quality|premium)\b", t):
        p["quality"] += 2
    if re.search(r"\b(flexible|no\s*lock[- ]?in|month[- ]to[- ]month|cancel\s*any|"
                 r"short\s*term)\b", t):
        p["terms"] += 2
    return {k: max(0, min(5, v)) for k, v in p.items()}


def _title_from(text):
    head = re.split(r"[.;\n]", text.strip())[0]
    head = _LEAD_INS.sub("", head).strip()
    # cut at the first constraint clause for a tidy title
    head = re.split(r"\b(under|with|for|by|within|live in|budget|that)\b", head,
                    maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,.-")
    head = (head or text.strip()[:60]).strip()[:120]
    # Capitalize only the first character so acronyms like "API" survive.
    return (head[:1].upper() + head[1:]) if head else head


# --------------------------------------------------------------------------- #
# Deterministic parser (the spine)
# --------------------------------------------------------------------------- #
def parse_need_deterministic(text, today=None):
    """Parse ``text`` into a requirement spec dict using rules only."""
    text = (text or "").strip()
    deadline, deadline_raw = _extract_deadline(text, today=today)

    # Strip the quantity phrase before capability extraction so usage figures
    # (e.g. "~50,000 min/month") never leak into must-haves.
    caps_text = _QTY_RE.sub(" ", text, count=1)

    must_m = re.search(
        r"(?:must[- ]?haves?|must\s+have|requires?|required|needs?\s+to\s+have|"
        r"with|supporting|that\s+supports?)\s*[:\-]?\s*"
        r"(.+?)(?:\.|;|nice[- ]?to[- ]?have|optional|prefer|budget|by\s|live\s|"
        r"within|deadline|$)",
        caps_text, re.IGNORECASE)
    nice_m = re.search(
        r"(?:nice[- ]?to[- ]?haves?|nice\s+to\s+have|optional(?:ly)?|prefer(?:ably)?|"
        r"would\s+like)\s*[:\-]?\s*(.+?)(?:\.|;|budget|by\s|live\s|within|$)",
        caps_text, re.IGNORECASE)

    return {
        "need": text,
        "title": _title_from(text),
        "category": _infer_category(text),
        "quantity": _extract_quantity(text),
        "deadline": deadline.isoformat() if deadline else None,
        "deadline_raw": deadline_raw,
        "budget_ceiling_usd": _extract_budget(text),
        "must_haves": _clean_items(must_m.group(1) if must_m else ""),
        "nice_to_haves": _clean_items(nice_m.group(1) if nice_m else ""),
        "priority": _derive_priority(text),
    }


# --------------------------------------------------------------------------- #
# Optional Hermes enhancement
# --------------------------------------------------------------------------- #
_HERMES_SYSTEM = (
    "You convert a procurement request into a STRICT JSON requirement spec. "
    "Output ONLY a single JSON object, no prose, no code fence. Keys: "
    "need (string), title (short string), category (string), quantity (string), "
    "deadline_raw (string), budget_ceiling_usd (number), must_haves (array of "
    "strings), nice_to_haves (array of strings), priority (object with integer "
    "0-5 keys price,time,risk,quality,terms). A higher priority weight means it "
    "matters more for this purchase."
)


def _extract_json(content):
    """Pull the first JSON object out of a model reply."""
    if not content:
        return None
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(content[start:end + 1])
    except (ValueError, TypeError):
        return None


def _merge_hermes(base, hermes, today=None):
    """Overlay validated Hermes fields onto the deterministic spec."""
    spec = dict(base)
    if not isinstance(hermes, dict):
        return spec
    for key in ("need", "title", "category", "quantity", "deadline_raw"):
        val = hermes.get(key)
        if isinstance(val, str) and val.strip():
            spec[key] = val.strip()
    if isinstance(hermes.get("budget_ceiling_usd"), (int, float)) and hermes["budget_ceiling_usd"] > 0:
        spec["budget_ceiling_usd"] = float(hermes["budget_ceiling_usd"])
    for key in ("must_haves", "nice_to_haves"):
        val = hermes.get(key)
        if isinstance(val, list):
            items = [str(x).strip() for x in val if str(x).strip()]
            if items:
                spec[key] = items
    pri = hermes.get("priority")
    if isinstance(pri, dict):
        merged = dict(spec["priority"])
        for k in merged:
            if isinstance(pri.get(k), (int, float)):
                merged[k] = max(0, min(5, int(pri[k])))
        spec["priority"] = merged
    # Recompute the concrete deadline date from whichever raw phrase we ended up with.
    dl, _ = _extract_deadline(spec.get("deadline_raw") or "", today=today)
    if dl:
        spec["deadline"] = dl.isoformat()
    return spec


def parse_need(text, use_hermes=False, today=None):
    """Parse a need into a requirement spec.

    Always returns a valid spec. When ``use_hermes`` is set and the agent is
    reachable, Hermes' parse is merged in; otherwise the deterministic result is
    returned unchanged. The chosen engine is recorded under ``spec['_engine']``.
    """
    spec = parse_need_deterministic(text, today=today)
    spec["_engine"] = "deterministic"
    if not use_hermes or not hermes_client.is_live():
        return spec
    try:
        result = hermes_client.chat(
            [{"role": "system", "content": _HERMES_SYSTEM},
             {"role": "user", "content": text}],
            use_tools=False, label="intake")
        # Ignore the audit-specific local fallback; only trust a real model reply.
        if result.get("engine") == "hermes":
            parsed = _extract_json(result.get("content", ""))
            if parsed:
                spec = _merge_hermes(spec, parsed, today=today)
                spec["_engine"] = "hermes"
    except Exception:  # never let intake fail because of the model
        pass
    return spec


# --------------------------------------------------------------------------- #
# Apply a spec onto a ProcurementRequest
# --------------------------------------------------------------------------- #
def apply_spec_to_request(req, spec, raw_text=""):
    """Populate a ProcurementRequest from a requirement spec (no commit)."""
    req.intake_raw = raw_text or spec.get("need", "")
    req.title = spec.get("title") or (raw_text or "Untitled request")[:120]
    if not req.description:
        req.description = raw_text or spec.get("need", "")
    req.category = spec.get("category", "") or req.category
    req.quantity_or_usage = spec.get("quantity", "") or req.quantity_or_usage
    if spec.get("deadline"):
        try:
            req.deadline = datetime.strptime(spec["deadline"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
    if spec.get("budget_ceiling_usd"):
        req.budget_ceiling = float(spec["budget_ceiling_usd"])
    if spec.get("must_haves"):
        req.must_haves = ", ".join(spec["must_haves"])
    if spec.get("nice_to_haves"):
        req.nice_to_haves = ", ".join(spec["nice_to_haves"])
    pri = spec.get("priority") or {}
    req.priority_price = int(pri.get("price", req.priority_price or 3))
    req.priority_time = int(pri.get("time", req.priority_time or 3))
    req.priority_risk = int(pri.get("risk", req.priority_risk or 3))
    req.priority_quality = int(pri.get("quality", req.priority_quality or 3))
    req.priority_terms = int(pri.get("terms", req.priority_terms or 3))
    # Persist the spec itself (strip the transient engine marker).
    clean = {k: v for k, v in spec.items() if k != "_engine"}
    req.requirement_spec = clean
    # Intake done -> ready for DISCOVER.
    if req.status in ("", "draft", None):
        req.status = "analyzing"
    return req
