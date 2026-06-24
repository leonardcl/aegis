"""Ledger query helpers."""
from sqlalchemy import func

from ..extensions import db
from ..models import LedgerEntry


def recent_entries(limit=10):
    return (
        LedgerEntry.query.order_by(LedgerEntry.timestamp.desc()).limit(limit).all()
    )


def all_entries():
    return LedgerEntry.query.order_by(LedgerEntry.timestamp.desc()).all()


def total_spend():
    """Sum of posted outflows."""
    total = (
        db.session.query(func.coalesce(func.sum(LedgerEntry.amount), 0.0))
        .filter(LedgerEntry.outcome == "posted")
        .scalar()
    )
    return float(total or 0.0)


def today_spend(today):
    total = (
        db.session.query(func.coalesce(func.sum(LedgerEntry.amount), 0.0))
        .filter(LedgerEntry.outcome == "posted")
        .filter(func.date(LedgerEntry.timestamp) == today.isoformat())
        .scalar()
    )
    return float(total or 0.0)


def month_spend(today=None):
    """Sum of posted outflows in the current calendar month (budget accumulator).

    ``today`` should be a UTC date (ledger timestamps are utcnow()-based)."""
    from datetime import datetime
    today = today or datetime.utcnow().date()
    start = today.replace(day=1)
    total = (
        db.session.query(func.coalesce(func.sum(LedgerEntry.amount), 0.0))
        .filter(LedgerEntry.outcome == "posted")
        .filter(func.date(LedgerEntry.timestamp) >= start.isoformat())
        .scalar()
    )
    return float(total or 0.0)


def add_entry(**kwargs):
    entry = LedgerEntry(**kwargs)
    db.session.add(entry)
    db.session.commit()
    return entry
