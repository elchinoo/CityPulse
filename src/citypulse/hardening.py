"""Read-only and AI access hardening for AgentPG's read-only group role (`a1p_readonly`).

An instance's read-only database users, and AgentPG's restricted MCP server (which runs as one of them), get SELECT on every public
table through default privileges. That is what an AI agent should have for the fleet data (sensors, readings, history, bursts, the
district polygons), but not for the tables that hold secrets. Those get SELECT revoked from the role and nothing replaces it: there
is no masked view because there is nothing in them an agent or an analyst needs.

CityPulse holds no personal data (sensors, readings and districts only; the admin is one local password or an identity provider
login), so there are no masked views (`apgkit.masking.mask_view`) in this app. The REVOKE is the kit's `masking.deny_tables`: each
statement on its own, the result checked with `has_table_privilege`, never assumed.

A start-time snapshot is not enough: `agentpg db-users create` and AgentPG's default privileges give the role SELECT on base tables
again later. `apgkit.masking.reassert_forever` (started in the API lifespan, every 5 minutes) therefore re-runs `apply` and counts
the tables it found open (`masking_drift_tables`).

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
    """REVOKE SELECT on every secret table from the read-only role (the kit's `masking.deny_tables`). Idempotent. Quietly does nothing
    when the role does not exist (plain Postgres in development or CI) and skips a table that does not exist (yet). Returns
    {"applied": bool, "reason": str, "tables": {name: "denied" | "still readable" | "missing"}}; a "still readable" means the REVOKE did
    not take (not the owner, or PUBLIC can read it): treat it as not protected."""
    tables = dict(SECRET_TABLES if tables is None else tables)
    res = masking.deny_tables(conn, list(tables), role)
    if not res["applied"] and not res["tables"] and not res["skipped"] and "does not exist" in res["reason"]:
        return {"applied": False, "reason": res["reason"], "tables": {}}
    out = {name: "missing" if name in res["skipped"] else ("denied" if res["tables"].get(name) else "still readable") for name in tables}
    bad = [n for n, v in out.items() if v == "still readable"]
    return {"applied": not bad, "reason": f"still readable: {', '.join(bad)}" if bad else "", "tables": out}


def hook(conn) -> None:
    """The `after_migrate` hook (see apgkit.migrate.run)."""
    deny_secret_tables(conn)


def apply(conn) -> None:
    """The idempotent hardening as `apply(conn)` for `masking.reassert_forever`: the same call as the migrate hook."""
    deny_secret_tables(conn)
