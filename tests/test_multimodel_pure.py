"""The multi-model features without a database: device metadata validation, the episode fingerprint, the deterministic demo history, the
tool definitions and the assembly of the one explain statement."""
import math
from datetime import UTC, datetime, timedelta

import pytest

from citypulse import catalog, explain, gateway, history, memory, otlp, sim, timeseries, tools

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)          # a Wednesday
GOOD = {"sensor": "temp-d01-0001", "type": "temperature", "district": "d01", "value": 21.5, "city": "paris"}
ATTRS = {"vendor": "Aerolab", "firmware": "2.1.4", "install_date": "2024-03-01", "calibration": {"offset": 0.12, "scale": 1.004}, "tags": ["roof", "north"]}


# ---- sensor.attrs: what the gateway accepts -----------------------------------------------------------------------------------
def test_attrs_accepts_vendor_firmware_install_date_calibration_and_tags():
    assert gateway.check_attrs(ATTRS) == ATTRS


@pytest.mark.parametrize("bad,fragment", [
    ("vendor=acme", "JSON object"), ([1, 2], "JSON object"), (None, "JSON object"),
    ({"Vendor": "x"}, "keys must match"), ({"a-b": 1}, "keys must match"), ({"1a": 1}, "keys must match"),
    ({"a": {"b": {"c": 1}}}, "at most 2 levels"), ({"tags": [{"a": 1}]}, "scalars only"), ({"tags": list("abcdefghi")}, "at most 8"),
    ({"x": "y" * 121}, "120 characters"), ({f"k{i}": i for i in range(17)}, "at most 16 keys"),
    ({"v": float("nan")}, "finite"), ({"v": float("inf")}, "finite"),
    ({"password": "x"}, "secrets"), ({"api_key": "x"}, "secrets"), ({"calibration": {"secret": 1}}, "secrets"), ({"auth_token": "t"}, "secrets"),
    ({"note": "Bearer abcdef"}, "secrets"), ({"note": "-----BEGIN PRIVATE KEY-----"}, "secrets"), ({"note": "AKIAABCDEFGHIJKLMNOP"}, "secrets"),
    ({"note": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIi"}, "secrets"), ({"note": "a" * 41}, "secrets"),
])
def test_attrs_rejects_with_a_reason(bad, fragment):
    out = gateway.check_attrs(bad)
    assert isinstance(out, str) and fragment in out, out


def test_attrs_are_size_capped_in_bytes():
    big = {f"k{i}": "é" * 60 for i in range(10)}                 # 10 keys x 60 two-byte characters: under the key and string caps, over 1024 bytes
    assert "1024 bytes" in gateway.check_attrs(big)


def test_attrs_that_look_like_words_are_not_mistaken_for_secrets():
    assert gateway.check_attrs({"compass_heading": 12, "passenger_count": 3, "authority": "city"}) == {"compass_heading": 12, "passenger_count": 3, "authority": "city"}


def test_validate_leaves_attrs_to_check_attrs_so_old_senders_are_unaffected():
    assert isinstance(gateway.validate({**GOOD, "attrs": ATTRS}, NOW), tuple)


def test_otlp_attrs_prefix_becomes_the_attrs_field():
    data = {"resourceMetrics": [{"resource": {"attributes": [{"key": "attrs.vendor", "value": {"stringValue": "Aerolab"}},
                                                             {"key": "city", "value": {"stringValue": "paris"}}]},
                                 "scopeMetrics": [{"metrics": [{"name": "temperature", "gauge": {"dataPoints": [
                                     {"asDouble": 20.5, "attributes": [{"key": "sensor.id", "value": {"stringValue": "paris.t-1"}},
                                                                       {"key": "attrs.firmware", "value": {"stringValue": "2.0.1"}}]}]}}]}]}]}
    items, skipped = otlp.to_readings(data)
    assert skipped == 0 and items[0]["attrs"] == {"vendor": "Aerolab", "firmware": "2.0.1"}


# ---- the simulator's seeded metadata ---------------------------------------------------------------------------------------------
def test_builtin_sensor_attrs_are_deterministic_valid_and_plausible():
    a = catalog.sensor_attrs("paris.temperature-d01-0000", "temperature", "d01")
    assert a == catalog.sensor_attrs("paris.temperature-d01-0000", "temperature", "d01")
    assert a == {"vendor": "Meteosense", "firmware": "2.1.4", "install_date": "2022-12-07",
                 "calibration": {"offset": 0.116, "scale": 1.0055, "date": "2023-07-28"}, "tags": ["kerb"]}, "fixed expectation: no drift between runs"
    firmware = {}
    for kind in catalog.TYPES:
        for i in range(40):
            attrs = catalog.sensor_attrs(f"x.{kind}-d01-{i}", kind, "d01")
            assert gateway.check_attrs(attrs) == attrs
            assert attrs["vendor"] in catalog.VENDORS[catalog.TYPES[kind].group]
            firmware[attrs["firmware"]] = firmware.get(attrs["firmware"], 0) + 1
    assert set(firmware) == set(catalog.FIRMWARE) and firmware["1.4.2"] < firmware["2.1.4"], "the oldest firmware is the minority"


def test_the_simulator_sends_attrs_with_the_first_reading_and_then_every_so_often():
    s = sim.Simulator(60, 1.0, sink=None, bursts=lambda: [], city="atlantis")      # a city without geography still has metadata
    sensor = s.sensors[0]
    t0 = datetime.now().timestamp()
    first = s.read(sensor, t0, [])
    assert first["attrs"] == catalog.sensor_attrs(sensor.id, sensor.type, sensor.district) and "lat" not in first
    assert all("attrs" not in s.read(sensor, t0 + i, []) for i in range(1, sim.POSITION_EVERY))
    assert "attrs" in s.read(sensor, t0 + 99, [])
    assert isinstance(gateway.validate(first, datetime.now(UTC)), tuple)


# ---- the fingerprint ------------------------------------------------------------------------------------------------------------
def stats(**kw):
    base = {"city": "paris", "district": "d01", "total": 100, "ok": 100, "stale": 0, "offline": 0, "fault": 0, "stuck": 0,
            "delta_mean": 0.005, "delta_p95": 0.015, "burst_share": 0.0, "silent": 0, "rate_ratio": 1.0}
    return base | kw


def test_the_fingerprint_has_16_dimensions_all_within_minus_one_and_one():
    vec = memory.to_vector(stats(), NOW)
    assert len(vec) == memory.DIM == len(memory.FEATURES) == 16
    assert all(-1 <= v <= 1 for v in vec)
    worst = memory.to_vector(stats(ok=0, stale=100, delta_mean=9, delta_p95=9, burst_share=3, silent=500, rate_ratio=99), NOW, overload=7)
    assert all(-1 <= v <= 1 for v in worst), "out-of-range inputs are clipped"


def test_each_dimension_follows_its_documented_definition():
    v = memory.explain_vector(memory.to_vector(
        stats(ok=40, stale=25, offline=16, fault=9, stuck=10, delta_mean=0.01, delta_p95=0.1, burst_share=0.5, silent=30, rate_ratio=0.6),
        datetime(2026, 10, 28, 6, 0, tzinfo=UTC), overload=0.25))           # Wednesday 06:00 UTC: hour fraction 6, day of week 2 + 6/24
    assert v["ok"] == 0.4
    assert (v["stale"], v["offline"], v["fault"], v["stuck"]) == (0.5, 0.4, 0.3, round(math.sqrt(0.1), 6))
    assert v["delta_mean"] == 0.2 and v["delta_p95"] == 1.0, "x20, clipped to 1"
    assert v["burst"] == 0.5 and v["silent"] == 0.3 and v["rate"] == 0.3 and v["overload"] == 0.25
    assert (v["hour_sin"], v["hour_cos"]) == (0.5, 0.0)
    dow = 2 + 6 / 24
    assert v["dow_sin"] == round(0.25 * math.sin(2 * math.pi * dow / 7), 6) and v["dow_cos"] == round(0.25 * math.cos(2 * math.pi * dow / 7), 6)
    assert v["verdict"] == round(3 / 3, 6), "40 % ok is a major outage: 3/3"
    assert memory.explain_vector(memory.to_vector(stats(), NOW))["verdict"] == 0.0


def test_missing_measurements_do_not_break_the_fingerprint():
    v = memory.explain_vector(memory.to_vector(stats(delta_mean=None, delta_p95=None, rate_ratio=None, total=0, ok=0), NOW))
    assert v["delta_mean"] == v["delta_p95"] == 0.0 and v["rate"] == 0.5, "no data counts as a normal rate"


def cosine(a, b):
    return 1 - sum(x * y for x, y in zip(a, b)) / (math.hypot(*a) * math.hypot(*b))


def test_the_fingerprint_separates_what_it_should_in_cosine_distance():
    healthy_a = memory.to_vector(stats(), NOW)
    healthy_b = memory.to_vector(stats(ok=99, stale=1, delta_mean=0.0055), NOW + timedelta(minutes=15))
    dropout = memory.to_vector(stats(ok=30, stale=20, offline=50, silent=70, rate_ratio=0.3, burst_share=1.0), NOW)
    dropout_later = memory.to_vector(stats(ok=35, stale=15, offline=50, silent=65, rate_ratio=0.35, burst_share=1.0), NOW + timedelta(hours=7))
    noise = memory.to_vector(stats(ok=55, fault=45, delta_mean=0.06, delta_p95=0.2, burst_share=1.0), NOW)
    assert cosine(healthy_a, healthy_b) < 0.01
    assert cosine(dropout, dropout_later) < cosine(dropout, noise) < cosine(dropout, healthy_a), "same kind of trouble < other trouble < healthy"
    assert cosine(dropout, healthy_a) > 0.2


def test_verdict_tiers_and_window_alignment():
    assert [memory.verdict_of(ok, 100) for ok in (100, 90, 60, 20)] == ["operational", "degraded", "partial_outage", "major_outage"]
    assert memory.verdict_of(0, 0) == "operational"
    assert memory.floor_window(datetime(2026, 10, 28, 12, 7, 31, tzinfo=UTC)) == NOW
    assert memory.floor_window(NOW) == NOW


# ---- the deterministic demo history ------------------------------------------------------------------------------------------------
def test_history_is_a_pure_function_of_city_district_and_time():
    end = datetime(2026, 10, 20, 9, 45, tzinfo=UTC)
    assert history.synth_stats("paris", "d03", end) == history.synth_stats("paris", "d03", end)
    assert history.synth_stats("paris", "d03", end) != history.synth_stats("lyon", "d03", end)
    assert history.unit("paris", "d01", "inc", 20000) == history.unit("paris", "d01", "inc", 20000)
    assert abs(history.unit("fixed") - 0.5) <= 0.5 and 0 <= history.unit("a", 1) < 1
    # a fixed expectation, so a change of the model (or a platform that hashes differently) is noticed
    stats_, over = history.synth_stats("paris", "d01", end)
    assert (stats_["total"], stats_["ok"], stats_["stale"], over) == (241, 240, 1, 0.0)
    assert memory.to_vector(stats_, end, over) == [0.995851, 0.064416, 0.0, 0.0, 0.0, 0.106684, 0.341388, 0.0, 0.004149, 0.508179, 0.0,
                                                   0.277785, -0.415735, 0.238194, 0.075919, 0.0]


def test_history_has_incidents_that_ramp_and_end_inside_their_day():
    found = 0
    for day in range(19900, 19960):
        inc = history.incident("paris", "d02", day)
        if not inc:
            continue
        found += 1
        assert 2 <= inc["len"] <= 9 and inc["start"] + inc["len"] <= history.PER_DAY and inc["kind"] in history.KINDS
        first = datetime.fromtimestamp((day * history.PER_DAY + inc["start"] + 1) * 900, UTC)        # the window ending after slot `start`
        mid = first + timedelta(minutes=15 * (inc["len"] // 2)) if inc["len"] > 2 else first
        s_first, kind = history.strength("paris", "d02", first)
        assert kind == inc["kind"] and s_first == pytest.approx(inc["severity"] * 0.5)
        if inc["len"] > 2:
            assert history.strength("paris", "d02", mid)[0] in (inc["severity"], inc["severity"] * 0.5)
        assert history.strength("paris", "d02", first - timedelta(minutes=15))[0] == 0.0 or history.incident("paris", "d02", day - 1)
    assert 8 <= found <= 30, "about 30 % of the days"
    assert history.incident("paris", "d02", 19900) == {"start": 7, "len": 5, "kind": "flood", "severity": pytest.approx(0.8018208)}
    assert history.strength("paris", "d02", datetime(2024, 6, 26, 2, 0, tzinfo=UTC))[0] == pytest.approx(0.4009104), "ramp: half strength first"


def test_incident_windows_move_the_verdict_and_quiet_windows_do_not():
    verdicts = {}
    for day in range(19900, 19990):
        for slot in range(0, 96, 3):
            end = datetime.fromtimestamp((day * 96 + slot + 1) * 900, UTC)
            s, over = history.synth_stats("lyon", "d05", end)
            v = memory.verdict_of(s["ok"], s["total"])
            verdicts[v] = verdicts.get(v, 0) + 1
            f, kind = history.strength("lyon", "d05", end)
            if f == 0:
                assert v == "operational" and s["burst_share"] == 0.0 and over == 0.0
            assert s["ok"] + s["stale"] + s["offline"] + s["fault"] + s["stuck"] == s["total"]
    assert verdicts["operational"] > 10 * sum(n for k, n in verdicts.items() if k != "operational")
    assert len(verdicts) >= 3, "incidents reach more than one tier"


def test_window_ends_are_aligned_oldest_first_and_stop_before_now():
    ends = history.window_ends(datetime(2026, 10, 28, 12, 7, tzinfo=UTC), 2)
    assert len(ends) == 192 and ends[-1] == NOW and ends[0] == NOW - timedelta(minutes=15 * 191)
    assert all(a < b for a, b in zip(ends, ends[1:]))


def test_seed_history_rejects_unknown_cities_and_silly_spans():
    with pytest.raises(ValueError, match="atlantis"):
        history.seed(None, ["atlantis"])
    with pytest.raises(ValueError, match="days"):
        history.seed(None, ["paris"], days=0)


# ---- time-series ranges --------------------------------------------------------------------------------------------------------
def test_series_range_defaults_and_limits():
    a, b = timeseries.parse_range("5m", None, None, NOW)
    assert b == NOW and b - a == timedelta(hours=6)
    a, b = timeseries.parse_range("1h", None, None, NOW)
    assert b - a == timedelta(hours=48)
    with pytest.raises(timeseries.BadRange, match="before"):
        timeseries.parse_range("5m", NOW, NOW - timedelta(hours=1), NOW)
    with pytest.raises(timeseries.BadRange, match="2100"):
        timeseries.parse_range("5m", NOW - timedelta(days=8), NOW, NOW)
    timeseries.parse_range("5m", NOW - timedelta(days=7), NOW, NOW)
    timeseries.parse_range("1h", NOW - timedelta(days=87), NOW, NOW)
    with pytest.raises(timeseries.BadRange, match="bucket"):
        timeseries.parse_range("1d", None, None, NOW)
    a, _ = timeseries.parse_range("5m", datetime(2026, 10, 28, 10, 0), NOW, NOW)             # naive means UTC
    assert a.tzinfo is not None and a.hour == 10


def test_partition_names_and_month_ranges():
    assert timeseries.partition_name(2026, 3) == "rollup_5m_202603"
    assert timeseries._months(datetime(2026, 11, 20, tzinfo=UTC), datetime(2027, 1, 5, tzinfo=UTC)) == [(2026, 11), (2026, 12), (2027, 1)]


# ---- tools and the explain statement ------------------------------------------------------------------------------------------------
def test_the_explain_statement_is_one_statement_and_degrades_by_leaving_fragments_out():
    full = explain.build_sql(True, True)
    assert full.count(";") == 0 and full.startswith("WITH") and "ST_Distance" in full and "<=>" in full and "jsonb_each_text" in full
    assert "ST_" not in explain.build_sql(False, True) and "NULL::jsonb AS geo" in explain.build_sql(False, True)
    assert "<=>" not in explain.build_sql(True, False) and "NULL::jsonb AS vector" in explain.build_sql(True, False)
    assert "set_config('hnsw.iterative_scan'" not in full, "pgvector older than 0.8 has no such setting"
    modern = explain.build_sql(True, True, iterative=True)
    assert modern.count("set_config('hnsw.iterative_scan', 'strict_order', true)") == 1 and "FROM episode e, cfg" in modern and "{CFG" not in modern and modern.count(";") == 0
    plain = explain.build_sql(False, False)
    assert "rel AS" in plain and "ts AS" in plain and "js AS" in plain and "ST_" not in plain and "<=>" not in plain
    for sql in (full, plain):
        assert "%(city)s" in sql and "%(district)s" in sql, "inputs travel as parameters"


def test_trend_and_prediction_summaries():
    pts = [{"bucket": f"2026-10-28T11:{m:02d}:00+00:00", "readings": n} for m, n in zip(range(0, 60, 5), [100] * 9 + [50, 50, 50])]
    t = explain._trend(pts)
    assert t == {"last_15m_readings": 150, "previous_avg_15m_readings": 300.0, "change": -0.5}
    assert explain._trend([])["change"] is None and explain._trend(pts[:3])["change"] is None
    sim_rows = [{"next_hour_verdict": "operational", "episode_lasted_min": 30.0}, {"next_hour_verdict": "operational", "episode_lasted_min": 60.0},
                {"next_hour_verdict": "partial_outage", "episode_lasted_min": None}]
    assert explain._prediction(sim_rows) == {"based_on": 3, "verdict_one_hour_later": {"operational": 2, "partial_outage": 1}, "median_episode_minutes": 45.0}
    assert explain._prediction([]) is None


def test_tool_definitions_have_schemas_a_model_can_use():
    listed = tools.listing()
    assert [t["name"] for t in listed] == ["explain_district", "similar_episodes", "district_series"]
    for t in listed:
        assert t["read_only"] and t["permission"] == "city:read" and len(t["description"]) > 80
        s = t["input_schema"]
        assert s["type"] == "object" and s["additionalProperties"] is False and set(s["required"]) == {"city", "district"}
        assert s["properties"]["city"]["pattern"] == catalog.CITY_ID.pattern


@pytest.mark.parametrize("args,fragment", [
    ({}, "missing argument: city"), ({"city": "paris"}, "missing argument: district"), ({"city": "Paris", "district": "d01"}, "does not match"),
    ({"city": "paris", "district": "d01", "k": 0}, "between 1 and"), ({"city": "paris", "district": "d01", "k": "3"}, "an integer"),
    ({"city": "paris", "district": "d01", "k": True}, "an integer"), ({"city": "paris", "district": "d01", "sql": "x"}, "unknown argument"),
    ("paris", "must be an object"),
])
def test_tool_arguments_are_checked_against_the_schema(args, fragment):
    with pytest.raises(tools.ToolError, match=fragment):
        tools.check_arguments(tools.TOOLS["explain_district"], args)


def test_tool_arguments_pass_when_they_fit_and_unknown_tools_are_unknown():
    assert tools.check_arguments(tools.TOOLS["explain_district"], {"city": "paris", "district": "d01", "k": 3}) == {"city": "paris", "district": "d01", "k": 3}
    assert tools.check_arguments(tools.TOOLS["district_series"], {"city": "paris", "district": "d01", "hours": 1.5, "bucket": "1h"})["hours"] == 1.5
    with pytest.raises(tools.ToolError, match="one of"):
        tools.check_arguments(tools.TOOLS["district_series"], {"city": "paris", "district": "d01", "bucket": "1d"})
    with pytest.raises(tools.UnknownTool):
        tools.call(None, "drop_everything", {})


def test_the_guide_shows_the_explain_statement_the_code_runs():
    """docs/GUIDE.md 7.15 prints the full statement; it must not drift from explain.build_sql."""
    from pathlib import Path
    guide = (Path(__file__).resolve().parents[1] / "docs" / "GUIDE.md").read_text()
    assert explain.build_sql(True, True, True) in guide
    assert "7.13" in (Path(timeseries.__file__).parent / "migrations" / "007_timeseries_memory.sql").read_text()
