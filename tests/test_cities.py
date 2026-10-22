"""Multi-city support against a disposable Postgres: ingest, mismatch, OTLP, filters, /api/cities, bursts, upgrade."""
import json
import os
import shutil

import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("set TEST_DATABASE_URL to run DB tests", allow_module_level=True)

from test_db import H, _db, login, otlp_payload, reading, token  # noqa: E402,F401  (the autouse _db fixture resets the schema)

from citypulse import chaos, core, gateway, migrate, reporter, status  # noqa: E402
from citypulse.db import connect  # noqa: E402


def city_of(sensor_id):
    with connect() as c:
        return c.execute("SELECT city FROM sensor WHERE id=%s", (sensor_id,)).fetchone()["city"]


def fleet():
    """Three cities: paris (4 sensors, 1 offline), tokyo (2), and the default city (3)."""
    items = [reading(i, district="d01", city="paris") | {"sensor": f"paris.t{i:04d}"} for i in range(4)]
    items += [reading(i, kind="rain", district="d02", city="tokyo") | {"sensor": f"tokyo.r{i:04d}"} for i in range(2)]
    items += [reading(i, kind="co2") for i in range(3)]          # no city: the default city
    assert gateway.accept(items) == {"accepted": 9, "rejected": 0, "errors": []}
    with connect() as c:
        c.execute("UPDATE sensor SET status='offline' WHERE id='paris.t0000'")
        c.commit()


# ---- ingest ---------------------------------------------------------------------------------------
def test_city_is_optional_and_defaults():
    assert gateway.accept([reading(1), reading(2, city="oslo"), reading(3, city=None)])["accepted"] == 3
    assert city_of("temperature-d01-0001") == "default" and city_of("temperature-d01-0002") == "oslo"
    assert city_of("temperature-d01-0003") == "default"


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
                    content=json.dumps(with_attr(otlp_payload(1), key, "lyon", where)))
    assert r.status_code == 200 and r.json() == {}
    assert city_of("temperature-d02-0007") == "lyon"


def test_otlp_without_city_is_default_and_point_wins_over_resource(client):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    hdr = {**ing, "Content-Type": "application/json"}
    client.post("/v1/metrics", headers=hdr, content=json.dumps(otlp_payload(1)))
    assert city_of("temperature-d02-0007") == "default"
    # an already known sensor under another city is a rejected data point, reported to the sender
    p = with_attr(with_attr(otlp_payload(1), "city", "lyon"), "city", "nice", "point")
    r = client.post("/v1/metrics", headers=hdr, content=json.dumps(p))
    assert r.json()["partialSuccess"]["rejectedDataPoints"] == "1"
    fresh = with_attr(with_attr(otlp_payload(1), "city", "lyon"), "city", "nice", "point")
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
    assert client.get("/api/status?city=default").json()["total"] == 3
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
    assert out["total"] == 3 and [c["city"] for c in out["cities"]] == ["default", "paris", "tokyo"]
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
    assert client.get("/api/cities?q=zz").json() == {"total": 0, "limit": 50, "offset": 0, "cities": []}
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
    assert migrate.run() == ["003_city.sql"]
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
    # the upgraded database behaves like a fresh one: old senders keep working, existing sensors stay in 'default'
    assert gateway.accept([reading(1), reading(60), reading(61, city="paris")])["accepted"] == 3
    assert gateway.accept([reading(2, city="paris")])["rejected"] == 1
    assert city_of("temperature-d01-0060") == "default" and city_of("temperature-d01-0061") == "paris"
    with connect() as c:
        s = status.summary(c)
        assert s["total"] == 52 and status.summary(c, "paris")["total"] == 1
        assert status.evaluate(c) >= 0


def test_city_values_are_never_interpolated_into_sql():
    evil = "x'; DROP TABLE sensor;--"
    with connect() as c:
        assert status.cities(c, evil)["total"] == 0
        assert status.summary(c, evil)["total"] == 0
        assert status.history(c, 5, evil) == [] and status.throughput(c, 5, evil) == []
        assert chaos.active(c, evil) == []
        assert c.execute("SELECT count(*) AS n FROM sensor").fetchone()["n"] == 0     # still there


def test_reporter_snapshot_reads_real_cities():
    assert reporter._snapshot()["multi"] is False                                     # empty database
    gateway.accept([reading(1), reading(2)])
    assert reporter._snapshot()["multi"] is False                                     # only the default city
    fleet()
    snap = reporter._snapshot()
    assert snap["multi"] is True and list(snap["cities"]) == ["paris"]               # only cities with a problem
    assert snap["cities"]["paris"]["total"] == 4
    with connect() as c:
        info = status.problem_cities(c)
    assert info == {"count": 3, "only_default": False, "problems": ["paris"]}
