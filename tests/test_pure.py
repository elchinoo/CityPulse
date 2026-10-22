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
    a, b = build_fleet(3000), build_fleet(3000)
    assert a == b and len({x[0] for x in a}) == len(a)
    assert all(SENSOR_ID.match(x[0]) and x[1] in TYPES and x[2] in DISTRICTS for x in a)
    assert 2900 <= len(a) <= 3100


GOOD = {"sensor": "temp-d01-0001", "type": "temperature", "district": "d01", "value": 21.5}


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
    sensor, kind, district, ts, value, protocol = gateway.validate({**GOOD, "ts": time.time() * 1000 - 5000}, now)
    assert (sensor, kind, district, value, protocol) == ("temp-d01-0001", "temperature", "d01", 21.5, "http")
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


def _sim(n=300, sink=None, bursts=None, seed=1):
    return Simulator(n, 1.0, sink or _Sink(), lambda: bursts or [], random.Random(seed))


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
