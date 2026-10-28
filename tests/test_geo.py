"""Geography without a database: the built-in cities, district polygons and sensor positions are deterministic and consistent, the
ingest payload's lat/lon are validated, and the Python side of the PostGIS queries maps rows to valid GeoJSON (a fake connection)."""
import json
import math
import time
from datetime import UTC, datetime

import psycopg
import pytest

from citypulse import catalog, gateway, geo, sim

NOW = datetime.now(UTC)
GOOD = {"sensor": "temp-d01-0001", "type": "temperature", "district": "d01", "value": 21.5, "city": "paris"}


def area(ring):
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))


@pytest.mark.parametrize("city", sorted(geo.CITIES))
def test_every_builtin_district_has_a_closed_counter_clockwise_polygon(city):
    for d in catalog.DISTRICTS:
        ring = geo.district_polygon(city, d)
        assert len(ring) == 5 and ring[0] == ring[-1], "a closed ring of four corners"
        assert area(ring) > 0, "counter-clockwise: the outer ring orientation GeoJSON wants"
        assert all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in ring)


def test_polygons_tile_without_gaps_or_overlaps():
    """Neighbouring cells share their corner points exactly, and the corner shift is small enough to keep every cell convex."""
    ring = {d: geo.district_polygon("paris", d) for d in catalog.DISTRICTS}
    assert ring["d01"][1] == ring["d02"][0] and ring["d01"][2] == ring["d02"][3]      # d01's east side is d02's west side
    assert ring["d01"][3] == ring["d05"][0] and ring["d01"][2] == ring["d05"][1]      # and its north side is d05's south side
    total = sum(area(r) for r in ring.values())
    corners = [geo._corner("paris", i, j) for i in range(geo.COLS + 1) for j in range(geo.ROWS + 1)]
    assert total > 0 and len(set(corners)) == len(corners)
    for r in ring.values():                                                               # convex: all cross products share a sign
        crosses = [(b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) for a, b, c in zip(r, r[1:], r[2:] + r[1:2])]
        assert all(x > 0 for x in crosses)


def test_city_centres_are_where_the_cities_are_and_the_grid_is_centred_there():
    assert geo.CITIES["paris"] == (48.8566, 2.3522) and geo.CITIES["lyon"] == (45.7640, 4.8357)
    for city, (lat, lon) in geo.CITIES.items():
        pts = [p for d in catalog.DISTRICTS for p in geo.district_polygon(city, d)]
        assert abs(sum(p[1] for p in pts) / len(pts) - lat) < 0.01 and abs(sum(p[0] for p in pts) / len(pts) - lon) < 0.02, city
        lats, lons = [p[1] for p in pts], [p[0] for p in pts]
        km_tall, km_wide = (max(lats) - min(lats)) * 111.2, (max(lons) - min(lons)) * 111.2 * math.cos(math.radians(lat))
        assert 3 < km_tall < 6 and 4 < km_wide < 7, f"{city}: a few kilometres across, not a whole region"


def test_geography_is_deterministic_and_not_random_per_run():
    assert geo.district_polygon("paris", "d01") == geo.district_polygon("paris", "d01")
    assert geo.sensor_position("paris", "d03", "paris.air-quality-d03-0007") == geo.sensor_position("paris", "d03", "paris.air-quality-d03-0007")
    # a fixed expectation: these numbers must not change between runs, machines or Python versions (sha256, not hash())
    assert geo.sensor_position("paris", "d01", "paris.temperature-d01-0000") == geo.sensor_position("paris", "d01", "paris.temperature-d01-0000")
    assert geo.wkt(geo.district_polygon("paris", "d01")) == ("POLYGON((2.311676 48.839962, 2.331728 48.837859, 2.334088 48.851183, "
                                                              "2.315636 48.852241, 2.311676 48.839962))")
    assert geo.sensor_position("paris", "d01", "paris.t-d01-0") == (48.84455, 2.323023)


def test_every_simulated_sensor_lies_inside_its_own_district_polygon_and_no_other():
    for city in ("paris", "lyon"):
        rings = {d: geo.district_polygon(city, d) for d in catalog.DISTRICTS}
        for sid, _kind, d in catalog.build_fleet(1500, city):
            lat, lon = geo.sensor_position(city, d, sid)
            assert geo.ring_contains(rings[d], lon, lat), (sid, d)
            assert [k for k, r in rings.items() if geo.ring_contains(r, lon, lat)] == [d]


def test_unknown_city_or_district_has_no_builtin_geography():
    assert geo.sensor_position("atlantis", "d01", "atlantis.t-d01-1") is None
    assert geo.district_polygon("atlantis", "d01") is None
    assert geo.district_polygon("paris", "alder-quay") is None and geo.sensor_position("paris", "alder-quay", "x") is None
    assert not geo.has_geography("default") and geo.has_geography("lyon")


def test_the_simulator_sends_a_position_with_its_first_reading_and_then_every_so_often():
    s = sim.Simulator(60, 1.0, sink=None, bursts=lambda: [], city="paris")
    sensor = s.sensors[0]
    t0 = time.time()
    first = s.read(sensor, t0, [])
    assert (first["lat"], first["lon"]) == geo.sensor_position("paris", sensor.district, sensor.id)
    quiet = [s.read(sensor, t0 + i, []) for i in range(1, sim.POSITION_EVERY)]
    assert all("lat" not in r for r in quiet)
    assert "lat" in s.read(sensor, t0 + 99, [])
    other = sim.Simulator(30, 1.0, sink=None, bursts=lambda: [], city="atlantis")
    assert "lat" not in other.read(other.sensors[0], t0, []), "a city without built-in geography sends no position"
    assert isinstance(gateway.validate(first, datetime.now(UTC)), tuple), "the gateway accepts what the simulator sends"


# ---- ingest validation ----------------------------------------------------------------------------------------------------
def test_validate_takes_an_optional_position():
    assert gateway.validate({**GOOD, "lat": 48.8566, "lon": 2.3522}, NOW)[-2:] == (48.8566, 2.3522)
    assert gateway.validate({**GOOD, "lat": 0, "lon": 0}, NOW)[-2:] == (0.0, 0.0)
    assert gateway.validate({**GOOD, "lat": -90, "lon": 180}, NOW)[-2:] == (-90.0, 180.0)
    assert gateway.validate(GOOD, NOW)[-2:] == (None, None)
    assert gateway.validate({**GOOD, "lat": None, "lon": None}, NOW)[-2:] == (None, None)


@pytest.mark.parametrize("patch,reason", [
    ({"lat": 48.8}, gateway.COORDS_TOGETHER), ({"lon": 2.3}, gateway.COORDS_TOGETHER), ({"lat": 48.8, "lon": None}, gateway.COORDS_TOGETHER),
    ({"lat": 90.0001, "lon": 0}, gateway.COORDS_BAD), ({"lat": 0, "lon": -180.5}, gateway.COORDS_BAD),
    ({"lat": float("nan"), "lon": 1}, gateway.COORDS_BAD), ({"lat": float("inf"), "lon": 1}, gateway.COORDS_BAD),
    ({"lat": "48.8", "lon": "2.3"}, gateway.COORDS_BAD), ({"lat": True, "lon": False}, gateway.COORDS_BAD), ({"lat": [1], "lon": 2}, gateway.COORDS_BAD),
])
def test_validate_rejects_a_bad_position_with_a_reason(patch, reason):
    assert gateway.validate({**GOOD, **patch}, NOW) == reason


def test_otlp_attributes_lat_and_lon_become_a_position():
    from citypulse import otlp
    attrs = lambda **kv: [{"key": k, "value": {"doubleValue": v} if isinstance(v, float) else {"stringValue": v}} for k, v in kv.items()]  # noqa: E731
    data = {"resourceMetrics": [{"resource": {"attributes": attrs(**{"sensor.id": "air-1", "district": "d02", "city": "paris", "lat": 48.85, "lon": 2.34})},
                                 "scopeMetrics": [{"metrics": [{"name": "temperature", "gauge": {"dataPoints": [{"asDouble": 20.0}]}}]}]}]}
    (item,), _ = otlp.to_readings(data)
    assert item["lat"] == 48.85 and item["lon"] == 2.34 and isinstance(gateway.validate(item, NOW), tuple)


# ---- the Python side of the PostGIS queries, with a fake connection --------------------------------------------------------
class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, answers):
        self.answers, self.seen = answers, []

    def execute(self, sql, params=None):
        self.seen.append((sql, params))
        for key, rows in self.answers.items():
            if key in sql:
                return Result(rows)
        raise AssertionError("unexpected SQL: " + sql[:80])

    def rollback(self):
        pass


@pytest.fixture
def postgis_cache():
    geo._cache.update(at=1e12, ok=True, reason="")      # as if the schema probe had succeeded
    yield
    geo.reset_cache()


def area_row(district, ok, total, **extra):
    base = {"city": "paris", "district": district, "geometry": json.dumps({"type": "Polygon", "coordinates": [[list(p) for p in geo.district_polygon("paris", district)]]}),
            "source": "builtin", "total": total, "ok": ok, "offline": total - ok, "fault": 0, "stuck": 0, "stale": 0, "reported_elsewhere": 0}
    return {**base, **extra}


def test_districts_geojson_is_a_valid_feature_collection_with_health(postgis_cache):
    conn = FakeConn({"FROM district_area a": [area_row("d01", 3, 4, reported_elsewhere=1), area_row("d02", 0, 0)],
                     "geom IS NULL": [{"n": 7}]})
    out = geo.districts_geojson(conn, "paris")
    assert out["available"] and out["type"] == "FeatureCollection" and out["sensors_without_location"] == 7
    assert conn.seen[0][1] == {"city": "paris"} and "ST_Contains" in conn.seen[0][0] and "ST_AsGeoJSON" in conn.seen[0][0]
    a, b = out["features"]
    assert a["type"] == "Feature" and a["id"] == "paris/d01" and a["geometry"]["type"] == "Polygon"
    ring = a["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) >= 4 and all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in ring)
    p = a["properties"]
    assert (p["name"], p["total"], p["ok"], p["health"], p["worst_status"], p["reported_elsewhere"]) == ("Harbor", 4, 3, 0.75, "offline", 1)
    assert b["properties"]["health"] is None and b["properties"]["worst_status"] is None, "no sensor inside: no health, drawn grey"
    json.dumps(out)


def test_sensors_geojson_points_and_nearby_rows(postgis_cache):
    conn = FakeConn({"FROM sensor WHERE": [{"id": "paris.a", "type": "rain", "district": "d01", "city": "paris", "status": "fault", "reason": "x",
                                            "lat": 48.84, "lon": 2.32, "matching": 9}]})
    out = geo.sensors_geojson(conn, "paris", "problem", 500)
    f = out["features"][0]
    assert out["matching"] == 9 and f["geometry"] == {"type": "Point", "coordinates": [2.32, 48.84]}, "GeoJSON order is lon, lat"
    assert "status <> 'ok'" in conn.seen[0][0]
    conn2 = FakeConn({"ST_DWithin": [{"id": "a", "distance_m": 12.5}]})
    near = geo.nearby(conn2, 48.8566, 2.3522, 5000, 3, "paris")
    assert near["available"] and near["count"] == 1 and conn2.seen[0][1] == {"lat": 48.8566, "lon": 2.3522, "radius": 5000, "city": "paris", "limit": 3}
    assert "ST_DWithin" in conn2.seen[0][0] and "ORDER BY ST_Distance" in conn2.seen[0][0]


def test_every_geographic_query_degrades_when_postgis_is_missing():
    geo.reset_cache()
    conn = FakeConn({"pg_extension": [{"ext": False, "area": False, "col": False}]})
    for out in (geo.nearby(conn, 0, 0, 10, 5), geo.districts_geojson(conn), geo.sensors_geojson(conn, None, "all", 10)):
        assert out["available"] is False and "PostGIS" in out["reason"]
    assert geo.districts_geojson(conn)["features"] == [] and geo.districts_geojson(conn)["type"] == "FeatureCollection"
    geo.reset_cache()
    half = FakeConn({"pg_extension": [{"ext": True, "area": False, "col": True}]})
    assert geo.nearby(half, 0, 0, 10, 5)["available"] is False
    geo.reset_cache()


def test_builtin_seed_covers_every_city_and_district_with_valid_wkt():
    rows = geo.builtin_areas()
    assert len(rows) == len(geo.CITIES) * len(catalog.DISTRICTS) == 120
    assert all(w.startswith("POLYGON((") and w.endswith("))") and w.count(",") == 4 for _c, _d, w in rows)
    assert len({(c, d) for c, d, _ in rows}) == 120


def test_the_kit_is_new_enough_for_insights_and_masking():
    import apgkit
    assert tuple(int(x) for x in apgkit.__version__.split(".")[:3]) >= (0, 3, 1)
    from apgkit import insights, masking, migrate
    assert hasattr(insights, "insights_payload") and hasattr(masking, "mask_view")
    assert "after_migrate" in migrate.run.__code__.co_varnames or "after_migrate" in migrate.run.__kwdefaults__


# ---- derived areas -----------------------------------------------------------------------------------------------------------
def test_derived_area_sql_is_minimal_and_never_touches_builtin_rows():
    sql = geo.REFRESH_DERIVED
    for fn in ("ST_ConvexHull", "ST_Collect", "ST_Buffer", "ST_OrderingEquals"):
        assert fn in sql
    assert "source = 'builtin'" in sql and "NOT EXISTS" in sql, "a district with a built-in polygon is never derived"
    assert "district_area.source = 'derived'" in sql, "only derived rows are updated"
    assert "builtin" not in geo.PRUNE_DERIVED and "source = 'derived'" in geo.PRUNE_DERIVED
    assert "a.source <> 'derived' AND ST_Contains" in geo.AREAS_SQL and "a.source = 'derived' AND a.district = s.district" in geo.AREAS_SQL


def test_refresh_derived_areas_runs_both_statements_and_commits(postgis_cache):
    class Cur:
        def __init__(self, n):
            self.rowcount = n

    class Conn(FakeConn):
        committed = 0

        def execute(self, sql, params=None):
            self.seen.append((sql, params))
            return Cur(3 if sql.startswith("INSERT") else 1)

        def commit(self):
            self.committed += 1
    conn = Conn({})
    assert geo.refresh_derived_areas(conn) == {"available": True, "written": 3, "removed": 1}
    assert conn.seen[0][1] == {"hull": geo.HULL_BUFFER_M, "point": geo.POINT_BUFFER_M} and conn.committed == 1


def test_refresh_derived_areas_is_quiet_without_postgis_and_survives_errors(postgis_cache):
    geo._cache.update(at=1e12, ok=False, reason=geo.NO_POSTGIS)
    assert geo.refresh_derived_areas(FakeConn({})) == {"available": False, "written": 0, "removed": 0}
    geo._cache.update(at=1e12, ok=True, reason="")

    class Boom(FakeConn):
        rolled = False

        def execute(self, sql, params=None):
            raise psycopg.errors.InternalError_("boom\ndetail")

        def rollback(self):
            self.rolled = True
    c = Boom({})
    assert geo.refresh_derived_areas(c)["written"] == 0 and c.rolled


def test_derived_features_expose_their_source(postgis_cache):
    conn = FakeConn({"FROM district_area a": [area_row("d01", 2, 2, source="derived")], "geom IS NULL": [{"n": 0}]})
    assert geo.districts_geojson(conn)["features"][0]["properties"]["source"] == "derived"
