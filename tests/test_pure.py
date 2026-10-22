"""Catalogue, validation, simulator behaviour and reporter maths: no database needed."""
import asyncio
import random
import time
from datetime import UTC, datetime

import httpx
import pytest
from apgkit.auth import TokenSource

from citypulse import chaos, gateway, reporter
from citypulse.catalog import DISTRICTS, SENSOR_ID, TYPES, build_fleet
from citypulse.sim import BUFFER_MAX, Fatal, Retry, Simulator

NOW = datetime.now(UTC)


def test_catalogue_has_27_types_with_sane_limits():
    assert len(TYPES) == 27
    for t in TYPES.values():
        assert t.lo < t.hi and t.interval > 0 and t.weight > 0
        assert t.lo <= t.normal(0) <= t.hi and t.lo <= t.normal(12) <= t.hi, t.key
    seven = {"rain", "light", "humidity", "temperature", "traffic_camera", "traffic_light", "radar"}
    assert seven <= set(TYPES)


def test_fleet_is_deterministic_unique_and_valid():
    a, b = build_fleet(3000, "paris"), build_fleet(3000, "paris")
    assert a == b and len({x[0] for x in a}) == len(a)
    assert all(SENSOR_ID.match(x[0]) and x[1] in TYPES and x[2] in DISTRICTS for x in a)
    assert 2900 <= len(a) <= 3100


GOOD = {"sensor": "temp-d01-0001", "type": "temperature", "district": "d01", "city": "paris", "value": 21.5}


@pytest.mark.parametrize("patch,reason", [
    ({"sensor": "!!"}, "bad sensor id"),
    ({"sensor": 5}, "bad sensor id"),
    ({"type": "teleporter"}, "unknown sensor type"),
    ({"district": "BAD DISTRICT"}, "bad district"),
    ({"value": "n/a"}, "value must be a number"),
    ({"value": None}, "value must be a number"),
    ({"value": True}, "value must be a number"),
    ({"value": float("nan")}, "value out of range"),
    ({"value": float("inf")}, "value out of range"),
    ({"value": 1e13}, "value out of range"),
    ({"ts": "yesterday-ish"}, "bad timestamp"),
    ({"protocol": "zigbee"}, "unknown protocol"),
    ({"ts": time.time() + 3600}, "timestamp in the future"),
    ({"ts": time.time() - 3 * 86400}, "timestamp older than 24 hours"),
])
def test_validate_rejects_with_a_reason(patch, reason):
    assert gateway.validate({**GOOD, **patch}, NOW) == reason


def test_validate_accepts_and_normalises():
    now = datetime.now(UTC)
    sensor, kind, district, ts, value, protocol, city = gateway.validate({**GOOD, "ts": time.time() * 1000 - 5000}, now)
    assert (sensor, kind, district, value, protocol, city) == ("temp-d01-0001", "temperature", "d01", 21.5, "http", "paris")
    assert 3 < (now - ts).total_seconds() < 9            # milliseconds epoch understood
    assert gateway.validate("not a dict", NOW) == "item is not an object"
    # a value past the physical limits is VALID input (it becomes a fault later), not a rejection
    assert isinstance(gateway.validate({**GOOD, "value": 9999}, NOW), tuple)


def test_batch_shape_is_checked():
    with pytest.raises(gateway.BadBatch):
        gateway.accept([])
    with pytest.raises(gateway.BadBatch):
        gateway.accept([GOOD] * (gateway.MAX_BATCH + 1))


def test_burst_targets_and_validation():
    b = chaos.Burst("noise", "district:d03", 3)
    assert b.matches("rain", "d03") and not b.matches("rain", "d04")
    assert chaos.Burst("noise", "type:rain", 1).matches("rain", "d09")
    assert chaos.Burst("noise", "all", 1).matches("co2", "d01")
    for bad in ("district:nope", "type:nope", "everything", "district:"):
        with pytest.raises(chaos.ChaosError):
            chaos.check_target(bad)


class _Sink:
    def __init__(self, behaviour=None):
        self.sent, self.behaviour = [], behaviour

    async def send(self, readings):
        if self.behaviour:
            raise self.behaviour
        self.sent.append(readings)


def _sim(n=300, sink=None, bursts=None, seed=1, city="paris"):
    return Simulator(n, 1.0, sink or _Sink(), lambda: bursts or [], random.Random(seed), city=city)


def test_normal_readings_pass_validation_and_stay_within_limits():
    s = _sim()
    now = time.time()
    items = [s.read(x, now, []) for x in s.sensors]
    assert all(i is not None for i in items)
    for i in items:
        out = gateway.validate(i, NOW)
        assert isinstance(out, tuple), (i, out)
        t = TYPES[i["type"]]
        assert t.lo <= i["value"] <= t.hi


def test_dropout_silences_only_the_target():
    s = _sim(bursts=[chaos.Burst("dropout", "district:d03", 5)])
    out = {x.district: s.read(x, time.time(), s.bursts()) for x in s.sensors}
    assert out["d03"] is None and out["d04"] is not None


def test_noise_produces_physically_impossible_values():
    s = _sim(bursts=[chaos.Burst("noise", "all", 5)])
    bad = 0
    for _ in range(40):
        for x in s.sensors:
            i = s.read(x, time.time(), s.bursts())
            t = TYPES[x.type]
            bad += not (t.lo <= i["value"] <= t.hi)
    assert bad > 0


def test_stuck_repeats_the_same_value_for_continuous_types_only():
    s = _sim(bursts=[chaos.Burst("stuck", "all", 5)])
    seen: dict[str, set] = {}
    for _ in range(6):
        for x in s.sensors:
            i = s.read(x, time.time(), s.bursts())
            seen.setdefault(x.id, set()).add(i["value"])
    frozen = [k for k, v in seen.items() if len(v) == 1]
    assert frozen and all(TYPES[next(x.type for x in s.sensors if x.id == k)].stuckable
                          or TYPES[next(x.type for x in s.sensors if x.id == k)].sigma == 0 for k in frozen)


def test_flood_shortens_the_reporting_interval_and_late_ages_the_timestamp():
    base, flood = _sim(seed=3), _sim(seed=3, bursts=[chaos.Burst("flood", "all", 4)])
    now = time.time()
    base.read(base.sensors[0], now, [])
    flood.read(flood.sensors[0], now, flood.bursts())
    assert flood.sensors[0].due - now < (base.sensors[0].due - now) / 5
    late = _sim(bursts=[chaos.Burst("late", "all", 3)])
    item = late.read(late.sensors[0], now, late.bursts())
    assert now - item["ts"] > 200


def test_corrupt_items_are_rejected_by_validation():
    s = _sim(bursts=[chaos.Burst("corrupt", "all", 5)])
    results = [gateway.validate(s.read(x, time.time(), s.bursts()), NOW) for _ in range(5) for x in s.sensors]
    rejected = sum(isinstance(r, str) for r in results)
    assert 0.2 < rejected / len(results) < 0.6


def test_delivery_buffers_backs_off_and_keeps_order():
    sink = _Sink(Retry(0))
    s = _sim(sink=sink)
    items = [{"n": i} for i in range(250)]
    s.hold(items)
    asyncio.run(s.deliver(100.0))
    assert len(s.buffer) == 250 and list(s.buffer) == items       # nothing lost, same order
    assert s.backoff >= 1.0 and s.resume_at > 100.0
    asyncio.run(s.deliver(100.0))                                 # still backing off: nothing sent
    assert sink.sent == []
    sink.behaviour = None
    s.resume_at = 0
    asyncio.run(s.deliver(200.0))
    assert s.sent == 250 and not s.buffer and s.backoff == 0


def test_buffer_is_bounded_and_counts_drops():
    s = _sim()
    s.hold([{"n": i} for i in range(BUFFER_MAX + 500)])
    assert len(s.buffer) == BUFFER_MAX and s.dropped == 500
    assert s.buffer[0] == {"n": 500}                              # oldest dropped first


def test_a_refused_token_is_reported_not_hidden():
    s = _sim(sink=_Sink(Fatal("ingest refused the token (HTTP 401)")))
    s.hold([{"n": 1}])
    asyncio.run(s.deliver(1.0))
    assert "401" in s.heartbeat()["problem"] and len(s.buffer) == 1


def test_reporter_points_and_p95():
    pts = reporter.fleet_points({"total": 100, "health": 0.9, "counts": {"ok": 90, "offline": 6, "stale": 2, "fault": 1, "stuck": 1},
                                 "districts": [{"total": 50, "ok": 49}, {"total": 40, "ok": 10}, {"total": 10, "ok": 0}]})
    assert {p["name"]: p["value"] for p in pts} == {"fleet_health_pct": 90.0, "offline_pct": 6.0, "stale_pct": 2.0,
                                                    "fault_pct": 1.0, "stuck_pct": 1.0,
                                                    "worst_district_pct": 25.0}   # districts under 20 sensors are ignored
    assert reporter.p95_ms() >= 0


def _pagerlens(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={})
    return httpx.MockTransport(handler)


def _idp(calls: list):
    iss = "https://id.example.org"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": iss, "token_endpoint": f"{iss}/oauth/token"})
        calls.append(request.content.decode())
        return httpx.Response(200, json={"access_token": "minted-token", "expires_in": 300})
    return iss, lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_reporter_sends_a_token_minted_by_the_identity_provider():
    calls, seen = [], []
    iss, http = _idp(calls)
    source = TokenSource(iss, "citypulse-service", lambda: "s", scope="pagerlens.ingest", audience="pagerlens", http=http)
    rep = reporter.Reporter("http://pagerlens.test", None, source, transport=_pagerlens(seen))
    asyncio.run(rep.post("/api/ingest/metrics", {}))
    asyncio.run(rep.post("/api/ingest/metrics", {}))
    assert seen == [("/api/ingest/metrics", "Bearer minted-token")] * 2
    assert len(calls) == 1 and "scope=pagerlens.ingest" in calls[0] and "audience=pagerlens" in calls[0]   # minted once, then cached


def test_a_static_pagerlens_token_wins_over_the_identity_provider(monkeypatch):
    calls, seen = [], []
    iss, http = _idp(calls)
    source = TokenSource(iss, "citypulse-service", lambda: "s", scope="pagerlens.ingest", http=http)
    rep = reporter.Reporter("http://pagerlens.test", "static-token", source, transport=_pagerlens(seen))
    asyncio.run(rep.post("/api/ingest/metrics", {}))
    assert seen == [("/api/ingest/metrics", "Bearer static-token")] and calls == []
    for k in ("PAGERLENS_TOKEN_FILE",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PAGERLENS_URL", "http://localhost:8001")
    monkeypatch.setenv("PAGERLENS_TOKEN", "static-token")
    monkeypatch.setenv("CITYPULSE_OIDC_ISSUER", iss)
    monkeypatch.setenv("CITYPULSE_SERVICE_CLIENT_SECRET", "s")
    assert reporter.maybe_start().source is None
    monkeypatch.delenv("PAGERLENS_TOKEN")
    monkeypatch.setenv("PAGERLENS_TOKEN_FILE", "/nonexistent/token")
    started = reporter.maybe_start()
    assert started.source is not None and started.source.scope == "pagerlens.ingest" and started.source.audience == "pagerlens"
    assert started.source.client_id == "citypulse-service"
    monkeypatch.delenv("CITYPULSE_SERVICE_CLIENT_SECRET")
    monkeypatch.setenv("CITYPULSE_SERVICE_CLIENT_SECRET_FILE", "/nonexistent/secret")
    assert reporter.maybe_start() is None


# ---- cities -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("city", ["paris", "a", "0-x_y", "x" * 32])
def test_validate_accepts_a_city(city):
    out = gateway.validate({**GOOD, "city": city}, NOW)
    assert isinstance(out, tuple) and out[6] == city


@pytest.mark.parametrize("city", ["", "Paris", "-x", "x" * 33, "a b", "a.b", 5, True, ["a"], {"a": 1}])
def test_validate_rejects_a_bad_city(city):
    assert gateway.validate({**GOOD, "city": city}, NOW) == "bad city"


def test_a_missing_or_null_city_is_rejected():
    no_city = {k: v for k, v in GOOD.items() if k != "city"}
    assert gateway.validate(no_city, NOW) == "city is required"
    assert gateway.validate({**GOOD, "city": None}, NOW) == "city is required"


def test_the_legacy_city_id_is_reserved():
    assert "reserved" in gateway.validate({**GOOD, "city": "default"}, NOW)
    assert isinstance(gateway.validate({**GOOD, "city": "default2"}, NOW), tuple)


def test_city_targets_for_bursts():
    b = chaos.Burst("noise", "city:paris", 3)
    assert b.matches("rain", "d01", "paris") and not b.matches("rain", "d01", "rome") and not b.matches("rain", "d01")
    assert chaos.Burst("noise", "all", 1).matches("co2", "d01", "rome")
    assert chaos.Burst("noise", "district:d03", 1).matches("rain", "d03", "rome")       # existing targets span every city
    assert chaos.check_target("city:paris") == "city:paris" and chaos.check_target("city:a_b-9") == "city:a_b-9"
    for bad in ("city:", "city:Paris", "city:" + "x" * 33, "city:a b", "city:-a", "cities:paris"):
        with pytest.raises(chaos.ChaosError):
            chaos.check_target(bad)


def test_the_simulator_sends_its_city_and_keeps_ids_unique_across_cities():
    plain, paris, rome = _sim(city="oslo"), _sim(city="paris"), _sim(city="rome")
    item = plain.read(plain.sensors[0], time.time(), [])
    assert item["city"] == "oslo" and item["sensor"].startswith("oslo.")                 # every reading names its city
    assert all("city" in x for x in paris.due_readings(time.time() + 3600, []))
    p = paris.read(paris.sensors[0], time.time(), [])
    assert p["city"] == "paris" and p["sensor"].startswith("paris.") and isinstance(gateway.validate(p, NOW), tuple)
    assert {s.id for s in paris.sensors}.isdisjoint({s.id for s in rome.sensors}) and len(paris.sensors) == len(plain.sensors)
    assert all(SENSOR_ID.match(f[0]) for f in build_fleet(50_000, "x" * 32))
    burst = [chaos.Burst("dropout", "city:rome", 5)]
    assert paris.read(paris.sensors[0], time.time(), burst) is not None
    assert rome.read(rome.sensors[0], time.time(), burst) is None


def test_citypulse_city_setting(monkeypatch):
    from citypulse.config import get_settings
    monkeypatch.delenv("CITYPULSE_CITY", raising=False)
    assert get_settings().sim_city == ""                       # no default: unset stays unset
    monkeypatch.setenv("CITYPULSE_CITY", "oslo")
    assert get_settings().sim_city == "oslo"
    monkeypatch.setenv("CITYPULSE_CITY", "Not Valid")
    with pytest.raises(SystemExit):
        get_settings()
    monkeypatch.setenv("CITYPULSE_CITY", "default")            # the retired legacy id is not a usable simulator city
    with pytest.raises(SystemExit, match="legacy"):
        get_settings()


@pytest.mark.parametrize("role,value", [("sim", None), ("all", None), ("sim", ""), ("all", "   ")])
def test_the_built_in_simulator_refuses_to_start_without_a_city(monkeypatch, role, value):
    from citypulse import cli, sim
    monkeypatch.setenv("CITYPULSE_ROLE", role)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@127.0.0.1:1/none")
    monkeypatch.setenv("CITYPULSE_INGEST_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("CITYPULSE_INGEST_TOKEN", "x")
    if value is None:
        monkeypatch.delenv("CITYPULSE_CITY", raising=False)
    else:
        monkeypatch.setenv("CITYPULSE_CITY", value)
    with pytest.raises(SystemExit, match="CITYPULSE_CITY"):
        sim.make(direct=role == "all")
    with pytest.raises(SystemExit, match="CITYPULSE_CITY"):
        cli.main(["start"])                                    # before it migrates or serves anything
    with pytest.raises(SystemExit, match="CITYPULSE_CITY"):
        cli.main(["serve"])


def test_the_api_role_needs_no_city_and_the_simulator_starts_with_one(monkeypatch):
    from citypulse import sim
    from citypulse.config import get_settings
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@127.0.0.1:1/none")
    monkeypatch.setenv("CITYPULSE_ROLE", "api")
    monkeypatch.delenv("CITYPULSE_CITY", raising=False)
    assert get_settings().role == "api"                        # nothing asks for a city
    monkeypatch.setenv("CITYPULSE_ROLE", "all")
    monkeypatch.setenv("CITYPULSE_CITY", "oslo")
    s = sim.make(direct=True)
    assert s.city == "oslo" and s.sensors[0].id.startswith("oslo.")


def test_otlp_maps_the_city_attribute():
    from citypulse import otlp

    def payload(res, point):
        a = lambda d: [{"key": k, "value": {"stringValue": v}} for k, v in d.items()]  # noqa: E731
        return {"resourceMetrics": [{"resource": {"attributes": a({"sensor.id": "temp-d01-0001", **res})},
                "scopeMetrics": [{"metrics": [{"name": "temperature", "gauge": {"dataPoints": [{"asDouble": 1.0, "attributes": a(point)}]}}]}]}]}
    for res, point, want in [({}, {}, None), ({"city": "a"}, {}, "a"), ({"city.id": "b"}, {}, "b"), ({}, {"city.id": "c"}, "c"),
                             ({"city": "a"}, {"city": "d"}, "d"), ({"city": "a"}, {"city.id": "e"}, "e"), ({"city": ""}, {}, None)]:
        items, skipped = otlp.to_readings(payload(res, point))
        assert skipped == 0 and items[0].get("city") == want, (res, point)


# ---- the reporter, per city -------------------------------------------------------------------------------
def _summary(city, ok, total, state):
    bad = total - ok
    return {"city": city, "state": state, "health": ok / total, "total": total, "reasons": [f"{bad} sensors down"],
            "counts": {"ok": ok, "stale": 0, "offline": bad, "fault": 0, "stuck": 0},
            "districts": [{"total": total, "ok": ok}]}


def _run_reporter(monkeypatch, snaps):
    posts: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append((request.url.path, __import__("json").loads(request.content)))
        return httpx.Response(200, json={})
    rep = reporter.Reporter("http://pagerlens.test", "t", transport=httpx.MockTransport(handler))
    feed = iter(snaps)
    monkeypatch.setattr(reporter, "_snapshot", lambda tracked=frozenset(): next(feed))
    return rep, posts


def _of(posts, path, key="service"):
    return [(b[key], b) for p, b in posts if p == path]


def test_reporter_with_one_city_behaves_like_with_many(monkeypatch):
    # one city with a problem: the alert is per city, never fleet-wide (there is no special case for a single city)
    snap = {"overall": _summary(None, 50, 100, "partial_outage"), "cities": {"paris": _summary("paris", 50, 100, "partial_outage")}}
    rep, posts = _run_reporter(monkeypatch, [snap])
    asyncio.run(rep.report())
    alerts = _of(posts, "/api/ingest/alerts")
    assert [a[0] for a in alerts] == ["citypulse-fleet-paris"] and alerts[0][1]["labels"]["city"] == "paris"
    assert {s for s, _ in _of(posts, "/api/ingest/services", "name")} == {"citypulse-ingest", "citypulse-fleet", "citypulse-fleet-paris"}
    # and a single healthy city raises nothing and registers no city service
    healthy = {"overall": _summary(None, 100, 100, "operational"), "cities": {}}
    rep, posts = _run_reporter(monkeypatch, [healthy])
    asyncio.run(rep.report())
    assert _of(posts, "/api/ingest/alerts") == []
    assert [s for s, _ in _of(posts, "/api/ingest/metrics")] == ["citypulse-ingest", "citypulse-fleet"]


def test_reporter_alerts_per_city_and_only_for_cities_with_problems(monkeypatch):
    overall = _summary(None, 70, 100, "degraded")
    snap = {"overall": overall, "cities": {"paris": _summary("paris", 5, 10, "partial_outage")}}
    rep, posts = _run_reporter(monkeypatch, [snap, snap])
    asyncio.run(rep.report())
    alerts = _of(posts, "/api/ingest/alerts")
    assert [a[0] for a in alerts] == ["citypulse-fleet-paris"]                      # no fleet-wide alert, no healthy city
    assert alerts[0][1]["labels"] == {"state": "partial_outage", "health_pct": 50.0, "city": "paris"}
    assert alerts[0][1]["severity"] == "sev2" and "paris" in alerts[0][1]["summary"]
    svc = {b["name"]: b for _, b in _of(posts, "/api/ingest/services", "name")}
    assert svc["citypulse-fleet-paris"]["depends_on"] == ["citypulse-ingest"]
    assert [s for s, _ in _of(posts, "/api/ingest/metrics")] == ["citypulse-ingest", "citypulse-fleet", "citypulse-fleet-paris"]
    asyncio.run(rep.report())                                                        # unchanged verdict: not repeated at once
    assert len(_of(posts, "/api/ingest/alerts")) == 1 and len(_of(posts, "/api/ingest/services", "name")) == 3


def test_reporter_escalates_and_recovers_a_city(monkeypatch):
    def snap(city_summary):
        return {"overall": _summary(None, 90, 100, "degraded"), "cities": {"rome": city_summary} if city_summary else {}}
    rep, posts = _run_reporter(monkeypatch, [snap(_summary("rome", 8, 10, "degraded")), snap(_summary("rome", 3, 10, "partial_outage")),
                                             snap(_summary("rome", 10, 10, "operational"))])
    asyncio.run(rep.report())
    asyncio.run(rep.report())
    assert [b["severity"] for _, b in _of(posts, "/api/ingest/alerts")] == ["sev3", "sev2"]   # worse: alert again at once
    assert rep.city_state == {"rome": "partial_outage"}
    asyncio.run(rep.report())
    last = _of(posts, "/api/ingest/metrics")[-1]
    assert last[0] == "citypulse-fleet-rome" and {p["name"]: p["value"] for p in last[1]["points"]}["fleet_health_pct"] == 100.0
    assert rep.city_state == {} and len(_of(posts, "/api/ingest/alerts")) == 2               # recovered: reported once, then dropped
