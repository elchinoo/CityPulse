"""Pattern memory: "have we seen this before?". A fingerprint of a district over a window is a fixed-size vector; pgvector finds the most
similar PAST fingerprints, and each past episode carries its outcome (the verdict an hour later, how long a bad stretch lasted).

THE FINGERPRINT (16 numbers, every one in [-1, 1], computed by `to_vector` from the raw statistics of one district over one window of
`WINDOW_SECS` = 900 s; cosine distance is used, so only the direction matters and the weights below are what makes a feature loud or quiet):

  dim  name           definition (sensors counted at the end of the window; "share" = count / sensors in the district)
   0   ok             share of sensors with status ok                                                       [0, 1]
   1   stale          sqrt(share of sensors stale)       \\
   2   offline        sqrt(share of sensors offline)      | sqrt lifts a few bad sensors above the noise floor: 1 % -> 0.10, 25 % -> 0.50
   3   fault          sqrt(share of sensors fault)        |
   4   stuck          sqrt(share of sensors stuck)       /
   5   delta_mean     mean over sensors of the mean |v[i] - v[i-1]| / (hi - lo) in the window, x 20, clipped to [0, 1]
   6   delta_p95      the 95th percentile of the same per-sensor figure, x 20, clipped to [0, 1]
   7   burst          share of the window during which a burst (chaos row) aimed at the district, its city or all was active   [0, 1]
   8   silent         share of sensors with no reading at all in the window                                  [0, 1]
   9   rate           readings received / readings expected (sum of window / interval over sensors), clipped to [0, 2], halved   [0, 1]
  10   overload       share of ingest batches answered 429 (process-wide, since the previous fingerprint; 0 when idle)  [0, 1]
  11   hour_sin       0.5 * sin(2 pi * hour_of_day / 24)       (UTC, fractional hour of the window end)
  12   hour_cos       0.5 * cos(2 pi * hour_of_day / 24)
  13   dow_sin        0.25 * sin(2 pi * day_of_week / 7)       (Monday 0, fractional: includes the hour)
  14   dow_cos        0.25 * cos(2 pi * day_of_week / 7)
  15   verdict        tier of the district's health (ok share) / 3: operational 0, degraded 1/3, partial_outage 2/3, major_outage 1

The time-of-day features are deliberately quieter (0.5 and 0.25) than the health features, so two incidents at different hours still look alike
while a quiet night and a quiet noon do not collapse to the same point. A healthy district's vector is dominated by dim 0 and the clock, so
"healthy at 03:00" matches "healthy at 03:00"; a troubled one is dominated by its bad shares and the load features.

STORAGE. `episode.features real[]` always (migration 007); with pgvector, `ensure()` adds a GENERATED `embedding vector(16)` and an HNSW
cosine index. Without pgvector fingerprints are still recorded and `/similar` and the explain card say so (`available: false`, with the reason).

WHEN. Every `WINDOW_SECS` (aligned to the quarter hour, trigger `schedule`) for every district, and whenever a district's verdict tier
changes (trigger `verdict_change`, window ending now). Outcomes are labelled later by `label()`.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg

from . import metrics
from .catalog import TYPES
from .status import tier

log = logging.getLogger("citypulse.memory")

DIM = 16
WINDOW_SECS = 900
VERDICTS = ["operational", "degraded", "partial_outage", "major_outage"]
FEATURES = ["ok", "stale", "offline", "fault", "stuck", "delta_mean", "delta_p95", "burst", "silent", "rate", "overload",
            "hour_sin", "hour_cos", "dow_sin", "dow_cos", "verdict"]
assert len(FEATURES) == DIM
MAX_DISTRICTS = 400            # districts fingerprinted per scheduled pass (the least healthy first)
MAX_CHANGES = 100              # verdict-change fingerprints per engine pass
SIMILAR_MAX = 20
FINAL_OUTCOME_AFTER = timedelta(hours=1)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def verdict_of(ok: int, total: int) -> str:
    """The district's verdict: the fleet's tiers applied to the share of its sensors that are ok (no sensors: operational)."""
    return tier(ok / total) if total else "operational"


def to_vector(stats: dict, window_end: datetime, overload: float = 0.0) -> list[float]:
    """The 16-number fingerprint (see the module docstring). `stats` has total, ok, stale, offline, fault, stuck, delta_mean, delta_p95
    (None when no sensor sent two readings), burst_share, silent and rate_ratio (None when nothing was expected)."""
    n = max(1, stats["total"])
    t = window_end.astimezone(UTC)
    hour = t.hour + t.minute / 60 + t.second / 3600
    dow = t.weekday() + hour / 24
    verdict = VERDICTS.index(verdict_of(stats["ok"], stats["total"]))
    vec = [
        stats["ok"] / n,
        math.sqrt(stats["stale"] / n), math.sqrt(stats["offline"] / n), math.sqrt(stats["fault"] / n), math.sqrt(stats["stuck"] / n),
        _clip(20 * (stats["delta_mean"] or 0.0)), _clip(20 * (stats["delta_p95"] or 0.0)),
        _clip(stats["burst_share"]),
        _clip(stats["silent"] / n),
        _clip(stats["rate_ratio"] if stats["rate_ratio"] is not None else 1.0, 0.0, 2.0) / 2,
        _clip(overload),
        0.5 * math.sin(2 * math.pi * hour / 24), 0.5 * math.cos(2 * math.pi * hour / 24),
        0.25 * math.sin(2 * math.pi * dow / 7), 0.25 * math.cos(2 * math.pi * dow / 7),
        verdict / 3,
    ]
    return [round(v, 6) for v in vec]


def explain_vector(vec: list[float]) -> dict:
    """The fingerprint with its dimension names (for the API and the docs)."""
    return dict(zip(FEATURES, vec))


# ---- process-wide 429 rate -----------------------------------------------------------------------------------------------------
_over = {"overloaded": 0.0, "total": 0.0}
_over_lock = threading.Lock()


def overload_rate() -> float:
    """Share of ingest batches answered 429 since the previous call (this process; 0 without traffic). Read once per fingerprint pass."""
    over = metrics.counter_value(metrics.BATCHES, result="overloaded")
    total = over + metrics.counter_value(metrics.BATCHES, result="ok") + metrics.counter_value(metrics.BATCHES, result="error")
    with _over_lock:
        d_over, d_total = over - _over["overloaded"], total - _over["total"]
        _over.update(overloaded=over, total=total)
    return d_over / d_total if d_total > 0 else 0.0


# ---- the schema hook: pgvector, guarded ----------------------------------------------------------------------------------------
_cache = {"at": 0.0, "ok": False, "reason": "", "iterative": False}
_cache_lock = threading.Lock()
NO_VECTOR = "pgvector is not available on this database (CREATE EXTENSION vector failed or was never run)"


def reset_cache() -> None:
    with _cache_lock:
        _cache.update(at=0.0, ok=False, reason="", iterative=False)


def _probe(conn) -> tuple[bool, str]:
    row = conn.execute(
        "SELECT (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS version, "
        "       EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') AS ext, "
        "       EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('episode') AND attname = 'embedding' AND NOT attisdropped) AS col").fetchone()
    if not row["ext"]:
        return False, NO_VECTOR
    if not row["col"]:
        return False, "pgvector is installed but the embedding column is not set up yet (it is created at startup)"
    _cache["iterative"] = _version_tuple(row["version"]) >= (0, 8)       # hnsw.iterative_scan exists from pgvector 0.8.0
    return True, ""


def _version_tuple(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in (version or "").split(".")[:2])
    except ValueError:
        return (0, 0)


def iterative_scan_supported() -> bool:
    """Whether the last availability check found pgvector 0.8 or newer (iterative HNSW scans). Reads the cache; call `status` first."""
    return bool(_cache["iterative"])


def status(conn, ttl: float = 30.0) -> tuple[bool, str]:
    """(available, reason): can similarity search run here? Cached for `ttl` seconds."""
    now = time.monotonic()
    with _cache_lock:
        if _cache["at"] and now - _cache["at"] < ttl:
            return _cache["ok"], _cache["reason"]
    try:
        ok, reason = _probe(conn)
    except psycopg.Error as exc:
        conn.rollback()
        ok, reason = False, f"vector check failed: {str(exc).splitlines()[0][:120]}"
    with _cache_lock:
        _cache.update(at=now, ok=ok, reason=reason)
    return ok, reason


SCHEMA = [
    # Generated from the plain array, so writers only ever write `features`; pgvector added later fills it for existing rows.
    "ALTER TABLE episode ADD COLUMN IF NOT EXISTS embedding vector(16) GENERATED ALWAYS AS (features::vector(16)) STORED",
    "CREATE INDEX IF NOT EXISTS episode_embedding_hnsw ON episode USING hnsw (embedding vector_cosine_ops)",
]


def ensure(conn) -> dict:
    """After-migrate hook (idempotent, every start): CREATE EXTENSION IF NOT EXISTS vector, the generated embedding column and the HNSW
    cosine index. Never raises for a missing extension. Returns {"available", "reason"}."""
    conn.commit()
    prev = conn.autocommit
    conn.autocommit = True                       # a failing CREATE EXTENSION must not poison the statements after it
    try:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error as exc:
            reason = f"{NO_VECTOR}: {str(exc).splitlines()[0][:120]}"
            log.info("vector memory skipped: %s", reason)
            return {"available": False, "reason": reason}
        try:
            for stmt in SCHEMA:
                conn.execute(stmt)
        except psycopg.Error as exc:
            reason = f"vector schema could not be set up: {str(exc).splitlines()[0][:120]}"
            log.warning(reason)
            return {"available": False, "reason": reason}
        ok, reason = _probe(conn)
        return {"available": ok, "reason": reason}
    finally:
        conn.autocommit = prev
        reset_cache()


# ---- computing fingerprints ----------------------------------------------------------------------------------------------------
STATS_SQL = """
WITH t AS (
  SELECT * FROM unnest(%(keys)s::text[], %(lo)s::float8[], %(hi)s::float8[], %(itv)s::int[]) AS t(type, lo, hi, itv)
), sens AS (
  SELECT s.id, s.city, s.district, s.status, t.lo, t.hi, t.itv
  FROM sensor s JOIN t ON t.type = s.type
  WHERE (%(cities)s::text[] IS NULL OR (s.city, s.district) IN (SELECT * FROM unnest(%(cities)s::text[], %(districts)s::text[])))
), d AS (
  SELECT r.sensor_id, abs(r.value - lag(r.value) OVER (PARTITION BY r.sensor_id ORDER BY r.ts)) / NULLIF(sn.hi - sn.lo, 0) AS dv
  FROM reading r JOIN sens sn ON sn.id = r.sensor_id
  WHERE r.ts >= %(t0)s AND r.ts < %(t1)s
), ps AS (
  SELECT sensor_id, count(*) AS n, avg(dv) AS mdv FROM d GROUP BY sensor_id
), agg AS (
  SELECT sn.city, sn.district, count(*)::int AS total,
         count(*) FILTER (WHERE sn.status = 'ok')::int AS ok, count(*) FILTER (WHERE sn.status = 'stale')::int AS stale,
         count(*) FILTER (WHERE sn.status = 'offline')::int AS offline, count(*) FILTER (WHERE sn.status = 'fault')::int AS fault,
         count(*) FILTER (WHERE sn.status = 'stuck')::int AS stuck,
         avg(ps.mdv) AS delta_mean, percentile_cont(0.95) WITHIN GROUP (ORDER BY ps.mdv) AS delta_p95,
         count(*) FILTER (WHERE ps.n IS NULL)::int AS silent,
         coalesce(sum(ps.n), 0)::float8 / NULLIF(sum(%(secs)s::float8 / sn.itv), 0) AS rate_ratio
  FROM sens sn LEFT JOIN ps ON ps.sensor_id = sn.id GROUP BY sn.city, sn.district
), bursts AS (
  SELECT a.city, a.district,
         least(1.0, sum(greatest(0, extract(epoch FROM least(c.until, %(t1)s::timestamptz) - greatest(c.started_at, %(t0)s::timestamptz)))) / %(secs)s) AS burst_share
  FROM agg a JOIN chaos c ON (c.target = 'all' OR c.target = 'city:' || a.city OR c.target = 'district:' || a.district)
                         AND c.until > %(t0)s AND c.started_at < %(t1)s
  GROUP BY a.city, a.district
)
SELECT agg.*, coalesce(b.burst_share, 0)::float8 AS burst_share FROM agg LEFT JOIN bursts b USING (city, district)
"""


def window_stats(conn, window_end: datetime, secs: int = WINDOW_SECS, pairs: list[tuple[str, str]] | None = None) -> list[dict]:
    """Raw statistics (the inputs of `to_vector`) for every district, or for the given (city, district) pairs, over the window ending at
    `window_end`. One SQL statement."""
    types = list(TYPES.values())
    return conn.execute(STATS_SQL, {
        "keys": [t.key for t in types], "lo": [t.lo for t in types], "hi": [t.hi for t in types], "itv": [t.interval for t in types],
        "cities": [p[0] for p in pairs] if pairs else None, "districts": [p[1] for p in pairs] if pairs else None,
        "t0": window_end - timedelta(seconds=secs), "t1": window_end, "secs": secs}).fetchall()


INSERT = ("INSERT INTO episode (city, district, window_end, window_secs, trigger, verdict, health, sensors, features) "
          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::real[]) ON CONFLICT (city, district, window_end) DO NOTHING")


def row_for(stats: dict, window_end: datetime, trigger: str, overload: float = 0.0, secs: int = WINDOW_SECS) -> tuple:
    total = stats["total"]
    return (stats["city"], stats["district"], window_end, secs, trigger, verdict_of(stats["ok"], total),
            stats["ok"] / total if total else 1.0, total, to_vector(stats, window_end, overload))


def record(conn, window_end: datetime, trigger: str, pairs: list[tuple[str, str]] | None = None) -> int:
    """Fingerprint the districts now and store the episodes. Returns how many rows were new."""
    stats = window_stats(conn, window_end, WINDOW_SECS, pairs)
    if pairs is None and len(stats) > MAX_DISTRICTS:   # a huge fleet: the districts most in trouble matter most
        stats = sorted(stats, key=lambda r: (r["ok"] / r["total"], r["city"], r["district"]))[:MAX_DISTRICTS]
    over = overload_rate()
    rows = [row_for(s, window_end, trigger, over) for s in stats if s["total"]]
    with conn.cursor() as cur:
        cur.executemany(INSERT, rows)
        return max(0, cur.rowcount) if rows else 0


# ---- the engine hook -----------------------------------------------------------------------------------------------------------
_state: dict = {"verdicts": None, "window": None}


def reset_state() -> None:
    _state.update(verdicts=None, window=None)


def district_verdicts(conn) -> dict[tuple[str, str], str]:
    rows = conn.execute("SELECT city, district, count(*)::int AS total, count(*) FILTER (WHERE status = 'ok')::int AS ok "
                        "FROM sensor GROUP BY city, district").fetchall()
    return {(r["city"], r["district"]): verdict_of(r["ok"], r["total"]) for r in rows}


def floor_window(now: datetime, secs: int = WINDOW_SECS) -> datetime:
    t = int(now.timestamp()) // secs * secs
    return datetime.fromtimestamp(t, UTC)


def tick(conn, now: datetime | None = None) -> dict:
    """Called by the status engine every 30 s (under its advisory lock, so one replica works). Fingerprints the districts whose verdict
    changed since the last call, and everything just after a quarter-hour boundary (labelling outcomes then). Never raises."""
    now = now or datetime.now(UTC)
    out = {"changed": 0, "scheduled": 0, "labelled": 0}
    try:
        verdicts = district_verdicts(conn)
        prev, _state["verdicts"] = _state["verdicts"], verdicts
        if prev is not None:
            changed = [k for k, v in verdicts.items() if k in prev and prev[k] != v][:MAX_CHANGES]
            if changed:
                out["changed"] = record(conn, now.replace(microsecond=0), "verdict_change", changed)
        boundary = floor_window(now)
        # Only just after the boundary: the sensors' statuses are read as they are NOW, which must still describe the window that ended.
        if _state["window"] != boundary and (now - boundary).total_seconds() < 120:
            _state["window"] = boundary
            out["scheduled"] = record(conn, boundary, "schedule")
            out["labelled"] = label(conn)
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        log.warning("pattern memory pass failed: %s", str(exc).splitlines()[0][:160])
    return out


# ---- labelling outcomes --------------------------------------------------------------------------------------------------------
LABEL_NEXT = """
UPDATE episode e SET verdict_1h = n.verdict
FROM (
  SELECT e2.id, (SELECT x.verdict FROM episode x
                 WHERE x.city = e2.city AND x.district = e2.district
                   AND x.window_end > e2.window_end AND x.window_end <= e2.window_end + interval '1 hour'
                 ORDER BY x.window_end DESC LIMIT 1) AS verdict
  FROM episode e2
  WHERE e2.verdict_1h IS NULL AND e2.window_end <= now() - interval '1 hour' AND e2.window_end > now() - %(since)s::interval
) n
WHERE e.id = n.id AND n.verdict IS NOT NULL
"""

# Gaps and islands. `grp` counts the operational fingerprints seen so far in a district, so every run of bad ones shares one `grp`
# (and the operational row that ends it opens the next). A run is only labelled when an operational row came before it inside the
# window (grp >= 1: its start is known) and one came after (its end is known).
LABEL_EPISODES = """
WITH base AS (
  SELECT id, city, district, window_end, verdict,
         sum((verdict = 'operational')::int) OVER (PARTITION BY city, district ORDER BY window_end) AS grp
  FROM episode WHERE window_end > now() - %(since)s::interval
), islands AS (
  SELECT city, district, grp, min(window_end) FILTER (WHERE verdict <> 'operational') AS started
  FROM base WHERE grp >= 1 GROUP BY city, district, grp
), ended AS (
  SELECT i.city, i.district, i.grp, i.started,
         (SELECT min(b.window_end) FROM base b WHERE b.city = i.city AND b.district = i.district AND b.grp = i.grp + 1) AS ended
  FROM islands i WHERE i.started IS NOT NULL
)
UPDATE episode e SET episode_start = en.started, ended_at = en.ended, duration_min = extract(epoch FROM en.ended - en.started) / 60
FROM base b JOIN ended en ON en.city = b.city AND en.district = b.district AND en.grp = b.grp
WHERE e.id = b.id AND b.verdict <> 'operational' AND en.ended IS NOT NULL AND e.ended_at IS NULL
"""


def label(conn, since: str = "7 days") -> int:
    """Fill in the outcomes of episodes: the verdict one hour later, and for a stretch of bad verdicts that has ended, when it began, when it
    ended and how long it lasted. Idempotent; only looks `since` back. Returns the number of rows updated."""
    n = conn.execute(LABEL_NEXT, {"since": since}).rowcount
    n += conn.execute(LABEL_EPISODES, {"since": since}).rowcount
    return n


# ---- similar episodes ----------------------------------------------------------------------------------------------------------
def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def latest(conn, city: str, district: str) -> dict | None:
    """The newest stored fingerprint of a district."""
    return conn.execute("SELECT id, city, district, window_end, verdict, health, sensors, features, trigger FROM episode "
                        "WHERE city = %s AND district = %s ORDER BY window_end DESC LIMIT 1", (city, district)).fetchone()


def similar(conn, city: str, district: str, k: int = 5, scope: str = "all") -> dict:
    """The `k` most similar PAST episodes to the district's latest fingerprint (cosine distance on pgvector, HNSW index), each with its
    outcome. Past means: older than an hour (the outcome is known) and not from this district within the last 3 hours (that is the same
    incident). While the district is in trouble only troubled episodes are offered. `scope` is `all` (every city) or `city`."""
    k = max(1, min(int(k), SIMILAR_MAX))
    ok, reason = status(conn)
    if not ok:
        return {"available": False, "reason": reason, "city": city, "district": district, "similar": []}
    cur = latest(conn, city, district)
    if not cur:
        return {"available": True, "city": city, "district": district, "current": None, "similar": [],
                "note": "no fingerprint has been recorded for this district yet (one is taken every 15 minutes and whenever its verdict changes)"}
    try:
        conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")      # pgvector 0.8+: keep scanning the index until enough rows pass the filters
    except psycopg.Error:
        conn.rollback()
    rows = conn.execute(SIMILAR_SQL, {"v": vector_literal(cur["features"]), "id": cur["id"], "city": city, "district": district,
                                       "same_city": scope == "city", "troubled": cur["verdict"] != "operational", "k": k}).fetchall()
    return {"available": True, "city": city, "district": district, "metric": "cosine distance (0 identical, 1 unrelated, 2 opposite)",
            "current": {"window_end": cur["window_end"], "verdict": cur["verdict"], "health": cur["health"],
                        "features": explain_vector(cur["features"])},
            "similar": [shape(r) for r in rows]}


SIMILAR_SQL = """
SELECT d.* FROM (
  -- one row per past episode: a 2-hour incident leaves eight near-identical fingerprints, and the answer should list incidents
  SELECT DISTINCT ON (c.city, c.district, coalesce(c.episode_start, c.window_end)) c.*
  FROM (SELECT e.city, e.district, e.window_end, e.verdict, e.health, e.verdict_1h, e.episode_start, e.ended_at, e.duration_min,
               e.embedding <=> %(v)s::vector AS distance
        FROM episode e
        WHERE e.id <> %(id)s AND e.embedding IS NOT NULL AND e.verdict_1h IS NOT NULL AND e.window_end < now() - interval '1 hour'
          AND NOT (e.city = %(city)s AND e.district = %(district)s AND e.window_end > now() - interval '3 hours')
          AND (NOT %(same_city)s OR e.city = %(city)s)
          AND (NOT %(troubled)s OR e.verdict <> 'operational')
        ORDER BY e.embedding <=> %(v)s::vector
        LIMIT %(k)s * 10) c
  ORDER BY c.city, c.district, coalesce(c.episode_start, c.window_end), c.distance) d
ORDER BY d.distance
LIMIT %(k)s
"""


def shape(r: dict) -> dict:
    """One similar-episode row as the API shows it."""
    resolved = (r["ended_at"] - r["window_end"]).total_seconds() / 60 if r.get("ended_at") else None
    return {"city": r["city"], "district": r["district"], "window_end": r["window_end"], "verdict": r["verdict"],
            "health": r["health"], "distance": round(r["distance"], 6),
            "outcome": {"verdict_1h": r["verdict_1h"], "episode_lasted_min": round(r["duration_min"], 1) if r["duration_min"] is not None else None,
                        "resolved_after_min": round(resolved, 1) if resolved is not None else None,
                        "episode_start": r["episode_start"], "ended_at": r["ended_at"]}}
