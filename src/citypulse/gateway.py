"""The ingest gateway: validate a batch of readings, apply backpressure, write it in one transaction.

Used by the HTTP routes and, in the `all` role, directly by the simulator. Bad items are rejected one by one and
counted (data quality is a signal), never failing the whole batch. When the gateway is saturated it answers
"overloaded" so senders back off and retry, which is what a real collection tier must do.
"""
import json
import math
import re
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg_pool import PoolTimeout

from . import chaos, metrics
from .catalog import CITY_ID, DISTRICT_ID, LEGACY_CITY, SENSOR_ID, TYPES
from .config import get_settings
from .db import connect, pool

MAX_BATCH = 1000
PROTOCOLS = {"http", "otlp", "direct", "modbus_tcp", "ethernet_ip", "ethercat", "profinet", "profibus"}
MAX_AGE, MAX_FUTURE, LATE_AFTER = timedelta(hours=24), timedelta(minutes=2), timedelta(minutes=5)


class Overloaded(Exception):
    """The gateway is saturated: retry later (HTTP 429)."""


class Unavailable(Exception):
    """The database cannot be reached (HTTP 503)."""


class BadBatch(ValueError):
    """The request as a whole is unusable (HTTP 422)."""


_slots_lock = threading.Lock()
_inflight = 0
_bursts = chaos.Cache(lambda: _load_bursts())
_known = {"count": 0, "at": 0.0}
MISMATCH = "city does not match the sensor's city"
CITY_REQUIRED = "city is required"
COORDS_TOGETHER = "lat and lon must be sent together"
COORDS_BAD = "lat must be within +-90 and lon within +-180 (decimal degrees)"
CITY_RESERVED = f"city {LEGACY_CITY!r} is reserved (legacy); use a real city id"
CITY_HINT = ('Every reading needs a "city" (a-z, 0-9, _ and -, up to 32 characters, e.g. "paris"). Fix: add "city": "<id>" to each '
             'reading (JSON); set the OTLP attribute city (resource or data point); built-in simulator: set CITYPULSE_CITY; '
             "the CityPulse Simulator or another sender: upgrade it or configure its city. See the Connect page.")
CITY_HINT_OTLP = "fix: set the OTLP attribute city on the resource or data point"


def _load_bursts() -> list[dict]:
    with connect(retries=1) as conn:
        return chaos.active(conn)


def _acquire() -> None:
    global _inflight
    with _slots_lock:
        if _inflight >= get_settings().ingest_concurrency:
            raise Overloaded("ingest gateway is saturated")
        _inflight += 1
    metrics.INFLIGHT.set(_inflight)


def _release() -> None:
    global _inflight
    with _slots_lock:
        _inflight -= 1
    metrics.INFLIGHT.set(_inflight)


def parse_ts(value, now: datetime) -> datetime:
    if value is None:
        return now
    if isinstance(value, bool):
        raise ValueError("bad timestamp")
    try:
        if isinstance(value, (int, float)):
            seconds = float(value) / 1000 if value > 1e11 else float(value)  # epoch seconds or milliseconds
            ts = datetime.fromtimestamp(seconds, UTC)
        else:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("bad timestamp") from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if ts < now - MAX_AGE:
        raise ValueError("timestamp older than 24 hours")
    if ts > now + MAX_FUTURE:
        raise ValueError("timestamp in the future")
    return min(ts, now)


def _coordinate(value, limit: float) -> float | None:
    """A finite number within +-limit, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
        return None
    return float(value)


def validate(item, now: datetime) -> tuple[str, str, str, datetime, float, str, str, float | None, float | None] | str:
    """A cleaned (sensor, type, district, ts, value, protocol, city, lat, lon) tuple, or the reason it was rejected.
    `city` is required: there is no default city. `lat` and `lon` are optional and come together (decimal degrees, WGS 84):
    both None when the sensor sent no position."""
    if not isinstance(item, dict):
        return "item is not an object"
    sensor, kind, district = item.get("sensor"), item.get("type"), item.get("district")
    if not isinstance(sensor, str) or not SENSOR_ID.match(sensor):
        return "bad sensor id"
    if kind not in TYPES:
        return "unknown sensor type"
    if not isinstance(district, str) or not DISTRICT_ID.match(district):
        return "bad district"
    city = item.get("city")
    if city is None:
        return CITY_REQUIRED
    if not isinstance(city, str) or not CITY_ID.match(city):
        return "bad city"
    if city == LEGACY_CITY:
        return CITY_RESERVED
    protocol = item.get("protocol", "http")
    if protocol not in PROTOCOLS:
        return "unknown protocol"
    lat = lon = None
    if item.get("lat") is not None or item.get("lon") is not None:
        if item.get("lat") is None or item.get("lon") is None:
            return COORDS_TOGETHER
        lat, lon = _coordinate(item["lat"], 90), _coordinate(item["lon"], 180)
        if lat is None or lon is None:
            return COORDS_BAD
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "value must be a number"
    value = float(value)
    if not math.isfinite(value) or abs(value) > 1e12:
        return "value out of range"
    try:
        ts = parse_ts(item.get("ts"), now)
    except ValueError as exc:
        return str(exc)
    return sensor, kind, district, ts, value, protocol, city, lat, lon


# ---- device metadata (sensor.attrs, JSONB) -----------------------------------------------------------------------------------
ATTRS_MAX_BYTES = 1024            # serialized size of one attrs object
ATTRS_MAX_KEYS, ATTRS_MAX_DEPTH, ATTRS_MAX_LIST, ATTRS_MAX_STR = 16, 2, 8, 120
ATTR_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Metadata describes a device (vendor, firmware, install date, calibration, tags); it is stored in clear text and readable by every
# `city:read` caller and by AgentPG's read-only role, so credentials do not belong in it. This is a tripwire for the obvious cases
# (key names and value shapes), not a secret scanner.
SECRET_KEY = re.compile(r"(^|_)(pass|password|passwd|secret|token|api_?key|credentials?|private_?key|bearer|cookie|session|auth|authorization|signature|certificate)(_|$)",
                        re.I)
SECRET_VALUE = re.compile(r"^(bearer\s|basic\s|cp_[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{10,}\.)"
                          r"|-----BEGIN |^[A-Za-z0-9+/=_-]{40,}$", re.I)
ATTRS_NOT_OBJECT = "attrs must be a JSON object"
ATTRS_SECRET = "attrs must not hold secrets (credential-like key or value)"


def _attr_problem(value, depth: int) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return None if math.isfinite(value) and abs(value) <= 1e15 else "attrs numbers must be finite"
    if isinstance(value, str):
        if len(value) > ATTRS_MAX_STR:
            return f"attrs strings are limited to {ATTRS_MAX_STR} characters"
        return ATTRS_SECRET if SECRET_VALUE.search(value) else None
    if isinstance(value, list):
        if depth >= ATTRS_MAX_DEPTH or len(value) > ATTRS_MAX_LIST:
            return f"attrs lists hold at most {ATTRS_MAX_LIST} items"
        if any(isinstance(v, (dict, list)) for v in value):
            return "attrs lists hold scalars only"
        return next((p for v in value if (p := _attr_problem(v, depth + 1))), None)
    if isinstance(value, dict):
        if depth >= ATTRS_MAX_DEPTH:
            return f"attrs nest at most {ATTRS_MAX_DEPTH} levels"
        if len(value) > ATTRS_MAX_KEYS:
            return f"attrs hold at most {ATTRS_MAX_KEYS} keys per object"
        for k, v in value.items():
            if not isinstance(k, str) or not ATTR_KEY.match(k):
                return "attrs keys must match ^[a-z][a-z0-9_]{0,39}$"
            if SECRET_KEY.search(k):
                return ATTRS_SECRET
            if (p := _attr_problem(v, depth + 1)):
                return p
        return None
    return "attrs hold JSON values only"


def check_attrs(value) -> dict | str:
    """The cleaned attrs object, or the reason it was rejected. At most 16 keys per object, two levels, 8-item lists of scalars, strings up
    to 120 characters, 1024 bytes in all, keys `[a-z][a-z0-9_]*`, no credential-looking key or value. `None` is not an object (omit the field)."""
    if not isinstance(value, dict):
        return ATTRS_NOT_OBJECT
    problem = _attr_problem(value, 0)
    if problem:
        return problem
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()) > ATTRS_MAX_BYTES:
        return f"attrs are limited to {ATTRS_MAX_BYTES} bytes"
    return value


def accept(items: list) -> dict:
    """Validate and store a batch. Returns {"accepted", "rejected", "errors"}, plus "hint" (how to fix the sender) when
    readings were rejected for lacking a city."""
    if not isinstance(items, list) or not items:
        raise BadBatch("send a non-empty list of readings")
    if len(items) > MAX_BATCH:
        raise BadBatch(f"at most {MAX_BATCH} readings per request")
    started = time.perf_counter()
    now = datetime.now(UTC)
    good, errors, rejected, attrs = [], {}, 0, {}
    for item in items:
        out = validate(item, now)
        if not isinstance(out, str) and item.get("attrs") is not None:   # optional device metadata: validated like the rest of the item
            checked = check_attrs(item["attrs"])
            if isinstance(checked, str):
                out = checked
            else:
                attrs[out[0]] = checked              # the last attrs a sensor sent in this batch win
        if isinstance(out, str):
            rejected += 1
            errors[out] = errors.get(out, 0) + 1
        else:
            good.append(out)
    try:
        _acquire()
    except Overloaded:
        metrics.BATCHES.labels("overloaded").inc()
        raise
    try:
        slow = [b.intensity for b in _bursts.get() if b.kind == "slow_db"]
        if slow:  # a struggling database: hold the slot while it "thinks"
            time.sleep(0.5 * max(slow))
        refused = _store(good, errors, attrs) if good else 0
    except Overloaded:
        metrics.BATCHES.labels("overloaded").inc()
        raise
    except (psycopg.Error, RuntimeError) as exc:
        metrics.BATCHES.labels("error").inc()
        raise Unavailable("database unavailable") from exc
    finally:
        _release()
    accepted, rejected = len(good) - refused, rejected + refused
    late = sum(1 for g in good if now - g[3] > LATE_AFTER)
    metrics.READINGS.labels("accepted").inc(accepted)
    for proto in {g[5] for g in good}:
        metrics.BY_PROTOCOL.labels(proto).inc(sum(1 for g in good if g[5] == proto))
    if rejected:
        metrics.READINGS.labels("rejected").inc(rejected)
    if late:
        metrics.READINGS.labels("late").inc(late)
    metrics.BATCHES.labels("ok").inc()
    metrics.BATCH_SECONDS.observe(time.perf_counter() - started)
    out = {"accepted": accepted, "rejected": rejected,
           "errors": [f"{n} x {msg}" for msg, n in sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]}
    if CITY_REQUIRED in errors:
        out["hint"] = CITY_HINT
    return out


def _store(good: list, errors: dict, attrs: dict | None = None) -> int:
    """Write the batch. Returns how many extra items were refused (city mismatch, sensor cap). `attrs` maps sensor id to its
    validated metadata object (only for the sensors that sent some)."""
    refused = 0
    attrs = attrs or {}
    try:
        with pool().connection(timeout=3) as conn:
            existing = {r["id"]: r["city"] for r in
                        conn.execute("SELECT id, city FROM sensor WHERE id = ANY(%s)", ([*{g[0] for g in good}],))}
            city_of = dict(existing)
            latest, mismatched, xy = {}, 0, {}
            for g in good:  # one row per sensor for the state upsert: the newest event time wins
                if city_of.setdefault(g[0], g[6]) != g[6]:   # a sensor's city is fixed by its first reading (also within one batch)
                    mismatched += 1
                    continue
                if g[7] is not None:                          # the position of a sensor is the last one it sent in this batch
                    xy[g[0]] = (g[7], g[8])
                cur = latest.get(g[0])
                if cur is None or g[3] >= cur[3]:
                    latest[g[0]] = g
            if mismatched:
                refused += mismatched
                errors[MISMATCH] = errors.get(MISMATCH, 0) + mismatched
                good = [g for g in good if city_of[g[0]] == g[6]]
            new = [s for s in latest if s not in existing]
            if new:
                room = max(0, get_settings().max_sensors - _sensor_count(conn))
                if len(new) > room:
                    drop = set(new[room:])
                    capped = sum(1 for g in good if g[0] in drop)
                    refused += capped
                    errors["sensor limit reached"] = errors.get("sensor limit reached", 0) + capped
                    good = [g for g in good if g[0] not in drop]
                    latest = {k: v for k, v in latest.items() if k not in drop}
            if good:
                conn.execute(
                    "INSERT INTO reading (sensor_id, ts, value) SELECT * FROM unnest(%s::text[], %s::timestamptz[], %s::float8[])",
                    ([g[0] for g in good], [g[3] for g in good], [g[4] for g in good]))
                rows = list(latest.values())
                cities = sorted({r[6] for r in rows})
                cols = [[r[k] for r in rows] for k in range(6)]
                # Optional columns travel only in batches that have some (the simulator sends a position every few dozen readings; attrs are sent on
                # first contact and on change), so the usual upsert never touches them and the sensor updates stay HOT.
                extra = []          # (column, array type, values per row, expression on the unnest alias u)
                if xy:
                    extra += [("lat", "float8", [xy[r[0]][0] if r[0] in xy else None for r in rows], "u.lat"),
                              ("lon", "float8", [xy[r[0]][1] if r[0] in xy else None for r in rows], "u.lon")]
                if attrs:
                    extra.append(("attrs", "text", [json.dumps(attrs[r[0]], separators=(",", ":")) if r[0] in attrs else None for r in rows],
                                  "u.attrs::jsonb"))
                head = "INSERT INTO sensor (id, type, district, last_ts, last_value, protocol, city" + "".join(f", {c}" for c, *_ in extra) + ") "
                tail = ("ON CONFLICT (id) DO UPDATE SET "
                        " last_value = CASE WHEN EXCLUDED.last_ts >= sensor.last_ts THEN EXCLUDED.last_value ELSE sensor.last_value END, "
                        " last_ts = greatest(EXCLUDED.last_ts, sensor.last_ts), last_received = now()"
                        + "".join(f", {c} = coalesce(EXCLUDED.{c}, sensor.{c})" for c, *_ in extra))
                sel = "".join(f", {e}" for *_, e in extra)
                arr = "".join(f", %s::{t}[]" for _, t, *_ in extra)
                alias = "".join(f", {c}" for c, *_ in extra)
                pos = [v for _, _, v, _ in extra]
                if len(cities) == 1:   # the usual batch: one city, passed as a scalar
                    conn.execute(head + "SELECT u.id, u.type, u.district, u.ts, u.val, u.proto, %s::text" + sel
                                 + " FROM unnest(%s::text[], %s::text[], %s::text[], %s::timestamptz[], %s::float8[], %s::text[]" + arr
                                 + ") AS u(id, type, district, ts, val, proto" + alias + ") " + tail, (cities[0], *cols, *pos))
                else:                  # several cities: a small dictionary plus an int index per row
                    index = {c: i + 1 for i, c in enumerate(cities)}
                    conn.execute(head + "SELECT u.id, u.type, u.district, u.ts, u.val, u.proto, (%s::text[])[u.ci]" + sel
                                 + " FROM unnest(%s::text[], %s::text[], %s::text[], %s::timestamptz[], %s::float8[], %s::text[]" + arr
                                 + ", %s::int[]) AS u(id, type, district, ts, val, proto" + alias + ", ci) " + tail,
                                 (cities, *cols, *pos, [index[r[6]] for r in rows]))
    except PoolTimeout as exc:
        raise Overloaded("database connections are exhausted") from exc
    return refused


def forget_sensor_count() -> None:
    """The sensor cap uses a cached count; call after sensors were deleted in bulk."""
    _known["at"] = 0.0


def _sensor_count(conn) -> int:
    if time.monotonic() - _known["at"] > 15:
        _known["count"] = conn.execute("SELECT count(*) AS n FROM sensor").fetchone()["n"]
        _known["at"] = time.monotonic()
    return _known["count"]
