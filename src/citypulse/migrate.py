"""Tiny plain-SQL migration runner (the runner itself lives in agentpg-kit)."""
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def run() -> list[str]:
    """Apply the SQL migrations. The shared tables (settings, sessions, API tokens, OIDC state) come from the kit."""
    from apgkit import migrate as kit_migrate
    from apgkit.auth.schema import SQL as AUTH_SQL

    from .core import db

    # Two replicas may start together: serialise migrations.
    with db.connect() as lock_conn:
        lock_conn.execute("SELECT pg_advisory_lock(727401)")
        try:
            applied = kit_migrate.run(db, MIGRATIONS)
            with db.connect() as conn:  # after the app's own migrations: it upgrades the session table and adds the OIDC state
                conn.execute(AUTH_SQL)
                conn.commit()
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(727401)")
    return applied
