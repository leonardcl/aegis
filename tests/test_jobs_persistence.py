"""Regression tests for the persistent, shared background-job registry.

These verify that a job round-trips through the SQLAlchemy ``Job`` table: it can
be created, fetched by id, status-updated, and that a *freshly-fetched* copy
(simulating a different gunicorn worker reading the same SQLite file) reflects
the persisted state rather than any in-process cache.
"""
from app.extensions import db
from app.models import Job
from app.services import jobs


def _make_job(job_id="abc123def456", kind="audit", **state):
    """Insert a job row directly through the storage helper, no network."""
    base = {"id": job_id, "kind": kind, "status": "running", "error": None}
    base.update(state)
    jobs._persist_new(job_id, kind, base)
    return job_id


def test_create_and_fetch_by_id(app):
    job_id = _make_job(period_days=30, summary=None)

    fetched = jobs.get_job(job_id)
    assert fetched["id"] == job_id
    assert fetched["kind"] == "audit"
    assert fetched["status"] == "running"
    assert fetched["period_days"] == 30


def test_get_job_unknown_returns_empty_dict(app):
    assert jobs.get_job("does-not-exist") == {}


def test_status_update_persists(app):
    job_id = _make_job(report_id=None, summary=None)

    jobs._update_job(job_id, status="done", report_id=7, summary="all balanced")

    fetched = jobs.get_job(job_id)
    assert fetched["status"] == "done"
    assert fetched["report_id"] == 7
    assert fetched["summary"] == "all balanced"
    # The mirrored column is updated too (used for cross-worker queries).
    row = Job.query.filter_by(job_id=job_id).first()
    assert row.status == "done"


def test_fresh_worker_sees_persisted_state(app):
    """Simulate a second worker: drop the SQLAlchemy session so the next read
    must come from the SQLite file, not the identity map / in-process cache."""
    job_id = _make_job(kind="autopilot", req_id=42, events=[], stage="")
    jobs._update_job(job_id, status="done", recommended_vendor_id=99,
                     negotiated=True)

    # New session == new worker reading the same database file.
    db.session.remove()

    fetched = jobs.get_job(job_id)
    assert fetched["status"] == "done"
    assert fetched["recommended_vendor_id"] == 99
    assert fetched["negotiated"] is True
    assert fetched["req_id"] == 42


def test_autopilot_dedup_by_ref(app):
    """A running autopilot for a request id is found via the indexed ``ref``
    column, so a re-submit can reuse the same job (cross-worker safe)."""
    jobs._persist_new("job-running-01", "autopilot",
                      {"id": "job-running-01", "kind": "autopilot",
                       "status": "running", "req_id": 5},
                      ref="5")
    db.session.remove()  # force a fresh read, as another worker would

    existing = Job.query.filter_by(
        kind="autopilot", ref="5", status="running").first()
    assert existing is not None
    assert existing.job_id == "job-running-01"
