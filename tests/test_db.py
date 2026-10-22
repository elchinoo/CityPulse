"""Gateway, status engine, bursts, auth scopes, OTLP and /metrics against a disposable Postgres."""
import gzip
import json
import os
import time

import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("set TEST_DATABASE_URL to run DB tests", allow_module_level=True)

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest  # noqa: E402

from citypulse import auth, chaos, core, db, gateway, migrate, otlp, status  # noqa: E402
from citypulse.db import connect  # noqa: E402

H = {"X-CityPulse-CSRF": "1"}
PASSWORD = "correct-horse-battery"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
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


def reading(i=1, kind="temperature", district="d01", value=20.0, **extra):
    return {"sensor": f"{kind.replace('_', '-')}-{district}-{i:04d}", "type": kind, "district": district,
            "value": value, **extra}


def login(client):
    code = auth.setup_code()
    assert client.post("/api/setup", json={"code": code, "password": PASSWORD}, headers=H).status_code == 200


def token(client, scope):
    r = client.post("/api/tokens", json={"name": scope, "scope": scope}, headers=H)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def age(sensor_id, seconds, value=None):
    with connect() as c:
        c.execute("UPDATE sensor SET last_ts = now() - make_interval(secs => %s), "
                  "last_value = coalesce(%s, last_value) WHERE id=%s", (seconds, value, sensor_id))
        c.commit()


def state_of(sensor_id):
    with connect() as c:
        return c.execute("SELECT status FROM sensor WHERE id=%s", (sensor_id,)).fetchone()["status"]


# ---- gateway -------------------------------------------------------------------------------------
def test_accept_stores_readings_and_keeps_latest_state():
    old = reading(1, value=10.0, ts=time.time() - 30)
    new = reading(1, value=12.5, ts=time.time() - 5)
    res = gateway.accept([new, old, reading(2), {"sensor": "!!"}])
    assert res["accepted"] == 3 and res["rejected"] == 1 and "bad sensor id" in res["errors"][0]
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM reading").fetchone()["n"] == 3
        s = c.execute("SELECT last_value FROM sensor WHERE id='temperature-d01-0001'").fetchone()
    assert s["last_value"] == 12.5          # newest EVENT time wins, whatever the arrival order
    gateway.accept([reading(1, value=99.0, ts=time.time() - 600)])
    with connect() as c:
        assert c.execute("SELECT last_value FROM sensor WHERE id='temperature-d01-0001'").fetchone()["last_value"] == 12.5


def test_the_collection_protocol_is_recorded_and_summarised():
    gateway.accept([reading(1, protocol="modbus_tcp"), reading(2, protocol="profibus"), reading(3, protocol="profibus"), reading(4)])
    with connect() as c:
        s = status.summary(c)
        assert {p["key"]: p["total"] for p in s["protocols"]} == {"modbus_tcp": 1, "profibus": 2, "http": 1}
        assert c.execute("SELECT protocol FROM sensor WHERE id='temperature-d01-0001'").fetchone()["protocol"] == "modbus_tcp"
    assert gateway.accept([reading(5, protocol="smoke-signals")])["rejected"] == 1


def test_sensor_cap_refuses_new_sensors_but_keeps_known_ones(monkeypatch):
    monkeypatch.setenv("MAX_SENSORS", "100")
    gateway.accept([reading(i) for i in range(100)])
    gateway._known.update(count=0, at=0.0)
    res = gateway.accept([reading(0), reading(500), reading(501)])
    assert res["accepted"] == 1 and res["rejected"] == 2 and "sensor limit reached" in res["errors"][0]


def test_saturated_gateway_answers_overloaded(monkeypatch):
    monkeypatch.setenv("INGEST_CONCURRENCY", "2")
    gateway._inflight = 2
    with pytest.raises(gateway.Overloaded):
        gateway.accept([reading(1)])
    gateway._inflight = 0
    assert gateway.accept([reading(1)])["accepted"] == 1


# ---- status engine ---------------------------------------------------------------------------------
def test_status_engine_judges_each_failure_mode():
    items = [reading(i, kind="temperature") for i in range(1, 7)]
    gateway.accept(items)
    ids = [f"temperature-d01-{i:04d}" for i in range(1, 7)]
    age(ids[1], 100)                  # temperature reports every 30 s: 100 s > 3 intervals -> stale
    age(ids[2], 400)                  # > 12 intervals -> offline
    age(ids[3], 0, value=500.0)       # hotter than the surface of anything we measure -> fault
    with connect() as c:              # five identical readings in the window -> stuck
        c.execute("INSERT INTO reading SELECT %s, now() - make_interval(secs => g.s * 5), 20.0 FROM generate_series(1,6) g(s)",
                  (ids[4],))
        c.commit()
    with connect() as c:
        status.evaluate(c)
        c.commit()
    assert [state_of(i) for i in ids] == ["ok", "stale", "offline", "fault", "stuck", "ok"]


def test_discrete_sensors_are_never_called_stuck():
    gateway.accept([reading(1, kind="parking", value=1.0)])
    sid = "parking-d01-0001"
    with connect() as c:
        c.execute("INSERT INTO reading SELECT %s, now() - make_interval(secs => g.s * 5), 1.0 FROM generate_series(1,8) g(s)", (sid,))
        status.evaluate(c)
        c.commit()
    assert state_of(sid) == "ok"


def test_status_recovers_when_data_returns():
    gateway.accept([reading(1)])
    sid = "temperature-d01-0001"
    age(sid, 400)
    with connect() as c:
        status.evaluate(c)
        c.commit()
    assert state_of(sid) == "offline"
    gateway.accept([reading(1, value=19.0)])
    with connect() as c:
        status.evaluate(c)
        c.commit()
    assert state_of(sid) == "ok"


def test_summary_tiers_and_district_outage():
    items = [reading(i, district=d) for d in ("d01", "d02") for i in range(30)]
    gateway.accept(items)
    with connect() as c:
        s = status.summary(c)
        assert s["state"] == "operational" and s["total"] == 60
        c.execute("UPDATE sensor SET status='offline' WHERE district='d01' AND id < 'temperature-d01-0020'")  # 20 of 30 down
        c.commit()
        s = status.summary(c)
    assert s["health"] == pytest.approx(40 / 60)
    assert s["state"] == "partial_outage" and any("Harbor" in r for r in s["reasons"])
    assert status.tier(0.99) == "operational" and status.tier(0.9) == "degraded" and status.tier(0.2) == "major_outage"


def test_tick_runs_once_and_prunes_old_readings():
    gateway.accept([reading(1)])
    with connect() as c:
        c.execute("INSERT INTO reading VALUES ('temperature-d01-0001', now() - interval '30 hours', 1.0)")
        c.commit()
    status._last.update(history=0.0, prune=0.0)
    assert status.tick() is True
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM reading WHERE ts < now() - interval '7 hours'").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM status_history WHERE scope='overall'").fetchone()["n"] == 1


# ---- bursts ---------------------------------------------------------------------------------------
def test_bursts_start_expire_stop_and_are_bounded():
    with connect() as c:
        chaos.start(c, "flood", "district:d03", 4, 60)
        assert [b["kind"] for b in chaos.active(c)] == ["flood"]
        assert len(chaos.storm(c, 60)) == 7
        for bad in ({"kind": "meteor"}, {"kind": "noise", "target": "x"}, {"kind": "noise", "intensity": 9},
                    {"kind": "noise", "seconds": 1}):
            with pytest.raises(chaos.ChaosError):
                chaos.start(c, **bad)
        c.execute("UPDATE chaos SET until = now() - interval '1 second' WHERE kind='flood'")
        assert "flood" not in [b["kind"] for b in chaos.active(c)]
        assert chaos.stop_all(c) >= 1 and chaos.active(c) == []


def test_slow_db_burst_slows_the_gateway():
    with connect() as c:
        chaos.start(c, "slow_db", "all", 1, 30)
    gateway._bursts._at = 0.0
    t = time.monotonic()
    gateway.accept([reading(1)])
    assert time.monotonic() - t >= 0.45


# ---- HTTP: auth, scopes, collection -------------------------------------------------------------------
def test_everything_is_denied_before_setup_and_setup_needs_the_code(client):
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/readings", json={"readings": [reading(1)]}, headers=H).status_code == 401
    assert client.get("/api/session").json()["state"] == "setup_required"
    bad = client.post("/api/setup", json={"code": "AAAA-BBBB-CCCC", "password": PASSWORD}, headers=H)
    assert bad.status_code == 403
    login(client)
    assert client.get("/api/status").status_code == 200


def test_token_scopes_are_strict(client):
    login(client)
    ing, rd, met = token(client, "ingest"), token(client, "read"), token(client, "metrics")
    client.cookies.clear()
    body = {"readings": [reading(1)]}
    assert client.post("/api/readings", json=body, headers=ing).json()["accepted"] == 1
    assert client.post("/api/readings", json=body, headers=rd).status_code == 403     # read cannot write
    assert client.post("/api/readings", json=body, headers=met).status_code == 403
    assert client.get("/api/status", headers=ing).status_code == 403                  # ingest cannot read
    assert client.get("/api/status", headers=rd).status_code == 200
    assert client.get("/metrics", headers=met).status_code == 200
    assert client.get("/metrics", headers=ing).status_code == 403
    assert client.get("/api/status", headers=met).status_code == 403
    assert client.post("/api/chaos/storm", json={}, headers=rd).status_code == 403    # only full can burst
    assert client.get("/api/tokens", headers=rd).status_code == 403


def test_revoked_token_stops_working(client):
    login(client)
    ing = token(client, "ingest")
    tid = client.get("/api/tokens").json()[0]["id"]
    body = {"readings": [reading(1)]}
    client.cookies.clear()
    assert client.post("/api/readings", json=body, headers=ing).status_code == 200
    login_again = client.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert login_again.status_code == 200
    assert client.delete(f"/api/tokens/{tid}", headers=H).status_code == 200
    client.cookies.clear()
    assert client.post("/api/readings", json=body, headers=ing).status_code == 401


def test_cookie_writes_need_the_csrf_header(client):
    login(client)
    assert client.post("/api/chaos/stop").status_code == 403
    assert client.post("/api/chaos/stop", headers=H).status_code == 200


def test_ingest_api_partial_success_overload_and_limits(client):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    r = client.post("/api/readings", headers=ing, json={"readings": [reading(1), {"sensor": "x"}, "junk"]})
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["rejected"] == 2
    assert client.post("/api/readings", headers=ing, json={"readings": []}).status_code == 422
    assert client.post("/api/readings", headers=ing, json={"readings": [reading(1)] * 1001}).status_code == 422
    gateway._inflight = 99
    r = client.post("/api/readings", headers=ing, json={"readings": [reading(1)]})
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    gateway._inflight = 0
    big = client.post("/api/readings", headers={**ing, "Content-Length": str(3 * 1024 * 1024)}, content=b"{}")
    assert big.status_code == 413


def test_dashboard_endpoints(client):
    login(client)
    gateway.accept([reading(i, kind=k) for i in range(1, 4) for k in ("temperature", "rain")])
    s = client.get("/api/status").json()
    assert s["total"] == 6 and s["state"] == "operational" and len(s["types"]) == 2 and s["bursts"] == []
    assert client.get("/api/sensors?state=all&type=rain").json().__len__() == 3
    assert client.get("/api/sensors?q=rain-d01").json() == []                       # problem filter hides healthy ones
    assert client.get("/api/sensors?type=nope").status_code == 422
    one = client.get("/api/sensors/rain-d01-0001").json()
    assert one["type"] == "rain" and len(one["readings"]) == 1
    assert client.get("/api/sensors/%21%21").status_code == 404
    assert len(client.get("/api/catalog").json()["types"]) == 27
    client.post("/api/chaos/storm", json={"seconds": 30}, headers=H)
    assert len(client.get("/api/chaos").json()["active"]) == 7
    assert client.post("/api/chaos", json={"kind": "meteor"}, headers=H).status_code == 422
    assert client.post("/api/chaos/stop", headers=H).json()["stopped"] == 7
    assert client.get("/api/history?minutes=5").status_code == 200


# ---- OTLP -------------------------------------------------------------------------------------------
def otlp_payload(n=3):
    return {"resourceMetrics": [{"resource": {"attributes": [
        {"key": "sensor.id", "value": {"stringValue": "temperature-d02-0007"}},
        {"key": "district", "value": {"stringValue": "d02"}}]},
        "scopeMetrics": [{"metrics": [{"name": "temperature", "gauge": {"dataPoints": [
            {"asDouble": 20.0 + i, "timeUnixNano": str(int((time.time() - i) * 1e9))} for i in range(n)]}}]}]}]}


def test_otlp_json_protobuf_and_gzip(client):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    r = client.post("/v1/metrics", headers={**ing, "Content-Type": "application/json"}, content=json.dumps(otlp_payload()))
    assert r.status_code == 200 and r.json() == {}
    z = client.post("/v1/metrics", headers={**ing, "Content-Type": "application/json", "Content-Encoding": "gzip"},
                    content=gzip.compress(json.dumps(otlp_payload(2)).encode()))
    assert z.status_code == 200
    from google.protobuf.json_format import ParseDict
    msg = ParseDict(otlp_payload(2), ExportMetricsServiceRequest())
    p = client.post("/v1/metrics", headers={**ing, "Content-Type": "application/x-protobuf"}, content=msg.SerializeToString())
    assert p.status_code == 200
    with connect() as c:
        assert c.execute("SELECT count(*) AS n FROM reading WHERE sensor_id='temperature-d02-0007'").fetchone()["n"] == 7
    anon = client.post("/v1/metrics", headers={"Content-Type": "application/json"}, content="{}")
    assert anon.status_code == 401


def test_otlp_reports_what_it_could_not_use(client):
    login(client)
    ing = token(client, "ingest")
    client.cookies.clear()
    payload = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [{"name": "mystery", "gauge": {"dataPoints": [{"asDouble": 1.0}]}}]}]}]}
    r = client.post("/v1/metrics", headers={**ing, "Content-Type": "application/json"}, content=json.dumps(payload))
    assert r.status_code == 200 and r.json()["partialSuccess"]["rejectedDataPoints"] == "1"
    assert client.post("/v1/metrics", headers={**ing, "Content-Type": "text/plain"}, content="x").status_code == 400
    assert client.post("/v1/metrics", headers={**ing, "Content-Type": "application/json", "Content-Encoding": "gzip"},
                       content=b"not gzip").status_code == 400
    bomb = gzip.compress(b"0" * (otlp.MAX_DECOMPRESSED + 10))
    assert client.post("/v1/metrics", headers={**ing, "Content-Type": "application/json", "Content-Encoding": "gzip"},
                       content=bomb).status_code == 400


# ---- /metrics -----------------------------------------------------------------------------------------
def test_prometheus_metrics_use_the_shared_names(client):
    login(client)
    met = token(client, "metrics")
    gateway.accept([reading(1), reading(2), {"sensor": "!!"}])
    client.cookies.clear()
    text = client.get("/metrics", headers=met).text
    for name in ("http_requests_total", "http_request_duration_seconds", "db_up{", "app_info{",
                 'citypulse_readings_total{result="accepted"}', 'citypulse_readings_total{result="rejected"}',
                 'citypulse_sensors{status="ok"} 2.0', "citypulse_fleet_health_ratio 1.0",
                 "citypulse_ingest_batch_seconds_bucket"):
        assert name in text, name
    assert 'db_up{app="citypulse"} 1.0' in text
