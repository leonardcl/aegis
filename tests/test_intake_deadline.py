"""Regression tests for calendar-aware deadline math in W1 INTAKE.

A month must be its real calendar length (28-31 days), not a fixed 30.

Run:  ~/miniconda3/envs/aegis/bin/python -m pytest tests/test_intake_deadline.py -q
"""
from datetime import date

from app.services import intake


# --------------------------------------------------------------------------- #
# _add_months helper — direct, deterministic checks
# --------------------------------------------------------------------------- #
def test_add_months_clamps_jan31_to_feb_nonleap():
    # 2026 is not a leap year -> Feb has 28 days.
    assert intake._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_add_months_clamps_jan31_to_feb_leap():
    # 2024 is a leap year -> Feb has 29 days.
    assert intake._add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_add_months_crosses_year_boundary():
    assert intake._add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_add_months_normal_31_day_landing():
    assert intake._add_months(date(2026, 3, 15), 1) == date(2026, 4, 15)


# --------------------------------------------------------------------------- #
# _extract_deadline — exercised through the parsing path
# --------------------------------------------------------------------------- #
def test_deadline_one_month_from_jan31_nonleap():
    # Jan 31 + 1 month would be Feb 31 under naive math; must clamp to Feb 28.
    # Fixed-30-day math would give Mar 2 (Jan 31 + 30d) -> wrong.
    dl, raw = intake._extract_deadline("ready in 1 month", today=date(2026, 1, 31))
    assert dl == date(2026, 2, 28)
    assert "1 month" in raw


def test_deadline_two_months_crossing_feb_leap():
    # Jan 15 2024 + 2 months -> Mar 15 (crosses a 29-day Feb).
    dl, _ = intake._extract_deadline("live in 2 months", today=date(2024, 1, 15))
    assert dl == date(2024, 3, 15)


def test_deadline_two_months_crossing_feb_nonleap():
    # Jan 15 2026 + 2 months -> Mar 15 (crosses a 28-day Feb).
    dl, _ = intake._extract_deadline("live in 2 months", today=date(2026, 1, 15))
    assert dl == date(2026, 3, 15)


def test_deadline_days_and_weeks_unchanged():
    # Non-month units keep exact day arithmetic.
    dl_d, _ = intake._extract_deadline("done in 10 days", today=date(2026, 1, 1))
    assert dl_d == date(2026, 1, 11)
    dl_w, _ = intake._extract_deadline("done in 2 weeks", today=date(2026, 1, 1))
    assert dl_w == date(2026, 1, 15)


def test_parse_need_deterministic_month_deadline():
    spec = intake.parse_need_deterministic(
        "I need a CRM live in 1 month", today=date(2026, 1, 31))
    assert spec["deadline"] == date(2026, 2, 28).isoformat()
