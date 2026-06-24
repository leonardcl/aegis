"""W3 migration — add the vendor enrichment column to an existing DB.

Idempotent (only adds the column if missing); preserves rows. A fresh
``python seed.py`` does not need this. See migrate_w0.py for the pattern.

Usage:
    python migrate_w3.py
"""
import sqlite3

from app import create_app

NEW_COLUMNS = {
    "vendor_option": [
        # JSON: {capabilities: [...], reliability: int, flexibility: int}
        ("enrichment_json", "TEXT DEFAULT ''"),
    ],
}


def _db_path():
    app = create_app()
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    path = uri.replace("sqlite:///", "", 1)
    if not path.startswith("/"):
        import os
        path = os.path.join(app.instance_path, path)
    return path


def run():
    path = _db_path()
    conn = sqlite3.connect(path)
    try:
        added, skipped = [], []
        for table, columns in NEW_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                print(f"⚠  table {table!r} missing — run seed.py first.")
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
        print(f"↩  Skipped (present): {', '.join(skipped)}")


if __name__ == "__main__":
    run()
