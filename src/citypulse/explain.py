"""Explain this district: ONE SQL statement, ONE round trip, five data models.

  relational   current status counts of the district's sensors (and which sensor types are in trouble)
  time series  the last hour from the 5-minute roll-up (date_bin), readings, sensors reporting, out-of-range readings
  JSON         which vendor / firmware (sensor.attrs, GIN-indexed JSONB) is over-represented among the district's problem sensors
  geo          the nearest healthy sensors to the problem centroid and the three neighbouring districts (PostGIS; only where it exists)
  vector       the most similar PAST episodes by fingerprint and what happened next (pgvector; only where it exists)

The statement is assembled from fixed fragments (never from user input: city, district and k travel as parameters). A model whose extension
is missing contributes `NULL::jsonb` instead of its CTE and the answer says why, so the other four still come back. This is the substrate
of an agent's reasoning loop (see `tools.py` for the tool definition): one call gives the evidence for "what is wrong here, has it happened
before and what came of it", with no chain of round trips.
"""
from __future__ import annotations

import statistics
from datetime import UTC, datetime

from . import geo, memory
from .catalog import district_name
from .status import tier

K_MAX = 10
JSON_KEYS = ("vendor", "firmware")


class NoSuchDistrict(LookupError):
    pass


REL = """
rel AS (
  SELECT jsonb_build_object(
    'sensors', count(*),
    'with_attrs', count(*) FILTER (WHERE attrs IS NOT NULL),
    'counts', jsonb_build_object('ok', count(*) FILTER (WHERE status = 'ok'), 'stale', count(*) FILTER (WHERE status = 'stale'),
                                 'offline', count(*) FILTER (WHERE status = 'offline'), 'fault', count(*) FILTER (WHERE status = 'fault'),
                                 'stuck', count(*) FILTER (WHERE status = 'stuck')),
    'problem_types', (SELECT coalesce(jsonb_agg(jsonb_build_object('type', q.type, 'problems', q.n) ORDER BY q.n DESC, q.type), '[]'::jsonb)
                      FROM (SELECT type, count(*) AS n FROM sensor
                            WHERE city = %(city)s AND district = %(district)s AND status <> 'ok'
                            GROUP BY type ORDER BY n DESC, type LIMIT 3) q)
  ) AS j
  FROM sensor WHERE city = %(city)s AND district = %(district)s
)"""

TS = """
ts AS (
  SELECT jsonb_build_object('bucket', '5m', 'points', coalesce(jsonb_agg(jsonb_build_object(
           'bucket', q.bucket, 'readings', q.readings, 'sensors', q.sensors, 'out_of_range', q.oor) ORDER BY q.bucket), '[]'::jsonb)) AS j
  FROM (SELECT bucket, sum(readings)::int AS readings, sum(sensors)::int AS sensors, sum(out_of_range)::int AS oor
        FROM rollup_5m
        WHERE city = %(city)s AND district = %(district)s
          AND bucket >= date_bin('5 minutes', now() - interval '1 hour', TIMESTAMPTZ '2000-01-01')
        GROUP BY bucket) q
)"""

JS = """
attr AS (
  SELECT a.key, a.val, (s.district = %(district)s) AS here, (s.status <> 'ok') AS bad
  FROM sensor s CROSS JOIN LATERAL jsonb_each_text(s.attrs) AS a(key, val)
  WHERE s.city = %(city)s AND s.attrs IS NOT NULL AND a.key IN ('vendor', 'firmware')
), cnt AS (
  SELECT key, val, count(*) AS city_n, count(*) FILTER (WHERE here AND bad) AS problem_n
  FROM attr GROUP BY key, val
), tot AS (
  SELECT key, sum(city_n) AS city_total, sum(problem_n) AS problem_total FROM cnt GROUP BY key
), js AS (
  SELECT jsonb_build_object('basis', 'problem sensors of the district against all sensors of the city that report the attribute',
           'attributes', coalesce(jsonb_agg(jsonb_build_object(
             'key', q.key, 'value', q.val, 'problem_sensors', q.problem_n, 'city_sensors', q.city_n,
             'share_of_problems', q.sp, 'share_of_city', q.sc, 'lift', q.lift, 'over_represented', q.lift >= 1.5 AND q.problem_n >= 2
           ) ORDER BY q.lift DESC, q.problem_n DESC, q.key, q.val), '[]'::jsonb)) AS j
  FROM (SELECT c.key, c.val, c.problem_n, c.city_n,
               round(c.problem_n::numeric / t.problem_total, 3) AS sp, round(c.city_n::numeric / t.city_total, 3) AS sc,
               round((c.problem_n::numeric / t.problem_total) / (c.city_n::numeric / t.city_total), 2) AS lift
        FROM cnt c JOIN tot t USING (key) WHERE t.problem_total > 0 AND c.problem_n > 0
        ORDER BY 7 DESC, c.problem_n DESC LIMIT 5) q
)"""

GEO = """
centre AS (
  SELECT coalesce(p.g, a.g) AS g, CASE WHEN p.g IS NOT NULL THEN 'problem sensors' ELSE 'all located sensors' END AS basis
  FROM (SELECT ST_Centroid(ST_Collect(geom::geometry))::geography AS g FROM sensor
        WHERE city = %(city)s AND district = %(district)s AND status <> 'ok' AND geom IS NOT NULL) p,
       (SELECT ST_Centroid(ST_Collect(geom::geometry))::geography AS g FROM sensor
        WHERE city = %(city)s AND district = %(district)s AND geom IS NOT NULL) a
), geo AS (
  SELECT jsonb_build_object(
    'centre', CASE WHEN c.g IS NULL THEN NULL
                   ELSE jsonb_build_object('lat', round(ST_Y(c.g::geometry)::numeric, 5), 'lon', round(ST_X(c.g::geometry)::numeric, 5), 'basis', c.basis) END,
    'nearest_healthy', (SELECT coalesce(jsonb_agg(jsonb_build_object('sensor', n.id, 'type', n.type, 'district', n.district, 'distance_m', n.m)
                                                 ORDER BY n.m, n.id), '[]'::jsonb)
                        FROM (SELECT h.id, h.type, h.district, round(ST_Distance(h.geom, c.g))::int AS m FROM sensor h
                              WHERE c.g IS NOT NULL AND h.city = %(city)s AND h.status = 'ok' AND h.geom IS NOT NULL
                              ORDER BY h.geom <-> c.g, h.id LIMIT 5) n),
    'neighbours', (SELECT coalesce(jsonb_agg(jsonb_build_object('district', n.district, 'distance_m', n.m, 'sensors', n.total, 'health', n.health)
                                             ORDER BY n.m, n.district), '[]'::jsonb)
                   FROM (SELECT b.district, round(ST_Distance(ST_Centroid(b.geom)::geography, ST_Centroid(a.geom)::geography))::int AS m,
                                h.total, round(h.ok::numeric / NULLIF(h.total, 0), 3) AS health
                         FROM district_area a JOIN district_area b ON b.city = a.city AND b.district <> a.district
                         LEFT JOIN LATERAL (SELECT count(*) AS total, count(*) FILTER (WHERE status = 'ok') AS ok FROM sensor x
                                            WHERE x.city = b.city AND x.district = b.district) h ON true
                         WHERE a.city = %(city)s AND a.district = %(district)s
                         ORDER BY m, b.district LIMIT 3) n)
  ) AS j
  FROM centre c
)"""

# pgvector 0.8+ only: an HNSW index returns at most `hnsw.ef_search` candidates and the filters below are applied AFTER it, so a rare kind of
# episode (a few percent of the history) could come back empty. Iterative scans keep reading the index until k rows pass the filters. The
# setting travels inside the statement (set_config in a CTE that `cur` depends on, so it runs before the index scan starts): still one statement.
ITERATIVE = """
cfg AS MATERIALIZED (SELECT set_config('hnsw.iterative_scan', 'strict_order', true) AS v),"""

VEC = """
cur AS (
  SELECT e.id, e.window_end, e.verdict, e.health, e.embedding FROM episode e{CFG_FROM}
  WHERE e.city = %(city)s AND e.district = %(district)s AND e.embedding IS NOT NULL{CFG_WHERE} ORDER BY e.window_end DESC LIMIT 1
), sim AS (
  SELECT jsonb_build_object('window_end', cur.window_end, 'verdict', cur.verdict, 'health', cur.health,
                            'similar', coalesce(x.arr, '[]'::jsonb)) AS j
  FROM cur CROSS JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
             'city', q.city, 'district', q.district, 'window_end', q.window_end, 'verdict', q.verdict, 'distance', round(q.distance::numeric, 6),
             'next_hour_verdict', q.verdict_1h, 'episode_lasted_min', round(q.duration_min::numeric, 1),
             'resolved_after_min', round((extract(epoch FROM q.ended_at - q.window_end) / 60)::numeric, 1)) ORDER BY q.distance) AS arr
    FROM (SELECT * FROM (
            -- one row per past episode: a 2-hour incident leaves eight near-identical fingerprints, and the answer should list incidents
            SELECT DISTINCT ON (c.city, c.district, coalesce(c.episode_start, c.window_end)) c.*
            FROM (SELECT e.city, e.district, e.window_end, e.verdict, e.verdict_1h, e.duration_min, e.ended_at, e.episode_start,
                         e.embedding <=> cur.embedding AS distance
                  FROM episode e
                  WHERE e.id <> cur.id AND e.embedding IS NOT NULL AND e.verdict_1h IS NOT NULL AND e.window_end < now() - interval '1 hour'
                    AND NOT (e.city = %(city)s AND e.district = %(district)s AND e.window_end > now() - interval '3 hours')
                    AND (cur.verdict = 'operational' OR e.verdict <> 'operational')
                  ORDER BY e.embedding <=> cur.embedding LIMIT %(k)s * 10) c
            ORDER BY c.city, c.district, coalesce(c.episode_start, c.window_end), c.distance) d
          ORDER BY d.distance LIMIT %(k)s) q
  ) x
)"""


def build_sql(with_geo: bool, with_vector: bool, iterative: bool = False) -> str:
    """The one statement. Fragments of the models that are unavailable are left out and their column is NULL."""
    ctes = [REL, TS, JS]
    if with_geo:
        ctes.append(GEO)
    if with_vector:
        if iterative:
            ctes.append(ITERATIVE.rstrip(","))
        ctes.append(VEC.replace("{CFG_FROM}", ", cfg" if iterative else "").replace("{CFG_WHERE}", " AND cfg.v IS NOT NULL" if iterative else ""))
    return ("WITH" + ",".join(ctes) + "\nSELECT (SELECT j FROM rel) AS relational, (SELECT j FROM ts) AS timeseries, (SELECT j FROM js) AS json_attrs,\n"
            + ("       (SELECT j FROM geo) AS geo,\n" if with_geo else "       NULL::jsonb AS geo,\n")
            + ("       (SELECT j FROM sim) AS vector" if with_vector else "       NULL::jsonb AS vector"))


def _trend(points: list[dict]) -> dict:
    """Readings of the last 15 minutes against the average 15 minutes of the 45 before them, from the roll-up points."""
    if not points:
        return {"last_15m_readings": None, "previous_avg_15m_readings": None, "change": None}
    pts = sorted(points, key=lambda p: p["bucket"])
    last, before = pts[-3:], pts[:-3]
    now_n = sum(p["readings"] for p in last)
    if not before:
        return {"last_15m_readings": now_n, "previous_avg_15m_readings": None, "change": None}
    prev = sum(p["readings"] for p in before) / max(1, len(before)) * 3
    return {"last_15m_readings": now_n, "previous_avg_15m_readings": round(prev, 1), "change": round(now_n / prev - 1, 3) if prev else None}


def _prediction(similar: list[dict]) -> dict | None:
    """What the similar past episodes say will happen: how often each verdict followed, and the median length of the bad stretches."""
    if not similar:
        return None
    nexts: dict[str, int] = {}
    for s in similar:
        nexts[s["next_hour_verdict"]] = nexts.get(s["next_hour_verdict"], 0) + 1
    lasted = [s["episode_lasted_min"] for s in similar if s.get("episode_lasted_min") is not None]
    return {"based_on": len(similar), "verdict_one_hour_later": dict(sorted(nexts.items(), key=lambda kv: -kv[1])),
            "median_episode_minutes": round(statistics.median(lasted), 1) if lasted else None}


def explain(conn, city: str, district: str, k: int = 3) -> dict:
    """Run the one statement and shape the answer. The only round trip to the database after the two cached availability checks
    (PostGIS and pgvector, 30 s each) is the statement itself. Raises NoSuchDistrict."""
    k = max(1, min(int(k), K_MAX))
    geo_ok, geo_reason = geo.status(conn)
    vec_ok, vec_reason = memory.status(conn)
    sql = build_sql(geo_ok, vec_ok, vec_ok and memory.iterative_scan_supported())
    row = conn.execute(sql, {"city": city, "district": district, "k": k}).fetchone()
    rel = row["relational"]
    if not rel or not rel["sensors"]:
        raise NoSuchDistrict(f"no sensor reports for district {district!r} in city {city!r}")
    counts, total = rel["counts"], rel["sensors"]
    health = counts["ok"] / total
    points = (row["timeseries"] or {}).get("points", [])
    vector = row["vector"]
    out = {
        "city": city, "district": district, "name": district_name(district), "generated_at": datetime.now(UTC),
        "verdict": tier(health), "health": round(health, 4), "round_trips": 1, "models": 5,
        "relational": rel,
        "timeseries": {"source": "rollup_5m (date_bin)", "points": points, **_trend(points)},
        "json": row["json_attrs"],
        "geo": row["geo"] if geo_ok else {"available": False, "reason": geo_reason},
        "vector": ({"available": True, "current": {k_: vector[k_] for k_ in ("window_end", "verdict", "health")}, "similar": vector["similar"],
                    "prediction": _prediction(vector["similar"]), "metric": "cosine distance"} if vector
                   else {"available": True, "current": None, "similar": [], "prediction": None,
                         "note": "no fingerprint recorded for this district yet"}) if vec_ok else {"available": False, "reason": vec_reason},
        "sql": sql,
    }
    if out["geo"] is not None and geo_ok:
        out["geo"] = {"available": True, **out["geo"]}
    return out
