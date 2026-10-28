"""The ingest gateway: validate a batch of readings, apply backpressure, write it in one transaction.

Used by the HTTP routes and, in the `all` role, directly by the simulator. Bad items are rejected one by one and
counted (data quality is a signal), never failing the whole batch. When the gateway is saturated it answers
"overloaded" so senders back off and retry, which is what a real collection tier must do.
"""
import math
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


def accept(items: list) -> dict:
    """Validate and store a batch. Returns {"accepted", "rejected", "errors"}, plus "hint" (how to fix the sender) when
    readings were rejected for lacking a city."""
    if not isinstance(items, list) or not items:
        raise BadBatch("send a non-empty list of readings")
    if len(items) > MAX_BATCH:
        raise BadBatch(f"at most {MAX_BATCH} readings per request")
    started = time.perf_counter()
    now = datetime.now(UTC)
    good, errors, rejected = [], {}, 0
    for item in items:
        out = validate(item, now)
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
        refused = _store(good, errors) if good else 0
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


def _store(good: list, errors: dict) -> int:
    """Write the batch. Returns how many extra items were refused (city mismatch, sensor cap)."""
    refused = 0
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
                head, tail = "INSERT INTO sensor (id, type, district, last_ts, last_value, protocol, city) ", (
                    "ON CONFLICT (id) DO UPDATE SET "
                    " last_value = CASE WHEN EXCLUDED.last_ts >= sensor.last_ts THEN EXCLUDED.last_value ELSE sensor.last_value END, "
                    " last_ts = greatest(EXCLUDED.last_ts, sensor.last_ts), last_received = now()")
                cols = [[r[k] for r in rows] for k in range(6)]
                # Positions travel only in batches that have some (the simulator sends one every few dozen readings), so the usual
                # upsert never touches lat/lon and the sensor updates stay HOT.
                pos = [[xy[r[0]][k] if r[0] in xy else None for r in rows] for k in range(2)] if xy else []
                if pos:
                    head = head.replace("protocol, city) ", "protocol, city, lat, lon) ")
                    tail += ", lat = coalesce(EXCLUDED.lat, sensor.lat), lon = coalesce(EXCLUDED.lon, sensor.lon)"
                sel, arr, alias = (", u.lat, u.lon", ", %s::float8[], %s::float8[]", ", lat, lon") if pos else ("", "", "")
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
