"""The multi-model features against a disposable Postgres: JSONB device metadata, roll-ups and partitions, the episode memory (labelling,
similarity search where pgvector exists), the demo history, and the one-statement explain across all models. Tests that need pgvector
or PostGIS skip on a server without it; the degradation tests run everywhere."""
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("set TEST_DATABASE_URL to run DB tests", allow_module_level=True)

from test_db import H, _db, login, otlp_payload, reading, token  # noqa: E402,F401  (the autouse _db fixture resets the schema)

from citypulse import cli, explain, gateway, geo, history, memory, migrate, status, timeseries  # noqa: E402
from citypulse.db import connect  # noqa: E402

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _by_hand(monkeypatch):
    """The session-wide TestClient runs the status engine in the background; keep its roll-up and memory passes (and its re-judging of
    sensors, see put_status) from touching the data these tests arrange."""
    monkeypatch.setattr(status, "EXTRAS", False)
    memory.reset_state()


def vector_or_skip():
    with connect() as c:
        ok, reason = memory.status(c, ttl=0)
    if not ok:
        pytest.skip(reason)


def attrs_reading(i, attrs, district="d01", city="paris", **kw):
    return reading(i, district=district, city=city, **kw) | {"sensor": f"{city}.t-{district}-{i:04d}", "attrs": attrs}


def put_status(sensor_ids, status_):
    """Put sensors in a state the status engine agrees with (it runs in the background of the session-wide TestClient and re-judges every
    sensor from its data every 10 s): a fault is an out-of-limits value, stale and offline are old readings, as in real life."""
    data = {"fault": "last_value = 999", "stale": "last_ts = now() - interval '120 seconds'", "offline": "last_ts = now() - interval '1000 seconds'"}[status_]
    with connect() as c:
        c.execute(f"UPDATE sensor SET status = %s, reason = 'test', {data} WHERE id = ANY(%s)", (status_, sensor_ids))
        c.commit()


def stats(city="paris", district="d01", ok=100, total=100, **kw):
    base = {"city": city, "district": district, "total": total, "ok": ok, "stale": total - ok, "offline": 0, "fault": 0, "stuck": 0,
            "delta_mean": 0.005, "delta_p95": 0.015, "burst_share": 0.0, "silent": 0, "rate_ratio": 1.0}
    return base | kw


def put_episode(conn, when, ok, city="paris", district="d01", trigger="schedule", **labels):
    """One stored fingerprint (`ok` of 100 sensors ok) with optional outcome labels."""
    row = memory.row_for(stats(city, district, ok=ok), when, trigger)
    conn.execute(memory.INSERT, row)
    if labels:
        conn.execute("UPDATE episode SET " + ", ".join(f"{k} = %s" for k in labels) + " WHERE city=%s AND district=%s AND window_end=%s",
                     (*labels.values(), city, district, when))


# ---- JSON model: sensor.attrs -----------------------------------------------------------------------------------------------------
ACME = {"vendor": "Aerolab", "firmware": "1.4.2", "install_date": "2024-03-01", "calibration": {"offset": 0.12, "scale": 1.004}, "tags": ["roof"]}
OTHER = {"vendor": "Hydrotek", "firmware": "2.1.4", "calibration": {"offset": -0.3, "scale": 0.998}, "tags": ["pole", "north"]}


def test_attrs_are_stored_replaced_and_kept_when_a_reading_has_none():
    assert gateway.accept([attrs_reading(1, ACME), reading(2, city="paris")])["accepted"] == 2
    with connect() as c:
        row = c.execute("SELECT attrs FROM sensor WHERE id = 'paris.t-d01-0001'").fetchone()
        assert row["attrs"] == ACME and c.execute("SELECT attrs FROM sensor WHERE id = 'temperature-d01-0002'").fetchone()["attrs"] is None
    gateway.accept([attrs_reading(1, ACME) | {"attrs": None, "value": 30.0}])                 # a null/absent attrs never erases
    with connect() as c:
        assert c.execute("SELECT attrs FROM sensor WHERE id = 'paris.t-d01-0001'").fetchone()["attrs"] == ACME
    gateway.accept([attrs_reading(1, OTHER)])                                                 # new attrs replace the object wholesale
    with connect() as c:
        assert c.execute("SELECT attrs FROM sensor WHERE id = 'paris.t-d01-0001'").fetchone()["attrs"] == OTHER


def test_attrs_in_a_multi_city_batch_and_next_to_a_position():
    res = gateway.accept([attrs_reading(1, ACME, city="paris", lat=48.85, lon=2.35), attrs_reading(2, OTHER, city="lyon"),
                          reading(3, city="rome", lat=41.9, lon=12.5)])
    assert res["accepted"] == 3
    with connect() as c:
        rows = {r["id"]: r for r in c.execute("SELECT id, city, attrs, lat, lon FROM sensor")}
    assert rows["paris.t-d01-0001"]["attrs"] == ACME and rows["paris.t-d01-0001"]["lat"] == 48.85
    assert rows["lyon.t-d01-0002"]["attrs"] == OTHER and rows["lyon.t-d01-0002"]["lat"] is None
    assert rows["temperature-d01-0003"]["attrs"] is None and rows["temperature-d01-0003"]["lon"] == 12.5


def test_bad_attrs_reject_the_reading_with_a_reason_and_store_nothing():
    res = gateway.accept([attrs_reading(1, {"api_key": "x"}), attrs_reading(2, "vendor=acme"), attrs_reading(3, {"Vendor": "a"}), attrs_reading(4, ACME)])
    assert res["accepted"] == 1 and res["rejected"] == 3
    assert any("secrets" in e for e in res["errors"]) and any("JSON object" in e for e in res["errors"])
    with connect() as c:
        assert [r["id"] for r in c.execute("SELECT id FROM sensor")] == ["paris.t-d01-0004"]


def test_attrs_through_otlp(client):
    login(client)
    ing = token(client, "ingest")
    good, bad = otlp_payload(n=1), otlp_payload(n=1)
    good["resourceMetrics"][0]["resource"]["attributes"] += [{"key": "attrs.vendor", "value": {"stringValue": "Aerolab"}}]
    bad["resourceMetrics"][0]["resource"]["attributes"] += [{"key": "attrs.password", "value": {"stringValue": "no"}}]
    bad["resourceMetrics"][0]["resource"]["attributes"][0]["value"]["stringValue"] = "temperature-d02-0008"
    assert client.post("/v1/metrics", json=good, headers=ing).status_code == 200
    assert client.post("/v1/metrics", json=bad, headers=ing).status_code == 200
    with connect() as c:
        rows = {r["id"]: r["attrs"] for r in c.execute("SELECT id, attrs FROM sensor")}
    assert rows == {"temperature-d02-0007": {"vendor": "Aerolab"}}, "the credential-like key rejected the whole reading"


def test_attrs_cost_the_per_reading_update_nothing_it_stays_hot():
    """The GIN index is on a column the per-reading upsert never changes (and re-sending identical attrs changes nothing), so the
    `fillfactor 70` HOT updates of `sensor` keep working: the whole point of the design in migration 006."""
    import time
    gateway.accept([attrs_reading(1, ACME)])
    for i in range(30):
        gateway.accept([reading(1, city="paris") | {"sensor": "paris.t-d01-0001", "value": 20.0 + i}])
    gateway.accept([attrs_reading(1, ACME)])
    row = None
    for _ in range(40):                      # backends report their statistics when idle, about a second later
        with connect() as c:
            row = c.execute("SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname = 'sensor'").fetchone()
        if row["n_tup_upd"] >= 31:
            break
        time.sleep(0.25)
    assert row["n_tup_upd"] >= 31 and row["n_tup_hot_upd"] == row["n_tup_upd"], row


def test_gin_index_exists_with_jsonb_path_ops_and_serves_containment_and_jsonpath():
    with connect() as c:
        d = c.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'sensor_attrs_gin'").fetchone()["indexdef"]
        assert "USING gin" in d and "jsonb_path_ops" in d
        items = [attrs_reading(i, ACME if i % 2 else OTHER) for i in range(1, 400)]
        gateway.accept(items)
        c.execute("ANALYZE sensor")
        c.execute("SET enable_seqscan = off")               # force the choice: the question is whether the index CAN serve the operator
        for where in ("attrs @> '{\"vendor\": \"Aerolab\"}'::jsonb", "attrs @? '$.calibration ? (@.offset > 0.1)'::jsonpath"):
            plan = "\n".join(next(iter(r.values())) for r in c.execute("EXPLAIN SELECT id FROM sensor WHERE " + where))
            assert "sensor_attrs_gin" in plan, plan


def test_attrs_shape_check_is_the_second_line_of_defence():
    gateway.accept([reading(1, city="paris")])
    with connect() as c:
        with pytest.raises(Exception, match="sensor_attrs_shape"):
            c.execute("UPDATE sensor SET attrs = '[1,2]'::jsonb")
        c.rollback()
        with pytest.raises(Exception, match="sensor_attrs_shape"):
            c.execute("UPDATE sensor SET attrs = (SELECT jsonb_build_object('a', string_agg(md5(i::text), '')) FROM generate_series(1, 300) i)")


def test_sensors_api_filters_on_attrs_with_containment_and_jsonpath(client):
    login(client)
    ing, rd = token(client, "ingest"), token(client, "read")
    items = [attrs_reading(i, ACME) for i in range(1, 4)] + [attrs_reading(i, OTHER) for i in range(4, 8)] + [reading(9, city="paris")]
    assert client.post("/api/readings", json={"readings": items}, headers=ing).json()["accepted"] == 8
    get = lambda **q: client.get("/api/sensors", params={"state": "all", **q}, headers=rd)       # noqa: E731
    r = get(attrs=json.dumps({"vendor": "Aerolab"}))
    assert r.status_code == 200 and len(r.json()) == 3 and all(s["attrs"]["vendor"] == "Aerolab" for s in r.json())
    assert len(get(attrs='{"firmware":"2.1.4","calibration":{"scale":0.998}}').json()) == 4, "containment reaches into nested objects"
    assert len(get(attrs='{"tags":["north"]}').json()) == 4, "and into arrays"
    assert get(attrs='{"vendor":"Nobody"}').json() == []
    assert len(get(attrs_path="$.calibration ? (@.offset > 0.1)").json()) == 3
    assert len(get(attrs_path='$.tags[*] ? (@ == "pole")').json()) == 4
    assert len(get(attrs=json.dumps({"vendor": "Hydrotek"}), district="d01", city="paris", type="temperature").json()) == 4, "combines with the other filters"
    for bad in ({"attrs": "not json"}, {"attrs": "[1]"}, {"attrs": "{}"}, {"attrs_path": "$.a ? (@ >"}, {"attrs_path": "banana"}):
        assert get(**bad).status_code == 422, bad
    assert all("attrs" in sensor for sensor in get().json()), "every sensor row carries its metadata (null when it has none)"
    one = client.get("/api/sensors/paris.t-d01-0001", headers=rd).json()
    assert one["attrs"] == ACME, "the sensor detail carries its metadata"


# ---- time series: roll-ups and partitions ---------------------------------------------------------------------------------------------
def months_with_partitions():
    with connect() as c:
        return {r["relname"] for r in c.execute("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                                                "WHERE i.inhparent = 'rollup_5m'::regclass")}


def test_rollup_partitions_cover_this_month_and_the_next_two_and_are_idempotent():
    now = datetime.now(UTC)
    want = {timeseries.partition_name(y, m) for y, m in timeseries._months(now, now + timedelta(days=62))}
    assert want <= months_with_partitions()
    with connect() as c:
        names = timeseries.ensure_partitions(c, now - timedelta(days=40), now)
        assert len(names) >= 3 and timeseries.ensure_partitions(c, now - timedelta(days=40), now) == names
        c.commit()
        kind = c.execute("SELECT relkind FROM pg_class WHERE relname = 'rollup_5m'").fetchone()["relkind"]
        assert kind == "p", "declaratively partitioned"
        assert c.execute("SELECT relkind FROM pg_class WHERE relname = 'rollup_1h'").fetchone()["relkind"] == "r"


def feed(n_sensors=6, per=4, district="d01", city="paris", kind="temperature", value=20.0, minutes_ago=(2, 7, 12, 17)):
    items = []
    for i in range(n_sensors):
        for m in minutes_ago[:per]:
            ts = datetime.now(UTC) - timedelta(minutes=m)
            items.append({"sensor": f"{city}.{kind}-{district}-{i:04d}", "type": kind, "district": district, "city": city, "value": value + i, "ts": ts.isoformat()})
    assert gateway.accept(items)["accepted"] == len(items)


def test_rollup_5m_counts_sensors_and_out_of_range_and_is_idempotent():
    feed()
    gateway.accept([{"sensor": "paris.temperature-d01-0000", "type": "temperature", "district": "d01", "city": "paris", "value": 999.0,
                     "ts": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()}])
    with connect() as c:
        n = timeseries.refresh(c, "5m")
        c.commit()
        assert n >= 3
        before = c.execute("SELECT * FROM rollup_5m ORDER BY bucket").fetchall()
        assert timeseries.refresh(c, "5m") == n and c.execute("SELECT * FROM rollup_5m ORDER BY bucket").fetchall() == before, "an upsert, not an append"
        total = c.execute("SELECT sum(readings) AS n, sum(out_of_range) AS bad, max(sensors) AS s, min(min_value) AS lo, max(max_value) AS hi FROM rollup_5m").fetchone()
        assert total["n"] == 6 * 4 + 1 and total["bad"] == 1 and total["s"] == 6 and total["hi"] == 999.0 and total["lo"] == 20.0
        assert all(r["bucket"].minute % 5 == 0 and r["bucket"].second == 0 for r in before), "date_bin aligns buckets"
        assert c.execute("SELECT count(*) AS n FROM rollup_5m").fetchone()["n"] == len(before)


def test_late_readings_land_in_an_old_bucket_inside_the_refresh_horizon():
    feed(per=1)
    with connect() as c:
        timeseries.refresh(c, "5m")
        first = c.execute("SELECT sum(readings) AS n FROM rollup_5m").fetchone()["n"]
        gateway.accept([{"sensor": "paris.temperature-d01-0000", "type": "temperature", "district": "d01", "city": "paris", "value": 21.0,
                         "ts": (datetime.now(UTC) - timedelta(minutes=20)).isoformat()}])
        timeseries.refresh(c, "5m")
        assert c.execute("SELECT sum(readings) AS n FROM rollup_5m").fetchone()["n"] == first + 1


def test_refresh_never_overwrites_a_bucket_whose_raw_rows_may_be_pruned(monkeypatch):
    """With a retention of 6 minutes, a bucket older than that would be recomputed from a partial set of rows."""
    monkeypatch.setenv("RETENTION_HOURS", "0.1")
    with connect() as c:
        bucket = c.execute("SELECT date_bin('5 minutes', now() - interval '20 minutes', TIMESTAMPTZ '2000-01-01') AS b").fetchone()["b"]
        gateway.accept([{"sensor": "paris.temperature-d01-0000", "type": "temperature", "district": "d01", "city": "paris", "value": 20.0,
                         "ts": (bucket + timedelta(minutes=1)).isoformat()}])
        c.execute("INSERT INTO rollup_5m VALUES ('paris', 'd01', 'temperature', %s, 99, 5, 0, 20, 19, 21)", (bucket,))
        timeseries.refresh(c, "5m")
        assert c.execute("SELECT readings FROM rollup_5m WHERE bucket = %s", (bucket,)).fetchone()["readings"] == 99, "history kept, not shrunk"


def test_rollup_1h_and_prune_drops_whole_old_partitions():
    feed()
    with connect() as c:
        timeseries.refresh(c, "1h")
        assert c.execute("SELECT sum(readings) AS n FROM rollup_1h").fetchone()["n"] == 24
        ancient = datetime(2020, 1, 15, tzinfo=UTC)
        timeseries.ensure_partitions(c, ancient, ancient)
        c.execute("INSERT INTO rollup_5m VALUES ('paris', 'd01', 'temperature', %s, 1, 1, 0, 1, 1, 1)", (ancient,))
        c.execute("INSERT INTO rollup_1h VALUES ('paris', 'd01', 'temperature', %s, 1, 1, 0, 1, 1, 1)", (ancient,))
        out = timeseries.prune(c)
        assert "rollup_5m_202001" in out["dropped_partitions"] and out["deleted_1h"] == 1
        assert "rollup_5m_202001" not in months_with_partitions()
        assert c.execute("SELECT count(*) AS n FROM rollup_5m").fetchone()["n"] == 0


def test_a_reading_outside_every_partition_is_refused_loudly_not_lost_in_a_default():
    with connect() as c:
        with pytest.raises(Exception, match="no partition"):
            c.execute("INSERT INTO rollup_5m VALUES ('paris', 'd01', 'temperature', '1999-01-01', 1, 1, 0, 1, 1, 1)")


def test_series_api(client):
    login(client)
    rd, ing = token(client, "read"), token(client, "ingest")
    feed(district="d01")
    feed(district="d02", city="paris")
    with connect() as c:
        timeseries.refresh(c, "5m")
        timeseries.refresh(c, "1h")
        c.commit()
    r = client.get("/api/districts/d01/series", params={"city": "paris"}, headers=rd)
    assert r.status_code == 200
    body = r.json()
    assert body["bucket"] == "5m" and body["count"] >= 3 and sum(p["readings"] for p in body["points"]) == 24
    assert all(set(p) == {"bucket", "readings", "sensors", "out_of_range"} for p in body["points"])
    assert [p["bucket"] for p in body["points"]] == sorted(p["bucket"] for p in body["points"])
    typed = client.get("/api/districts/d01/series", params={"city": "paris", "type": "temperature"}, headers=rd).json()
    assert all({"avg", "min", "max"} <= set(p) for p in typed["points"]) and typed["points"][0]["avg"] == pytest.approx(22.5)
    assert client.get("/api/districts/d01/series", params={"city": "paris", "type": "rain"}, headers=rd).json()["points"] == []
    hourly = client.get("/api/districts/d01/series", params={"city": "paris", "bucket": "1h"}, headers=rd).json()
    assert hourly["bucket"] == "1h" and sum(p["readings"] for p in hourly["points"]) == 24
    assert client.get("/api/districts/d01/series", params={"city": "lyon"}, headers=rd).json()["points"] == [], "district ids are per city"
    start = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    narrow = client.get("/api/districts/d01/series", params={"city": "paris", "from": start, "to": datetime.now(UTC).isoformat()}, headers=rd).json()
    assert 0 < sum(p["readings"] for p in narrow["points"]) < 24
    for bad in ({"city": "paris", "bucket": "1d"}, {"city": "paris", "type": "teleporter"}, {}, {"city": "Bad City"},
                {"city": "paris", "from": "2020-01-01T00:00:00Z", "to": "2020-06-01T00:00:00Z"},
                {"city": "paris", "from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"}):
        assert client.get("/api/districts/d01/series", params=bad, headers=rd).status_code == 422, bad
    assert client.get("/api/districts/d01/series", params={"city": "paris"}, headers=ing).status_code == 403
    client.cookies.clear()
    assert client.get("/api/districts/d01/series", params={"city": "paris"}).status_code in (401, 403)
    assert client.get("/api/districts/geojson", headers=rd).status_code == 200, "the geojson route still wins over {district_id}"


def test_the_engine_refreshes_rollups_in_its_minute(monkeypatch):
    monkeypatch.setattr(status, "EXTRAS", True)
    feed()
    status._last.update(history=0.0, prune=0.0)
    assert status.tick()
    with connect() as c:
        assert c.execute("SELECT sum(readings) AS n FROM rollup_5m").fetchone()["n"] == 24
        assert c.execute("SELECT sum(readings) AS n FROM rollup_1h").fetchone()["n"] == 24


# ---- vector pattern memory ------------------------------------------------------------------------------------------------------------
def test_ensure_adds_the_generated_embedding_column_and_an_hnsw_cosine_index():
    vector_or_skip()
    with connect() as c:
        col = c.execute("SELECT format_type(atttypid, atttypmod) AS t, attgenerated FROM pg_attribute "
                        "WHERE attrelid = 'episode'::regclass AND attname = 'embedding'").fetchone()
        assert col == {"t": "vector(16)", "attgenerated": "s"}
        d = c.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'episode_embedding_hnsw'").fetchone()["indexdef"]
        assert "USING hnsw" in d and "vector_cosine_ops" in d
        assert memory.ensure(c)["available"] is True and memory.ensure(c)["available"] is True, "idempotent"
        put_episode(c, NOW, 60)
        c.commit()
        assert c.execute("SELECT embedding::text AS e, features FROM episode").fetchone()["e"].count(",") == 15


def test_a_database_without_pgvector_still_records_and_says_so(monkeypatch):
    """The fingerprints are plain real[]: recorded either way. Only the search needs pgvector."""
    with connect() as c:
        put_episode(c, NOW - timedelta(hours=2), 60, verdict_1h="operational")
        c.commit()
        assert c.execute("SELECT cardinality(features) AS n FROM episode").fetchone()["n"] == 16
    monkeypatch.setattr(memory, "status", lambda conn, ttl=30.0: (False, memory.NO_VECTOR))
    with connect() as c:
        out = memory.similar(c, "paris", "d01")
        assert out == {"available": False, "reason": memory.NO_VECTOR, "city": "paris", "district": "d01", "similar": []}


def test_labelling_fills_the_next_hour_verdict_and_how_long_a_bad_stretch_lasted():
    base = (NOW - timedelta(hours=6)).replace(second=0, microsecond=0)
    at = lambda i: base + timedelta(minutes=15 * i)                                              # noqa: E731
    #        0     1     2    3    4    5    6    7    8   9
    oks = [100, 100, 40, 60, 90, 100, 100, 100, 30, 20]
    with connect() as c:
        for i, ok in enumerate(oks):
            put_episode(c, at(i), ok)
        put_episode(c, at(0), 30, district="d09")                                                 # starts bad: its start is unknown, so unlabelled
        for i, ok in enumerate([100, 20, 100]):
            put_episode(c, at(i + 1), ok, district="d09")
        assert memory.label(c) > 0
        assert memory.label(c) == 0, "idempotent"
        rows = {r["window_end"]: r for r in c.execute("SELECT * FROM episode WHERE district = 'd01' ORDER BY window_end")}
    r = [rows[at(i)] for i in range(len(oks))]
    # the verdict one hour later: the newest fingerprint within (t, t + 1 h]
    assert r[0]["verdict_1h"] == "degraded"           # at(4) is 90 % ok
    assert r[1]["verdict_1h"] == "operational" and r[2]["verdict_1h"] == "operational"
    assert r[5]["verdict_1h"] == "major_outage"       # at(9) is 20 % ok: partial window (t, t+1h] stops at at(9)
    assert r[9]["verdict_1h"] is None, "nothing later yet: stays unlabelled"
    # the first bad stretch: rows 2..4 (verdicts major, partial, degraded), began at(2), ended at(5): 45 minutes
    for i in (2, 3, 4):
        assert r[i]["episode_start"] == at(2) and r[i]["ended_at"] == at(5) and r[i]["duration_min"] == 45.0
    for i in (0, 1, 5, 6, 7):
        assert r[i]["duration_min"] is None and r[i]["ended_at"] is None, "operational rows have no episode"
    for i in (8, 9):
        assert r[i]["ended_at"] is None, "an episode that is still going has no end"
    with connect() as c:
        d9 = {x["window_end"]: x for x in c.execute("SELECT * FROM episode WHERE district = 'd09'")}
    assert d9[at(0)]["duration_min"] is None, "a stretch already running at the start of the data has an unknown beginning"
    assert d9[at(2)]["duration_min"] == 15.0 and d9[at(2)]["episode_start"] == at(2) and d9[at(2)]["ended_at"] == at(3)


def unit_vec(*hot):
    """A stored feature vector from sparse dims (cosine only cares about direction)."""
    v = [0.0] * 16
    for i, x in hot:
        v[i] = x
    return v


def put_raw(conn, city, district, when, features, verdict, verdict_1h, duration=None, ended=None):
    conn.execute("INSERT INTO episode (city, district, window_end, window_secs, trigger, verdict, health, sensors, features, verdict_1h, duration_min, ended_at) "
                 "VALUES (%s, %s, %s, 900, 'seed', %s, 0.5, 100, %s::real[], %s, %s, %s)",
                 (city, district, when, verdict, features, verdict_1h, duration, ended))


def test_similar_returns_the_nearest_past_episodes_with_their_outcomes_and_honours_the_rules():
    vector_or_skip()
    cur = unit_vec((0, 0.4), (2, 0.8), (7, 1.0), (8, 0.7))                  # a dropout-like fingerprint
    with connect() as c:
        t = NOW - timedelta(days=2)
        put_raw(c, "paris", "d01", NOW - timedelta(minutes=1), cur, "major_outage", None)                          # the current one
        put_raw(c, "paris", "d02", t, unit_vec((0, 0.4), (2, 0.8), (7, 1.0), (8, 0.7)), "major_outage", "operational", 45.0, t + timedelta(minutes=45))   # same
        put_raw(c, "lyon", "d05", t, unit_vec((0, 0.45), (2, 0.7), (7, 1.0), (8, 0.6)), "partial_outage", "partial_outage", 90.0, t + timedelta(minutes=90))   # close
        put_raw(c, "paris", "d03", t, unit_vec((0, 0.9), (3, 0.9), (5, 0.8), (9, 0.5)), "partial_outage", "operational", 30.0, t + timedelta(minutes=30))   # other trouble
        put_raw(c, "paris", "d04", t, unit_vec((0, 1.0), (11, 0.3)), "operational", "operational")                  # healthy: filtered while in trouble
        put_raw(c, "paris", "d01", NOW - timedelta(hours=2), cur, "major_outage", "major_outage")                  # the same district two hours ago: same incident, excluded
        put_raw(c, "paris", "d06", NOW - timedelta(minutes=20), cur, "major_outage", "major_outage")               # too recent
        put_raw(c, "paris", "d07", t, cur, "major_outage", None)                                                   # outcome unknown yet
        c.commit()
        out = memory.similar(c, "paris", "d01", k=5)
        assert out["available"] and out["current"]["verdict"] == "major_outage" and out["current"]["features"]["burst"] == 1.0
        got = [(s["city"], s["district"]) for s in out["similar"]]
        assert got == [("paris", "d02"), ("lyon", "d05"), ("paris", "d03")], got
        first = out["similar"][0]
        assert first["distance"] == pytest.approx(0.0, abs=1e-6)
        assert first["outcome"]["verdict_1h"] == "operational" and first["outcome"]["episode_lasted_min"] == 45.0 and first["outcome"]["resolved_after_min"] == 45.0
        assert out["similar"][1]["distance"] < out["similar"][2]["distance"]
        assert [s["district"] for s in memory.similar(c, "paris", "d01", k=1)["similar"]] == ["d02"]
        assert [s["city"] for s in memory.similar(c, "paris", "d01", k=5, scope="city")["similar"]] == ["paris", "paris"]
        assert memory.similar(c, "paris", "nowhere")["similar"] == [] and "no fingerprint" in memory.similar(c, "paris", "nowhere")["note"]


def test_one_past_incident_is_listed_once_however_many_windows_it_left():
    vector_or_skip()
    with connect() as c:
        t = NOW - timedelta(days=2)
        put_raw(c, "paris", "d01", NOW - timedelta(minutes=1), unit_vec((0, 0.4), (7, 1.0)), "major_outage", None)
        for i, x in enumerate((0.40, 0.42, 0.45, 0.50)):                    # four windows of ONE incident at paris d02
            put_raw(c, "paris", "d02", t + timedelta(minutes=15 * i), unit_vec((0, x), (7, 1.0)), "major_outage", "operational", 60.0, t + timedelta(hours=2))
        c.execute("UPDATE episode SET episode_start = %s WHERE district = 'd02'", (t,))
        put_raw(c, "paris", "d03", t, unit_vec((0, 0.9), (7, 1.0)), "major_outage", "operational", 30.0, t + timedelta(minutes=30))   # a different incident
        c.commit()
        out = memory.similar(c, "paris", "d01", k=3)["similar"]
        assert [(s["district"], s["window_end"]) for s in out] == [("d02", t), ("d03", t)], "the closest window of each incident, nothing else"


def test_a_healthy_district_is_matched_with_healthy_history():
    vector_or_skip()
    with connect() as c:
        t = NOW - timedelta(days=1)
        put_raw(c, "paris", "d01", NOW, unit_vec((0, 1.0), (11, 0.3), (12, 0.4)), "operational", None)
        put_raw(c, "paris", "d02", t, unit_vec((0, 1.0), (11, 0.3), (12, 0.4)), "operational", "operational")
        put_raw(c, "paris", "d03", t, unit_vec((0, 0.3), (2, 0.9), (7, 1.0)), "major_outage", "operational")
        out = memory.similar(c, "paris", "d01")
        assert [s["district"] for s in out["similar"]] == ["d02", "d03"], "no problem-only filter for a healthy district"


def build_district(city="paris", district="d03"):
    """Twelve sensors: six old-firmware Aerolab (five faulty), six Hydrotek (one stale), plus a healthy neighbour district."""
    items = [attrs_reading(i, ACME | {"firmware": "1.4.2"}, district=district, city=city) for i in range(1, 7)]
    items += [attrs_reading(i, OTHER, district=district, city=city) for i in range(7, 13)]
    items += [attrs_reading(i, OTHER, district="d04", city=city) for i in range(20, 30)]
    assert gateway.accept(items)["accepted"] == 22
    put_status([f"{city}.t-{district}-{i:04d}" for i in range(1, 6)], "fault")
    put_status([f"{city}.t-{district}-0007"], "stale")
    with connect() as c:
        timeseries.refresh(c, "5m")
        c.commit()


class Counting:
    """A connection that counts the statements sent through it."""

    def __init__(self, conn):
        self.conn, self.n = conn, 0

    def execute(self, *a, **k):
        self.n += 1
        return self.conn.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_explain_answers_five_models_in_one_statement():
    build_district()
    with connect() as c:
        out = explain.explain(c, "paris", "d03")                       # warms the 30 s availability caches
        counted = Counting(c)
        out = explain.explain(counted, "paris", "d03", k=3)
        assert counted.n == 1, "ONE round trip once the availability checks are cached"
    assert out["round_trips"] == 1 and out["models"] == 5 and out["verdict"] == "partial_outage" and out["health"] == pytest.approx(6 / 12)
    rel = out["relational"]
    assert rel["sensors"] == 12 and rel["counts"] == {"ok": 6, "stale": 1, "offline": 0, "fault": 5, "stuck": 0}
    assert rel["problem_types"] == [{"type": "temperature", "problems": 6}] and rel["with_attrs"] == 12
    ts = out["timeseries"]
    assert ts["source"].startswith("rollup_5m") and sum(p["readings"] for p in ts["points"]) == 12 and ts["points"][0]["sensors"] >= 1
    js = out["json"]
    top = js["attributes"][0]
    assert (top["key"], top["value"]) in {("firmware", "1.4.2"), ("vendor", "Aerolab")}, "both describe the same six sensors: the lift is identical"
    assert top["over_represented"] and top["problem_sensors"] == 5 and top["share_of_problems"] == pytest.approx(5 / 6, abs=1e-3)
    assert top["share_of_city"] == pytest.approx(6 / 22, abs=1e-3) and top["lift"] == pytest.approx((5 / 6) / (6 / 22), abs=0.01)
    hydro = next(a for a in js["attributes"] if a["value"] == "Hydrotek")
    assert hydro["problem_sensors"] == 1 and not hydro["over_represented"]
    assert out["sql"].count("WITH") >= 1 and "%(city)s" in out["sql"]
    json.dumps(out, default=str)


@pytest.mark.skipif(os.environ.get("CITYPULSE_EXPECT_POSTGIS") != "1", reason="PostGIS (set CITYPULSE_EXPECT_POSTGIS=1 on a server that has it)")
def test_explain_geo_part_with_postgis():
    build_district()
    with connect() as c:
        for i in range(1, 13):
            c.execute("UPDATE sensor SET lat = 48.85 + %s * 0.0005, lon = 2.35 WHERE id = %s", (i, f"paris.t-d03-{i:04d}"))
        for i in range(20, 30):
            c.execute("UPDATE sensor SET lat = 48.86 + %s * 0.0005, lon = 2.36 WHERE id = %s", (i, f"paris.t-d04-{i:04d}"))
        c.commit()
        geo.reset_cache()
        out = explain.explain(c, "paris", "d03")["geo"]
    assert out["available"] and out["centre"]["basis"] == "problem sensors" and len(out["nearest_healthy"]) == 5
    assert out["nearest_healthy"] == sorted(out["nearest_healthy"], key=lambda n: n["distance_m"])
    assert out["neighbours"][0]["district"] in {"d02", "d04", "d07", "d01", "d05", "d09"}


def test_explain_degrades_part_by_part_and_keeps_the_other_models(monkeypatch):
    build_district()
    monkeypatch.setattr(memory, "status", lambda conn, ttl=30.0: (False, memory.NO_VECTOR))
    monkeypatch.setattr(geo, "status", lambda conn, ttl=30.0: (False, geo.NO_POSTGIS))
    with connect() as c:
        out = explain.explain(c, "paris", "d03")
    assert out["geo"] == {"available": False, "reason": geo.NO_POSTGIS} and out["vector"] == {"available": False, "reason": memory.NO_VECTOR}
    assert out["relational"]["sensors"] == 12 and out["timeseries"]["points"] and out["json"]["attributes"], "relational, time series and JSON are unaffected"
    assert "ST_" not in out["sql"] and "<=>" not in out["sql"]


def test_explain_on_an_unknown_district_is_a_clear_error():
    build_district()
    with connect() as c, pytest.raises(explain.NoSuchDistrict, match="nowhere"):
        explain.explain(c, "paris", "nowhere")
    with connect() as c, pytest.raises(explain.NoSuchDistrict):
        explain.explain(c, "lyon", "d03")


def test_explain_finds_similar_past_episodes_and_predicts_from_them():
    vector_or_skip()
    build_district()
    with connect() as c:
        out = history.seed(c, ["paris", "lyon"], days=10)
        assert out["inserted"] == 10 * 96 * 12 * 2 and out["similarity_search"]
        assert memory.record(c, datetime.now(UTC).replace(microsecond=0), "schedule", [("paris", "d03")]) == 1
        c.commit()
        res = explain.explain(c, "paris", "d03", k=3)
    vec = res["vector"]
    assert vec["available"] and vec["current"]["verdict"] == "partial_outage" and len(vec["similar"]) == 3
    assert [s["distance"] for s in vec["similar"]] == sorted(s["distance"] for s in vec["similar"])
    assert all(s["verdict"] != "operational" and s["next_hour_verdict"] for s in vec["similar"]), "a troubled district meets troubled history"
    assert vec["prediction"]["based_on"] == 3 and sum(vec["prediction"]["verdict_one_hour_later"].values()) == 3


def test_explain_without_iterative_scans_still_runs_on_pgvector_before_0_8(monkeypatch):
    """The statement for pgvector < 0.8 has no cfg CTE; here it runs on a newer server to prove it is valid SQL and answers."""
    vector_or_skip()
    build_district()
    with connect() as c:
        history.seed(c, ["paris"], days=3)
        memory.record(c, datetime.now(UTC).replace(microsecond=0), "schedule", [("paris", "d03")])
        c.commit()
        explain.explain(c, "paris", "d03")                                                         # refresh the availability cache
        monkeypatch.setattr(memory, "iterative_scan_supported", lambda: False)
        out = explain.explain(c, "paris", "d03")
    assert "set_config" not in out["sql"] and out["vector"]["available"] and out["vector"]["current"]["verdict"] == "partial_outage"
    assert isinstance(out["vector"]["similar"], list)


def test_record_fingerprints_the_live_districts_and_ticks_react_to_verdict_changes():
    gateway.accept([reading(i, district="d01", city="paris") | {"sensor": f"paris.t-d01-{i:04d}"} for i in range(1, 11)])
    boundary = memory.floor_window(datetime.now(UTC))
    memory.reset_state()
    with connect() as c:
        first = memory.tick(c, boundary + timedelta(seconds=5))
        assert first["changed"] == 0 and first["scheduled"] == 1, "the first pass has nothing to compare with; the quarter hour fingerprints"
        row = c.execute("SELECT * FROM episode").fetchone()
        assert (row["city"], row["district"], row["trigger"], row["verdict"], row["sensors"], row["window_end"]) == ("paris", "d01", "schedule", "operational", 10, boundary)
        assert len(row["features"]) == 16 and row["features"][0] == 1.0 and row["features"][7] == 0.0
        assert memory.tick(c, boundary + timedelta(seconds=35)) == {"changed": 0, "scheduled": 0, "labelled": 0}, "same window: nothing new"
        put_status([f"paris.t-d01-{i:04d}" for i in range(1, 9)], "offline")
        changed = memory.tick(c, boundary + timedelta(seconds=65))
        assert changed["changed"] == 1
        vc = c.execute("SELECT * FROM episode WHERE trigger = 'verdict_change'").fetchone()
        assert vc["verdict"] == "major_outage" and vc["health"] == pytest.approx(0.2) and vc["features"][2] == pytest.approx((0.8) ** 0.5, abs=1e-5)
        assert memory.tick(c, boundary + timedelta(seconds=95))["changed"] == 0, "no change, no new fingerprint"
        late = memory.tick(c, boundary + timedelta(seconds=600))
        assert late["scheduled"] == 0, "too long after the boundary: the statuses no longer describe that window"


def test_a_burst_shows_up_in_the_fingerprint():
    from citypulse import chaos
    gateway.accept([reading(i, district="d01", city="paris") | {"sensor": f"paris.t-d01-{i:04d}"} for i in range(1, 5)])
    with connect() as c:
        chaos.start(c, "dropout", "district:d01", 3, 600, "paris")
        c.commit()
        rows = memory.window_stats(c, datetime.now(UTC) + timedelta(seconds=600))     # the window ends as the burst does: 600 of its 900 s
        assert len(rows) == 1 and rows[0]["burst_share"] == pytest.approx(600 / 900, abs=0.01)
        quiet = memory.window_stats(c, datetime.now(UTC) + timedelta(hours=2))[0]
        assert quiet["burst_share"] == 0.0 and quiet["silent"] == 4


def test_seed_history_is_deterministic_idempotent_labelled_and_removable():
    with connect() as c:
        now = datetime.now(UTC).replace(second=0, microsecond=0)         # relative to the wall clock: labelling compares with now()
        last = memory.floor_window(now)
        out = history.seed(c, ["paris"], days=14, now=now)
        assert out["inserted"] == 14 * 96 * 12 and out["windows"] == 14 * 96
        digest = c.execute("SELECT md5(string_agg(district || window_end::text || features::text, ',' ORDER BY district, window_end)) AS m FROM episode").fetchone()["m"]
        again = history.seed(c, ["paris"], days=14, now=now)
        assert again["inserted"] == 0, "re-running adds nothing"
        stats_ = c.execute("SELECT count(*) AS n, count(verdict_1h) AS labelled, count(duration_min) AS lasted, "
                           "count(*) FILTER (WHERE verdict <> 'operational') AS bad, max(window_end) AS last FROM episode").fetchone()
        assert stats_["last"] == last and stats_["labelled"] > 0.99 * stats_["n"]
        assert 0 < stats_["bad"] < 0.1 * stats_["n"] and stats_["lasted"] > 0.5 * stats_["bad"], "episodes ended and know how long they lasted"
        lasted = c.execute("SELECT min(duration_min) AS lo, max(duration_min) AS hi FROM episode WHERE duration_min IS NOT NULL").fetchone()
        assert 15 <= lasted["lo"] and lasted["hi"] <= 9 * 15
        c.execute("DELETE FROM episode")
        history.seed(c, ["paris"], days=14, now=now)
        assert c.execute("SELECT md5(string_agg(district || window_end::text || features::text, ',' ORDER BY district, window_end)) AS m FROM episode").fetchone()["m"] == digest
        live = memory.row_for(stats(), now, "schedule")
        c.execute(memory.INSERT, (*live[:2], now + timedelta(hours=1), *live[3:]))
        assert history.seed(c, ["paris"], days=14, now=now, reset=True)["removed"] == 14 * 96 * 12
        assert c.execute("SELECT count(*) FILTER (WHERE trigger = 'schedule') AS live FROM episode").fetchone()["live"] == 1, "reset only removes seeded rows"


def test_seed_history_cli(capsys):
    cli.main(["seed-history", "--cities", "lyon", "--days", "2"])
    out = capsys.readouterr().out
    assert "seeded 2304 episodes" in out and "lyon" in out
    with pytest.raises(SystemExit) as e:
        cli.main(["seed-history", "--cities", "atlantis"])
    assert "atlantis" in str(e.value)


def test_deleting_a_city_deletes_its_rollups_and_episodes(client):
    login(client)
    build_district()
    with connect() as c:
        put_episode(c, NOW - timedelta(hours=1), 50)
        put_episode(c, NOW - timedelta(hours=1), 50, city="lyon")
        c.commit()
    admin = client.delete("/api/cities/paris", headers=H)
    assert admin.status_code == 200 and admin.json()["deleted"]["rollups"] >= 1 and admin.json()["deleted"]["episodes"] == 1
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM rollup_5m WHERE city = 'paris'").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM episode").fetchone()["n"] == 1


# ---- the API ----------------------------------------------------------------------------------------------------------------------------
def test_explain_similar_and_tools_over_http(client):
    login(client)
    rd, ing = token(client, "read"), token(client, "ingest")
    build_district()
    r = client.get("/api/districts/d03/explain", params={"city": "paris"}, headers=rd)
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "partial_outage" and body["round_trips"] == 1 and set(body) >= {"relational", "timeseries", "json", "geo", "vector", "sql"}
    assert client.get("/api/districts/d03/explain", params={"city": "paris", "k": 99}, headers=rd).status_code == 422
    assert client.get("/api/districts/d03/explain", headers=rd).status_code == 422, "city is required: a district id is per city"
    assert client.get("/api/districts/nowhere/explain", params={"city": "paris"}, headers=rd).status_code == 404
    assert client.get("/api/districts/d03/explain", params={"city": "paris"}, headers=ing).status_code == 403
    sim = client.get("/api/districts/d03/similar", params={"city": "paris", "k": 2}, headers=rd)
    assert sim.status_code == 200 and "available" in sim.json()
    assert client.get("/api/districts/d03/similar", params={"city": "paris", "scope": "galaxy"}, headers=rd).status_code == 422
    listing = client.get("/api/tools", headers=rd).json()["tools"]
    assert [t["name"] for t in listing] == ["explain_district", "similar_episodes", "district_series"]
    assert client.get("/api/tools", headers=ing).status_code == 403
    call = client.post("/api/tools/explain_district", json={"arguments": {"city": "paris", "district": "d03", "k": 2}}, headers=rd | H)
    assert call.status_code == 200 and call.json()["tool"] == "explain_district" and call.json()["result"]["relational"]["sensors"] == 12
    assert client.post("/api/tools/district_series", json={"arguments": {"city": "paris", "district": "d03", "hours": 1}}, headers=rd | H).json()["result"]["count"] >= 1
    assert client.post("/api/tools/similar_episodes", json={"arguments": {"city": "paris", "district": "d03"}}, headers=rd | H).status_code == 200
    assert client.post("/api/tools/explain_district", json={"arguments": {"city": "paris"}}, headers=rd | H).status_code == 422
    assert client.post("/api/tools/explain_district", json={"arguments": {"city": "paris", "district": "d03", "drop": 1}}, headers=rd | H).status_code == 422
    assert client.post("/api/tools/explain_district", json={"arguments": {"city": "paris", "district": "zz"}}, headers=rd | H).status_code == 404
    assert client.post("/api/tools/nope", json={"arguments": {}}, headers=rd | H).status_code == 404
    assert client.post("/api/tools/explain_district", json={"arguments": {"city": "paris", "district": "d03"}}, headers=ing | H).status_code == 403


def test_migration_007_upgrades_a_database_that_has_sensors_and_readings():
    """The new columns and tables are additive: existing rows keep working."""
    gateway.accept([reading(1, city="paris")])
    with connect() as c:
        c.execute("DELETE FROM schema_migrations WHERE name IN ('006_attrs.sql', '007_timeseries_memory.sql')")
        c.execute("ALTER TABLE sensor DROP COLUMN attrs")
        c.execute("DROP TABLE rollup_5m, rollup_1h, episode CASCADE")
        c.commit()
    assert migrate.run() == ["006_attrs.sql", "007_timeseries_memory.sql"]
    with connect() as c:
        assert c.execute("SELECT attrs FROM sensor").fetchone()["attrs"] is None
        assert timeseries.partition_name(NOW.year, NOW.month) in months_with_partitions()
    assert migrate.run() == []
