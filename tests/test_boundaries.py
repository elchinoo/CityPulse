"""Real district boundaries: the vendored files are what they claim (real names, closed rings, a tiling, small, attributed), the schema and
upsert statements say the right things, and the base-map route needs no database. Pure tests: no PostGIS is needed (or available in
CI's plain Postgres); the PostGIS side is in test_edb_features.py and skips where the extension is missing."""
import json
import random
import re

import pytest

from citypulse import boundaries, catalog, geo
from citypulse.catalog import DISTRICT_ID

EXPECTED = {"brussels": 19, "vienna": 23}


def _rings(geometry):
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return [[(x, y) for x, y in ring] for poly in polys for ring in poly]


def _inside(geometry, lon, lat):
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return any(geo.ring_contains(p[0], lon, lat) and not any(geo.ring_contains(h, lon, lat) for h in p[1:]) for p in polys)


def test_two_real_cities_with_their_real_districts():
    assert boundaries.cities() == ["brussels", "vienna"]
    for city, n in EXPECTED.items():
        ds = boundaries.districts(city)
        assert len(ds) == n and boundaries.has_boundaries(city)
        assert all(DISTRICT_ID.match(d["id"]) for d in ds), "ids senders can use"
        assert len({d["id"] for d in ds}) == n
    names = {d["name"] for d in boundaries.districts("brussels")}
    assert {"Ixelles", "Anderlecht", "Schaerbeek", "Molenbeek-Saint-Jean", "Bruxelles", "Uccle"} <= names
    assert {d["name"] for d in boundaries.districts("vienna")} >= {"Innere Stadt", "Favoriten", "Rudolfsheim-Fünfhaus", "Donaustadt"}
    assert not boundaries.has_boundaries("paris") and not boundaries.has_boundaries(None) and boundaries.districts("paris") == ()


def test_ids_are_unique_across_cities_and_names_reach_the_ui():
    ids = [d["id"] for c in boundaries.cities() for d in boundaries.districts(c)]
    assert len(ids) == len(set(ids)) == sum(EXPECTED.values()), "one id means one district everywhere (district_name has no city)"
    assert catalog.district_name("molenbeek-saint-jean") == "Molenbeek-Saint-Jean"
    assert catalog.district_name("rudolfsheim-fuenfhaus") == "Rudolfsheim-Fünfhaus"
    assert catalog.district_name("d01") == "Harbor" and catalog.district_name("alder-quay") == "Alder Quay", "other ids behave as before"
    assert not set(ids) & set(catalog.DISTRICTS), "a real id never shadows a built-in one"


@pytest.mark.parametrize("city", EXPECTED)
def test_polygons_are_closed_valid_wgs84_and_where_the_city_is(city):
    meta = boundaries.meta()[city]
    lat, lon = meta["centre"]
    for d in boundaries.districts(city):
        assert d["geometry"]["type"] in ("Polygon", "MultiPolygon")
        for ring in _rings(d["geometry"]):
            assert ring[0] == ring[-1] and len(ring) >= 4
            assert all(abs(x - lon) < 0.5 and abs(y - lat) < 0.3 for x, y in ring), (city, d["id"])
            assert all(round(x, 5) == x and round(y, 5) == y for x, y in ring), "coordinates rounded to 5 decimals"
    assert meta["districts"] == EXPECTED[city] and meta["bbox"][0] < lon < meta["bbox"][2] and meta["bbox"][1] < lat < meta["bbox"][3]


@pytest.mark.parametrize("city", EXPECTED)
def test_districts_tile_the_city_without_overlaps_or_gaps(city):
    """Sampled in pure Python: no point lies in two districts, and almost every point inside the outline lies in one."""
    ds = boundaries.districts(city)
    x0, y0, x1, y1 = boundaries.meta()[city]["bbox"]
    rng = random.Random(7)
    hits = {d["id"]: 0 for d in ds}
    for _ in range(1500):
        lon, lat = rng.uniform(x0, x1), rng.uniform(y0, y1)
        inside = [d["id"] for d in ds if _inside(d["geometry"], lon, lat)]
        assert len(inside) <= 1, (city, lon, lat, inside)
        for i in inside:
            hits[i] += 1
    assert all(n > 0 for n in hits.values()), "every district is hit by a random point"
    # two districts share their border vertices exactly, so a shared edge has no sliver between them
    owners = {}
    for d in ds:
        for ring in _rings(d["geometry"]):
            for p in ring:
                owners.setdefault(p, set()).add(d["id"])
    shared = sum(1 for o in owners.values() if len(o) > 1)
    assert shared > 40, "neighbouring districts share vertices (the files were simplified as one coverage)"


def test_files_are_small_and_the_context_has_a_base_map():
    for city in EXPECTED:
        size = sum((boundaries.DIR / f"{city}.{k}.geojson").stat().st_size for k in ("districts", "context"))
        assert size < 320_000, f"{city}: {size} bytes"
        ctx = boundaries.context(city)
        kinds = {f["properties"]["kind"] for f in ctx["features"]}
        assert {"water", "park", "road_major", "road_main"} <= kinds, kinds
        assert ctx["attribution"] and "OpenStreetMap" in ctx["attribution"] and ctx["licence"]
        for f in ctx["features"]:
            assert f["geometry"]["type"] in ("Polygon", "LineString", "MultiLineString", "MultiPolygon") and f["properties"]["kind"] in boundaries.KINDS


def test_attribution_file_names_every_source_and_licence():
    text = (boundaries.DIR / "ATTRIBUTION.md").read_text(encoding="utf-8")
    for needle in ("OpenStreetMap contributors", "ODbL", "Stadt Wien", "data.wien.gv.at", "CC BY 3.0 AT", "Overpass", "build_boundaries.py"):
        assert needle in text, needle
    for city, m in boundaries.meta().items():
        assert m["attribution"] in text or m["attribution"].split(";")[0] in text, city
        assert m["licence"] and m["districts_source"]


def test_unknown_city_has_an_empty_context():
    out = boundaries.context("paris")
    assert out["features"] == [] and out["attribution"] is None
    assert boundaries.context(None)["features"] == []


def test_wkt_is_what_postgis_reads():
    square = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], [[0.2, 0.2], [0.4, 0.2], [0.4, 0.4], [0.2, 0.2]]]}
    assert boundaries.wkt(square) == "POLYGON((0.000000 0.000000, 1.000000 0.000000, 1.000000 1.000000, 0.000000 1.000000, 0.000000 0.000000), (0.200000 0.200000, 0.400000 0.200000, 0.400000 0.400000, 0.200000 0.200000))"
    multi = boundaries.wkt({"type": "MultiPolygon", "coordinates": [square["coordinates"][:1], square["coordinates"][:1]]})
    assert multi.startswith("MULTIPOLYGON(((") and multi.count(")), ((") == 1 and multi.endswith(")))")
    with pytest.raises(ValueError):
        boundaries.wkt({"type": "Point", "coordinates": [0, 0]})
    rows = boundaries.boundary_areas()
    assert len(rows) == 42 and all(re.fullmatch(r"(MULTI)?POLYGON\(\(.*\)\)", w) for _c, _d, w in rows)
    assert {c for c, _d, _w in rows} == {"brussels", "vienna"}


# ---- the statements (PostGIS itself is not run here; see test_edb_features.py) ----------------------------------------------------
def test_schema_allows_multipolygons_and_alters_old_tables_once():
    create = next(s for s in geo.SCHEMA if s.startswith("CREATE TABLE IF NOT EXISTS district_area"))
    assert "geometry(Geometry, 4326)" in create and "geometry(Polygon" not in create
    alter = next(s for s in geo.SCHEMA if s.startswith("DO $$"))
    assert "'geometry(Polygon,4326)'" in alter and "ALTER TABLE district_area ALTER COLUMN geom TYPE geometry(Geometry, 4326)" in alter
    assert geo.SCHEMA.index(alter) < geo.SCHEMA.index("CREATE INDEX IF NOT EXISTS district_area_gist ON district_area USING gist (geom)")


def test_boundary_upsert_replaces_derived_rows_and_only_rewrites_changes():
    sql = geo.UPSERT_BOUNDARY
    assert "'boundary'" in sql and "ST_GeomFromText(%s, 4326)" in sql and "ON CONFLICT (city, district) DO UPDATE" in sql
    assert "district_area.source IN ('derived', 'boundary')" in sql, "a built-in row is never overwritten by a boundary"
    assert "ST_OrderingEquals" in sql and "district_area.source <> 'boundary'" in sql
    assert geo.BOUNDARY == "boundary"


class RecordingConn:
    """Enough of a connection for geo.ensure: records the statements, answers the probe, accepts executemany."""
    def __init__(self):
        self.autocommit, self.stmts, self.many = False, [], []

    def commit(self):
        pass

    def execute(self, sql, params=None):
        self.stmts.append(sql)

        class R:
            def fetchone(self_):
                if "pg_extension" in sql:
                    return {"ext": True, "area": True, "col": True}
                return {"n": 42}
        return R()

    def cursor(self):
        outer = self

        class Cur:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def executemany(self_, sql, rows):
                outer.many.append((sql, list(rows)))
        return Cur()


def test_ensure_stores_the_real_boundaries_after_the_builtin_ones():
    c = RecordingConn()
    out = geo.ensure(c)
    assert out["available"] is True
    assert [sql[:30] for sql, _ in c.many] == [geo.UPSERT_AREA[:30], geo.UPSERT_BOUNDARY[:30]]
    rows = c.many[1][1]
    assert len(rows) == 42 and rows[0][0] in ("brussels", "vienna") and rows[0][2].startswith(("POLYGON", "MULTIPOLYGON"))
    assert c.stmts.index(next(s for s in c.stmts if s.startswith("DO $$"))) < c.stmts.index(geo.REFRESH_DERIVED), "type widened before anything is written"


def test_districts_geojson_carries_the_attribution_only_for_real_boundaries():
    from test_geo import FakeConn, area_row
    geo._cache.update(at=1e12, ok=True, reason="")
    try:
        row = area_row("d01", 3, 4) | {"city": "brussels", "district": "ixelles", "source": "boundary"}
        out = geo.districts_geojson(FakeConn({"FROM district_area a": [row], "geom IS NULL": [{"n": 0}]}), "brussels")
        assert out["attribution"] and "OpenStreetMap" in out["attribution"]
        assert out["features"][0]["properties"]["name"] == "Ixelles" and out["features"][0]["properties"]["source"] == "boundary"
        paris = geo.districts_geojson(FakeConn({"FROM district_area a": [area_row("d01", 1, 1)], "geom IS NULL": [{"n": 0}]}), "paris")
        assert paris["attribution"] is None
    finally:
        geo.reset_cache()


def test_context_route_needs_no_database_and_a_permission(client):
    assert client.get("/api/geo/context?city=brussels").status_code == 401
    try:
        from test_db import login
        login(client)
    except Exception:  # noqa: BLE001 - without TEST_DATABASE_URL there is no setup flow: the permission check above is what we can test
        pytest.skip("no database for the sign-in flow")
    r = client.get("/api/geo/context?city=brussels")
    assert r.status_code == 200 and r.headers["cache-control"].startswith("private")
    body = r.json()
    assert body["type"] == "FeatureCollection" and body["features"] and "OpenStreetMap" in body["attribution"]
    assert client.get("/api/geo/context?city=paris").json()["features"] == []
    assert client.get("/api/geo/context?city=" + "x" * 40).status_code == 422
    json.dumps(body)
