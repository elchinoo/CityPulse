"""Time series on core Postgres: per-district roll-ups of `reading` at 5 minutes and 1 hour, built with `date_bin`, kept in tables the
app refreshes incrementally, and the series API behind `GET /api/districts/{id}/series`.

No extension is involved (no TimescaleDB, no ClickHouse: neither exists on AgentPG). What a real time-series workload needs, in plain Postgres:

* the raw stream stays in `reading` (BRIN on ts, retention by batched DELETE: GUIDE 16, "why reading is not partitioned");
* roll-ups are keyed by `(city, district, type, bucket)`; `rollup_5m` is declaratively partitioned by month (created on demand, dropped
  whole after `ROLLUP_5M_DAYS`), `rollup_1h` is a plain table kept for `ROLLUP_1H_DAYS`;
* refreshing recomputes only the last few buckets from the raw rows and upserts them (late data lands in an old bucket: the refresh horizon
  covers it), and never touches a bucket whose raw rows may already have been pruned.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import psycopg

from .catalog import TYPES
from .config import get_settings

log = logging.getLogger("citypulse.timeseries")

ORIGIN = "TIMESTAMPTZ '2000-01-01'"           # fixed origin of every date_bin: buckets are the same everywhere, in every process
BUCKETS = {"5m": ("rollup_5m", "5 minutes", timedelta(minutes=5)), "1h": ("rollup_1h", "1 hour", timedelta(hours=1))}
HORIZON = {"5m": timedelta(minutes=30), "1h": timedelta(hours=2)}      # how far back a refresh recomputes
ROLLUP_5M_DAYS, ROLLUP_1H_DAYS = 35, 400
MAX_POINTS = 2100                                                       # a series is at most this many buckets (7 days of 5m)
DEFAULT_SPAN = {"5m": timedelta(hours=6), "1h": timedelta(hours=48)}

COLUMNS = "(city, district, type, bucket, readings, sensors, out_of_range, avg_value, min_value, max_value)"
SET = ", ".join(f"{c} = EXCLUDED.{c}" for c in ("readings", "sensors", "out_of_range", "avg_value", "min_value", "max_value"))


class BadRange(ValueError):
    """The requested series is unusable (HTTP 422)."""


def partition_name(year: int, month: int) -> str:
    return f"rollup_5m_{year:04d}{month:02d}"


def _months(start: datetime, end: datetime) -> list[tuple[int, int]]:
    y, m, out = start.year, start.month, []
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def ensure_partitions(conn, start: datetime, end: datetime) -> list[str]:
    """Create the monthly partitions of rollup_5m that cover [start, end] and the following month. Idempotent. Returns the partition names."""
    names = []
    for year, month in _months(start.astimezone(UTC), (end.astimezone(UTC) + timedelta(days=32)).replace(day=1)):
        name = partition_name(year, month)
        lo = datetime(year, month, 1, tzinfo=UTC)
        hi = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=UTC)
        # DDL cannot take parameters; the bounds are datetimes built here, never user input
        conn.execute(f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF rollup_5m FOR VALUES FROM ('{lo.isoformat()}') TO ('{hi.isoformat()}')")
        names.append(name)
    return names


def ensure(conn) -> dict:
    """After-migrate hook (idempotent, every start): the partitions for this month and the next two."""
    conn.commit()
    now = datetime.now(UTC)
    try:
        names = ensure_partitions(conn, now, now + timedelta(days=31))
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        log.warning("roll-up partitions could not be created: %s", str(exc).splitlines()[0][:160])
        return {"partitions": []}
    return {"partitions": names}


def _limits() -> tuple[list[str], list[float], list[float]]:
    types = list(TYPES.values())
    return [t.key for t in types], [t.lo for t in types], [t.hi for t in types]


def refresh(conn, bucket: str = "5m", now: datetime | None = None) -> int:
    """Recompute the newest buckets of one roll-up from `reading` and upsert them. Returns the number of rows written.

    The window starts `HORIZON` back, but never earlier than the first whole bucket still inside the retention of `reading`: a bucket whose
    oldest rows were already pruned would otherwise be overwritten with a smaller count."""
    table, width, step = BUCKETS[bucket]
    now = now or datetime.now(UTC)
    retention = timedelta(hours=get_settings().retention_hours)
    if bucket == "5m":
        ensure_partitions(conn, now - HORIZON[bucket], now)
    keys, lo, hi = _limits()
    sql = (
        f"INSERT INTO {table} {COLUMNS} "
        f"SELECT s.city, s.district, s.type, date_bin('{width}', r.ts, {ORIGIN}) AS bucket, count(*), count(DISTINCT r.sensor_id), "
        "       count(*) FILTER (WHERE r.value < t.lo OR r.value > t.hi), avg(r.value), min(r.value), max(r.value) "
        "FROM reading r JOIN sensor s ON s.id = r.sensor_id "
        "JOIN unnest(%(keys)s::text[], %(lo)s::float8[], %(hi)s::float8[]) AS t(type, lo, hi) ON t.type = s.type "
        f"WHERE r.ts >= greatest(date_bin('{width}', %(now)s::timestamptz - %(horizon)s, {ORIGIN}), "
        f"                      date_bin('{width}', %(now)s::timestamptz - %(retention)s, {ORIGIN}) + %(step)s) "
        "  AND r.ts < %(now)s "
        f"GROUP BY 1, 2, 3, 4 ON CONFLICT (city, district, type, bucket) DO UPDATE SET {SET}")
    return conn.execute(sql, {"keys": keys, "lo": lo, "hi": hi, "now": now, "horizon": HORIZON[bucket], "retention": retention,
                              "step": step}).rowcount


def prune(conn, now: datetime | None = None) -> dict:
    """Drop whole monthly partitions of rollup_5m that lie entirely past retention, and delete old rows of the rest and of rollup_1h."""
    now = now or datetime.now(UTC)
    cut5, cut1 = now - timedelta(days=ROLLUP_5M_DAYS), now - timedelta(days=ROLLUP_1H_DAYS)
    dropped = []
    for r in conn.execute("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                          "WHERE i.inhparent = 'rollup_5m'::regclass").fetchall():
        name = r["relname"]
        try:
            y, m = int(name[-6:-2]), int(name[-2:])
        except ValueError:
            continue
        month_end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=UTC)
        if name.startswith("rollup_5m_") and month_end <= cut5:
            conn.execute(f"DROP TABLE {name}")
            dropped.append(name)
    n5 = conn.execute("DELETE FROM rollup_5m WHERE bucket < %s", (cut5,)).rowcount
    n1 = conn.execute("DELETE FROM rollup_1h WHERE bucket < %s", (cut1,)).rowcount
    return {"dropped_partitions": dropped, "deleted_5m": n5, "deleted_1h": n1}


def tick(conn) -> None:
    """The engine's minute: refresh both roll-ups (a failure here must never stop the status engine)."""
    try:
        refresh(conn, "5m")
        refresh(conn, "1h")
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        log.warning("roll-up refresh failed: %s", str(exc).splitlines()[0][:160])


# ---- the series API ----------------------------------------------------------------------------------------------------------
def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def parse_range(bucket: str, start: datetime | None, end: datetime | None, now: datetime | None = None) -> tuple[datetime, datetime]:
    """(from, to) for a series; defaults to the last 6 hours (5m) or 48 hours (1h). Raises BadRange for an inverted or too long range."""
    if bucket not in BUCKETS:
        raise BadRange("bucket must be 5m or 1h")
    now = now or datetime.now(UTC)
    end = _utc(end) if end else now
    start = _utc(start) if start else end - DEFAULT_SPAN[bucket]
    if start >= end:
        raise BadRange("from must be before to")
    if (end - start) / BUCKETS[bucket][2] > MAX_POINTS:
        raise BadRange(f"at most {MAX_POINTS} buckets per series ({'7 days' if bucket == '5m' else '87 days'} at {bucket}); narrow from/to or use bucket=1h")
    return start, end


def series(conn, city: str, district: str, bucket: str, start: datetime | None = None, end: datetime | None = None,
           type: str | None = None) -> dict:
    """Roll-up points of one district, oldest first: readings, distinct sensors, readings outside the physical limits of their type and
    (for one `type`, whose unit makes it meaningful) the mean, minimum and maximum. Empty buckets are absent, not zero."""
    if type is not None and type not in TYPES:
        raise BadRange("unknown sensor type")
    start, end = parse_range(bucket, start, end)
    table = BUCKETS[bucket][0]
    one = type is not None
    rows = conn.execute(
        "SELECT bucket, sum(readings)::int AS readings, sum(sensors)::int AS sensors, sum(out_of_range)::int AS out_of_range"
        + (", sum(avg_value * readings) / NULLIF(sum(readings), 0) AS avg, min(min_value) AS min, max(max_value) AS max" if one else "")
        + f" FROM {table} WHERE city = %(city)s AND district = %(district)s AND bucket >= %(a)s AND bucket < %(b)s"
        + (" AND type = %(type)s" if one else "") + " GROUP BY bucket ORDER BY bucket",
        {"city": city, "district": district, "a": start, "b": end, "type": type}).fetchall()
    return {"city": city, "district": district, "bucket": bucket, "from": start, "to": end, "type": type, "count": len(rows),
            "source": f"{table} (date_bin roll-up of reading)", "points": rows}
