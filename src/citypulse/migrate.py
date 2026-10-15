"""Tiny plain-SQL migration runner."""
from pathlib import Path

from .db import connect

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def run() -> list[str]:
    applied: list[str] = []
    with connect() as conn:
        # Two replicas may start together: serialise migrations.
        conn.execute("SELECT pg_advisory_xact_lock(727401)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
        conn.commit()
    return applied
