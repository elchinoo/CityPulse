"""The status engine: judge every sensor, then roll the verdicts up per type, per district and overall.

Per sensor (first match wins):
  offline  no reading for 12 reporting intervals
  stale    no reading for 3 intervals
  fault    the latest value is outside the physical limits of its type
  stuck    five or more recent readings, all identical (continuous types only)
  ok       otherwise
Overall: operational >= 97% ok, degraded >= 85%, partial_outage >= 50%, else major_outage; a district below 50% ok
(with at least 20 sensors) also forces at least partial_outage.
"""
import asyncio
import sys
import time

import psycopg

from . import chaos
from .catalog import DISTRICTS, TYPES
from .config import get_settings
from .db import connect

STATUSES = ["ok", "stale", "offline", "fault", "stuck"]
TIERS = [(0.97, "operational"), (0.85, "degraded"), (0.5, "partial_outage"), (0.0, "major_outage")]
_RANK = {"operational": 0, "degraded": 1, "partial_outage": 2, "major_outage": 3}
TICK_SECONDS, HISTORY_EVERY, PRUNE_EVERY = 10, 30, 60
_last = {"history": 0.0, "prune": 0.0}


def tier(ratio: float) -> str:
    return next(name for floor, name in TIERS if ratio >= floor)


def evaluate(conn) -> int:
    """Re-judge every sensor. Returns how many changed status."""
    types = list(TYPES.values())
    return conn.execute(
        """
        WITH t AS (
          SELECT * FROM unnest(%s::text[], %s::float8[], %s::float8[], %s::int[], %s::bool[]) AS t(type, lo, hi, itv, stuckable)
        ), stuck AS (
          SELECT r.sensor_id FROM reading r
          JOIN sensor s ON s.id = r.sensor_id JOIN t ON t.type = s.type
          WHERE t.stuckable AND r.ts > now() - make_interval(secs => t.itv * 6)
          GROUP BY r.sensor_id HAVING count(*) >= 5 AND min(r.value) = max(r.value)
        ), judged AS (
          SELECT s.id,
            CASE WHEN s.last_ts < now() - make_interval(secs => t.itv * 12) THEN 'offline'
                 WHEN s.last_ts < now() - make_interval(secs => t.itv * 3) THEN 'stale'
                 WHEN s.last_value < t.lo OR s.last_value > t.hi THEN 'fault'
                 WHEN s.id IN (SELECT sensor_id FROM stuck) THEN 'stuck'
                 ELSE 'ok' END AS status,
            CASE WHEN s.last_ts < now() - make_interval(secs => t.itv * 12) THEN 'no reading for 12+ intervals'
                 WHEN s.last_ts < now() - make_interval(secs => t.itv * 3) THEN 'no reading for 3+ intervals'
                 WHEN s.last_value < t.lo OR s.last_value > t.hi THEN 'value outside physical limits'
                 WHEN s.id IN (SELECT sensor_id FROM stuck) THEN 'identical readings'
                 ELSE NULL END AS reason
          FROM sensor s JOIN t ON t.type = s.type
        )
        UPDATE sensor s SET status = j.status, reason = j.reason,
               status_since = CASE WHEN s.status IS DISTINCT FROM j.status THEN now() ELSE s.status_since END
        FROM judged j
        WHERE s.id = j.id AND (s.status IS DISTINCT FROM j.status OR s.reason IS DISTINCT FROM j.reason)
        """,
        ([t.key for t in types], [t.lo for t in types], [t.hi for t in types],
         [t.interval for t in types], [t.stuckable for t in types]),
    ).rowcount


def _bucket(rows: list[dict], key: str, label) -> list[dict]:
    out: dict[str, dict] = {}
    for r in rows:
        k = r[key]
        o = out.setdefault(k, {"key": k, "label": label(k), "total": 0, **{s: 0 for s in STATUSES}})
        o["total"] += r["n"]
        o[r["status"]] += r["n"]
    return sorted(out.values(), key=lambda o: o["key"])


def summary(conn) -> dict:
    """Counts per status, per type, per district, the district x type matrix, and the overall verdict."""
    rows = conn.execute("SELECT district, type, status, count(*) AS n FROM sensor GROUP BY 1,2,3").fetchall()
    counts = {s: 0 for s in STATUSES}
    matrix: dict[tuple, dict] = {}
    for r in rows:
        counts[r["status"]] += r["n"]
        m = matrix.setdefault((r["district"], r["type"]), {"district": r["district"], "type": r["type"], "total": 0, "ok": 0})
        m["total"] += r["n"]
        m["ok"] += r["n"] if r["status"] == "ok" else 0
    total = sum(counts.values())
    health = counts["ok"] / total if total else 1.0
    by_type = _bucket(rows, "type", lambda k: TYPES[k].label if k in TYPES else k)
    by_district = _bucket(rows, "district", lambda k: DISTRICTS.get(k, k))
    overall = tier(health) if total else "operational"
    reasons = []
    for d in by_district:
        if d["total"] >= 20 and d["ok"] / d["total"] < 0.5:
            overall = max(overall, "partial_outage", key=_RANK.get)
            reasons.append(f"{d['label']} is {round(100 * d['ok'] / d['total'])}% healthy")
    for t in by_type:
        if t["total"] >= 5 and t["ok"] / t["total"] < 0.85:
            reasons.append(f"{t['label']}: {round(100 * t['ok'] / t['total'])}% healthy")
    if total and health < 0.97:
        reasons.insert(0, f"fleet is {round(100 * health, 1)}% healthy ({total - counts['ok']} of {total} sensors have a problem)")
    return {"state": overall, "health": health, "total": total, "counts": counts, "types": by_type,
            "districts": by_district, "matrix": list(matrix.values()), "reasons": reasons[:8]}


def history(conn, minutes: int) -> list[dict]:
    return conn.execute(
        "SELECT ts, total, ok FROM status_history WHERE scope='overall' AND ts > now() - make_interval(mins => %s) ORDER BY ts",
        (minutes,)).fetchall()


def throughput(conn, minutes: int) -> list[dict]:
    """Readings per 30 s bucket, by event time (late data lands in older buckets, which is itself a signal)."""
    return conn.execute(
        "SELECT date_bin('30 seconds', ts, TIMESTAMPTZ '2000-01-01') AS ts, count(*) AS n FROM reading "
        "WHERE ts > now() - make_interval(mins => %s) GROUP BY 1 ORDER BY 1", (minutes,)).fetchall()


def snapshot_history(conn, s: dict) -> None:
    rows = [("overall", s["total"], s["counts"]["ok"])]
    rows += [(f"district:{d['key']}", d["total"], d["ok"]) for d in s["districts"]]
    rows += [(f"type:{t['key']}", t["total"], t["ok"]) for t in s["types"]]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO status_history (ts, scope, total, ok) VALUES (date_trunc('second', now()), %s,%s,%s) "
                        "ON CONFLICT DO NOTHING", rows)


def prune(conn) -> None:
    hours = get_settings().retention_hours
    while conn.execute(
            "DELETE FROM reading WHERE ctid IN (SELECT ctid FROM reading WHERE ts < now() - %s * interval '1 hour' LIMIT 50000)",
            (hours,)).rowcount >= 50000:
        conn.commit()
    conn.execute("DELETE FROM status_history WHERE ts < now() - %s * interval '1 hour'", (hours,))
    conn.execute("DELETE FROM sensor WHERE last_ts < now() - interval '24 hours'")  # decommissioned
    chaos.prune(conn)


def tick() -> bool:
    """One engine cycle. Safe to run on several replicas: only the holder of the advisory lock works."""
    with connect(retries=1) as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(727402) AS ok").fetchone()["ok"]:
            return False
        evaluate(conn)
        conn.commit()
        now = time.monotonic()
        if now - _last["history"] >= HISTORY_EVERY:
            _last["history"] = now
            snapshot_history(conn, summary(conn))
        if now - _last["prune"] >= PRUNE_EVERY:
            _last["prune"] = now
            prune(conn)
        conn.commit()
    return True


async def run_forever() -> None:
    while True:
        try:
            await asyncio.to_thread(tick)
        except (psycopg.Error, RuntimeError) as exc:  # database asleep or restarting: keep trying
            print(f"status engine: {str(exc).splitlines()[0][:160]}", file=sys.stderr, flush=True)
        await asyncio.sleep(TICK_SECONDS)
