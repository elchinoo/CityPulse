"""Tiny plain-SQL migration runner (the runner itself lives in agentpg-kit)."""
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def run() -> list[str]:
    """Apply the SQL migrations. The shared tables (settings, sessions, API tokens, OIDC state) come from the kit."""
    from apgkit import migrate as kit_migrate
    from apgkit.auth.schema import SQL as AUTH_SQL

    from . import geo, hardening
    from .core import db

    # Two replicas may start together: serialise migrations.
    with db.connect() as lock_conn:
        lock_conn.execute("SELECT pg_advisory_lock(727401)")
        try:
            # Hooks run after the migrations, at every start: PostGIS and the district polygons (skipped quietly without PostGIS), and the
            # read-only hardening (skipped quietly without the role a1p_readonly).
            applied = kit_migrate.run(db, MIGRATIONS, after_migrate=[geo.ensure, hardening.hook])
            with db.connect() as conn:  # after the app's own migrations: it upgrades the session table and adds the OIDC state
                conn.execute(AUTH_SQL)
                conn.commit()
                hardening.deny_secret_tables(conn)   # once more: oidc_login only exists now (on a first start the hook above ran too early for it)
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(727401)")
    return applied
