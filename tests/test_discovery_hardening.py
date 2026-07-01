"""Hardening regression tests for W2 DISCOVER (app/services/discovery.py).

Covers three fixes:
  (a) malformed model JSON -> safe empty list AND a diagnostic log (never silent),
  (b) well-formed JSON (incl. a ```json fenced block) parses correctly,
  (c) multi-word vendor names match case-insensitively / whitespace-tolerantly.

All offline: Hermes is monkeypatched, no real network is touched.
"""
import logging

import pytest

from app.services import discovery


SPEC = {
    "need": "cloud hosting platform",
    "category": "SaaS",
    "budget_ceiling_usd": 500,
    "quantity": 1,
    "must_haves": ["uptime"],
}


def _result(domain="acme.example.com", url="https://acme.example.com/pricing",
            title="Acme Cloud Inc - Cloud Platform"):
    return {"domain": domain, "url": url, "title": title,
            "snippet": "Acme cloud hosting.", "_verdict": "alive",
            "_text": "Acme Cloud Inc plans from $49/mo."}


@pytest.fixture
def live_hermes(monkeypatch):
    """Force the live path on; tests supply chat() output per-case."""
    monkeypatch.setattr(discovery.hermes_client, "is_live", lambda: True)

    def _set(content):
        monkeypatch.setattr(
            discovery.hermes_client, "chat",
            lambda *a, **k: {"engine": "hermes", "content": content})
    return _set


# --------------------------------------------------------------------------- #
# (a) malformed model JSON -> safe empty list, but logged (not silent)
# --------------------------------------------------------------------------- #
def test_malformed_json_returns_empty_and_logs(live_hermes, caplog):
    live_hermes('Here you go: [{"vendor": "Acme", bogus json}]')
    with caplog.at_level(logging.WARNING, logger="app.services.discovery"):
        out = discovery._hermes_pick(SPEC, [_result()], 4, discovery._noop)
    assert out == []
    assert any("discover" in r.message.lower() for r in caplog.records)
    assert caplog.records, "a malformed response must produce a log record"


def test_no_array_in_output_returns_empty_and_logs(live_hermes, caplog):
    live_hermes("I could not find any vendors, sorry.")
    with caplog.at_level(logging.WARNING, logger="app.services.discovery"):
        out = discovery._hermes_pick(SPEC, [_result()], 4, discovery._noop)
    assert out == []
    assert any("no json array" in r.message.lower() for r in caplog.records)


def test_extract_json_array_never_raises_on_garbage(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.discovery"):
        assert discovery._extract_json_array("not json [oops") == []
        assert discovery._extract_json_array("") == []
        assert discovery._extract_json_array(None) == []


# --------------------------------------------------------------------------- #
# (b) well-formed JSON parses correctly, including a ```json fenced block
# --------------------------------------------------------------------------- #
def test_plain_json_array_parses():
    data = discovery._extract_json_array('[{"vendor": "Acme"}, {"vendor": "Beta"}]')
    assert [d["vendor"] for d in data] == ["Acme", "Beta"]


def test_fenced_json_block_parses():
    content = (
        "Sure, here are the vendors:\n"
        "```json\n"
        '[{"vendor": "Acme Cloud Inc"}, {"vendor": "Beta"}]\n'
        "```\n"
        "Hope that helps!"
    )
    data = discovery._extract_json_array(content)
    assert [d["vendor"] for d in data] == ["Acme Cloud Inc", "Beta"]


def test_hermes_pick_parses_fenced_block(live_hermes):
    content = (
        "```json\n"
        '[{"vendor": "Acme Cloud Inc", "product": "Hosting", '
        '"url": "https://acme.example.com/pricing", "price": 49, '
        '"price_from_page": true, "pricing_model": "subscription_monthly", '
        '"price_basis": "$49/mo listed", "lead_time_days": 1, '
        '"supports": ["uptime"]}]\n'
        "```"
    )
    live_hermes(content)
    out = discovery._hermes_pick(SPEC, [_result()], 4, discovery._noop)
    assert len(out) == 1
    assert out[0]["vendor"] == "Acme Cloud Inc"
    assert out[0]["url"] == "https://acme.example.com/pricing"
    assert out[0]["price"] == 49.0


# --------------------------------------------------------------------------- #
# (c) multi-word vendor matching is case-insensitive & whitespace-tolerant
# --------------------------------------------------------------------------- #
def test_vendor_in_title_multiword_match():
    assert discovery._vendor_in_title(
        "Acme Cloud Inc", "Welcome to ACME  CLOUD INC, the best host")
    # word-order / extra-spacing tolerant
    assert discovery._vendor_in_title(
        "Acme Cloud Inc", "Inc. Cloud — Acme product page")


def test_vendor_in_title_no_false_match():
    assert not discovery._vendor_in_title("Acme Cloud Inc", "Totally unrelated page")
    assert not discovery._vendor_in_title("", "anything")
    assert not discovery._vendor_in_title("Acme", "")


def test_hermes_pick_repins_invented_url_via_multiword_title(live_hermes):
    """Model returns a multi-word vendor with an invented url; matching by the
    full (multi-word) title must re-pin it to the real result url."""
    content = (
        '[{"vendor": "Acme Cloud Inc", "product": "Hosting", '
        '"url": "https://invented-bogus.example/x", "price": 49, '
        '"price_from_page": false, "pricing_model": "subscription_monthly", '
        '"price_basis": "est.", "lead_time_days": 1, "supports": ["uptime"]}]'
    )
    live_hermes(content)
    results = [_result(domain="realacme.io",
                       url="https://realacme.io/pricing",
                       title="Acme Cloud Inc — Managed Cloud Hosting")]
    out = discovery._hermes_pick(SPEC, results, 4, discovery._noop)
    assert len(out) == 1
    assert out[0]["url"] == "https://realacme.io/pricing"
