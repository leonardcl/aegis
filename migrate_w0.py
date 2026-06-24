"""W0 migration — add on-demand procurement columns to an existing DB.

Idempotent: only adds columns that are missing, so it is safe to run repeatedly
and it preserves all existing rows. A fresh ``python seed.py`` does not need this
(``db.create_all()`` builds the new schema from the models); this exists purely to
upgrade a database that was created before the W0 model fields were added.

Usage:
    python migrate_w0.py
"""
import sqlite3

from app import create_app
from app.extensions import db

# table -> list of (column, SQLite column definition) to add if missing.
NEW_COLUMNS = {
    "procurement_request": [
        ("intake_raw", "TEXT DEFAULT ''"),
        ("requirement_spec_json", "TEXT DEFAULT ''"),
        ("stripe_subscription_id", "VARCHAR(120) DEFAULT ''"),
        ("purchased_at", "DATETIME"),
    ],
    "vendor_option": [
        ("url", "VARCHAR(500) DEFAULT ''"),
        ("source", "VARCHAR(20) DEFAULT 'manual'"),
        ("disqualified", "BOOLEAN DEFAULT 0"),
        ("disqualify_reason", "VARCHAR(300) DEFAULT ''"),
        ("price_basis", "VARCHAR(120) DEFAULT ''"),
        ("negotiation_json", "TEXT DEFAULT ''"),
    ],
}


def _db_path():
    app = create_app()
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    # sqlite:///relative/path  or  sqlite:////absolute/path
    path = uri.replace("sqlite:///", "", 1)
    if not path.startswith("/"):
        # Flask resolves relative SQLite paths against the instance folder.
        import os

        path = os.path.join(app.instance_path, path)
    return path


def _existing_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def run():
    path = _db_path()
    conn = sqlite3.connect(path)
    try:
        added, skipped = [], []
        for table, columns in NEW_COLUMNS.items():
            existing = _existing_columns(conn, table)
            if not existing:
                print(f"⚠  table {table!r} does not exist yet — run seed.py first.")
                continue
            for name, ddl in columns:
                if name in existing:
                    skipped.append(f"{table}.{name}")
                    continue
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append(f"{table}.{name}")
        conn.commit()
    finally:
        conn.close()

    print(f"DB: {path}")
    print(f"✅ Added {len(added)} column(s): {', '.join(added) or '(none)'}")
    if skipped:
        print(f"↩  Already present, skipped {len(skipped)}: {', '.join(skipped)}")


if __name__ == "__main__":
    run()
