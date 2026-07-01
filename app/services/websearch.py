"""Real web search + URL liveness validation — stdlib only (urllib).

The procurement DISCOVER stage used to ask the model to *recall* vendors from
memory, which produced wrong vendors (a robot-arm request fell through to a
hardcoded SaaS catalogue) and hallucinated / dead links. This module gives the
flow a real grounding signal:

* ``web_search(query)`` — a genuine web search via the DuckDuckGo HTML endpoint
  (no API key, no dependency). Returns real result pages: title, url, snippet,
  domain. Vendor URLs therefore come from pages that actually exist, instead of
  being invented by the model.
* ``check_url(url)`` — a **3-state** liveness verdict (``alive`` / ``blocked`` /
  ``dead``). This matters: reputable vendor sites frequently bot-block a bare
  HEAD/GET with 403/405/429 (verified: densorobotics.com returns 403 but is very
  much alive), so a naive "non-2xx == dead" check would wrongly drop real
  vendors. We keep ``blocked`` hosts (the domain resolves and serves) and only
  drop ``dead`` ones (DNS failure, 404/410, 5xx, connect timeout).

Everything is defensive: any network failure degrades to an empty list / a
conservative verdict so discovery never raises because of the network.
"""
import concurrent.futures
import gzip
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# DuckDuckGo's HTML endpoint rejects the default urllib User-Agent; present a
# normal desktop browser UA so we get real results.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_DDG_URL = "https://html.duckduckgo.com/html/"
_BRAVE_URL = "https://search.brave.com/search"

# Anchors carrying a real result link in the DDG HTML layout.
_RESULT_A_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# Brave Search HTML layout: each organic web result is a block carrying
# ``data-type="web"``; its first https href is the destination, the title sits in
# a ``search-snippet-title`` element and the description in a ``generic-snippet``
# one. Class names carry a volatile ``svelte-<hash>`` suffix, so every pattern
# matches the stable semantic prefix with a wildcard and never the hash.
_BRAVE_BLOCK_RE = re.compile(r'data-type="web"')
_BRAVE_URL_RE = re.compile(r'href="(https?://[^"]+)"')
_BRAVE_TITLE_RE = re.compile(
    r'class="[^"]*search-snippet-title[^"]*"[^>]*>(.*?)</', re.S)
_BRAVE_DESC_RE = re.compile(
    r'class="[^"]*generic-snippet[^"]*"[^>]*>(.*?)</div>', re.S)

# Pure aggregators / marketplaces / encyclopedias — a link here is rarely the
# vendor's own site, so we drop them from the candidate pool (kept minimal to
# avoid false negatives).
_AGGREGATOR_DOMAINS = {
    "duckduckgo.com", "google.com", "bing.com",
    "wikipedia.org", "youtube.com", "reddit.com", "quora.com", "pinterest.com",
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "medium.com",
    "amazon.com", "ebay.com", "alibaba.com", "aliexpress.com", "indiamart.com",
}


def _strip_tags(html):
    return _TAG_RE.sub("", html or "").replace("&amp;", "&").strip()


def _decode_ddg(href):
    """DDG sometimes wraps results as //duckduckgo.com/l/?uddg=<encoded>. Return
    the real destination URL (or the href unchanged when it's already direct)."""
    if not href:
        return href
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("uddg"):
                return qs["uddg"][0]
    except ValueError:
        pass
    return href


def domain_of(url):
    """Registrable-ish domain (drops a leading www.)."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _http(url, method, timeout):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", _UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,*/*")
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    return urllib.request.urlopen(req, timeout=timeout)


# --------------------------------------------------------------------------- #
# Web search — multi-engine failover chain
#
# DuckDuckGo is the default first engine (no key, privacy-friendly), but it
# periodically bot-blocks a server IP at the TLS layer (the handshake hangs until
# timeout) or returns nothing. When the first engine yields no results we fail
# over to the next, so DISCOVER/AUTOPILOT keep working. Order is configurable via
# ``PROCUREMENT_SEARCH_ENGINES`` (csv, e.g. "brave,duckduckgo") — handy on a box
# where DuckDuckGo is blocked, to skip its stall and put Brave first.
# --------------------------------------------------------------------------- #
def _http_html(url, data=None, timeout=10):
    """GET (or POST when ``data`` is given) a URL and return decoded HTML.

    Returns ``""`` on any network/decode failure so an engine simply yields no
    results and the chain falls through to the next one."""
    try:
        req = urllib.request.Request(
            url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", _UA)
        req.add_header("Accept", "text/html,application/xhtml+xml,*/*")
        req.add_header("Accept-Language", "en-US,en;q=0.9")
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("web_search: fetch failed for %s: %s", domain_of(url) or url, exc)
        return ""


def _search_duckduckgo(query, timeout):
    """Raw ``{title, url, snippet}`` items from DuckDuckGo's HTML endpoint."""
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    html = _http_html(_DDG_URL, data=data, timeout=timeout)
    if not html:
        return []
    snippets = [_strip_tags(s) for s in _SNIPPET_RE.findall(html)]
    items = []
    for i, (href, title) in enumerate(_RESULT_A_RE.findall(html)):
        items.append({
            "title": _strip_tags(title),
            "url": _decode_ddg(href),
            "snippet": snippets[i] if i < len(snippets) else "",
        })
    return items


def _search_brave(query, timeout):
    """Raw ``{title, url, snippet}`` items scraped from Brave Search HTML.

    Failover engine: Brave serves parseable server-side result HTML to a normal
    browser UA (no key, no JS), so it works where DuckDuckGo is blocked."""
    url = _BRAVE_URL + "?" + urllib.parse.urlencode({"q": query, "source": "web"})
    html = _http_html(url, timeout=timeout)
    if not html:
        return []
    items = []
    # Split into per-result blocks; blocks[0] is the pre-results head, skip it.
    blocks = _BRAVE_BLOCK_RE.split(html)
    for block in blocks[1:]:
        m = _BRAVE_URL_RE.search(block)
        if not m:
            continue
        tm = _BRAVE_TITLE_RE.search(block)
        dm = _BRAVE_DESC_RE.search(block)
        items.append({
            "title": _strip_tags(tm.group(1)) if tm else "",
            "url": m.group(1),
            "snippet": _strip_tags(dm.group(1)) if dm else "",
        })
    return items


# Engine registry — name -> callable(query, timeout) -> raw item list.
_ENGINES = {
    "duckduckgo": _search_duckduckgo,
    "brave": _search_brave,
}
_DEFAULT_ENGINE_ORDER = ("duckduckgo", "brave")


def _engine_order():
    """Resolved engine order: ``PROCUREMENT_SEARCH_ENGINES`` csv, else default.
    Unknown names are dropped; an empty/garbage value falls back to the default."""
    raw = os.environ.get("PROCUREMENT_SEARCH_ENGINES", "")
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    order = [n for n in names if n in _ENGINES]
    return order or list(_DEFAULT_ENGINE_ORDER)


def _dedupe_results(items, max_results):
    """Apply the aggregator filter + per-domain dedup + cap shared by all
    engines. Returns the public ``{title, url, snippet, domain}`` shape."""
    out, seen = [], set()
    for it in items:
        url = it.get("url", "")
        dom = domain_of(url)
        if not url.startswith("http") or not dom or dom in seen:
            continue
        if any(dom == a or dom.endswith("." + a) for a in _AGGREGATOR_DOMAINS):
            continue
        seen.add(dom)
        out.append({
            "title": it.get("title", "") or dom,
            "url": url,
            "snippet": it.get("snippet", "") or "",
            "domain": dom,
        })
        if len(out) >= max_results:
            break
    return out


def web_search(query, max_results=8, timeout=10):
    """Real web search with engine failover. Returns a list of
    ``{title, url, snippet, domain}``.

    Tries each configured engine in order and returns the first that yields any
    results. Never raises — returns ``[]`` only when every engine comes up empty,
    so discovery can degrade honestly.
    """
    for name in _engine_order():
        engine = _ENGINES.get(name)
        if not engine:
            continue
        try:
            items = engine(query, timeout)
        except Exception as exc:  # noqa: BLE001 - one engine must never break the chain
            logger.warning("web_search: engine %s raised: %s", name, exc)
            items = []
        results = _dedupe_results(items, max_results)
        if results:
            logger.info("web_search: %d result(s) from %s for %r",
                        len(results), name, query[:60])
            return results
        logger.info("web_search: engine %s returned nothing for %r; trying next",
                    name, query[:60])
    return []


# --------------------------------------------------------------------------- #
# URL liveness — 3-state
# --------------------------------------------------------------------------- #
# 401/403/405/406/429: the host resolves and serves but bot-blocks us — the
# vendor is real, so we KEEP it (tagged 'blocked'), we just couldn't fully fetch.
_BLOCKED_CODES = {401, 403, 405, 406, 429}


def check_url(url, timeout=6):
    """Return ``(verdict, status_or_reason)`` where verdict is one of
    ``"alive"`` (2xx/3xx), ``"blocked"`` (resolves but bot-guarded), or
    ``"dead"`` (DNS failure, 404/410, 5xx, timeout, malformed)."""
    if not url or not url.startswith("http"):
        return "dead", "no url"
    for method in ("HEAD", "GET"):
        try:
            with _http(url, method, timeout) as resp:
                code = getattr(resp, "status", 200) or 200
                return ("alive" if code < 400 else
                        ("blocked" if code in _BLOCKED_CODES else "dead")), code
        except urllib.error.HTTPError as e:
            if e.code in _BLOCKED_CODES:
                return "blocked", e.code
            if method == "GET":               # 404/410/5xx after trying GET too
                return "dead", e.code
            # else: some servers reject HEAD — fall through and retry with GET
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            if method == "GET":
                return "dead", str(getattr(e, "reason", e))
    return "dead", "unreachable"


def validate_url(url, timeout=6):
    """Boolean convenience: True when the URL is ``alive`` or ``blocked`` (i.e.
    the vendor's site exists), False when ``dead``."""
    return check_url(url, timeout=timeout)[0] != "dead"


def validate_urls(urls, timeout=6, workers=6):
    """Concurrently check many URLs. Returns ``{url: (verdict, status)}``."""
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        return {}
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_url, u, timeout): u for u in urls}
        for fut in concurrent.futures.as_completed(futs):
            u = futs[fut]
            try:
                out[u] = fut.result()
            except Exception:  # never let one URL kill the batch
                out[u] = ("dead", "error")
    return out


# --------------------------------------------------------------------------- #
# Page content — read the actual vendor page so prices come from the site,
# not a model estimate.
# --------------------------------------------------------------------------- #
_DROP_BLOCKS_RE = re.compile(
    r"<(script|style|noscript|template|svg|head)[^>]*>.*?</\1>", re.S | re.I)
_PRICE_HINT_RE = re.compile(r"(price|pricing|cost|\$|usd|/mo|per month|quote)", re.I)


def fetch_page_text(url, timeout=6, max_bytes=800_000, max_chars=5000):
    """Fetch a page and return ``(verdict, text)``.

    ``verdict`` is the same 3-state liveness value as :func:`check_url`. ``text``
    is human-readable page text (scripts/styles/markup stripped, whitespace
    collapsed), trimmed to ``max_chars`` and centred on the first price signal so
    the most price-relevant content survives the trim. Empty text for
    blocked/dead pages. Never raises."""
    if not url or not url.startswith("http"):
        return "dead", ""
    try:
        with _http(url, "GET", timeout) as resp:
            code = getattr(resp, "status", 200) or 200
            if code >= 400:
                return ("blocked" if code in _BLOCKED_CODES else "dead"), ""
            raw = resp.read(max_bytes)
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            html = raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return ("blocked" if e.code in _BLOCKED_CODES else "dead"), ""
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "dead", ""

    body = _DROP_BLOCKS_RE.sub(" ", html)
    text = _strip_tags(body)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        m = _PRICE_HINT_RE.search(text)
        start = max(0, m.start() - 800) if m else 0
        text = text[start:start + max_chars]
    return "alive", text


def fetch_pages(urls, timeout=6, workers=6, max_chars=5000):
    """Concurrently fetch many pages. Returns ``{url: (verdict, text)}``."""
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        return {}
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_page_text, u, timeout, 800_000, max_chars): u
                for u in urls}
        for fut in concurrent.futures.as_completed(futs):
            u = futs[fut]
            try:
                out[u] = fut.result()
            except Exception:
                out[u] = ("dead", "")
    return out
