"""This app's instances of the shared blocks (agentpg-kit), and the authorization model of CityPulse.

Permissions are what the code checks (`city:control`); roles (given to people by the identity provider) and scopes
(given to services, local API tokens and the static key) are mapped onto them here, in one place.
"""
import os
import threading
import time

from apgkit import metrics as metrics_mod
from apgkit.auth import AppAuth, Authenticator, LocalAuth, Principal, from_env
from apgkit.config import env_bool
from apgkit.db import Database

from . import __version__

APP = "citypulse"
COOKIE = "cp_session"
CSRF_HEADER = "x-citypulse-csrf"
TOKEN_PREFIX = "cp_"

db = Database(pool_max=10)
metrics = metrics_mod.Metrics(APP, __version__)
# db_insights_* gauges at /metrics (statistics from edb_stat_monitor / pg_stat_statements, cached 30 s; db_insights_available 0 where unreadable).
metrics.add_insights_collector(lambda: db.connect(retries=1), top=5, min_calls=5, ttl=30)

ALL = frozenset({"*"})
PERMISSIONS = {
    "ingest:write": "push readings (sensors, collectors, OTLP); nothing else",
    "city:read": "the dashboard's data: status, history, sensors, active bursts",
    "city:control": "start and stop bursts (the simulated city's faults)",
    "metrics:scrape": "scrape /metrics",
    "settings:manage": "API tokens and the admin password",
    "city:delete": "delete a city with all its sensors, readings, history and burst targets (irreversible)",
}
# `city:delete` is held by the wildcard only: the admin role and the `full` local scope (and the static key); no other role or scope lists it.
ROLE_PERMISSIONS = {
    "admin": ALL,
    "operator": frozenset({"city:read", "city:control"}),
    "viewer": frozenset({"city:read"}),
}
SCOPE_PERMISSIONS = {
    # local API tokens: each one keeps exactly the access it had before
    "ingest": frozenset({"ingest:write"}), "read": frozenset({"city:read"}), "metrics": frozenset({"metrics:scrape"}), "full": ALL,
    # scopes the identity provider can grant to services
    "citypulse.ingest": frozenset({"ingest:write"}), "citypulse.read": frozenset({"city:read"}),
    "citypulse.metrics": frozenset({"metrics:scrape"}),
}
fed = from_env("CITYPULSE", APP)
APP_AUTH = AppAuth(APP, fed.client_id, ROLE_PERMISSIONS, SCOPE_PERMISSIONS)
local = LocalAuth(APP_AUTH, env_prefix="CITYPULSE", token_prefix=TOKEN_PREFIX, token_scopes={"read", "ingest", "metrics", "full"})

# Sensors push many batches a second, so a valid token is remembered briefly instead of costing a database
# round trip (and a write) per request. Revocation takes effect at once on this process and within the TTL elsewhere.
_TOKEN_TTL = 30.0
_token_cache: dict[str, tuple[float, Principal]] = {}
_cache_lock = threading.Lock()


def clear_token_cache() -> None:
    with _cache_lock:
        _token_cache.clear()


def _cached_token(raw: str) -> Principal | None:
    from apgkit.ids import sha256
    digest, now = sha256(raw), time.monotonic()
    with _cache_lock:
        hit = _token_cache.get(digest)
    if hit and hit[0] > now:
        return hit[1]
    with db.connect(retries=1) as conn:
        principal = local.token_principal(conn, raw)
    with _cache_lock:
        if principal is None:
            _token_cache.pop(digest, None)
        else:
            if len(_token_cache) > 1000:
                _token_cache.clear()
            _token_cache[digest] = (now + _TOKEN_TTL, principal)
    return principal


# CITYPULSE_ALLOW_ANONYMOUS is honoured only on a loopback bind (or CITYPULSE_ALLOW_ANONYMOUS_PUBLIC=1): pass the address the
# server really binds to (cli.py: HOST, default 127.0.0.1), preferring CITYPULSE_HOST when set.
BIND_HOST = os.environ.get("CITYPULSE_HOST") or os.environ.get("HOST", "127.0.0.1")
auth = Authenticator(APP_AUTH, local, db, cookie_name=COOKIE, verifier=fed.verifier, bind_host=BIND_HOST,
                     allow_anonymous=lambda: env_bool("CITYPULSE_ALLOW_ANONYMOUS"),
                     bearer_resolvers={TOKEN_PREFIX: _cached_token})
public_url_env = "CITYPULSE_PUBLIC_URL"
