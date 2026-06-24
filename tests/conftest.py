"""Shared pytest fixtures for Aegis CFO tests.

IMPORTANT: we point DATABASE_URL at a throwaway temp file *before* importing the
app, because Flask-SQLAlchemy binds the engine when ``create_app()`` runs. If we
only overrode ``app.config`` afterwards the engine would still point at the real
``instance/aegis.sqlite`` and ``drop_all()`` in teardown would wipe demo data.
"""
import os
import tempfile

# Must run before any ``import app`` so app.config picks up the temp DB.
_TMPDIR = tempfile.mkdtemp(prefix="aegis-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "test.sqlite")
os.environ["HERMES_API_URL"] = ""  # force deterministic / offline paths
os.environ["HERMES_TOOL_TOKEN"] = ""  # no bearer auth on /hermes/* in tests
# Both are set before importing app so config.load_dotenv() (override=False)
# leaves these test values in place instead of pulling the real .env.

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db as _db  # noqa: E402


@pytest.fixture
def app():
    app = create_app()
    app.config.update(
        TESTING=True,
        PROCUREMENT_DISCOVERY_MODE="seed",
        PROCUREMENT_DISCOVERY_LIMIT=4,
    )
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def client(app):
    return app.test_client()
