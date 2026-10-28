import asyncio
import contextlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from apgkit import insights as kit_insights
from apgkit.auth import TokenError
from apgkit.auth.rp import safe_return_to
from apgkit.config import env_bool
from apgkit.guard import SecurityGuard
from apgkit.web import is_https, mount_kit_static, public_url
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    __version__,
    chaos,
    core,
    explain,
    gateway,
    geo,
    memory,
    metrics,
    otlp,
    purge,
    reporter,
    sim,
    status,
    timeseries,
    tools,
)
from .catalog import CITY_ID, DISTRICT_ID, DISTRICTS, GROUPS, TYPES
from .config import get_settings
from .core import COOKIE, CSRF_HEADER, auth, fed, local
from .db import connect

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 2 * 1024 * 1024
_announced = False


def _announce_setup_code() -> None:
    global _announced
    if not _announced:
        _announced = True
        print("\n=== CityPulse first-run setup ===\n"
              "Open the app in your browser and enter this one-time code to create the admin password:\n"
              f"    {local.setup_code()}\n"
              "(It changes every time the server restarts until setup is complete.)\n", file=sys.stderr, flush=True)


def _startup_tasks() -> None:
    try:
        with connect(retries=1) as conn:
            if local.needs_setup(conn) and not fed.enabled:
                _announce_setup_code()
    except Exception:  # noqa: BLE001 - database may be asleep; it is retried on the first request
        pass


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = get_settings()
    tasks: list[asyncio.Task] = []
    if cfg.role in ("api", "all"):
        _startup_tasks()
        tasks.append(asyncio.create_task(status.run_forever()))
        rep = reporter.maybe_start()
        if rep:
            tasks.append(asyncio.create_task(rep.run()))
    if cfg.role in ("sim", "all"):
        tasks.append(asyncio.create_task(sim.make(direct=cfg.role == "all").run()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


# --- authentication -------------------------------------------------------------------------
# Fail closed: with no credential configured every protected route is denied (and the setup flow is offered),
# unless the operator explicitly sets CITYPULSE_ALLOW_ANONYMOUS=true. The mechanics (local admin, API tokens, federated
# JWTs, sessions, rate limiting) are agentpg-kit's `Authenticator`; here we only name the permission each route needs.

def _db_unavailable() -> HTTPException:
    return auth.db_unavailable()


def require(permission: str = "city:read"):
    """Dependency: the caller must hold `permission`. A full token, the static key and the admin session hold all of them;
    other tokens map to exactly one ('read' reads, 'ingest' pushes readings, 'metrics' scrapes /metrics)."""
    return auth.require(permission)


app = FastAPI(title="CityPulse", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityGuard, csrf_header=CSRF_HEADER, csrf_prefixes=("/api/",), body_prefixes=("/api/", "/v1/"),
                   max_body=MAX_BODY, observe=core.metrics.observe)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
mount_kit_static(app)


CITY_PATTERN = CITY_ID.pattern
CityParam = Query(None, max_length=32, pattern=CITY_PATTERN, description="Only this city; all cities when omitted")


def _api_role() -> None:
    if get_settings().role == "sim":
        raise HTTPException(404, "this instance runs the simulator only")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__, "role": get_settings().role}


@app.get("/readyz")
def readyz():
    try:
        with connect(retries=1) as conn:
            conn.execute("SELECT 1")
        return {"status": "ready"}
    except Exception:  # noqa: BLE001 - database may be waking; never echo connection details
        return JSONResponse({"status": "waking-database"}, status_code=503)


@app.get("/metrics", dependencies=[Depends(require("metrics:scrape"))])
def prometheus_metrics():
    body, ctype = metrics.render()
    return Response(body, media_type=ctype)


@app.get("/")
def index():
    _api_role()
    return FileResponse(STATIC / "index.html")


# --- session, setup, login, tokens ----------------------------------------------------------

class SetupBody(BaseModel):
    code: str = Field(max_length=64)
    password: str = Field(max_length=256)


class LoginBody(BaseModel):
    password: str | None = Field(default=None, max_length=256)
    api_key: str | None = Field(default=None, max_length=512)


class PasswordBody(BaseModel):
    current: str | None = Field(default=None, max_length=256)
    new: str = Field(max_length=256)


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    scope: str = Field(pattern="^(read|ingest|metrics|full)$")


def _start_session(conn, request: Request, response: Response, **who) -> None:
    raw = local.create_session(conn, **who)
    response.set_cookie(COOKIE, raw, httponly=True, samesite="strict" if not who else "lax", secure=is_https(request),
                        max_age=12 * 3600, path="/")


def _guard_attempts(request: Request) -> str:
    client = auth.client(request)
    if auth.limiter.limited(client):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    return client


@app.get("/api/session")
def session_state(request: Request):
    """Public: tells the UI which screen to show. Reveals no secrets."""
    _api_role()
    try:
        principal = auth.authenticate(request)
        with connect(retries=1) as conn:
            setup = local.needs_setup(conn) and not env_bool("CITYPULSE_ALLOW_ANONYMOUS") and not fed.enabled
            has_password = local.admin_configured(conn)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    if setup:
        _announce_setup_code()
    state = "setup_required" if setup else "logged_in" if principal else "logged_out"
    out = {"state": state, "has_admin_password": has_password,
           "password_login": has_password and (fed.local_login or not fed.enabled),
           "key_login": local.static_key_configured() and (fed.local_login or not fed.enabled),
           "sso": {"issuer": fed.issuer, "login_url": "/auth/login"} if fed.enabled else None, "version": __version__}
    if principal:
        out["user"] = {"name": principal.name, "email": principal.email, "source": principal.source, "roles": sorted(principal.roles),
                       "permissions": ["*"] if "*" in principal.permissions else sorted(principal.permissions)}
    return out


# ---- federated sign-in (only when CITYPULSE_OIDC_ISSUER is set) ----------------------------------------------
@app.get("/auth/login")
def sso_login(request: Request, next: str | None = None):
    if not fed.enabled:
        raise HTTPException(404, "single sign-on is not configured")
    try:
        with connect(retries=1) as conn:
            url = fed.rp.begin(conn, public_url(request, core.public_url_env) + "/auth/callback", next)
    except psycopg.Error as exc:
        raise _db_unavailable() from exc
    except Exception:  # noqa: BLE001 - the identity provider may be unreachable
        return RedirectResponse("/?sso_error=the identity provider is unavailable", status_code=303)
    return RedirectResponse(url, status_code=303)


@app.get("/auth/callback")
def sso_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not fed.enabled:
        raise HTTPException(404, "single sign-on is not configured")
    if error or not code or not state:
        return RedirectResponse("/?sso_error=the sign-in was cancelled or failed", status_code=303)
    try:
        with connect(retries=1) as conn:
            claims, back, id_token = fed.rp.finish(conn, public_url(request, core.public_url_env) + "/auth/callback", code, state)
            roles = [r for r in claims.get("roles", []) if isinstance(r, str)]
            resp = RedirectResponse(safe_return_to(back), status_code=303)
            if not core.APP_AUTH.permissions_for(roles=roles):
                return RedirectResponse("/?sso_error=your account has no role in CityPulse; ask an administrator", status_code=303)
            _start_session(conn, request, resp, subject=str(claims["sub"]), name=str(claims.get("name") or ""),
                           email=str(claims.get("email") or ""), roles=roles, source="oidc", id_token_hint=id_token)
            return resp
    except TokenError as exc:
        return RedirectResponse("/?sso_error=" + str(exc)[:120], status_code=303)


@app.post("/api/setup")
def setup(body: SetupBody, request: Request, response: Response):
    _api_role()
    client = _guard_attempts(request)
    try:
        with connect(retries=1) as conn:
            try:
                local.complete_setup(conn, body.code, body.password)
            except PermissionError as exc:
                auth.limiter.record(client)
                raise HTTPException(403, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    _api_role()
    client = _guard_attempts(request)
    if fed.enabled and not fed.local_login:
        raise HTTPException(403, "local sign-in is turned off; use single sign-on")
    try:
        with connect(retries=1) as conn:
            ok = False
            if body.password is not None:
                ok = local.login_password(conn, body.password)
            elif body.api_key is not None and local.static_key_matches(body.api_key):
                ok = True
            if not ok:
                auth.limiter.record(client)
                raise HTTPException(401, "invalid credentials")
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    out: dict = {"ok": True}
    try:
        with connect(retries=1) as conn:
            row = local.delete_session(conn, request.cookies.get(COOKIE))
        if row and row["source"] == "oidc" and fed.enabled:
            with contextlib.suppress(Exception):
                out["logout_url"] = fed.rp.logout_url(row["id_token_hint"], public_url(request, core.public_url_env) + "/")
    except (psycopg.Error, RuntimeError):
        pass  # the cookie is cleared regardless; the session row expires on its own
    response.delete_cookie(COOKIE, path="/")
    return out


@app.post("/api/admin-password", dependencies=[Depends(require("settings:manage"))])
def admin_password(body: PasswordBody, request: Request):
    """Set the admin password (first time, when signed in with a static key) or change it."""
    _api_role()
    client = _guard_attempts(request)
    with connect() as conn:
        try:
            if local.admin_configured(conn):
                if body.current is None:
                    raise HTTPException(422, "current password is required")
                local.change_password(conn, body.current, body.new)
            else:
                local.set_admin_password(conn, body.new)
        except PermissionError as exc:
            auth.limiter.record(client)
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        conn.execute("DELETE FROM web_session")  # sign every browser out after a password change
        conn.commit()
    return {"ok": True, "signed_out_everywhere": True}


@app.get("/api/tokens", dependencies=[Depends(require("settings:manage"))])
def list_tokens():
    _api_role()
    with connect() as conn:
        return local.list_tokens(conn)


@app.post("/api/tokens", dependencies=[Depends(require("settings:manage"))])
def create_token(body: TokenBody):
    """The token value is returned exactly once; only its hash is stored."""
    _api_role()
    with connect() as conn:
        token_id, raw = local.create_token(conn, body.name.strip(), body.scope)
    return {"id": token_id, "token": raw, "scope": body.scope}


@app.delete("/api/tokens/{token_id}", dependencies=[Depends(require("settings:manage"))])
def revoke_token(token_id: int):
    _api_role()
    with connect() as conn:
        revoked = local.revoke_token(conn, token_id)
        core.clear_token_cache()
        if not revoked:
            raise HTTPException(404, "no such active token")
    return {"ok": True}


# --- collection: push API and OTLP --------------------------------------------------------------

class ReadingsBody(BaseModel):
    readings: list[Any] = Field(max_length=gateway.MAX_BATCH)


@app.post("/api/readings", dependencies=[Depends(require("ingest:write"))])
def post_readings(body: ReadingsBody):
    """Batched sensor readings. Bad items are rejected and counted; the rest are stored.
    429 + Retry-After means the gateway is saturated: keep the batch and retry with backoff."""
    _api_role()
    try:
        return gateway.accept(body.readings)
    except gateway.BadBatch as exc:
        raise HTTPException(422, str(exc)) from exc
    except gateway.Overloaded as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
    except gateway.Unavailable as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc


async def _read_capped(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_BODY:
            raise HTTPException(413, "request body too large (2 MB limit)")
    return bytes(data)


@app.post("/v1/metrics", dependencies=[Depends(require("ingest:write"))])
async def otlp_metrics(request: Request):
    """OTLP/HTTP metrics: gauge and sum data points become readings (see otlp.py for the attribute mapping)."""
    _api_role()
    raw = await _read_capped(request)
    ctype = request.headers.get("content-type", "")
    try:
        data = otlp.decode(raw, ctype, request.headers.get("content-encoding", "").lower())
        accepted, rejected, note = await asyncio.to_thread(otlp.store, data)
    except otlp.OtlpError as exc:
        raise HTTPException(400, str(exc)) from exc
    except gateway.BadBatch as exc:
        raise HTTPException(400, str(exc)) from exc
    except gateway.Overloaded as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
    except gateway.Unavailable as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc
    body, content_type = otlp.ack(rejected, note, "protobuf" in ctype.lower())
    return Response(body, media_type=content_type)


# --- the dashboard's data ------------------------------------------------------------------------

@app.get("/api/catalog", dependencies=[Depends(require("city:read"))])
def catalog():
    return {
        # The built-in simulator's districts only. Real districts are whatever the sensors report: GET /api/cities/{city}/districts.
        "districts": [{"id": k, "name": v} for k, v in DISTRICTS.items()],
        "groups": GROUPS,
        "types": [{"key": t.key, "label": t.label, "unit": t.unit, "group": t.group, "interval": t.interval,
                   "lo": t.lo, "hi": t.hi} for t in TYPES.values()],
    }


@app.get("/api/status", dependencies=[Depends(require("city:read"))])
def get_status(city: str | None = CityParam):
    _api_role()
    try:
        with connect(retries=1) as conn:
            s = status.summary(conn, city)
            beat = sim.heartbeat(conn)
            # Role api has no built-in simulator (the data comes from a separate one); an old heartbeat left in the database is not shown.
            s["sim"] = beat if beat and (get_settings().role != "api" or beat["running"]) else None
            s["bursts"] = chaos.active(conn, city)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return s


@app.get("/api/history", dependencies=[Depends(require("city:read"))])
def get_history(minutes: int = Query(60, ge=5, le=720), city: str | None = CityParam):
    _api_role()
    with connect() as conn:
        return {"city": city, "health": status.history(conn, minutes, city), "throughput": status.throughput(conn, minutes, city)}


@app.get("/api/cities", dependencies=[Depends(require("city:read"))])
def list_cities(q: str = Query("", max_length=32, pattern=r"^[A-Za-z0-9_-]*$"), limit: int = Query(50, ge=1, le=200),
                offset: int = Query(0, ge=0, le=1_000_000), sort: str = Query("name", pattern="^(name|worst)$")):
    """One page of cities: sensor counts per status, tier, worst sensor status and newest reading time.
    `q` is a case-insensitive prefix; `sort=worst` lists the least healthy first. `total` counts the matches."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            return status.cities(conn, q, limit, offset, sort)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


@app.get("/api/cities/{city_id}/districts", dependencies=[Depends(require("city:read"))])
def city_districts(city_id: str = PathParam(max_length=32, pattern=CITY_PATTERN)):
    """The districts that report in this city (ids are free-form: `alder-quay`), biggest first, at most 200, with `total` for all
    of them. Each has `id`, display `name`, `total` sensors, `counts` per status, `health` and `worst_status`. 404 if the city has no sensors."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            out = status.districts(conn, city_id)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    if not out["total"]:
        raise HTTPException(404, f"no sensor reports for city {city_id!r}")
    return out


@app.delete("/api/cities/{city_id}", dependencies=[Depends(require("city:delete"))])
def delete_city(city_id: str = PathParam(max_length=32, pattern=CITY_PATTERN)):
    """Admin only. Irreversibly deletes the city with its sensors, readings, status history and burst targets, in batches of
    at most 5000 rows (one transaction each), and reports how many of each were removed. Stop the city's senders first:
    a sender that keeps reporting recreates its sensors. 404 if no sensor reports for the city, 409 if another deletion runs."""
    _api_role()
    try:
        out = purge.delete_city(city_id)
    except purge.NoSuchCity as exc:
        raise HTTPException(404, str(exc)) from exc
    except purge.Busy as exc:
        raise HTTPException(409, str(exc)) from exc
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    gateway.forget_sensor_count()
    return out


_SENSOR_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")


# --- geography (PostGIS): the routes below answer {"available": false, "reason": ...} where PostGIS is not installed ---------
# They must be declared before /api/sensors/{sensor_id}, which would otherwise take "nearby" and "geojson" for a sensor id.

@app.get("/api/sensors/nearby", dependencies=[Depends(require("city:read"))])
def sensors_nearby(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
                   radius_m: float = Query(1000, gt=0, le=1_000_000), limit: int = Query(20, ge=1, le=200), city: str | None = CityParam):
    """Sensors with a position within `radius_m` metres of (lat, lon), nearest first, each with `distance_m` (ST_DWithin and
    ST_Distance on geography). Optionally only one city."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            return geo.nearby(conn, lat, lon, radius_m, limit, city)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


@app.get("/api/sensors/geojson", dependencies=[Depends(require("city:read"))])
def sensors_geojson(state: str = Query("problem", pattern="^(problem|all|ok|stale|offline|fault|stuck)$"), limit: int = Query(500, ge=1, le=2000),
                    city: str | None = CityParam):
    """Sensors that have a position, as a GeoJSON FeatureCollection of points (`matching` counts all that fit the filter)."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            return geo.sensors_geojson(conn, city, state, limit)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


@app.get("/api/districts/geojson", dependencies=[Depends(require("city:read"))])
def districts_geojson(city: str | None = CityParam):
    """District polygons as a GeoJSON FeatureCollection. Each feature has the sensors inside it (decided by ST_Contains on the
    sensor's position), their counts per status and `health` (ok share, null with no sensor inside). Only cities with sensors."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            return geo.districts_geojson(conn, city)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


@app.get("/api/insights", dependencies=[Depends(require("settings:manage"))])
def insights_route(order: str = "total", limit: int = 20):
    """Database insights (edb_stat_monitor or pg_stat_statements, edb_pg_tuner, query_advisor). Admin only: statement text can reveal
    table names and the shape of the workload. A source that is missing or not readable says so in the payload."""
    _api_role()
    order = order if order in ("total", "mean", "calls") else "total"
    try:
        with connect(retries=1) as conn:
            return kit_insights.insights_payload(conn, order, max(1, min(limit, 200)))
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


@app.get("/api/sensors", dependencies=[Depends(require("city:read"))])
def list_sensors(state: str = Query("problem", pattern="^(problem|all|ok|stale|offline|fault|stuck)$"),
                 type: str | None = Query(None, max_length=32), district: str | None = Query(None, max_length=24, pattern=DISTRICT_ID.pattern),
                 city: str | None = CityParam,
                 q: str | None = Query(None, max_length=64), limit: int = Query(100, ge=1, le=500),
                 attrs: str | None = Query(None, max_length=512, description='JSON object the sensor metadata must contain, e.g. {"vendor":"Aerolab"} (jsonb @>, GIN)'),
                 attrs_path: str | None = Query(None, max_length=256, description="SQL/JSON path the metadata must satisfy, e.g. $.calibration ? (@.offset > 0.1) (jsonb @?)")):
    """Sensors, worst and newest first. `attrs` filters on device metadata with containment (`attrs @> :attrs`, served by the GIN index);
    `attrs_path` with a jsonpath predicate (`attrs @? :path`, also served by it). Both may be combined with every other filter."""
    _api_role()
    if type is not None and type not in TYPES:
        raise HTTPException(422, "unknown sensor type")
    where, args = [], []
    if attrs is not None:
        try:
            wanted = json.loads(attrs)
        except ValueError as exc:
            raise HTTPException(422, "attrs must be a JSON object, e.g. {\"vendor\":\"Aerolab\"}") from exc
        if not isinstance(wanted, dict) or not wanted:
            raise HTTPException(422, "attrs must be a non-empty JSON object")
        where.append("attrs @> %s::jsonb")
        args.append(json.dumps(wanted))
    if attrs_path is not None:
        where.append("attrs @? %s::jsonpath")
        args.append(attrs_path)
    if state == "problem":
        where.append("status <> 'ok'")
    elif state != "all":
        where.append("status = %s")
        args.append(state)
    if type:
        where.append("type = %s")
        args.append(type)
    if district:
        where.append("district = %s")
        args.append(district)
    if city:
        where.append("city = %s")
        args.append(city)
    if q:
        where.append("position(%s in id) > 0")
        args.append(q.lower())
    sql = ("SELECT id, type, district, city, protocol, status, reason, last_ts, last_value, status_since, attrs FROM sensor"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY status_since DESC LIMIT %s")
    try:
        with connect() as conn:
            return conn.execute(sql, [*args, limit]).fetchall()
    except (psycopg.errors.SyntaxError, psycopg.errors.InvalidTextRepresentation, psycopg.errors.DataException) as exc:
        if attrs_path is None:
            raise
        raise HTTPException(422, "attrs_path is not a valid SQL/JSON path: " + str(exc).splitlines()[0][:160]) from exc


@app.get("/api/sensors/{sensor_id}", dependencies=[Depends(require("city:read"))])
def get_sensor(sensor_id: str):
    _api_role()
    if not _SENSOR_ID.match(sensor_id):
        raise HTTPException(404, "no such sensor")
    with connect() as conn:
        row = conn.execute("SELECT * FROM sensor WHERE id=%s", (sensor_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such sensor")
        row.pop("geom", None)                          # the geography column (PostGIS) is binary; lat and lon carry the position
        row["readings"] = conn.execute(
            "SELECT ts, value FROM reading WHERE sensor_id=%s ORDER BY ts DESC LIMIT 60", (sensor_id,)).fetchall()
    return row


# --- districts: time series, pattern memory, one query across all models -----------------------------------------------------------
# A district id is only unique within a city, so these routes need `city`. `/api/districts/geojson` (above) has one segment and never clashes.
DistrictPath = PathParam(max_length=24, pattern=DISTRICT_ID.pattern)
CityRequired = Query(..., max_length=32, pattern=CITY_PATTERN, description="The city the district belongs to")


@app.get("/api/districts/{district_id}/series", dependencies=[Depends(require("city:read"))])
def district_series(district_id: str = DistrictPath, city: str = CityRequired, bucket: str = Query("5m", pattern="^(5m|1h)$"),
                    start: datetime | None = Query(None, alias="from", description="ISO 8601; default 6 h (5m) or 48 h (1h) before `to`"),
                    to: datetime | None = Query(None, description="ISO 8601; default now"), type: str | None = Query(None, max_length=32)):
    """Roll-up series of one district (`date_bin` buckets of `reading`, kept in `rollup_5m` / `rollup_1h`): readings, distinct sensors and
    out-of-range readings per bucket, plus mean/min/max for one sensor `type`. At most 2100 buckets per request."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            return timeseries.series(conn, city, district_id, bucket, start, to, type)
    except timeseries.BadRange as exc:
        raise HTTPException(422, str(exc)) from exc
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


@app.get("/api/districts/{district_id}/similar", dependencies=[Depends(require("city:read"))])
def district_similar(district_id: str = DistrictPath, city: str = CityRequired, k: int = Query(5, ge=1, le=memory.SIMILAR_MAX),
                     scope: str = Query("all", pattern="^(all|city)$")):
    """The `k` most similar PAST episodes to this district's latest fingerprint (pgvector, cosine distance, HNSW), each with its outcome:
    the verdict an hour later and how long the bad stretch lasted. `available: false` with a reason where pgvector is not installed."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            out = memory.similar(conn, city, district_id, k, scope)
            conn.commit()
            return out
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


@app.get("/api/districts/{district_id}/explain", dependencies=[Depends(require("city:read"))])
def district_explain(district_id: str = DistrictPath, city: str = CityRequired, k: int = Query(3, ge=1, le=explain.K_MAX)):
    """One SQL statement, one round trip, five data models: current status counts, the last hour of the roll-up, the vendor or firmware
    over-represented among problem sensors, the nearest healthy sensors and neighbouring districts, and the most similar past episodes with
    what happened next. A model whose extension is missing answers `available: false` and the rest still comes back."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            out = explain.explain(conn, city, district_id, k)
            conn.commit()
            return out
    except explain.NoSuchDistrict as exc:
        raise HTTPException(404, str(exc)) from exc
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


class ToolCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/tools", dependencies=[Depends(require("city:read"))])
def list_tools():
    """Tool definitions (name, description, JSON Schema `input_schema`) an AIDB pipeline, MCP server or LLM agent can use. Read-only tools."""
    return {"tools": tools.listing()}


@app.post("/api/tools/{name}", dependencies=[Depends(require("city:read"))])
def call_tool(name: str, body: ToolCall):
    """Call one tool with `{"arguments": {...}}`; the arguments are checked against the tool's schema (422 when they do not fit)."""
    _api_role()
    try:
        with connect(retries=1) as conn:
            out = tools.call(conn, name, body.arguments)
            conn.commit()
            return out
    except tools.UnknownTool as exc:
        raise HTTPException(404, str(exc)) from exc
    except (tools.ToolError, timeseries.BadRange) as exc:
        raise HTTPException(422, str(exc)) from exc
    except explain.NoSuchDistrict as exc:
        raise HTTPException(404, str(exc)) from exc
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc


# --- bursts ----------------------------------------------------------------------------------------

class BurstBody(BaseModel):
    kind: str = Field(max_length=20)
    target: str = Field(default="all", max_length=40)
    intensity: int = Field(default=3, ge=1, le=5)
    seconds: int = Field(default=120, ge=5, le=chaos.MAX_SECONDS)
    city: str | None = Field(default=None, max_length=32, pattern=CITY_PATTERN)   # a district target must have reported in this city


class StormBody(BaseModel):
    seconds: int = Field(default=180, ge=5, le=chaos.MAX_SECONDS)
    city: str | None = Field(default=None, max_length=32, pattern=CITY_PATTERN)   # confine the storm to one city


@app.get("/api/chaos", dependencies=[Depends(require("city:read"))])
def get_chaos(city: str | None = CityParam):
    _api_role()
    with connect() as conn:
        return {"active": chaos.active(conn, city), "kinds": chaos.KINDS}


@app.post("/api/chaos", dependencies=[Depends(require("city:control"))])
def post_chaos(body: BurstBody):
    _api_role()
    try:
        with connect() as conn:
            return {"id": chaos.start(conn, body.kind, body.target, body.intensity, body.seconds, body.city)}
    except chaos.ChaosError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/chaos/storm", dependencies=[Depends(require("city:control"))])
def post_storm(body: StormBody):
    """The burst button: several faults at once."""
    _api_role()
    try:
        with connect() as conn:
            if body.city and not conn.execute("SELECT 1 FROM sensor WHERE city = %s LIMIT 1", (body.city,)).fetchone():
                raise chaos.ChaosError(f"no sensor reports for city {body.city!r}")
            return {"ids": chaos.storm(conn, body.seconds, city=body.city)}
    except chaos.ChaosError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/chaos/stop", dependencies=[Depends(require("city:control"))])
def post_stop():
    _api_role()
    with connect() as conn:
        return {"stopped": chaos.stop_all(conn)}
