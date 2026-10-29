"""Geography: where the built-in cities are, the district polygons and sensor positions generated for them, and the PostGIS
queries behind the map (nearby sensors, district GeoJSON with health).

Everything here degrades. Without the PostGIS extension (plain Postgres in CI, a platform that does not offer it) the schema hook
logs and returns, readings that carry `lat`/`lon` are still stored in two plain columns, and every geographic query answers
`{"available": false, "reason": ...}` instead of failing. When PostGIS is installed later, the hook (it runs at every start) adds
the geography column and backfills it from those two columns.

Built-in geography (deterministic: nothing is random per run). Each city in CITIES has a real centre. Its twelve districts
(`d01`..`d12`, the built-in simulator's catalogue) are the cells of a 4 x 3 grid centred there, each cell about 1.4 km square. Grid
corners are shared by neighbouring cells and shifted by a fixed pseudo-random amount (a hash of city and corner), so the polygons
tile without gaps but are not rectangles. A sensor is placed from a hash of its id inside the middle half of its district's cell,
which can never reach the shifted edge, so it always lies inside its polygon.

Real boundaries. A city that has files in `geodata/` (see boundaries.py: Brussels with its 19 communes, Vienna with its 23 districts) gets
its REAL district polygons, stored by `ensure` with source 'boundary' (a real commune can be a MultiPolygon, so the column type is the
generic geometry(Geometry, 4326); an older table of Polygon is altered once). The simulator places each sensor inside the polygon
of its district, so `ST_Contains` agrees with the id the sensor reports. A boundary row replaces a 'derived' one of the same
district; it is never touched by the derived refresh, and districts without a real polygon (a sensor that reports another id)
still get a derived area.

Derived geography. A fleet that is not the built-in simulator (a real one, or an external simulator in a city that has no files in geodata/)
has no polygons, but its sensors report positions. `refresh_derived_areas` computes one area per (city, district) that has located
sensors and no built-in polygon: the convex hull of the sensors (ST_ConvexHull over ST_Collect), grown by a few metres of
geography buffer (ST_Buffer) so it is never razor-thin, or just a buffered point or line when the sensors are fewer than three or
collinear. It stores them with source 'derived'; built-in rows are never touched. Derived hulls of neighbouring districts can
overlap, so a sensor is counted in a derived district by the id it REPORTS (a sensor lies in two hulls at most by geometry, in one
by id); built-in districts keep the containment rule (ST_Contains), which cannot overlap because the cells tile.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time

import psycopg

from . import boundaries
from .catalog import DISTRICTS, district_name

log = logging.getLogger("citypulse.geo")

# Real city centres (latitude, longitude). Any other city id has no built-in geography: its sensors may still send lat/lon.
CITIES: dict[str, tuple[float, float]] = {
    "paris": (48.8566, 2.3522), "lyon": (45.7640, 4.8357), "marseille": (43.2965, 5.3698), "london": (51.5074, -0.1278),
    "berlin": (52.5200, 13.4050), "madrid": (40.4168, -3.7038), "rome": (41.9028, 12.4964), "amsterdam": (52.3676, 4.9041),
    "barcelona": (41.3874, 2.1686), "lisbon": (38.7223, -9.1393),
}
COLS, ROWS = 4, 3
CELL_LAT = 0.0125                  # degrees of latitude per cell, about 1.39 km
SHIFT = 0.18                       # corner shift, as a share of a cell
INNER = 0.5                        # sensors use the middle INNER x INNER of a cell
BUILTIN = "builtin"
BOUNDARY = "boundary"               # a real district polygon from geodata/ (OpenStreetMap, open data of the city)
DERIVED = "derived"
HULL_BUFFER_M = 40                 # grown around a hull of three or more points (metres, geography)
POINT_BUFFER_M = 75                # around a single point or a straight line of points
REFRESH_EVERY = 300.0              # seconds between derived-area refreshes (api role)


def _unit(*parts: object) -> float:
    """A stable number in [0, 1) from the parts (sha256, so it is the same in every process and on every machine)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _cell_lon(lat: float) -> float:
    return CELL_LAT / math.cos(math.radians(lat))


def has_geography(city: str) -> bool:
    return city in CITIES


def _corner(city: str, i: int, j: int) -> tuple[float, float]:
    """(lon, lat) of grid corner i (0..COLS) j (0..ROWS), shifted deterministically; shared by the cells that touch it."""
    clat, clon = CITIES[city]
    cell_lon = _cell_lon(clat)
    lon = clon + (i - COLS / 2) * cell_lon + (_unit(city, "x", i, j) - 0.5) * 2 * SHIFT * cell_lon
    lat = clat + (j - ROWS / 2) * CELL_LAT + (_unit(city, "y", i, j) - 0.5) * 2 * SHIFT * CELL_LAT
    return round(lon, 6), round(lat, 6)


def _cell(district: str) -> tuple[int, int] | None:
    ids = list(DISTRICTS)
    if district not in ids:
        return None
    k = ids.index(district)
    return k % COLS, k // COLS


def district_polygon(city: str, district: str) -> list[tuple[float, float]] | None:
    """The closed outer ring (counter-clockwise, as GeoJSON wants it) of a built-in district as (lon, lat), or None."""
    cell = _cell(district)
    if city not in CITIES or cell is None:
        return None
    i, j = cell
    ring = [_corner(city, i, j), _corner(city, i + 1, j), _corner(city, i + 1, j + 1), _corner(city, i, j + 1)]
    return [*ring, ring[0]]


def sensor_position(city: str, district: str, sensor_id: str) -> tuple[float, float] | None:
    """(lat, lon) of a built-in simulator sensor: inside its district's polygon, the same one every run. None for a city or
    district without built-in geography."""
    cell = _cell(district)
    if city not in CITIES or cell is None:
        return None
    i, j = cell
    clat, clon = CITIES[city]
    cell_lon = _cell_lon(clat)
    mid_lon = clon + (i + 0.5 - COLS / 2) * cell_lon
    mid_lat = clat + (j + 0.5 - ROWS / 2) * CELL_LAT
    lon = mid_lon + (_unit(sensor_id, "lon") - 0.5) * INNER * cell_lon
    lat = mid_lat + (_unit(sensor_id, "lat") - 0.5) * INNER * CELL_LAT
    return round(lat, 6), round(lon, 6)


def ring_contains(ring: list[tuple[float, float]], lon: float, lat: float) -> bool:
    """Even-odd point-in-polygon on a closed ring (used by tests and as the reference for what PostGIS ST_Contains must say)."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


# ---- database: schema hook ---------------------------------------------------------------------------------------------------
_cache = {"at": 0.0, "ok": False, "reason": ""}
_cache_lock = threading.Lock()
NO_POSTGIS = "PostGIS is not available on this database (CREATE EXTENSION postgis failed or was never run)"


def reset_cache() -> None:
    with _cache_lock:
        _cache.update(at=0.0, ok=False, reason="")


def _probe(conn) -> tuple[bool, str]:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis') AS ext, "
        "       to_regclass('district_area') IS NOT NULL AS area, "
        "       EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('sensor') AND attname = 'geom' AND NOT attisdropped) AS col").fetchone()
    if not row["ext"]:
        return False, NO_POSTGIS
    if not (row["area"] and row["col"]):
        return False, "PostGIS is installed but the geography schema is not set up yet (it is created at startup)"
    return True, ""


def status(conn, ttl: float = 30.0) -> tuple[bool, str]:
    """(available, reason). Cached for `ttl` seconds: it is asked on every map request."""
    now = time.monotonic()
    with _cache_lock:
        if _cache["at"] and now - _cache["at"] < ttl:
            return _cache["ok"], _cache["reason"]
    try:
        ok, reason = _probe(conn)
    except psycopg.Error as exc:
        conn.rollback()
        ok, reason = False, f"geography check failed: {str(exc).splitlines()[0][:120]}"
    with _cache_lock:
        _cache.update(at=now, ok=ok, reason=reason)
    return ok, reason


SCHEMA = [
    # Generated from the two plain columns, so a sender only ever writes lat/lon and the geography follows (NULL without a position).
    "ALTER TABLE sensor ADD COLUMN IF NOT EXISTS geom geography(Point, 4326) GENERATED ALWAYS AS "
    "(CASE WHEN lat IS NULL OR lon IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography END) STORED",
    # geography index: ST_DWithin / ST_Distance (nearby sensors). The geometry expression index serves ST_Contains (districts).
    "CREATE INDEX IF NOT EXISTS sensor_geom_gist ON sensor USING gist (geom)",
    "CREATE INDEX IF NOT EXISTS sensor_geom_geometry_gist ON sensor USING gist ((geom::geometry))",
    "CREATE TABLE IF NOT EXISTS district_area ("
    " city text NOT NULL CHECK (city ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),"
    " district text NOT NULL CHECK (district ~ '^[a-z0-9][a-z0-9_-]{0,23}$'),"
    " geom geometry(Geometry, 4326) NOT NULL,"
    " source text NOT NULL DEFAULT 'builtin',"
    " PRIMARY KEY (city, district))",
    # A table created by an earlier version holds Polygon only; real communes can be MultiPolygons. Altered once (the test is the column's
    # declared type), the GiST index is rebuilt by the ALTER.
    "DO $$ BEGIN IF (SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a WHERE a.attrelid = to_regclass('district_area') "
    "AND a.attname = 'geom' AND NOT a.attisdropped) = 'geometry(Polygon,4326)' THEN "
    "ALTER TABLE district_area ALTER COLUMN geom TYPE geometry(Geometry, 4326); END IF; END $$",
    "CREATE INDEX IF NOT EXISTS district_area_gist ON district_area USING gist (geom)",
]
UPSERT_AREA = (
    "INSERT INTO district_area (city, district, geom, source) VALUES (%s, %s, ST_GeomFromText(%s, 4326), %s) "
    "ON CONFLICT (city, district) DO UPDATE SET geom = EXCLUDED.geom "
    "WHERE district_area.source = %s AND NOT ST_OrderingEquals(district_area.geom, EXCLUDED.geom)")
# Real boundaries replace derived areas of the same district and refresh themselves (only a changed geometry or source is written).
UPSERT_BOUNDARY = (
    "INSERT INTO district_area (city, district, geom, source) VALUES (%s, %s, ST_GeomFromText(%s, 4326), 'boundary') "
    "ON CONFLICT (city, district) DO UPDATE SET geom = EXCLUDED.geom, source = 'boundary' "
    "WHERE district_area.source IN ('derived', 'boundary') "
    "AND (district_area.source <> 'boundary' OR NOT ST_OrderingEquals(district_area.geom, EXCLUDED.geom))")


# One statement. `grouped` has one row per (city, district) with located sensors whose ids fit the table's CHECK constraints; the hull
# is a Polygon only for >= 3 non-collinear points, otherwise a Point or LineString that gets the larger buffer. ST_Buffer on geography
# works in metres; ST_GeometryN(.., 1) is the polygon itself (a buffer is one polygon) and keeps the column type Polygon.
REFRESH_DERIVED = (
    "INSERT INTO district_area (city, district, geom, source) "
    "SELECT g.city, g.district, ST_GeometryN(ST_Buffer(g.hull::geography, "
    "CASE WHEN ST_GeometryType(g.hull) = 'ST_Polygon' THEN %(hull)s ELSE %(point)s END)::geometry, 1), 'derived' "
    "FROM (SELECT s.city, s.district, ST_ConvexHull(ST_Collect(s.geom::geometry)) AS hull FROM sensor s "
    "      WHERE s.geom IS NOT NULL AND s.city ~ '^[a-z0-9][a-z0-9_-]{0,31}$' AND s.district ~ '^[a-z0-9][a-z0-9_-]{0,23}$' "
    "      GROUP BY s.city, s.district) g "
    "WHERE NOT EXISTS (SELECT 1 FROM district_area b WHERE b.city = g.city AND b.district = g.district AND b.source IN ('builtin', 'boundary')) "
    "ON CONFLICT (city, district) DO UPDATE SET geom = EXCLUDED.geom "
    "WHERE district_area.source = 'derived' AND NOT ST_OrderingEquals(district_area.geom, EXCLUDED.geom)")
# Derived rows whose district has no located sensor any more (deleted city or sensors). Built-in rows are never removed.
PRUNE_DERIVED = (
    "DELETE FROM district_area a WHERE a.source = 'derived' "
    "AND NOT EXISTS (SELECT 1 FROM sensor s WHERE s.city = a.city AND s.district = a.district AND s.geom IS NOT NULL)")


def refresh_derived_areas(conn) -> dict:
    """Recompute the areas derived from sensor positions (see the module docstring). Cheap (one grouped scan of located sensors, writes
    only changed rows), idempotent, and quiet without PostGIS. Returns {"available", "written", "removed"}."""
    ok, _reason = status(conn)
    if not ok:
        return {"available": False, "written": 0, "removed": 0}
    try:
        written = conn.execute(REFRESH_DERIVED, {"hull": HULL_BUFFER_M, "point": POINT_BUFFER_M}).rowcount
        removed = conn.execute(PRUNE_DERIVED).rowcount
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        log.warning("derived areas not refreshed: %s", str(exc).splitlines()[0][:160])
        return {"available": True, "written": 0, "removed": 0}
    return {"available": True, "written": written, "removed": removed}


async def run_forever(every: float = REFRESH_EVERY) -> None:
    """Background loop for the api role: refresh the derived areas every few minutes in a worker thread, never blocking requests."""
    import asyncio

    from .db import connect
    while True:
        try:
            def once() -> None:
                with connect(retries=1) as conn:
                    refresh_derived_areas(conn)
            await asyncio.to_thread(once)
        except Exception as exc:  # noqa: BLE001 - database asleep or restarting: try again next round
            log.info("derived areas: %s", str(exc).splitlines()[0][:160])
        await asyncio.sleep(every)


def wkt(ring: list[tuple[float, float]]) -> str:
    return "POLYGON((" + ", ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in ring) + "))"


def builtin_areas() -> list[tuple[str, str, str]]:
    """(city, district, WKT) for every built-in city and district."""
    return [(c, d, wkt(district_polygon(c, d))) for c in CITIES for d in DISTRICTS]  # type: ignore[arg-type]


def ensure(conn) -> dict:
    """After-migrate hook (idempotent, run at every start): create the PostGIS extension if the platform lets us, add the geography
    column, the indexes and the district polygon table, and store the built-in polygons. Never raises for a missing extension.
    Returns {"available", "reason", "areas"}."""
    conn.commit()
    prev = conn.autocommit
    conn.autocommit = True                       # each statement on its own: a failing CREATE EXTENSION must not poison the rest
    try:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        except psycopg.Error as exc:
            reason = f"{NO_POSTGIS}: {str(exc).splitlines()[0][:120]}"
            log.info("geography skipped: %s", reason)
            return {"available": False, "reason": reason, "areas": 0}
        try:
            for stmt in SCHEMA:
                conn.execute(stmt)
            with conn.cursor() as cur:
                cur.executemany(UPSERT_AREA, [(c, d, w, BUILTIN, BUILTIN) for c, d, w in builtin_areas()])
                cur.executemany(UPSERT_BOUNDARY, boundaries.boundary_areas())
        except psycopg.Error as exc:
            reason = f"geography schema could not be set up: {str(exc).splitlines()[0][:120]}"
            log.warning(reason)
            return {"available": False, "reason": reason, "areas": 0}
        ok, reason = _probe(conn)
        if ok:
            try:
                conn.execute(REFRESH_DERIVED, {"hull": HULL_BUFFER_M, "point": POINT_BUFFER_M})   # once at start, after the built-in rows exist
                conn.execute(PRUNE_DERIVED)
            except psycopg.Error as exc:
                log.warning("derived areas not computed at start: %s", str(exc).splitlines()[0][:160])
        areas = conn.execute("SELECT count(*) AS n FROM district_area").fetchone()["n"] if ok else 0
        return {"available": ok, "reason": reason, "areas": areas}
    finally:
        conn.autocommit = prev
        reset_cache()


# ---- database: queries -------------------------------------------------------------------------------------------------------
def unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason}


def nearby(conn, lat: float, lon: float, radius_m: float, limit: int, city: str | None = None) -> dict:
    """Sensors within `radius_m` metres of a point, nearest first, with the distance in metres (ST_DWithin on geography uses the
    GiST index; ST_Distance is the exact spheroidal distance)."""
    ok, reason = status(conn)
    if not ok:
        return unavailable(reason)
    rows = conn.execute(
        "WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography AS g) "
        "SELECT s.id, s.type, s.district, s.city, s.status, s.lat, s.lon, round(ST_Distance(s.geom, p.g)::numeric, 1)::float8 AS distance_m "
        "FROM sensor s, p WHERE s.geom IS NOT NULL AND ST_DWithin(s.geom, p.g, %(radius)s) "
        "AND (%(city)s::text IS NULL OR s.city = %(city)s) ORDER BY ST_Distance(s.geom, p.g), s.id LIMIT %(limit)s",
        {"lat": lat, "lon": lon, "radius": radius_m, "city": city, "limit": limit}).fetchall()
    return {"available": True, "center": {"lat": lat, "lon": lon}, "radius_m": radius_m, "count": len(rows), "sensors": rows}


SEVERITY = ["offline", "fault", "stuck", "stale"]
AREAS_SQL = (
    "WITH located AS ("
    " SELECT s.id, s.city, s.status, s.district AS reported, a.district AS inside "
    " FROM sensor s LEFT JOIN district_area a ON a.city = s.city AND ((a.source <> 'derived' AND ST_Contains(a.geom, s.geom::geometry)) OR (a.source = 'derived' AND a.district = s.district)) "
    " WHERE s.geom IS NOT NULL AND (%(city)s::text IS NULL OR s.city = %(city)s)) "
    "SELECT a.city, a.district, ST_AsGeoJSON(a.geom, 6) AS geometry, a.source, count(l.id) AS total, "
    + ", ".join(f"count(l.id) FILTER (WHERE l.status = '{k}') AS {k}" for k in ("ok", *SEVERITY))
    + ", count(l.id) FILTER (WHERE l.reported <> a.district) AS reported_elsewhere "
    "FROM district_area a LEFT JOIN located l ON l.city = a.city AND l.inside = a.district "
    "WHERE (%(city)s::text IS NULL OR a.city = %(city)s) AND EXISTS (SELECT 1 FROM sensor x WHERE x.city = a.city) "
    "GROUP BY a.city, a.district, a.geom, a.source ORDER BY a.city, a.district")


def districts_geojson(conn, city: str | None = None) -> dict:
    """District polygons as a GeoJSON FeatureCollection with the health of the sensors inside each one. Which district a sensor is
    in is decided by ST_Contains on its position, not by the district id it reports; `reported_elsewhere` counts the sensors
    whose reported id differs. Only cities that have sensors are listed."""
    ok, reason = status(conn)
    if not ok:
        return {**unavailable(reason), "type": "FeatureCollection", "features": []}
    import json
    features = []
    for r in conn.execute(AREAS_SQL, {"city": city}).fetchall():
        total, n_ok = r["total"], r["ok"]
        features.append({
            "type": "Feature", "id": f"{r['city']}/{r['district']}", "geometry": json.loads(r["geometry"]),
            "properties": {"city": r["city"], "district": r["district"], "name": district_name(r["district"]), "source": r["source"],
                           "total": total, "ok": n_ok, "health": (n_ok / total) if total else None,
                           "worst_status": next((k for k in SEVERITY if r[k]), "ok") if total else None,
                           "counts": {k: r[k] for k in ("ok", *SEVERITY)}, "reported_elsewhere": r["reported_elsewhere"]}})
    without = conn.execute("SELECT count(*) AS n FROM sensor WHERE geom IS NULL AND (%(city)s::text IS NULL OR city = %(city)s)",
                           {"city": city}).fetchone()["n"]
    info = boundaries.meta().get(city) if city else None
    return {"available": True, "type": "FeatureCollection", "city": city, "features": features, "sensors_without_location": without,
            "attribution": info["attribution"] if info and any(f["properties"]["source"] == BOUNDARY for f in features) else None}


def sensors_geojson(conn, city: str | None, state: str, limit: int) -> dict:
    """Located sensors as GeoJSON points (`state` is 'problem', 'all' or one status), worst and newest problems first."""
    ok, reason = status(conn)
    if not ok:
        return {**unavailable(reason), "type": "FeatureCollection", "features": []}
    where = ["geom IS NOT NULL", "(%(city)s::text IS NULL OR city = %(city)s)"]
    if state == "problem":
        where.append("status <> 'ok'")
    elif state != "all":
        where.append("status = %(state)s")
    rows = conn.execute(
        "SELECT id, type, district, city, status, reason, lat, lon, count(*) OVER () AS matching FROM sensor WHERE " + " AND ".join(where)
        + " ORDER BY status_since DESC, id LIMIT %(limit)s", {"city": city, "state": state, "limit": limit}).fetchall()
    features = [{"type": "Feature", "id": r["id"], "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                 "properties": {"id": r["id"], "type": r["type"], "district": r["district"], "city": r["city"], "status": r["status"],
                                "reason": r["reason"]}} for r in rows]
    return {"available": True, "type": "FeatureCollection", "city": city, "state": state, "matching": rows[0]["matching"] if rows else 0,
            "features": features}
