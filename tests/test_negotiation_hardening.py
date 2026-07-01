"""Hardening regression tests for negotiation (app/services/negotiation.py).

Covers the two fixes:
  (a) floor_pct > 1.0 (and < 0) is clamped to [0, 1] with a warning log -- never
      silently used -- so the resulting hidden floor / agreed price stays sane;
  (b) a normal floor_pct (0.8) behaves as before (floor = 80% of the anchor,
      positive savings, no warning).

All offline: conftest forces HERMES_API_URL="" so the deterministic engine runs;
no real network is touched.
"""
import logging

import pytest

from app.services import negotiation
from app.services.negotiation import _clean_floor_pct


# --------------------------------------------------------------------------- #
# (a) out-of-range floor_pct is clamped, not silently used
# --------------------------------------------------------------------------- #
def test_clean_floor_pct_clamps_above_one(caplog):
    with caplog.at_level(logging.WARNING):
        fp = _clean_floor_pct(1.5)
    assert fp == 1.0                       # clamped into [0, 1]
    assert any("floor_pct" in r.message for r in caplog.records)


def test_clean_floor_pct_clamps_below_zero(caplog):
    with caplog.at_level(logging.WARNING):
        fp = _clean_floor_pct(-0.3)
    assert fp == 0.0
    assert any("floor_pct" in r.message for r in caplog.records)


def test_clean_floor_pct_non_numeric_defaults(caplog):
    with caplog.at_level(logging.WARNING):
        fp = _clean_floor_pct("not-a-number")
    assert fp == negotiation._DEFAULT_FLOOR_PCT
    assert any("floor_pct" in r.message for r in caplog.records)


def test_out_of_range_floor_pct_yields_sane_price(caplog):
    """An absurd floor_pct of 5.0 must NOT push the seller floor to 5x current.
    Clamped to 1.0 the floor caps at the anchor, so the agreed price stays within
    (0, current] and savings are never negative."""
    current = 10_000.0
    with caplog.at_level(logging.WARNING):
        res = negotiation.negotiate_deterministic("Acme", current, floor_pct=5.0)
    assert res["engine"] == "local"
    # floor clamped to <= current => agreed price sane, savings never negative.
    assert 0 < res["agreed_amount"] <= current
    assert res["savings"] >= 0.0
    assert any("floor_pct" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# (b) a normal floor_pct behaves as before
# --------------------------------------------------------------------------- #
def test_clean_floor_pct_passthrough_normal():
    assert _clean_floor_pct(0.8) == 0.8


def test_normal_floor_pct_behaves_as_before(caplog):
    current = 10_000.0
    with caplog.at_level(logging.WARNING):
        res = negotiation.negotiate_deterministic("Acme", current, floor_pct=0.8)
    # Hidden floor at 80% of anchor -> agreed price never below it, savings positive.
    assert res["agreed_amount"] >= current * 0.8
    assert res["agreed_amount"] <= current
    assert res["savings"] > 0.0
    # A valid value must not emit the clamp/default warning.
    assert not any("floor_pct" in r.message for r in caplog.records)


def test_none_floor_pct_uses_default():
    current = 10_000.0
    res = negotiation.negotiate_deterministic("Acme", current, floor_pct=None)
    assert res["agreed_amount"] >= current * negotiation._DEFAULT_FLOOR_PCT
