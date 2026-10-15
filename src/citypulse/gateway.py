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
from .catalog import DISTRICT_ID, SENSOR_ID, TYPES
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


def validate(item, now: datetime) -> tuple[str, str, str, datetime, float, str] | str:
    """A cleaned (sensor, type, district, ts, value, protocol) tuple, or the reason it was rejected."""
    if not isinstance(item, dict):
        return "item is not an object"
    sensor, kind, district = item.get("sensor"), item.get("type"), item.get("district")
    if not isinstance(sensor, str) or not SENSOR_ID.match(sensor):
        return "bad sensor id"
    if kind not in TYPES:
        return "unknown sensor type"
    if not isinstance(district, str) or not DISTRICT_ID.match(district):
        return "bad district"
    protocol = item.get("protocol", "http")
    if protocol not in PROTOCOLS:
        return "unknown protocol"
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
    return sensor, kind, district, ts, value, protocol


def accept(items: list) -> dict:
    """Validate and store a batch. Returns {"accepted", "rejected", "errors"}."""
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
    return {"accepted": accepted, "rejected": rejected,
            "errors": [f"{n} x {msg}" for msg, n in sorted(errors.items(), key=lambda kv: -kv[1])[:5]]}


def _store(good: list, errors: dict) -> int:
    """Write the batch. Returns how many extra items were refused (sensor cap)."""
    latest = {}
    for g in good:  # one row per sensor for the state upsert: the newest event time wins
        cur = latest.get(g[0])
        if cur is None or g[3] >= cur[3]:
            latest[g[0]] = g
    refused = 0
    try:
        with pool().connection(timeout=3) as conn:
            known = {r["id"] for r in conn.execute("SELECT id FROM sensor WHERE id = ANY(%s)", ([*latest],))}
            new = [s for s in latest if s not in known]
            if new:
                room = max(0, get_settings().max_sensors - _sensor_count(conn))
                if len(new) > room:
                    drop = set(new[room:])
                    refused = sum(1 for g in good if g[0] in drop)
                    errors["sensor limit reached"] = errors.get("sensor limit reached", 0) + refused
                    good = [g for g in good if g[0] not in drop]
                    latest = {k: v for k, v in latest.items() if k not in drop}
            if good:
                conn.execute(
                    "INSERT INTO reading (sensor_id, ts, value) SELECT * FROM unnest(%s::text[], %s::timestamptz[], %s::float8[])",
                    ([g[0] for g in good], [g[3] for g in good], [g[4] for g in good]))
                rows = list(latest.values())
                conn.execute(
                    "INSERT INTO sensor (id, type, district, last_ts, last_value, protocol) "
                    "SELECT * FROM unnest(%s::text[], %s::text[], %s::text[], %s::timestamptz[], %s::float8[], %s::text[]) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    " last_value = CASE WHEN EXCLUDED.last_ts >= sensor.last_ts THEN EXCLUDED.last_value ELSE sensor.last_value END, "
                    " last_ts = greatest(EXCLUDED.last_ts, sensor.last_ts), last_received = now()",
                    ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
                     [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows]))
    except PoolTimeout as exc:
        raise Overloaded("database connections are exhausted") from exc
    return refused


def _sensor_count(conn) -> int:
    if time.monotonic() - _known["at"] > 15:
        _known["count"] = conn.execute("SELECT count(*) AS n FROM sensor").fetchone()["n"]
        _known["at"] = time.monotonic()
    return _known["count"]
