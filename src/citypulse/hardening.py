"""Read-only and AI access hardening for AgentPG's read-only group role (`a1p_readonly`).

An instance's read-only database users, and AgentPG's restricted MCP server (which runs as one of them), get SELECT on every public
table through default privileges. That is what an AI agent should have for the fleet data (sensors, readings, history, bursts, the
district polygons), but not for the tables that hold secrets. Those get SELECT revoked from the role and nothing replaces it: there
is no masked view because there is nothing in them an agent or an analyst needs.

CityPulse holds no personal data (sensors, readings and districts only; the admin is one local password or an identity provider
login), so there are no masked views (`apgkit.masking.mask_view`) in this app. The kit has no revoke-only helper yet, so this module
does the REVOKE itself in the same way: each statement on its own, the result checked with `has_table_privilege`, never assumed.

Run by `migrate.run()` after the migrations and the kit's auth tables exist, at EVERY start: default privileges give the role SELECT
again on every new table, so a table added later is readable until the next start revokes it. Only the table owner can revoke;
a non-owner REVOKE only raises a warning, which is why the privilege is read back and reported.
"""
from __future__ import annotations

import logging

from apgkit import masking

log = logging.getLogger("citypulse.hardening")

# table -> what it holds (documented in GUIDE 7.12 and returned to the caller; nothing here is a column list: the whole table is denied)
SECRET_TABLES: dict[str, str] = {
    "app_setting": "key/value settings: the admin password hash, the vault's wrapped data key and salts, encrypted values, setup state",
    "api_token": "sha256 of every API token (and its prefix and scope)",
    "web_session": "sha256 of every browser session token, the signed-in user's identity and the identity provider's id_token hint",
    "oidc_login": "single sign-on login state: state, PKCE verifier and nonce",
}


def deny_secret_tables(conn, tables=None, role: str = masking.READONLY_ROLE) -> dict:
    """REVOKE SELECT on every secret table from the read-only role. Idempotent. Quietly does nothing when the role does not exist
    (plain Postgres in development or CI) and skips a table that does not exist (yet). Returns {"applied": bool, "reason": str,
    "tables": {name: "denied" | "still readable" | "missing"}}; a "still readable" means the REVOKE did not take (not the owner,
    or PUBLIC can read it): treat it as not protected."""
    tables = dict(SECRET_TABLES if tables is None else tables)
    conn.commit()
    prev = conn.autocommit
    conn.autocommit = True
    try:
        if not conn.execute("SELECT 1 AS ok FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            log.info("read-only hardening skipped: role %s does not exist", role)
            return {"applied": False, "reason": f"role {role} does not exist", "tables": {}}
        out: dict[str, str] = {}
        for name in tables:
            if not conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{name}",)).fetchone()["ok"]:
                out[name] = "missing"
                continue
            # the names are the constants above, quoted as identifiers all the same
            conn.execute(f'REVOKE SELECT ON TABLE public."{name}" FROM "{role}"')
            readable = conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, f"public.{name}")).fetchone()["ok"]
            out[name] = "still readable" if readable else "denied"
            if readable:
                log.warning("read-only hardening: %s can still read %s (not the owner, or PUBLIC has SELECT)", role, name)
        bad = [n for n, v in out.items() if v == "still readable"]
        return {"applied": not bad, "reason": f"still readable: {', '.join(bad)}" if bad else "", "tables": out}
    finally:
        conn.autocommit = prev


def hook(conn) -> None:
    """The `after_migrate` hook (see apgkit.migrate.run)."""
    deny_secret_tables(conn)
