import asyncio
import contextlib
import re
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__, auth, chaos, gateway, metrics, otlp, reporter, sim, status
from .auth import Principal
from .catalog import DISTRICTS, GROUPS, TYPES
from .config import get_settings
from .db import connect

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 2 * 1024 * 1024
COOKIE = "cp_session"
CSRF_HEADER = "x-citypulse-csrf"
_announced = False


def _announce_setup_code() -> None:
    global _announced
    if not _announced:
        _announced = True
        print("\n=== CityPulse first-run setup ===\n"
              "Open the app in your browser and enter this one-time code to create the admin password:\n"
              f"    {auth.setup_code()}\n"
              "(It changes every time the server restarts until setup is complete.)\n", file=sys.stderr, flush=True)


def _startup_tasks() -> None:
    try:
        with connect(retries=1) as conn:
            if auth.needs_setup(conn):
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
# unless the operator explicitly sets CITYPULSE_ALLOW_ANONYMOUS=true.

_FAIL_WINDOW, _FAIL_LIMIT = 60.0, 10
_failures: dict[str, list[float]] = {}


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def _rate_limited(client: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failures.get(client, []) if now - t < _FAIL_WINDOW]
    _failures[client] = recent
    return len(recent) >= _FAIL_LIMIT


def _record_failure(client: str) -> None:
    _failures.setdefault(client, []).append(time.monotonic())
    if len(_failures) > 10_000:
        _failures.clear()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else None


def _declared_length(request: Request) -> int:
    value = request.headers.get("content-length", "")
    return int(value) if value.isdigit() else 0


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _db_unavailable() -> HTTPException:
    return HTTPException(503, "database unavailable; it may be waking up, retry in a few seconds")


def authenticate(request: Request) -> Principal | None:
    """The caller's Principal, or None if no credential was presented. Raises 401/429 for bad credentials."""
    if get_settings().allow_anonymous:
        return Principal("anonymous", "full")
    client = _client(request)
    if _rate_limited(client):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    raw = _bearer(request)
    if raw is not None:
        if auth.static_key_matches(raw):
            return Principal("static", "full")
        if raw.startswith(auth.TOKEN_PREFIX):
            try:
                with connect(retries=1) as conn:
                    principal = auth.token_principal(conn, raw)
            except (psycopg.Error, RuntimeError) as exc:
                raise _db_unavailable() from exc
            if principal:
                return principal
        _record_failure(client)
        raise HTTPException(401, "invalid credentials")
    cookie = request.cookies.get(COOKIE)
    if cookie:
        try:
            with connect(retries=1) as conn:
                ok = auth.session_valid(conn, cookie)
        except (psycopg.Error, RuntimeError) as exc:
            raise _db_unavailable() from exc
        if ok:
            return Principal("session", "full")
    return None


def require(scope: str = "read"):
    """Dependency: the caller must hold `scope`. 'full' (admin session or full token) satisfies everything;
    other tokens must match exactly ('read' reads, 'ingest' pushes readings, 'metrics' scrapes /metrics)."""

    def dep(request: Request) -> Principal:
        principal = authenticate(request)
        if principal is None:
            raise HTTPException(401, "authentication required")
        if principal.scope != "full" and principal.scope != scope:
            raise HTTPException(403, f"this token cannot do that (it is a '{principal.scope}' token)")
        return principal

    return dep


class Guard(BaseHTTPMiddleware):
    """Records request metrics, applies the body cap and the CSRF check, and sets security headers."""

    async def dispatch(self, request, call_next):
        started = time.perf_counter()
        status_code = 500
        try:
            resp = await self._dispatch(request, call_next)
            status_code = resp.status_code
            return resp
        finally:
            route = request.scope.get("route")
            label = getattr(route, "path", None) or "other"
            if label != "/metrics":
                metrics.observe(request.method, label, status_code, started)

    async def _dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith(("/api/", "/v1/")) and _declared_length(request) > MAX_BODY:
            return JSONResponse({"detail": "request body too large (2 MB limit)"}, status_code=413)
        if (path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}
                and _bearer(request) is None and not request.headers.get(CSRF_HEADER)):
            # Cookie-authenticated (browser) writes must carry a custom header, which cross-site pages can't set.
            return JSONResponse({"detail": "missing CSRF header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return resp


app = FastAPI(title="CityPulse", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(Guard)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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


@app.get("/metrics", dependencies=[Depends(require("metrics"))])
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


def _start_session(conn, request: Request, response: Response) -> None:
    raw = auth.create_session(conn)
    response.set_cookie(COOKIE, raw, httponly=True, samesite="strict", secure=_is_https(request),
                        max_age=auth.SESSION_HOURS * 3600, path="/")


def _guard_attempts(request: Request) -> str:
    client = _client(request)
    if _rate_limited(client):
        raise HTTPException(429, "too many failed attempts; wait a minute")
    return client


@app.get("/api/session")
def session_state(request: Request):
    """Public: tells the UI which screen to show. Reveals no secrets."""
    _api_role()
    try:
        principal = authenticate(request)
        with connect(retries=1) as conn:
            setup = auth.needs_setup(conn) and not get_settings().allow_anonymous
            has_password = auth.admin_configured(conn)
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    if setup:
        _announce_setup_code()
    state = "setup_required" if setup else "logged_in" if principal else "logged_out"
    return {"state": state, "has_admin_password": has_password, "password_login": has_password,
            "key_login": auth.static_key_configured(), "version": __version__}


@app.post("/api/setup")
def setup(body: SetupBody, request: Request, response: Response):
    _api_role()
    client = _guard_attempts(request)
    try:
        with connect(retries=1) as conn:
            try:
                auth.complete_setup(conn, body.code, body.password)
            except PermissionError as exc:
                _record_failure(client)
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
    try:
        with connect(retries=1) as conn:
            ok = False
            if body.password is not None:
                ok = auth.login_password(conn, body.password)
            elif body.api_key is not None and auth.static_key_matches(body.api_key):
                ok = True
            if not ok:
                _record_failure(client)
                raise HTTPException(401, "invalid credentials")
            _start_session(conn, request, response)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    try:
        with connect(retries=1) as conn:
            auth.delete_session(conn, request.cookies.get(COOKIE))
    except (psycopg.Error, RuntimeError):
        pass  # the cookie is cleared regardless; the session row expires on its own
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/admin-password", dependencies=[Depends(require("full"))])
def admin_password(body: PasswordBody, request: Request):
    """Set the admin password (first time, when signed in with a static key) or change it."""
    _api_role()
    client = _guard_attempts(request)
    with connect() as conn:
        try:
            if auth.admin_configured(conn):
                if body.current is None:
                    raise HTTPException(422, "current password is required")
                auth.change_password(conn, body.current, body.new)
            else:
                auth.set_admin_password(conn, body.new)
        except PermissionError as exc:
            _record_failure(client)
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        conn.execute("DELETE FROM web_session")  # sign every browser out after a password change
        conn.commit()
    return {"ok": True, "signed_out_everywhere": True}


@app.get("/api/tokens", dependencies=[Depends(require("full"))])
def list_tokens():
    _api_role()
    with connect() as conn:
        return auth.list_tokens(conn)


@app.post("/api/tokens", dependencies=[Depends(require("full"))])
def create_token(body: TokenBody):
    """The token value is returned exactly once; only its hash is stored."""
    _api_role()
    with connect() as conn:
        token_id, raw = auth.create_token(conn, body.name.strip(), body.scope)
    return {"id": token_id, "token": raw, "scope": body.scope}


@app.delete("/api/tokens/{token_id}", dependencies=[Depends(require("full"))])
def revoke_token(token_id: int):
    _api_role()
    with connect() as conn:
        if not auth.revoke_token(conn, token_id):
            raise HTTPException(404, "no such active token")
    return {"ok": True}


# --- collection: push API and OTLP --------------------------------------------------------------

class ReadingsBody(BaseModel):
    readings: list[Any] = Field(max_length=gateway.MAX_BATCH)


@app.post("/api/readings", dependencies=[Depends(require("ingest"))])
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


@app.post("/v1/metrics", dependencies=[Depends(require("ingest"))])
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

@app.get("/api/catalog", dependencies=[Depends(require("read"))])
def catalog():
    return {
        "districts": [{"id": k, "name": v} for k, v in DISTRICTS.items()],
        "groups": GROUPS,
        "types": [{"key": t.key, "label": t.label, "unit": t.unit, "group": t.group, "interval": t.interval,
                   "lo": t.lo, "hi": t.hi} for t in TYPES.values()],
    }


@app.get("/api/status", dependencies=[Depends(require("read"))])
def get_status():
    _api_role()
    try:
        with connect(retries=1) as conn:
            s = status.summary(conn)
            s["sim"] = sim.heartbeat(conn)
            s["bursts"] = chaos.active(conn)
    except (psycopg.Error, RuntimeError) as exc:
        raise _db_unavailable() from exc
    return s


@app.get("/api/history", dependencies=[Depends(require("read"))])
def get_history(minutes: int = Query(60, ge=5, le=720)):
    _api_role()
    with connect() as conn:
        return {"health": status.history(conn, minutes), "throughput": status.throughput(conn, minutes)}


_SENSOR_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")


@app.get("/api/sensors", dependencies=[Depends(require("read"))])
def list_sensors(state: str = Query("problem", pattern="^(problem|all|ok|stale|offline|fault|stuck)$"),
                 type: str | None = Query(None, max_length=32), district: str | None = Query(None, max_length=24),
                 q: str | None = Query(None, max_length=64), limit: int = Query(100, ge=1, le=500)):
    _api_role()
    if type is not None and type not in TYPES:
        raise HTTPException(422, "unknown sensor type")
    where, args = [], []
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
    if q:
        where.append("position(%s in id) > 0")
        args.append(q.lower())
    sql = ("SELECT id, type, district, protocol, status, reason, last_ts, last_value, status_since FROM sensor"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY status_since DESC LIMIT %s")
    with connect() as conn:
        return conn.execute(sql, [*args, limit]).fetchall()


@app.get("/api/sensors/{sensor_id}", dependencies=[Depends(require("read"))])
def get_sensor(sensor_id: str):
    _api_role()
    if not _SENSOR_ID.match(sensor_id):
        raise HTTPException(404, "no such sensor")
    with connect() as conn:
        row = conn.execute("SELECT * FROM sensor WHERE id=%s", (sensor_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such sensor")
        row["readings"] = conn.execute(
            "SELECT ts, value FROM reading WHERE sensor_id=%s ORDER BY ts DESC LIMIT 60", (sensor_id,)).fetchall()
    return row


# --- bursts ----------------------------------------------------------------------------------------

class BurstBody(BaseModel):
    kind: str = Field(max_length=20)
    target: str = Field(default="all", max_length=40)
    intensity: int = Field(default=3, ge=1, le=5)
    seconds: int = Field(default=120, ge=5, le=chaos.MAX_SECONDS)


class StormBody(BaseModel):
    seconds: int = Field(default=180, ge=5, le=chaos.MAX_SECONDS)


@app.get("/api/chaos", dependencies=[Depends(require("read"))])
def get_chaos():
    _api_role()
    with connect() as conn:
        return {"active": chaos.active(conn), "kinds": chaos.KINDS}


@app.post("/api/chaos", dependencies=[Depends(require("full"))])
def post_chaos(body: BurstBody):
    _api_role()
    try:
        with connect() as conn:
            return {"id": chaos.start(conn, body.kind, body.target, body.intensity, body.seconds)}
    except chaos.ChaosError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/chaos/storm", dependencies=[Depends(require("full"))])
def post_storm(body: StormBody):
    """The burst button: several faults at once."""
    _api_role()
    try:
        with connect() as conn:
            return {"ids": chaos.storm(conn, body.seconds)}
    except chaos.ChaosError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/chaos/stop", dependencies=[Depends(require("full"))])
def post_stop():
    _api_role()
    with connect() as conn:
        return {"stopped": chaos.stop_all(conn)}
