"""EDB and AgentPG features against a disposable Postgres: database insights (admin only, degrades where the EDB extensions are
absent), read-only hardening for AgentPG's `a1p_readonly` role, and PostGIS geography (nearby sensors, district GeoJSON,
containment). Tests that need the PostGIS extension are skipped on a server that does not offer it; everything that must degrade
runs on plain Postgres."""
import os

import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("set TEST_DATABASE_URL to run DB tests", allow_module_level=True)

import psycopg  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from citypulse import catalog, core, db, gateway, geo, hardening, migrate  # noqa: E402
from citypulse.db import connect  # noqa: E402

TEST_URL = os.environ["TEST_DATABASE_URL"]
H = {"X-CityPulse-CSRF": "1"}
PASSWORD = "correct-horse-battery"  # pragma: allowlist secret
LOGIN = "cp_ro_login"


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    for v in ("CITYPULSE_API_KEY", "CITYPULSE_ALLOW_ANONYMOUS", "PAGERLENS_URL", "PAGERLENS_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CITYPULSE_API_KEY_FILE", "/nonexistent/key")
    db.reset_pool()
    core.auth.limiter.clear()
    core.local._setup_code = None
    core.clear_token_cache()
    gateway._inflight = 0
    gateway._known.update(count=0, at=0.0)
    gateway._bursts._at = 0.0
    with connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.commit()
    migrate.run()
    yield
    db.reset_pool()


def login(client):
    code = core.local.setup_code()
    assert client.post("/api/setup", json={"code": code, "password": PASSWORD}, headers=H).status_code == 200


def token(client, scope):
    r = client.post("/api/tokens", json={"name": scope, "scope": scope}, headers=H)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def reading(sensor, city="paris", district="d01", **extra):
    return {"sensor": sensor, "type": "temperature", "district": district, "value": 20.0, "city": city, **extra}


@pytest.fixture
def postgis():
    with connect() as c:
        ok, reason = geo.status(c, ttl=0)
    if not ok:
        pytest.skip(f"PostGIS is not available on the test server ({reason})")


# ---- database insights ----------------------------------------------------------------------------------------------------
def test_insights_is_admin_only_and_degrades_on_plain_postgres(client):
    assert client.get("/api/insights").status_code == 401
    login(client)
    rd, ing, full = token(client, "read"), token(client, "ingest"), token(client, "full")
    client.cookies.clear()
    assert client.get("/api/insights", headers=rd).status_code == 403          # statement text is admin material
    assert client.get("/api/insights", headers=ing).status_code == 403
    r = client.get("/api/insights?order=mean&limit=5", headers=full)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"sources", "top_queries", "tuner", "advisor"}
    with connect() as c:
        installed = {x["extname"] for x in c.execute("SELECT extname FROM pg_extension")}
    for name, src in out["sources"].items():
        assert isinstance(src["available"], bool)
        if name not in installed:                                              # edb_* extensions are absent on plain Postgres
            assert src["available"] is False and src["reason"], name
    for part in ("tuner", "advisor"):
        assert isinstance(out[part]["available"], bool)
    if "edb_stat_monitor" not in installed and "pg_stat_statements" not in installed:
        assert out["top_queries"]["available"] is False and out["tuner"]["available"] is False and out["advisor"]["available"] is False
    # a bad order or an absurd limit is clamped, never a 500
    assert client.get("/api/insights?order=bogus&limit=99999", headers=full).status_code == 200
    assert client.get("/api/insights?limit=-3", headers=full).status_code == 200


def test_insights_metrics_are_exported_and_never_fail_a_scrape(client):
    login(client)
    met = token(client, "metrics")
    client.cookies.clear()
    r = client.get("/metrics", headers=met)
    assert r.status_code == 200
    assert 'db_insights_available{app="citypulse"}' in r.text


# ---- read-only hardening (a1p_readonly) -----------------------------------------------------------------------------------
@pytest.fixture
def roles():
    with connect() as c:
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='a1p_readonly') THEN CREATE ROLE a1p_readonly NOLOGIN; END IF; END $$")
        c.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{LOGIN}') THEN CREATE ROLE {LOGIN} LOGIN; END IF; END $$")
        c.execute(f"GRANT a1p_readonly TO {LOGIN}")
        c.commit()


def _like_agentpg():
    """A fresh schema where, as on an AgentPG instance, the read-only group role gets SELECT on every new table by default privilege."""
    with connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.execute("GRANT USAGE ON SCHEMA public TO a1p_readonly")
        c.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO a1p_readonly")
        c.commit()
    migrate.run()


def _ro():
    return psycopg.connect(make_conninfo(TEST_URL, user=LOGIN), row_factory=dict_row, autocommit=True)


def test_read_only_role_reads_the_fleet_but_not_the_secrets(client, roles):
    _like_agentpg()                                    # also proves the first start: oidc_login is created after the migrate hook ran
    gateway.accept([reading("paris.t-d01-0001")])
    with _ro() as ro:
        assert ro.execute("SELECT count(*) AS n FROM sensor").fetchone()["n"] == 1
        for table in ("reading", "status_history", "chaos", "schema_migrations"):
            ro.execute(f"SELECT * FROM {table}")
        for table in hardening.SECRET_TABLES:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                ro.execute(f"SELECT * FROM {table}")
        for write in ("DELETE FROM sensor", "UPDATE sensor SET status = 'ok'", "INSERT INTO chaos (kind, intensity, until) VALUES ('noise', 1, now())"):
            with pytest.raises(psycopg.Error):
                ro.execute(write)
    with connect() as c:                               # the app's own (owner) connection is unaffected
        assert c.execute("SELECT count(*) AS n FROM app_setting").fetchone()["n"] >= 0
    login(client)                                      # and so is the application: setup and sign-in read and write the secret tables
    assert client.get("/api/status").status_code == 200
    assert client.post("/api/logout", headers=H).status_code == 200


def test_hardening_is_idempotent_and_reapplied_after_a_new_grant(roles):
    _like_agentpg()
    with connect() as c:
        first, second = hardening.deny_secret_tables(c), hardening.deny_secret_tables(c)
        assert first == second and first["applied"] and set(first["tables"]) == set(hardening.SECRET_TABLES)
        assert set(first["tables"].values()) == {"denied"}
        c.execute("GRANT SELECT ON api_token TO a1p_readonly")        # what default privileges do for a table created later
        c.commit()
    with _ro() as ro:
        assert ro.execute("SELECT count(*) AS n FROM api_token").fetchone()["n"] == 0
    migrate.run()                                                    # the next start closes it again
    with _ro() as ro, pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute("SELECT * FROM api_token")


def test_hardening_skips_quietly_without_the_role_and_for_missing_tables(roles):
    with connect() as c:
        assert hardening.deny_secret_tables(c, role="cp_no_such_role") == {"applied": False, "reason": "role cp_no_such_role does not exist", "tables": {}}
        out = hardening.deny_secret_tables(c, {"api_token": "x", "no_such_table": "y"})
        assert out["tables"] == {"api_token": "denied", "no_such_table": "missing"} and out["applied"]
        assert c.autocommit is False                                 # the connection is handed back as it was
    migrate.run()                                                    # the hook runs at every start: twice in a row must not raise
    migrate.run()


def test_secret_tables_are_the_ones_that_exist_and_hold_secrets():
    with connect() as c:
        have = {r["table_name"] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    assert set(hardening.SECRET_TABLES) <= have, "every listed table exists after the migrations"
    # nothing else in the schema stores credentials: a new table with such a column must be added to SECRET_TABLES on purpose
    with connect() as c:
        suspects = {r["table_name"] for r in c.execute(
            "SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND (column_name ~ '(hash|secret|password|token|cipher|verifier|nonce|key)$' OR column_name IN ('value'))")}
    assert suspects <= set(hardening.SECRET_TABLES) | {"reading"}, suspects - set(hardening.SECRET_TABLES)


# ---- positions and geography -------------------------------------------------------------------------------------------------
def test_a_position_is_validated_stored_and_shown_with_or_without_postgis(client):
    res = gateway.accept([reading("paris.a-d01-0001", lat=48.8566, lon=2.3522), reading("paris.b-d01-0002", lat=91, lon=2),
                          reading("paris.c-d01-0003", lat=48.8), reading("paris.d-d01-0004")])
    assert res["accepted"] == 2 and res["rejected"] == 2
    assert any("lat must be within" in e for e in res["errors"]) and any("together" in e for e in res["errors"])
    login(client)
    s = client.get("/api/sensors/paris.a-d01-0001").json()
    assert (s["lat"], s["lon"]) == (48.8566, 2.3522) and "geom" not in s
    assert client.get("/api/sensors/paris.d-d01-0004").json()["lat"] is None
    gateway.accept([reading("paris.a-d01-0001")])                  # a later reading without a position keeps the stored one
    assert client.get("/api/sensors/paris.a-d01-0001").json()["lat"] == 48.8566
    gateway.accept([reading("paris.a-d01-0001", lat=48.86, lon=2.36)])                         # and a sender can correct it
    assert client.get("/api/sensors/paris.a-d01-0001").json()["lon"] == 2.36
    gateway.accept([reading("lyon.x-d01-0001", city="lyon", lat=45.76, lon=4.83), reading("paris.e-d01-0005", lat=48.85, lon=2.35)])   # two cities in one batch
    assert client.get("/api/sensors/lyon.x-d01-0001").json()["lat"] == 45.76


def test_geography_routes_degrade_without_postgis(client):
    with connect() as c:
        c.execute("DROP EXTENSION IF EXISTS postgis CASCADE")       # a no-op on plain Postgres; removes it on a server that has it
        c.commit()
    geo.reset_cache()
    gateway.accept([reading("paris.a-d01-0001", lat=48.8566, lon=2.3522)])                       # the ingest keeps working and stores the position
    login(client)
    for url in ("/api/sensors/nearby?lat=48.8566&lon=2.3522", "/api/districts/geojson", "/api/sensors/geojson?state=all"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.text)
        body = r.json()
        assert body["available"] is False and "PostGIS" in body["reason"], url
    assert client.get("/api/districts/geojson").json()["features"] == []
    assert client.get("/api/sensors/nearby?lat=95&lon=0").status_code == 422     # input is still validated
    assert client.get("/api/status").status_code == 200                           # and nothing else is affected


def test_ensure_on_a_server_without_postgis_returns_quietly():
    with connect() as c:
        offered = c.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'postgis'").fetchone()
        if offered:
            pytest.skip("this server offers PostGIS; the missing-extension path is covered by test_geography_routes_degrade_without_postgis")
        out = geo.ensure(c)
    assert out["available"] is False and "PostGIS" in out["reason"] and out["areas"] == 0
    assert migrate.run() == []                                                    # and the migrations still run


def test_the_geographic_schema_is_built_once_and_seeded(postgis):
    with connect() as c:
        first = geo.ensure(c)
        second = geo.ensure(c)
        assert first == second and first["available"] and first["areas"] == len(geo.CITIES) * len(catalog.DISTRICTS)
        assert c.execute("SELECT count(*) AS n FROM district_area WHERE NOT ST_IsValid(geom)").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM district_area a JOIN district_area b ON a.city = b.city AND a.district < b.district "
                         "AND ST_Overlaps(a.geom, b.geom)").fetchone()["n"] == 0
        idx = {r["indexname"] for r in c.execute("SELECT indexname FROM pg_indexes WHERE tablename IN ('sensor', 'district_area')")}
        assert {"sensor_geom_gist", "sensor_geom_geometry_gist", "district_area_gist"} <= idx
        c.execute("UPDATE district_area SET source = 'custom', geom = ST_Translate(geom, 0.1, 0) WHERE city = 'paris' AND district = 'd01'")
        c.commit()
        geo.ensure(c)                                                              # a polygon an operator replaced is never overwritten
        assert c.execute("SELECT source FROM district_area WHERE city = 'paris' AND district = 'd01'").fetchone()["source"] == "custom"


def test_postgis_added_later_backfills_positions_stored_before(postgis):
    with connect() as c:
        c.execute("ALTER TABLE sensor DROP COLUMN geom")
        c.commit()
    gateway.accept([reading("paris.a-d01-0001", lat=48.8566, lon=2.3522), reading("paris.b-d01-0002")])
    with connect() as c:
        geo.ensure(c)
        rows = {r["id"]: r["geom"] for r in c.execute("SELECT id, geom FROM sensor")}
    assert rows["paris.a-d01-0001"] is not None and rows["paris.b-d01-0002"] is None


def test_nearby_orders_by_distance_in_metres(client, postgis):
    gateway.accept([reading("paris.centre-d01-0001", lat=48.8566, lon=2.3522), reading("paris.north-d01-0002", lat=48.8700, lon=2.3522),
                    reading("lyon.centre-d01-0003", city="lyon", lat=45.7640, lon=4.8357), reading("paris.nopos-d01-0004")])
    login(client)
    r = client.get("/api/sensors/nearby?lat=48.8566&lon=2.3522&radius_m=500000&limit=10")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["available"] and [s["id"] for s in out["sensors"]] == ["paris.centre-d01-0001", "paris.north-d01-0002", "lyon.centre-d01-0003"]
    d = [s["distance_m"] for s in out["sensors"]]
    assert d[0] == 0 and 1450 < d[1] < 1550, "0.0134 degrees of latitude is about 1.49 km"
    assert 390_000 < d[2] < 393_000, "Paris to Lyon is about 391 km as the crow flies"
    assert d == sorted(d)
    near = client.get("/api/sensors/nearby?lat=48.8566&lon=2.3522&radius_m=100000").json()
    assert [s["id"] for s in near["sensors"]] == ["paris.centre-d01-0001", "paris.north-d01-0002"], "Lyon is outside 100 km; a sensor with no position never appears"
    assert [s["id"] for s in client.get("/api/sensors/nearby?lat=45.76&lon=4.83&radius_m=500000&city=lyon").json()["sensors"]] == ["lyon.centre-d01-0003"]
    assert len(client.get("/api/sensors/nearby?lat=48.8566&lon=2.3522&radius_m=500000&limit=2").json()["sensors"]) == 2
    for bad in ("lat=91&lon=0", "lat=0&lon=181", "lat=1&lon=1&radius_m=0", "lat=1&lon=1&radius_m=2000000", "lat=1&lon=1&limit=0", "lon=1", "lat=nan&lon=1"):
        assert client.get(f"/api/sensors/nearby?{bad}").status_code == 422, bad


def test_geography_routes_need_the_read_permission(client, postgis):
    assert client.get("/api/sensors/nearby?lat=1&lon=1").status_code == 401
    assert client.get("/api/districts/geojson").status_code == 401
    assert client.get("/api/sensors/geojson").status_code == 401
    login(client)
    ing, met = token(client, "ingest"), token(client, "metrics")
    client.cookies.clear()
    for url in ("/api/sensors/nearby?lat=1&lon=1", "/api/districts/geojson", "/api/sensors/geojson"):
        assert client.get(url, headers=ing).status_code == 403 and client.get(url, headers=met).status_code == 403


def test_districts_geojson_assigns_sensors_by_containment_and_reports_health(client, postgis):
    fleet = catalog.build_fleet(300, "paris")
    items = []
    for sid, kind, d in fleet:
        lat, lon = geo.sensor_position("paris", d, sid)
        items.append({"sensor": sid, "type": kind, "district": d, "city": "paris", "value": 20.0, "lat": lat, "lon": lon})
    lat, lon = geo.sensor_position("paris", "d01", "paris.rogue-d03-0001")
    items.append({"sensor": "paris.rogue-d03-0001", "type": "temperature", "district": "d03", "city": "paris", "value": 20.0, "lat": lat, "lon": lon})
    assert gateway.accept(items)["accepted"] == len(items)
    gateway.accept([reading("paris.nopos-d02-0001", district="d02"), reading("lyon.far-d01-0001", city="lyon", lat=45.76, lon=4.83)])
    d01 = [sid for sid, _k, d in fleet if d == "d01"]
    with connect() as c:
        c.execute("UPDATE sensor SET status = 'offline' WHERE id = ANY(%s)", (d01[:3],))
        c.commit()
    login(client)
    r = client.get("/api/districts/geojson?city=paris")
    assert r.status_code == 200, r.text
    gj = r.json()
    assert gj["available"] and gj["type"] == "FeatureCollection" and gj["city"] == "paris" and gj["sensors_without_location"] == 1
    props = {f["properties"]["district"]: f["properties"] for f in gj["features"]}
    assert sorted(props) == sorted(catalog.DISTRICTS), "only the city asked for, one feature per built-in district"
    expected = {d: sum(1 for _s, _k, x in fleet if x == d) for d in catalog.DISTRICTS}
    expected["d01"] += 1                                                             # the rogue sensor sits in d01 whatever it reports
    for d, p in props.items():
        assert p["total"] == expected[d], d
    assert props["d01"]["reported_elsewhere"] == 1 and props["d03"]["reported_elsewhere"] == 0
    assert props["d01"]["counts"]["offline"] == 3 and props["d01"]["worst_status"] == "offline"
    assert props["d01"]["health"] == pytest.approx((expected["d01"] - 3) / expected["d01"])
    assert props["d02"]["worst_status"] == "ok" and props["d02"]["health"] == 1
    for f in gj["features"]:                                                         # valid GeoJSON polygons
        assert f["type"] == "Feature" and f["geometry"]["type"] == "Polygon" and f["properties"]["name"]
        ring = f["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1] and len(ring) >= 4 and all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in ring)
    both = client.get("/api/districts/geojson").json()
    assert {f["properties"]["city"] for f in both["features"]} == {"paris", "lyon"}, "cities without sensors are not listed"
    # the same containment as the pure-Python reference
    with connect() as c:
        wrong = c.execute("SELECT count(*) AS n FROM sensor s JOIN district_area a ON a.city = s.city AND ST_Contains(a.geom, s.geom::geometry) "
                          "WHERE s.city = 'paris' AND a.district <> s.district AND s.id <> 'paris.rogue-d03-0001'").fetchone()["n"]
    assert wrong == 0


def test_sensors_geojson_lists_located_sensors_as_points(client, postgis):
    gateway.accept([reading("paris.a-d01-0001", lat=48.84, lon=2.32), reading("paris.b-d01-0002", lat=48.85, lon=2.33), reading("paris.c-d01-0003")])
    with connect() as c:
        c.execute("UPDATE sensor SET status = 'fault', reason = 'value outside physical limits' WHERE id = 'paris.b-d01-0002'")
        c.commit()
    login(client)
    out = client.get("/api/sensors/geojson?city=paris").json()
    assert out["available"] and out["matching"] == 1 and [f["id"] for f in out["features"]] == ["paris.b-d01-0002"]
    f = out["features"][0]
    assert f["geometry"] == {"type": "Point", "coordinates": [2.33, 48.85]} and f["properties"]["status"] == "fault"
    allp = client.get("/api/sensors/geojson?city=paris&state=all").json()
    assert allp["matching"] == 2, "a sensor with no position is not on the map"
    assert client.get("/api/sensors/geojson?state=weird").status_code == 422
    assert client.get("/api/sensors/paris.b-d01-0002").status_code == 200, "the sensor route still works next to /nearby and /geojson"
