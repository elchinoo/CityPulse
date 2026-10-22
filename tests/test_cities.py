"""Multi-city support against a disposable Postgres: ingest, mandatory city, OTLP, filters, /api/cities, bursts, deleting a city, upgrade."""
import json
import os
import shutil

import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("set TEST_DATABASE_URL to run DB tests", allow_module_level=True)

from test_db import H, _db, login, otlp_payload, reading, token  # noqa: E402,F401  (the autouse _db fixture resets the schema)

from citypulse import chaos, core, gateway, metrics, migrate, purge, reporter, status  # noqa: E402
from citypulse.catalog import DISTRICTS  # noqa: E402
from citypulse.db import connect  # noqa: E402


def city_of(sensor_id):
    with connect() as c:
        return c.execute("SELECT city FROM sensor WHERE id=%s", (sensor_id,)).fetchone()["city"]


def fleet():
    """Three cities: paris (4 sensors, 1 offline), tokyo (2), and rome (3)."""
    items = [reading(i, district="d01", city="paris") | {"sensor": f"paris.t{i:04d}"} for i in range(4)]
    items += [reading(i, kind="rain", district="d02", city="tokyo") | {"sensor": f"tokyo.r{i:04d}"} for i in range(2)]
    items += [reading(i, kind="co2", city="rome") for i in range(3)]
    assert gateway.accept(items) == {"accepted": 9, "rejected": 0, "errors": []}
    with connect() as c:
        c.execute("UPDATE sensor SET status='offline' WHERE id='paris.t0000'")
        c.commit()


# ---- ingest ---------------------------------------------------------------------------------------
def no_city(i=1, **kw):
    item = reading(i, **kw)
    del item["city"]
    return item


def test_a_reading_without_a_city_is_rejected_and_counted():
    res = gateway.accept([no_city(1), reading(2, city="oslo"), reading(3, city=None), no_city(4)])
    assert res["accepted"] == 1 and res["rejected"] == 3
    assert res["errors"] == ["3 x city is required"]
    assert "CITYPULSE_CITY" in res["hint"] and '"city"' in res["hint"] and "OTLP" in res["hint"]   # the response lists the fix
    with connect() as c:
        assert [r["city"] for r in c.execute("SELECT city FROM sensor")] == ["oslo"]            # no default city was invented
        assert c.execute("SELECT count(*) AS n FROM reading").fetchone()["n"] == 1
    assert "hint" not in gateway.accept([reading(5, city="oslo")])                              # clean batches carry no hint
    assert "hint" not in gateway.accept([reading(6, city="Bad")])                               # only the missing city gets it


def test_the_reserved_legacy_city_id_is_rejected():
    res = gateway.accept([reading(1, city="default"), reading(2, city="default2")])
    assert res["accepted"] == 1 and res["rejected"] == 1 and "reserved" in res["errors"][0]


def test_the_database_has_no_city_default_either():
    with connect() as c:
        with pytest.raises(Exception, match="null value in column \"city\""):
            c.execute("INSERT INTO sensor (id, type, district, last_ts, last_value) VALUES ('temperature-d01-0001','temperature','d01',now(),20)")
        c.rollback()


@pytest.mark.parametrize("bad", ["", "Paris", "-x", "a b", "x" * 33, 7, ["a"], "a;drop", "a'b"])
def test_bad_city_is_rejected_and_counted(bad):
    res = gateway.accept([reading(1, city=bad), reading(2)])
    assert res["accepted"] == 1 and res["rejected"] == 1 and "bad city" in res["errors"][0]


def test_a_sensors_city_is_fixed_by_its_first_reading():
    gateway.accept([reading(1, city="oslo")])
    res = gateway.accept([reading(1, city="rome", value=21.0), reading(1, value=22.0), reading(1, city="oslo", value=23.0)])
    assert res["accepted"] == 1 and res["rejected"] == 2 and "city does not match" in res["errors"][0]
    assert city_of("temperature-d01-0001") == "oslo"
    with connect() as c:
        s = c.execute("SELECT last_value FROM sensor WHERE id='temperature-d01-0001'").fetchone()
        assert s["last_value"] == 23.0
        assert c.execute("SELECT count(*) AS n FROM reading").fetchone()["n"] == 2   # rejected readings are not stored


def test_city_conflict_inside_one_batch_keeps_the_first():
    res = gateway.accept([reading(1, city="oslo"), reading(1, city="rome"), reading(1, city="oslo")])
    assert res["accepted"] == 2 and res["rejected"] == 1
    assert city_of("temperature-d01-0001") == "oslo"


def test_mixed_cities_in_one_batch_are_stored_with_their_own_city():
    gateway.accept([reading(i, city=f"c{i % 3}") for i in range(30)])
    with connect() as c:
        rows = c.execute("SELECT city, count(*) AS n FROM sensor GROUP BY 1 ORDER BY 1").fetchall()
    assert [(r["city"], r["n"]) for r in rows] == [("c0", 10), ("c1", 10), ("c2", 10)]


def test_ingest_api_accepts_city_and_rejects_mismatch(client):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    r = client.post("/api/readings", headers=ing, json={"readings": [reading(1, city="oslo")]})
    assert r.json()["accepted"] == 1
    r = client.post("/api/readings", headers=ing, json={"readings": [reading(1, city="rome"), reading(1, city="Bad")]})
    assert r.json()["accepted"] == 0 and r.json()["rejected"] == 2
    assert city_of("temperature-d01-0001") == "oslo"


def test_ingest_api_rejects_a_missing_city_with_the_fix(client):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    r = client.post("/api/readings", headers=ing, json={"readings": [no_city(1), no_city(2), reading(3, city="oslo")]})
    body = r.json()
    assert r.status_code == 200 and body["accepted"] == 1 and body["rejected"] == 2
    assert body["errors"] == ["2 x city is required"] and "Fix:" in body["hint"]
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM sensor").fetchone()["n"] == 1


# ---- OTLP -----------------------------------------------------------------------------------------
def with_attr(payload, key, value, where="resource"):
    attr = {"key": key, "value": {"stringValue": value}}
    rm = payload["resourceMetrics"][0]
    if where == "resource":
        rm["resource"]["attributes"].append(attr)
    else:
        for p in rm["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"]:
            p["attributes"] = [attr]
    return payload


@pytest.mark.parametrize("key,where", [("city", "resource"), ("city.id", "resource"), ("city", "point"), ("city.id", "point")])
def test_otlp_city_attribute(client, key, where):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    r = client.post("/v1/metrics", headers={**ing, "Content-Type": "application/json"},
                    content=json.dumps(with_attr(otlp_payload(1, city=None), key, "lyon", where)))
    assert r.status_code == 200 and r.json() == {}
    assert city_of("temperature-d02-0007") == "lyon"


def test_otlp_without_city_is_rejected_with_the_fix(client):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    hdr = {**ing, "Content-Type": "application/json"}
    r = client.post("/v1/metrics", headers=hdr, content=json.dumps(otlp_payload(2, city=None)))
    assert r.status_code == 200
    ps = r.json()["partialSuccess"]
    assert ps["rejectedDataPoints"] == "2" and "city is required" in ps["errorMessage"] and "city" in ps["errorMessage"].split("fix:")[1]
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM sensor").fetchone()["n"] == 0
    # protobuf senders get the same partial success
    from google.protobuf.json_format import ParseDict
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
        ExportMetricsServiceRequest,
        ExportMetricsServiceResponse,
    )
    msg = ParseDict(otlp_payload(1, city=None), ExportMetricsServiceRequest())
    r = client.post("/v1/metrics", headers={**ing, "Content-Type": "application/x-protobuf"}, content=msg.SerializeToString())
    ack = ExportMetricsServiceResponse()
    ack.ParseFromString(r.content)
    assert ack.partial_success.rejected_data_points == 1 and "city is required" in ack.partial_success.error_message
    # a city on the resource or the data point is enough
    ok = client.post("/v1/metrics", headers=hdr, content=json.dumps(with_attr(otlp_payload(1, city=None), "city", "lyon", "point")))
    assert ok.json() == {} and city_of("temperature-d02-0007") == "lyon"


def test_otlp_point_wins_over_resource(client):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    hdr = {**ing, "Content-Type": "application/json"}
    # an already known sensor under another city is a rejected data point, reported to the sender
    client.post("/v1/metrics", headers=hdr, content=json.dumps(otlp_payload(1, city="lyon")))
    assert city_of("temperature-d02-0007") == "lyon"
    p = with_attr(with_attr(otlp_payload(1, city=None), "city", "lyon"), "city", "nice", "point")
    r = client.post("/v1/metrics", headers=hdr, content=json.dumps(p))
    assert r.json()["partialSuccess"]["rejectedDataPoints"] == "1"
    fresh = with_attr(with_attr(otlp_payload(1, city=None), "city", "lyon"), "city", "nice", "point")
    fresh["resourceMetrics"][0]["resource"]["attributes"][0]["value"]["stringValue"] = "temperature-d02-0008"
    client.post("/v1/metrics", headers=hdr, content=json.dumps(fresh))
    assert city_of("temperature-d02-0008") == "nice"


# ---- read API -------------------------------------------------------------------------------------
def test_filters_on_every_read_endpoint(client):
    login(client)
    fleet()
    s = client.get("/api/status").json()
    assert s["total"] == 9 and s["city"] is None
    p = client.get("/api/status?city=paris").json()
    assert p["total"] == 4 and p["city"] == "paris" and p["counts"]["offline"] == 1
    assert [d["key"] for d in p["districts"]] == ["d01"] and [t["key"] for t in p["types"]] == ["temperature"]
    assert all(m["district"] == "d01" for m in p["matrix"])
    assert client.get("/api/status?city=rome").json()["total"] == 3
    assert client.get("/api/status?city=nowhere").json()["total"] == 0
    assert len(client.get("/api/sensors?state=all").json()) == 9
    rows = client.get("/api/sensors?state=all&city=tokyo").json()
    assert {r["id"] for r in rows} == {"tokyo.r0000", "tokyo.r0001"} and all(r["city"] == "tokyo" for r in rows)
    assert [r["id"] for r in client.get("/api/sensors?city=paris").json()] == ["paris.t0000"]   # problems only
    assert client.get("/api/sensors?state=all&city=paris&district=d02").json() == []
    assert client.get("/api/sensors/paris.t0001").json()["city"] == "paris"
    h = client.get("/api/history?minutes=5&city=paris").json()
    assert h["city"] == "paris" and h["health"] == [] and isinstance(h["throughput"], list)
    assert sum(r["n"] for r in client.get("/api/history?minutes=5&city=paris").json()["throughput"]) == 4
    assert sum(r["n"] for r in client.get("/api/history?minutes=5").json()["throughput"]) == 9
    for bad in ("Paris", "a b", "x" * 33, "a'--"):
        for url in ("/api/status", "/api/sensors", "/api/history", "/api/chaos"):
            assert client.get(url, params={"city": bad}).status_code == 422, (url, bad)


def test_city_history_is_recorded_per_city(client):
    login(client)
    fleet()
    with connect() as c:
        status.snapshot_history(c, status.summary(c))
        c.commit()
    h = client.get("/api/history?minutes=5&city=paris").json()["health"]
    assert len(h) == 1 and (h[0]["total"], h[0]["ok"]) == (4, 3)
    assert len(client.get("/api/history?minutes=5").json()["health"]) == 1


def test_city_history_is_limited_when_there_are_thousands_of_cities(monkeypatch):
    monkeypatch.setattr(status, "CITY_HISTORY_MAX", 5)
    gateway.accept([reading(i, city=f"c{i:03d}") for i in range(40)])
    with connect() as c:
        c.execute("UPDATE sensor SET status='offline' WHERE id IN ('temperature-d01-0007','temperature-d01-0008')")
        status.snapshot_history(c, status.summary(c))
        c.commit()
        scopes = {r["scope"] for r in c.execute("SELECT scope FROM status_history WHERE scope LIKE 'city:%'")}
    assert scopes == {"city:c007", "city:c008"}          # only the cities with a problem


def test_cities_endpoint_lists_counts_and_status(client):
    login(client)
    fleet()
    out = client.get("/api/cities").json()
    assert out["total"] == 3 and [c["city"] for c in out["cities"]] == ["paris", "rome", "tokyo"]
    assert out["legacy_city"] is None and not any(c["legacy"] for c in out["cities"])
    paris = next(c for c in out["cities"] if c["city"] == "paris")
    assert paris["total"] == 4 and paris["counts"] == {"ok": 3, "stale": 0, "offline": 1, "fault": 0, "stuck": 0}
    assert paris["worst_status"] == "offline" and paris["state"] == "partial_outage"
    assert paris["health"] == 0.75 and paris["last_ts"]
    assert next(c for c in out["cities"] if c["city"] == "tokyo")["worst_status"] == "ok"
    assert [c["city"] for c in client.get("/api/cities?sort=worst").json()["cities"]][0] == "paris"


def test_cities_endpoint_search_paging_and_bounds(client):
    login(client)
    gateway.accept([reading(i, city=f"{p}{i % 4}") for i, p in enumerate(["ab", "ab", "ab", "ab", "abc", "b", "b_x", "bxx"] * 5)])
    names = [c["city"] for c in client.get("/api/cities?limit=200").json()["cities"]]
    assert names == sorted(names)
    assert client.get("/api/cities?q=ab").json()["total"] == len([n for n in names if n.startswith("ab")])
    assert all(c["city"].startswith("ab") for c in client.get("/api/cities?q=AB").json()["cities"])    # case-insensitive
    under = [c["city"] for c in client.get("/api/cities?q=b_").json()["cities"]]
    assert under and all(n.startswith("b_") for n in under)                                            # '_' is not a wildcard
    assert client.get("/api/cities?q=zz").json() == {"total": 0, "limit": 50, "offset": 0, "legacy_city": None, "cities": []}
    page = client.get("/api/cities?limit=3&offset=2").json()
    assert page["total"] == len(names) and [c["city"] for c in page["cities"]] == names[2:5]
    assert client.get("/api/cities?offset=9999").json()["total"] == len(names)
    for bad in ("limit=0", "limit=201", "offset=-1", "sort=oops", "q=a%25", "q=" + "a" * 33):
        assert client.get("/api/cities?" + bad).status_code == 422, bad


def test_cities_endpoint_scales_to_thousands_of_cities(client):
    login(client)
    for start in range(0, 3000, 1000):
        gateway.accept([reading(i % 9000, city=f"city{i:05d}") | {"sensor": f"temperature-d01-{i:05d}"}
                        for i in range(start, start + 1000)])
    page = client.get("/api/cities?limit=200&offset=100").json()
    assert page["total"] == 3000 and len(page["cities"]) == 200 and page["cities"][0]["city"] == "city00100"
    assert client.get("/api/cities?q=city0299").json()["total"] == 10


def test_cities_needs_read_permission(client):
    login(client)
    ing, met = token(client, "ingest"), token(client, "metrics")
    rd = token(client, "read")
    client.cookies.clear()
    assert client.get("/api/cities").status_code == 401
    assert client.get("/api/cities", headers=ing).status_code == 403
    assert client.get("/api/cities", headers=met).status_code == 403
    assert client.get("/api/cities", headers=rd).status_code == 200


# ---- bursts ---------------------------------------------------------------------------------------
def test_burst_can_target_a_city(client):
    login(client)
    fleet()
    assert client.post("/api/chaos", json={"kind": "noise", "target": "city:paris"}, headers=H).status_code == 200
    assert client.post("/api/chaos", json={"kind": "noise", "target": "city:nowhere"}, headers=H).status_code == 422
    assert client.post("/api/chaos", json={"kind": "noise", "target": "city:Bad"}, headers=H).status_code == 422
    assert client.post("/api/chaos", json={"kind": "flood", "target": "district:d01"}, headers=H).status_code == 200
    assert client.post("/api/chaos", json={"kind": "dropout"}, headers=H).status_code == 200      # target defaults to all
    assert len(client.get("/api/chaos").json()["active"]) == 3
    paris = [b["target"] for b in client.get("/api/chaos?city=paris").json()["active"]]
    tokyo = [b["target"] for b in client.get("/api/chaos?city=tokyo").json()["active"]]
    assert sorted(paris) == ["all", "city:paris", "district:d01"] and sorted(tokyo) == ["all", "district:d01"]
    assert len(client.get("/api/status?city=tokyo").json()["bursts"]) == 2
    assert client.post("/api/chaos/stop", headers=H).json()["stopped"] == 3


def test_city_storm_is_confined_to_the_city(client):
    login(client)
    fleet()
    assert client.post("/api/chaos/storm", json={"seconds": 30, "city": "tokyo"}, headers=H).status_code == 200
    targets = sorted({b["target"] for b in client.get("/api/chaos").json()["active"]})
    assert targets == ["all", "city:tokyo"]                  # slow_db is gateway-wide; every other fault is the city's
    assert client.post("/api/chaos/storm", json={"city": "nowhere"}, headers=H).status_code == 422
    assert client.post("/api/chaos/storm", json={"city": "Bad City"}, headers=H).status_code == 422
    client.post("/api/chaos/stop", headers=H)
    assert client.post("/api/chaos/storm", json={"seconds": 30}, headers=H).status_code == 200
    assert not any(b["target"].startswith("city:") for b in client.get("/api/chaos").json()["active"])


def test_city_control_needs_control_permission(client):
    login(client)
    fleet()
    rd = token(client, "read")
    client.cookies.clear()
    assert client.post("/api/chaos", json={"kind": "noise", "target": "city:paris"}, headers={**rd, **H}).status_code == 403


# ---- the database -----------------------------------------------------------------------------------
def test_upgrade_from_the_schema_before_cities(tmp_path):
    from apgkit import migrate as kit_migrate
    old = tmp_path / "old"
    old.mkdir()
    for name in ("001_init.sql", "002_protocol.sql"):
        shutil.copy(migrate.MIGRATIONS / name, old / name)
    with connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.commit()
    assert len(kit_migrate.run(core.db, old)) == 2
    with connect() as c:
        for i in range(50):
            c.execute("INSERT INTO sensor (id, type, district, last_ts, last_value) VALUES (%s,'temperature','d01',now(),20)",
                      (f"temperature-d01-{i:04d}",))
        c.execute("INSERT INTO chaos (kind, target, intensity, until) VALUES ('noise','district:d01',2, now() + interval '5 min')")
        c.execute("INSERT INTO reading VALUES ('temperature-d01-0001', now(), 1.0)")
        c.commit()
        assert "city" not in {r["column_name"] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='sensor'")}
    assert migrate.run() == ["003_city.sql", "004_no_default_city.sql"]
    assert migrate.run() == []                                     # idempotent
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM sensor WHERE city = 'default'").fetchone()["n"] == 50
        assert c.execute("SELECT count(*) AS n FROM chaos").fetchone()["n"] == 1
        assert c.execute("SELECT count(*) AS n FROM reading").fetchone()["n"] == 1
        ix = {r["indexname"] for r in c.execute("SELECT indexname FROM pg_indexes WHERE tablename='sensor'")}
        assert {"sensor_city_district_type", "sensor_district_type", "sensor_pkey"} <= ix
        c.execute("INSERT INTO chaos (kind, target, intensity, until) VALUES ('noise','city:paris',2, now() + interval '5 min')")
        with pytest.raises(Exception, match="chaos_target_check"):
            c.execute("INSERT INTO chaos (kind, target, intensity, until) VALUES ('noise','city:Bad',2, now() + interval '5 min')")
        c.rollback()
        with pytest.raises(Exception, match="sensor_city_check"):
            c.execute("UPDATE sensor SET city = 'No Good' WHERE id = 'temperature-d01-0001'")
        c.rollback()
    # the upgraded database behaves like a fresh one for new data (a city is required), existing sensors stay in 'default'
    res = gateway.accept([reading(70), no_city(60), reading(61, city="paris")])
    assert res["accepted"] == 2 and res["errors"] == ["1 x city is required"]
    assert gateway.accept([reading(2, city="paris")])["rejected"] == 1                   # sensor 0002 is a legacy sensor
    assert city_of("temperature-d01-0001") == "default" and city_of("temperature-d01-0061") == "paris"
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM sensor").fetchone()["n"] == 52
        s = status.summary(c)
        assert s["total"] == 52 and status.summary(c, "paris")["total"] == 1
        assert status.evaluate(c) >= 0


def legacy_database(tmp_path, sensors=50):
    """A database as the pre-004 code left it: the city column with its DEFAULT, `sensors` sensors in the city 'default'."""
    from apgkit import migrate as kit_migrate
    old = tmp_path / "old"
    old.mkdir()
    for name in ("001_init.sql", "002_protocol.sql", "003_city.sql"):
        shutil.copy(migrate.MIGRATIONS / name, old / name)
    with connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.commit()
    kit_migrate.run(core.db, old)
    with connect() as c:
        for i in range(sensors):
            c.execute("INSERT INTO sensor (id, type, district, last_ts, last_value) VALUES (%s,'temperature','d01',now(),20)",
                      (f"temperature-d01-{i:04d}",))      # no city: the old column default assigns 'default'
            c.execute("INSERT INTO reading VALUES (%s, now(), 1.0)", (f"temperature-d01-{i:04d}",))
        c.execute("INSERT INTO sensor (id, type, district, city, last_ts, last_value) VALUES ('paris.t0001','temperature','d01','paris',now(),20)")
        c.commit()


def test_upgrade_keeps_legacy_default_sensors_readable_and_flagged(client, tmp_path):
    legacy_database(tmp_path)
    assert migrate.run() == ["004_no_default_city.sql"]            # drops the column default only
    assert migrate.run() == []
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM sensor WHERE city='default'").fetchone()["n"] == 50    # nothing was deleted
        assert c.execute("SELECT count(*) AS n FROM reading").fetchone()["n"] == 50
        assert c.execute("SELECT column_default FROM information_schema.columns WHERE table_name='sensor' AND column_name='city'"
                         ).fetchone()["column_default"] is None
        assert c.execute("SELECT 1 FROM pg_constraint WHERE conname='sensor_city_check'").fetchone()   # the constraint stays
    login(client)
    out = client.get("/api/cities").json()
    assert out["legacy_city"] == "default" and out["total"] == 2
    row = {c["city"]: c for c in out["cities"]}
    assert row["default"]["legacy"] is True and row["default"]["total"] == 50 and row["paris"]["legacy"] is False
    st = client.get("/api/status?city=default").json()
    assert st["legacy"] is True and st["total"] == 50 and client.get("/api/status?city=paris").json()["legacy"] is False
    assert client.get("/api/status").json()["legacy"] is False
    assert len(client.get("/api/sensors?state=all&city=default&limit=500").json()) == 50
    assert client.get("/api/sensors/temperature-d01-0003").json()["city"] == "default"
    # the legacy city cannot be created or refilled any more
    res = gateway.accept([no_city(3), reading(3, city="default")])
    assert res["accepted"] == 0 and res["rejected"] == 2
    assert gateway.accept([reading(3, city="paris")])["rejected"] == 1                       # a legacy sensor cannot move either
    # the status engine's 24 h decommissioning leaves legacy sensors alone, and still removes silent sensors of real cities
    with connect() as c:
        c.execute("UPDATE sensor SET last_ts = now() - interval '2 days'")
        status.prune(c)
        c.commit()
        assert c.execute("SELECT count(*) AS n FROM sensor WHERE city='default'").fetchone()["n"] == 50
        assert c.execute("SELECT count(*) AS n FROM sensor WHERE city='paris'").fetchone()["n"] == 0
    # purge the junk with the documented call
    admin = client.delete("/api/cities/default", headers=H)
    assert admin.status_code == 200 and admin.json()["deleted"]["sensors"] == 50 and admin.json()["deleted"]["readings"] == 50
    assert client.get("/api/cities").json()["legacy_city"] is None
    assert client.delete("/api/cities/default", headers=H).status_code == 404


# ---- deleting a city -----------------------------------------------------------------------------------
def seed_city(city, sensors, readings_each, history=0):
    with connect() as c:
        c.execute("INSERT INTO sensor (id, type, district, city, last_ts, last_value) "
                  "SELECT %s || '.t' || lpad(g::text, 5, '0'), 'temperature', 'd01', %s, now(), 20 FROM generate_series(1, %s) g",
                  (city, city, sensors))
        c.execute("INSERT INTO reading (sensor_id, ts, value) SELECT %s || '.t' || lpad(g::text, 5, '0'), now() - make_interval(secs => r), 1 "
                  "FROM generate_series(1, %s) g, generate_series(1, %s) r", (city, sensors, readings_each))
        if history:
            c.execute("INSERT INTO status_history (ts, scope, total, ok) SELECT now() - make_interval(secs => g), %s, 5, 5 "
                      "FROM generate_series(1, %s) g", (f"city:{city}", history))
        c.execute("INSERT INTO chaos (kind, target, intensity, until) VALUES ('noise', %s, 2, now() + interval '5 min')", (f"city:{city}",))
        c.commit()


def counts():
    with connect() as c:
        return {t: c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in ("sensor", "reading", "status_history", "chaos")}


def test_delete_city_removes_everything_of_that_city_only_in_chunks():
    seed_city("paris", 20, 10, history=13)                 # 200 readings
    seed_city("tokyo", 5, 4, history=3)
    with connect() as c:
        c.execute("INSERT INTO status_history (ts, scope, total, ok) VALUES (now(), 'overall', 25, 25), (now(), 'district:d01', 25, 25)")
        c.execute("INSERT INTO chaos (kind, target, intensity, until) VALUES ('noise', 'all', 2, now() + interval '5 min')")
        c.commit()
    before = counts()
    out = purge.delete_city("paris", chunk=7)
    assert out["city"] == "paris" and out["deleted"] == {"chaos": 1, "readings": 200, "status_history": 13, "sensors": 20}
    assert out["batches"] == 1 + 29 + 2 + 3                # ceil(n / 7) batches for each kind: 1, 200 -> 29, 13 -> 2, 20 -> 3
    after = counts()
    assert after == {"sensor": before["sensor"] - 20, "reading": before["reading"] - 200,
                     "status_history": before["status_history"] - 13, "chaos": before["chaos"] - 1}
    with connect() as c:                                   # the other city, overall history and the global burst are untouched
        assert c.execute("SELECT count(*) AS n FROM sensor WHERE city='tokyo'").fetchone()["n"] == 5
        assert c.execute("SELECT count(*) AS n FROM reading WHERE sensor_id LIKE 'tokyo.%%'").fetchone()["n"] == 20
        assert c.execute("SELECT count(*) AS n FROM status_history WHERE scope IN ('city:tokyo','overall','district:d01')").fetchone()["n"] == 5
        assert {r["target"] for r in c.execute("SELECT target FROM chaos")} == {"city:tokyo", "all"}
        assert c.execute("SELECT count(*) AS n FROM reading WHERE sensor_id LIKE 'paris.%%'").fetchone()["n"] == 0
    with pytest.raises(purge.NoSuchCity):
        purge.delete_city("paris")                         # gone


def test_delete_city_commits_in_bounded_transactions(monkeypatch):
    seed_city("big", 30, 5)                                # 150 readings
    sizes = []
    real = purge._drain

    def spy(conn, sql, args, chunk):
        orig = conn.execute

        def execute(q, params=None, **kw):
            cur = orig(q, params, **kw)
            if str(q).startswith("DELETE"):
                sizes.append(cur.rowcount)
            return cur
        conn.execute = execute
        try:
            return real(conn, sql, args, chunk)
        finally:
            del conn.execute
    monkeypatch.setattr(purge, "_drain", spy)
    purge.delete_city("big", chunk=40)
    assert sizes and max(sizes) <= 40 and sum(sizes) == 150 + 30 + 1       # readings + sensors + the burst target


def test_delete_a_large_city_in_5000_row_batches(client):
    seed_city("huge", 60, 250)                             # 15,000 readings: three full batches of 5000 and the empty end
    login(client)
    r = client.delete("/api/cities/huge", headers=H)
    assert r.status_code == 200
    out = r.json()
    assert out["deleted"] == {"chaos": 1, "readings": 15000, "status_history": 0, "sensors": 60}
    assert out["batches"] == 1 + 3 + 0 + 1                 # readings: 5000 + 5000 + 5000 (the empty probe after them is not counted)
    assert counts() == {"sensor": 0, "reading": 0, "status_history": 0, "chaos": 0}
    assert gateway._known["at"] == 0.0                     # the sensor-cap cache is reset


def test_delete_city_endpoint_errors_and_validation(client):
    login(client)
    assert client.delete("/api/cities/nowhere", headers=H).status_code == 404
    for bad in ("Paris", "a b", "x" * 33, "-x"):
        assert client.delete(f"/api/cities/{bad}", headers=H).status_code in (404, 422), bad
    assert client.delete("/api/cities/Paris", headers=H).status_code == 422
    assert client.delete("/api/cities/paris").status_code in (401, 403)       # a browser session needs the CSRF header
    seed_city("paris", 2, 2)
    with connect() as c:                                   # another deletion is running: answer 409, delete nothing
        assert c.execute("SELECT pg_try_advisory_lock(%s) AS ok", (purge.LOCK_ID,)).fetchone()["ok"]
        try:
            assert client.delete("/api/cities/paris", headers=H).status_code == 409
        finally:
            c.execute("SELECT pg_advisory_unlock(%s)", (purge.LOCK_ID,))
            c.commit()
    assert counts()["sensor"] == 2
    assert client.delete("/api/cities/paris", headers=H).status_code == 200


def test_delete_city_permission_per_role_and_token_scope(client):
    login(client)
    seed_city("paris", 2, 2)
    toks = {s: token(client, s) for s in ("ingest", "read", "metrics", "full")}
    client.cookies.clear()
    assert client.delete("/api/cities/paris", headers=H).status_code == 401              # nobody
    for scope in ("ingest", "read", "metrics"):
        assert client.delete("/api/cities/paris", headers=toks[scope]).status_code == 403, scope
    assert counts()["sensor"] == 2
    # roles and scopes as the code maps them: only the wildcard holders (admin role, full scope) have city:delete
    grants = core.APP_AUTH.permissions_for
    assert "city:delete" in core.PERMISSIONS
    assert "*" in grants(roles=["citypulse:admin"])
    assert "city:delete" not in grants(roles=["citypulse:operator"]) and "*" not in grants(roles=["citypulse:operator"])
    assert "city:delete" not in grants(roles=["citypulse:viewer"]) and "*" not in grants(roles=["citypulse:viewer"])
    for scope in ("ingest", "read", "metrics", "citypulse.ingest", "citypulse.read", "citypulse.metrics"):
        assert "city:delete" not in core.SCOPE_PERMISSIONS[scope] and "*" not in core.SCOPE_PERMISSIONS[scope]
    assert core.SCOPE_PERMISSIONS["full"] == core.ALL
    assert client.delete("/api/cities/paris", headers=toks["full"]).status_code == 200


def test_city_values_are_never_interpolated_into_sql():
    evil = "x'; DROP TABLE sensor;--"
    with connect() as c:
        assert status.cities(c, evil)["total"] == 0
        assert status.summary(c, evil)["total"] == 0
        assert status.history(c, 5, evil) == [] and status.throughput(c, 5, evil) == []
        assert chaos.active(c, evil) == []
        assert c.execute("SELECT count(*) AS n FROM sensor").fetchone()["n"] == 0     # still there


def test_reporter_snapshot_reads_real_cities():
    assert reporter._snapshot()["cities"] == {}                                       # empty database
    gateway.accept([reading(1), reading(2)])
    assert reporter._snapshot()["cities"] == {}                                       # one healthy city: nothing to alert on
    with connect() as c:
        c.execute("UPDATE sensor SET status='offline' WHERE id='temperature-d01-0001'")
        c.commit()
    assert list(reporter._snapshot()["cities"]) == ["testville"]                      # one city is treated like many
    with connect() as c:
        c.execute("UPDATE sensor SET status='ok'")
        c.commit()
    fleet()
    snap = reporter._snapshot()
    assert list(snap["cities"]) == ["paris"]                                          # only cities with a problem
    assert snap["cities"]["paris"]["total"] == 4 and "multi" not in snap
    with connect() as c:
        info = status.problem_cities(c)
    assert info == {"count": 4, "problems": ["paris"]}


# ---- districts are whatever the sensors report -----------------------------------------------------
def named_fleet():
    """Two cities with districts of their own: nothing shared, nothing from the catalogue (except one legacy id in rome)."""
    items = [reading(i, district=d, city="oslo") | {"sensor": f"oslo.t{d}{i:03d}"} for d, n in (("alder-quay", 5), ("north-mill", 2)) for i in range(n)]
    items += [reading(i, district=d, city="rome") | {"sensor": f"rome.t{d}{i:03d}"} for d, n in (("d01", 3), ("via_nova", 1)) for i in range(n)]
    assert gateway.accept(items)["rejected"] == 0
    with connect() as c:
        c.execute("UPDATE sensor SET status='fault' WHERE id LIKE 'oslo.talder-quay00%' AND id < 'oslo.talder-quay002'")
        c.commit()


def test_districts_endpoint_lists_what_the_city_reports(client):
    login(client)
    named_fleet()
    o = client.get("/api/cities/oslo/districts").json()
    assert o["city"] == "oslo" and o["total"] == 2 and o["limit"] == 200
    assert [(d["id"], d["name"], d["total"]) for d in o["districts"]] == [("alder-quay", "Alder Quay", 5), ("north-mill", "North Mill", 2)]
    aq = o["districts"][0]
    assert aq["worst_status"] == "fault" and aq["counts"]["fault"] == 2 and aq["counts"]["ok"] == 3 and aq["health"] == 0.6
    assert o["districts"][1]["worst_status"] == "ok" and o["districts"][1]["health"] == 1.0
    r = client.get("/api/cities/rome/districts").json()
    assert [(d["id"], d["name"]) for d in r["districts"]] == [("d01", "Harbor"), ("via_nova", "Via Nova")]   # legacy keeps its catalogue name
    assert client.get("/api/cities/nowhere/districts").status_code == 404
    assert client.get("/api/cities/Bad_City/districts").status_code == 422
    client.cookies.clear()
    assert client.get("/api/cities/oslo/districts").status_code in (401, 403)


def test_districts_endpoint_is_bounded_with_a_total():
    items = [reading(1, district=f"dist-{i:03d}", city="big") | {"sensor": f"big.t{i:04d}"} for i in range(230)]
    items += [reading(2, district="dist-007", city="big") | {"sensor": f"big.extra{i}"} for i in range(3)]
    assert gateway.accept(items)["rejected"] == 0
    with connect() as c:
        out = status.districts(c, "big")
        assert out["total"] == 230 and len(out["districts"]) == 200 and out["districts"][0]["id"] == "dist-007"   # biggest first
        assert len(status.districts(c, "big", 5)["districts"]) == 5
        s = status.summary(c, "big")                                    # the overview is bounded the same way
        assert s["districts_total"] == 230 and len(s["districts"]) == 200
        assert {m["district"] for m in s["matrix"]} <= {d["key"] for d in s["districts"]}
        assert [d["label"] for d in s["districts"]][0] == "Dist 000"


def test_status_matrix_and_labels_use_the_real_districts(client):
    login(client)
    named_fleet()
    s = client.get("/api/status?city=oslo").json()
    assert [(d["key"], d["label"]) for d in s["districts"]] == [("alder-quay", "Alder Quay"), ("north-mill", "North Mill")]
    assert {m["district"] for m in s["matrix"]} == {"alder-quay", "north-mill"} and s["districts_total"] == 2
    g = client.get("/api/status").json()                                # a district id can only exist in one city, or in many
    assert {d["key"] for d in g["districts"]} == {"alder-quay", "north-mill", "d01", "via_nova"}
    assert [r["district"] for r in client.get("/api/sensors?state=all&city=oslo&district=north-mill").json()] == ["north-mill"] * 2
    assert client.get("/api/sensors?district=Bad%20Id").status_code == 422
    assert "d12" in {d["id"] for d in client.get("/api/catalog").json()["districts"]}     # the built-in simulator keeps the catalogue


def test_burst_accepts_any_district_that_has_reported(client):
    login(client)
    named_fleet()
    ok = lambda body: client.post("/api/chaos", json={"kind": "noise", **body}, headers=H).status_code   # noqa: E731
    assert ok({"target": "district:alder-quay", "city": "oslo"}) == 200
    assert ok({"target": "district:alder-quay"}) == 200                  # without a city: reported anywhere
    assert ok({"target": "district:via_nova"}) == 200
    assert ok({"target": "district:never-seen"}) == 422                  # valid id, but nobody reports it
    assert ok({"target": "district:never-seen", "city": "oslo"}) == 422
    assert ok({"target": "district:via_nova", "city": "oslo"}) == 422    # reports in rome, not in the selected city
    assert ok({"target": "district:Bad Id"}) == 422
    assert ok({"target": "district:d07"}) == 200                         # a catalogue id is always accepted (the built-in simulator)
    assert ok({"target": "district:d07", "city": "oslo"}) == 422         # ... unless a city is named and it does not report there


def test_a_district_burst_hits_only_that_district_in_any_city():
    b = chaos.Burst("dropout", "district:alder-quay", 5)
    assert b.matches("rain", "alder-quay", "oslo") and not b.matches("rain", "north-mill", "oslo")


def test_storm_darkens_a_district_that_exists():
    named_fleet()
    with connect() as c:
        ids = chaos.storm(c, 60)
        dark = c.execute("SELECT target FROM chaos WHERE kind='dropout' AND id = ANY(%s)", (ids,)).fetchone()["target"]
        assert dark.removeprefix("district:") in {"alder-quay", "north-mill", "d01", "via_nova"}
        ids = chaos.storm(c, 60, city="oslo")
        assert c.execute("SELECT target FROM chaos WHERE kind='dropout' AND id = ANY(%s)", (ids,)).fetchone()["target"] == "city:oslo"
        assert chaos._districts(c, "oslo") == ["alder-quay", "north-mill"]
    with connect() as c:
        c.execute("DELETE FROM sensor")
        c.commit()
        assert set(chaos._districts(c, None)) == set(DISTRICTS)   # nothing reports: the catalogue


def test_the_metrics_label_districts_by_id():
    named_fleet()
    labels = {x.labels["district"] for f in metrics.FleetCollector().collect() if f.name == "citypulse_district_health_ratio" for x in f.samples}
    assert labels == {"alder-quay", "north-mill", "d01", "via_nova"}
