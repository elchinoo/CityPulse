"""AIDB mode on plain Postgres (what CI runs): the switch and its default, the derived views and what they leave out, the incident texts, the
fallbacks when AIDB is absent, errs or answers badly, the seal key (vault) paths, the API routes and the operator commands. Nothing here needs the
AIDB extension; the tests against a real AIDB are in test_aidb_integration.py (marker `aidb`)."""
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("set TEST_DATABASE_URL to run DB tests", allow_module_level=True)

import psycopg  # noqa: E402
from apgkit import aidb, retrieval  # noqa: E402
from apgkit import sealed as sealing  # noqa: E402

from citypulse import (  # noqa: E402
    aidb_features,
    aidb_mode,
    aidb_objects,
    cli,
    core,
    db,
    gateway,
    memory,
    migrate,
)
from citypulse.db import connect  # noqa: E402

H = {"X-CityPulse-CSRF": "1"}
PASSWORD = "correct-horse-battery"  # pragma: allowlist secret
KEY = "k" * 40  # pragma: allowlist secret
BEARER = {"Authorization": f"Bearer {KEY}"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    for v in ("CITYPULSE_ALLOW_ANONYMOUS", "PAGERLENS_URL", "PAGERLENS_TOKEN", "CITYPULSE_RETRIEVAL_BACKEND", "CITYPULSE_AIDB_ADMIN_DSN",
              "CITYPULSE_ENCRYPTION_KEY", "LLM_BASE_URL", "LLM_CHAT_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CITYPULSE_API_KEY", KEY)
    monkeypatch.setenv("CITYPULSE_ENCRYPTION_KEY_FILE", "/nonexistent/key")
    monkeypatch.setenv("LLM_API_KEY_FILE", "/nonexistent/key")
    db.reset_pool()
    core.auth.limiter.clear()
    core.clear_token_cache()
    core.vault.lock()
    aidb_mode.reset_cache()
    aidb_features.reset_retriever()
    aidb_mode._reported.clear()
    gateway._inflight = 0
    gateway._known.update(count=0, at=0.0)
    for attempt in range(4):             # the session's engine and masking loops touch the catalog too: "tuple concurrently updated" is theirs, retry
        try:
            with connect() as c:
                c.execute("DROP SCHEMA IF EXISTS cp_ask CASCADE; DROP SCHEMA IF EXISTS cp_ai CASCADE; DROP SCHEMA public CASCADE; CREATE SCHEMA public")
                c.commit()
            break
        except psycopg.errors.InternalError_:
            if attempt == 3:
                raise
            time.sleep(0.5)
    migrate.run()
    yield
    core.vault.lock()
    aidb_mode.reset_cache()
    aidb_features.reset_retriever()
    db.reset_pool()


# ---- data ------------------------------------------------------------------------------------------------------------------------
def stats(city, district, total=20, ok=20, offline=0, stale=0, fault=0, stuck=0, burst=0.0):
    return {"city": city, "district": district, "total": total, "ok": ok, "stale": stale, "offline": offline, "fault": fault, "stuck": stuck,
            "delta_mean": 0.01, "delta_p95": 0.02, "burst_share": burst, "silent": offline, "rate_ratio": 1.0 - offline / total}


def add_episode(conn, s, end, trigger="schedule"):
    conn.execute(memory.INSERT, memory.row_for(s, end, trigger))


def seed(conn):
    """Three districts of one city with sensors in different states, a burst, and its aftermath; an incident of another city with a known outcome."""
    for d, bad in (("d01", 12), ("d02", 3), ("d03", 1)):
        conn.execute("INSERT INTO sensor (id, type, district, city, last_ts, last_value, status, reason, attrs) "
                     "SELECT 'temperature-' || %s || '-' || lpad(g::text, 4, '0'), 'temperature', %s, 'testville', "
                     "CASE WHEN g <= %s THEN now() - interval '1 hour' ELSE now() END, 20, "
                     "CASE WHEN g <= %s THEN 'offline' ELSE 'ok' END, CASE WHEN g <= %s THEN 'no reading for 6 minutes' END, "
                     "'{\"vendor\": \"acme-secret-vendor\"}'::jsonb FROM generate_series(1, 20) g", (d, d, bad, bad, bad))
    now = datetime.now(UTC).replace(microsecond=0)
    t = now - timedelta(hours=3)
    conn.execute("INSERT INTO sensor (id, type, district, city, last_ts, last_value) VALUES ('temperature-alder-quay-0001', 'temperature', 'alder-quay', 'alderton', now(), 20)")
    conn.execute("INSERT INTO chaos (kind, target, intensity, started_at, until) VALUES ('dropout', 'city:testville', 4, %s, %s)", (t, t + timedelta(minutes=10)))
    conn.execute("INSERT INTO chaos (kind, target, intensity, started_at, until) VALUES ('flood', 'district:d02', 2, %s, %s)",
                 (now - timedelta(hours=10), now - timedelta(hours=10) + timedelta(minutes=10)))
    during = t + timedelta(minutes=5)
    for d, ok, off in (("d01", 8, 12), ("d02", 17, 3), ("d03", 19, 1)):
        add_episode(conn, stats("testville", d, ok=ok, offline=off, burst=0.9), during)
        add_episode(conn, stats("testville", d), during + timedelta(minutes=70))          # recovered: gives the outcome
    add_episode(conn, stats("testville", "d02", ok=10, stale=10, burst=0.5), now - timedelta(hours=10) + timedelta(minutes=5))
    add_episode(conn, stats("testville", "d02"), now - timedelta(hours=8))
    add_episode(conn, stats("alderton", "alder-quay"), now - timedelta(days=2, minutes=15), "seed")      # (an outcome needs an operational window before)
    add_episode(conn, stats("alderton", "alder-quay", ok=12, offline=8, burst=1.0), now - timedelta(days=2), "seed")
    add_episode(conn, stats("alderton", "alder-quay"), now - timedelta(days=2) + timedelta(minutes=90), "seed")
    add_episode(conn, stats("alderton", "north-gate"), now - timedelta(days=3, minutes=15), "seed")
    add_episode(conn, stats("alderton", "north-gate", ok=14, stuck=6), now - timedelta(days=3), "seed")
    add_episode(conn, stats("alderton", "north-gate"), now - timedelta(days=3) + timedelta(minutes=45), "seed")
    memory.label(conn, since="5 days")
    conn.commit()


# ---- the switch and the defaults ---------------------------------------------------------------------------------------------------
def test_the_switch_defaults_to_todays_behaviour():
    assert aidb_mode.requested() == {"retrieval_backend": "app"} and not aidb_mode.any_requested() and not aidb_mode.narratives_on()


def test_switch_values_are_parsed_and_an_unknown_one_means_the_default(monkeypatch, caplog):
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", " AIDB ")
    assert aidb_mode.requested() == {"retrieval_backend": "aidb"} and aidb_mode.narratives_on()
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "vector")
    with caplog.at_level(logging.ERROR):
        assert aidb_mode.requested() == {"retrieval_backend": "app"} and not aidb_mode.any_requested()
    assert any("is not one of" in r.message for r in caplog.records)


def test_with_the_switch_off_nothing_of_aidb_mode_runs(client, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("AIDB mode code ran with the switch off")
    for fn in ("usable", "state", "inspect"):
        monkeypatch.setattr(aidb_mode, fn, forbidden)
    monkeypatch.setattr(aidb_features, "call_agent", forbidden)
    monkeypatch.setattr(retrieval, "select", forbidden)
    with connect() as conn:
        seed(conn)
        conn.execute("DELETE FROM cp_ai.incident_text")
        out = memory.tick(conn, datetime.now(UTC).replace(minute=1, second=0, microsecond=0))
        assert set(out) == {"changed", "scheduled", "labelled"}, "the engine's result keeps its shape"
        conn.commit()
        assert conn.execute("SELECT count(*) AS n FROM cp_ai.incident_text").fetchone()["n"] == 0, "the default path writes no incident text"
        assert conn.execute("SELECT count(*) AS n FROM cp_ai.ask_log").fetchone()["n"] == 0
        assert not conn.execute("SELECT 1 FROM app_setting WHERE key LIKE 'vault%%'").fetchone(), "no seal key is created while AIDB mode is off"
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["aidb"] == {"enabled": False}
    a = client.post("/api/ask", json={"question": "which districts lost the most sensors?"}, headers=BEARER)
    assert a.status_code == 503 and "switched off" in a.json()["detail"]


def test_default_similarity_is_full_text_search_over_the_incident_texts(client):
    with connect() as conn:
        seed(conn)
        assert aidb_features.backfill_narratives(conn) >= 4
        conn.commit()
    r = client.get("/api/incidents/similar", params={"q": "many sensors offline while a burst was active"}, headers=BEARER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backend"] == "app" and body["requested"] == "app" and "fallback" not in body
    assert body["items"] and body["items"][0]["district"] in {"alder-quay", "d01", "d02", "d03"}
    assert {"episode_id", "city", "district", "verdict", "outcome", "narrative", "score"} <= set(body["items"][0])
    by_state = client.get("/api/incidents/similar", params={"city": "testville", "district": "d01"}, headers=BEARER).json()
    assert by_state["backend"] == "app" and by_state["current"]["district"] == "d01" and by_state["query"]
    assert client.get("/api/incidents/similar", params={"city": "testville", "district": "nowhere"}, headers=BEARER).status_code == 404
    assert client.get("/api/incidents/similar", headers=BEARER).status_code == 422
    assert client.get("/api/incidents/similar", params={"q": "x"}, headers=BEARER).status_code == 422


# ---- the views: what a question can read ----------------------------------------------------------------------------------------------
def test_the_views_have_exactly_the_documented_columns(client):
    with connect() as conn:
        seed(conn)
        for view, columns in aidb_objects.VIEWS.items():
            schema, name = view.split(".")
            got = [r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                                                          "ORDER BY ordinal_position", (schema, name)).fetchall()]
            assert tuple(got) == columns, view
            conn.execute(f"SELECT * FROM {view} LIMIT 1")                                # noqa: S608  (constant names)
        assert set(aidb_objects.VIEW_ORDER) == set(aidb_objects.VIEWS)
        conn.rollback()


def test_no_raw_reading_and_no_secret_table_is_reachable_from_cp_ask(client):
    with connect() as conn:
        seed(conn)
        deps = {r["t"] for r in conn.execute("""
            SELECT DISTINCT t.relname AS t FROM pg_depend d JOIN pg_rewrite rw ON rw.oid = d.objid
            JOIN pg_class v ON v.oid = rw.ev_class JOIN pg_namespace vn ON vn.oid = v.relnamespace AND vn.nspname = 'cp_ask'
            JOIN pg_class t ON t.oid = d.refobjid AND t.relkind IN ('r', 'p') WHERE d.classid = 'pg_rewrite'::regclass""").fetchall()}
        assert deps == {"sensor", "rollup_5m", "episode", "chaos"}, deps       # never `reading`, a token, a session or a setting
        blob = " ".join(r["t"] for view in aidb_objects.VIEWS for r in conn.execute(f"SELECT v::text AS t FROM {view} v").fetchall())   # noqa: S608
        assert "acme-secret-vendor" not in blob, "device attributes never reach a view"
        cols = [c for cols in aidb_objects.VIEWS.values() for c in cols]
        assert not {"attrs", "lat", "lon", "token_hash", "value"} & set(cols)
        conn.rollback()


def test_the_views_answer_the_burst_question(client):
    with connect() as conn:
        seed(conn)
        rows = conn.execute("SELECT b.burst_id, i.district, i.peak_sensors_not_ok FROM cp_ask.burst_impact i JOIN cp_ask.bursts b USING (burst_id) "
                            "WHERE b.started_at = (SELECT max(started_at) FROM cp_ask.bursts) ORDER BY i.peak_sensors_not_ok DESC").fetchall()
        assert [(r["district"], r["peak_sensors_not_ok"]) for r in rows] == [("d01", 12), ("d02", 3), ("d03", 1)]
        d = {r["district"]: r for r in conn.execute("SELECT * FROM cp_ask.districts WHERE city = 'testville'").fetchall()}
        assert d["d01"]["verdict"] == "major_outage" or d["d01"]["verdict"] == "partial_outage"
        assert d["d01"]["sensors_offline"] == 12 and d["d03"]["verdict"] == "degraded" or d["d03"]["verdict"] == "operational"
        inc = {r["district"]: r for r in conn.execute("SELECT * FROM cp_ask.incidents WHERE city = 'alderton'").fetchall()}
        assert inc["alder-quay"]["offline_pct"] == 40.0 and inc["alder-quay"]["burst_active_pct"] == 100.0 and inc["alder-quay"]["duration_min"] is not None
        assert inc["north-gate"]["stuck_pct"] == 30.0 and inc["north-gate"]["verdict_1h"] is not None
        conn.rollback()


def test_the_objects_hook_is_idempotent_and_a_role_that_may_not_create_does_not_stop_the_app(client):
    with connect() as conn:
        assert aidb_objects.apply(conn) is True and aidb_objects.apply(conn) is True and aidb_objects.present(conn)
        conn.commit()
        conn.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cp_nocreate') THEN "
                     "CREATE ROLE cp_nocreate LOGIN PASSWORD 'cp-nocreate-throwaway' NOSUPERUSER; END IF; END $$")      # pragma: allowlist secret
        conn.commit()
    from psycopg.conninfo import make_conninfo
    dsn = make_conninfo(os.environ["TEST_DATABASE_URL"], user="cp_nocreate", password="cp-nocreate-throwaway")        # pragma: allowlist secret
    try:
        with connect() as conn:
            conn.execute("GRANT USAGE ON SCHEMA cp_ask, cp_ai TO cp_nocreate")
            conn.commit()
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as weak:
            assert aidb_objects.apply(weak) is True            # the views exist from the owner's run: used as they are, logged, never raised
        with connect() as conn:
            conn.execute("DROP SCHEMA cp_ask CASCADE")
            conn.commit()
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as weak:
            assert aidb_objects.apply(weak) is False            # nothing there and nothing it may create: logged, not raised
    finally:
        with connect() as conn:
            conn.execute("DROP OWNED BY cp_nocreate")
            conn.execute("DROP ROLE IF EXISTS cp_nocreate")
            conn.commit()


# ---- incident texts ---------------------------------------------------------------------------------------------------------------------
def test_a_narrative_is_derived_deterministic_and_holds_no_identifier(client):
    with connect() as conn:
        seed(conn)
        n = aidb_features.backfill_narratives(conn)
        assert n == conn.execute("SELECT count(*) AS n FROM cp_ask.incidents").fetchone()["n"] > 3
        assert aidb_features.backfill_narratives(conn) == n
        before = conn.execute("SELECT episode_id, updated_at FROM cp_ai.incident_text ORDER BY episode_id").fetchall()
        aidb_features.backfill_narratives(conn)
        assert conn.execute("SELECT episode_id, updated_at FROM cp_ai.incident_text ORDER BY episode_id").fetchall() == before, "repeating changes nothing"
        texts = {r["district"]: r["narrative"] for r in conn.execute(
            "SELECT i.district, t.narrative FROM cp_ai.incident_text t JOIN cp_ask.incidents i USING (episode_id) WHERE i.city = 'alderton'").fetchall()}
        assert texts["alder-quay"].startswith("40% of the 20 sensors went offline") and "active for 100% of the window" in texts["alder-quay"]
        assert "lasted" in texts["alder-quay"] and texts["north-gate"].startswith("30% of the 20 sensors repeated the same value (stuck)")
        for t in texts.values():
            assert "temperature-d01" not in t and "acme-secret-vendor" not in t
        conn.rollback()


def test_touch_writes_only_when_aidb_mode_is_on_and_picks_up_outcomes(client, monkeypatch):
    with connect() as conn:
        seed(conn)
        assert aidb_features.touch(conn) == 0 and conn.execute("SELECT count(*) AS n FROM cp_ai.incident_text").fetchone()["n"] == 0
        monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
        n = aidb_features.touch(conn)
        assert n > 3 and aidb_features.touch(conn) == 0, "a second pass finds nothing pending"
        # an incident whose outcome is not known yet gets a shorter text, and the full one once the outcome arrives
        row = conn.execute("SELECT episode_id FROM cp_ask.incidents WHERE district = 'north-gate'").fetchone()
        conn.execute("UPDATE episode SET verdict_1h = NULL, ended_at = NULL, duration_min = NULL, episode_start = NULL WHERE id = %s", (row["episode_id"],))
        conn.execute("DELETE FROM cp_ai.incident_text WHERE episode_id = %s", (row["episode_id"],))
        aidb_features.touch(conn)
        short = conn.execute("SELECT narrative FROM cp_ai.incident_text WHERE episode_id = %s", (row["episode_id"],)).fetchone()["narrative"]
        assert "an hour later" not in short and "lasted" not in short
        conn.execute("UPDATE episode SET verdict_1h = 'operational', ended_at = now(), duration_min = 45, episode_start = now() - interval '45 min' WHERE id = %s",
                     (row["episode_id"],))
        assert aidb_features.touch(conn) == 1
        full = conn.execute("SELECT narrative FROM cp_ai.incident_text WHERE episode_id = %s", (row["episode_id"],)).fetchone()["narrative"]
        assert "an hour later" in full and "lasted 45 minutes" in full
        conn.rollback()


def test_the_engine_pass_writes_incident_texts_when_on_and_a_failure_never_reaches_it(client, monkeypatch):
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
    memory.reset_state()
    with connect() as conn:
        seed(conn)
        out = memory.tick(conn, datetime.now(UTC).replace(minute=1, second=0, microsecond=0))
        assert out["scheduled"] >= 0
        conn.commit()
        assert conn.execute("SELECT count(*) AS n FROM cp_ai.incident_text").fetchone()["n"] > 3
        conn.execute("DROP TABLE cp_ai.incident_text")
        conn.commit()
        assert aidb_features.touch(conn) == 0                      # logged, swallowed
        memory.reset_state()
        assert memory.tick(conn, datetime.now(UTC).replace(minute=1, second=0, microsecond=0)) is not None


def test_deleting_a_city_removes_its_incident_texts(client, monkeypatch):
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
    with connect() as conn:
        seed(conn)
        aidb_features.touch(conn)
        conn.commit()
    assert client.delete("/api/cities/alderton", headers={**BEARER, **H}).status_code in (200, 404)
    with connect() as conn:
        assert not conn.execute("SELECT 1 FROM cp_ai.incident_text t JOIN episode e ON e.id = t.episode_id WHERE e.city = 'alderton'").fetchone()
        assert conn.execute("SELECT count(*) AS n FROM cp_ai.incident_text WHERE episode_id NOT IN (SELECT id FROM episode)").fetchone()["n"] == 0


# ---- AIDB requested but absent: the fallbacks ------------------------------------------------------------------------------------------------
def test_with_aidb_absent_similarity_falls_back_loudly_and_the_app_stays_ready(client, monkeypatch, caplog):
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
    with connect() as conn:
        seed(conn)
        aidb_features.touch(conn)
        conn.commit()
    with caplog.at_level(logging.ERROR):
        r = client.get("/api/incidents/similar", params={"q": "sensors offline during a burst"}, headers=BEARER)
    assert r.status_code == 200 and r.json()["backend"] == "app" and r.json()["requested"] == "aidb" and r.json()["items"]
    assert any("RETRIEVAL BACKEND FALLBACK" in rec.message for rec in caplog.records)
    z = client.get("/readyz")
    assert z.status_code == 200 and z.json()["status"] == "ready" and z.json()["aidb"]["enabled"] is True and z.json()["aidb"]["state"] == "unavailable"
    a = client.post("/api/ask", json={"question": "which districts lost the most sensors?"}, headers=BEARER)
    assert a.status_code == 503 and "not available" in a.json()["detail"]
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM cp_ai.ask_log").fetchone()["n"] == 0, "a refused question is not an asked question"


def test_the_retrieval_backend_is_chosen_by_the_kit_when_usable(monkeypatch):
    """With the state forced to ready, the kit's `retrieval.select` picks the AIDB backend; a failing AIDB at query time falls back to the app one."""
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
    monkeypatch.setattr(aidb_mode, "usable", lambda feature, conn: (True, ""))
    seen = {}

    def select(prefix, *, app, aidb=None, connect=None):
        seen.update(prefix=prefix, kb=aidb.kb, app=app.table, column=app.text_column)
        return aidb
    monkeypatch.setattr(retrieval, "select", select)
    with connect() as conn:
        r = aidb_features.retriever(conn)
    assert isinstance(r, retrieval.AidbRetrieval) and seen == {"prefix": "CITYPULSE", "kb": "cp_ai.pipeline_cp_incident_text", "app": "incident_text",
                                                               "column": "narrative"}
    aidb_features.reset_retriever()

    class Broken:
        name = "aidb"

        def retrieve(self, *a, **k):
            raise psycopg.errors.UndefinedTable("kb gone")
    monkeypatch.setattr(aidb_features, "retriever", lambda conn: Broken())
    with connect() as conn:
        seed(conn)
        aidb_features.backfill_narratives(conn)
        conn.commit()
        out = aidb_features.similar_incidents(conn, q="sensors offline during a burst", k=3)
    assert out["backend"] == "app" and "UndefinedTable" in out["fallback"] and out["items"]


# ---- questions: the route and the ledger ---------------------------------------------------------------------------------------------------
@pytest.fixture
def agent(monkeypatch):
    """A usable agent whose answers and action log the test scripts (no AIDB, no model)."""
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
    monkeypatch.setattr(aidb_mode, "usable", lambda feature, conn: (True, ""))
    box = {"call": aidb_features.AgentCall("District d01 lost 12 sensors.", "11111111-2222-3333-4444-555555555555", "", 12),
           "ran": [{"tool": "semantic_kb_search", "sql": None, "ok": True, "rows": None, "error": ""},
                   {"tool": "run_sql_query", "sql": "SELECT district, peak_sensors_not_ok FROM cp_ask.burst_impact", "ok": True,
                    "rows": [{"district": "d01", "peak_sensors_not_ok": 12}, {"district": "d02", "peak_sensors_not_ok": 3}], "error": ""}],
           "prompts": []}

    def call(conn, name, prompt, conversation_id=None):
        box["prompts"].append((name, prompt))
        return box["call"]
    monkeypatch.setattr(aidb_features, "call_agent", call)
    monkeypatch.setattr(aidb_features, "executed", lambda conn, cid, since: box["ran"])
    return box


QUESTION = "which districts lost the most sensors during the last burst? my pin is 4821"


def test_ask_returns_the_answer_the_sql_and_the_rows_and_ledgers_a_hash(client, agent, caplog):
    with caplog.at_level(logging.INFO, logger="citypulse.aidb"):
        r = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "District d01 lost 12 sensors." and body["sql"] == "SELECT district, peak_sensors_not_ok FROM cp_ask.burst_impact"
    assert body["rows"][0] == {"district": "d01", "peak_sensors_not_ok": 12} and body["row_count"] == 2 and body["truncated"] is False
    assert [s["tool"] for s in body["steps"]] == ["semantic_kb_search", "run_sql_query"] and body["agent"] == "cp_ask_agent"
    assert agent["prompts"] == [("cp_ask_agent", QUESTION)]
    with connect() as conn:
        rows = conn.execute("SELECT * FROM cp_ai.ask_log").fetchall()
        assert len(rows) == 1 and rows[0]["question_hash"] == aidb_features.short_hash(QUESTION) and rows[0]["question_chars"] == len(QUESTION)
        assert rows[0]["outcome"] == "ok" and rows[0]["rows_returned"] == 2 and rows[0]["actor"]
        assert "4821" not in json.dumps(rows[0], default=str), "the ledger holds the hash and the length, never the text"
    assert "4821" not in caplog.text and aidb_features.short_hash(QUESTION) in caplog.text, "the log holds the hash and the length, never the text"


def test_ask_caps_the_rows_and_reports_a_refused_sql_instead_of_hiding_it(client, agent):
    agent["ran"][1]["rows"] = [{"n": i} for i in range(aidb_features.MAX_ROWS + 50)]
    body = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).json()
    assert len(body["rows"]) == aidb_features.MAX_ROWS and body["row_count"] == aidb_features.MAX_ROWS + 50 and body["truncated"] is True
    agent["ran"] = [{"tool": "run_sql_query", "sql": "SELECT * FROM public.reading", "ok": False, "rows": None,
                     "error": "Tool error: permission denied for table reading [decision_id=abc]"}]
    agent["call"] = aidb_features.AgentCall("I could not read that.", "11111111-2222-3333-4444-555555555555", "", 5)
    body = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).json()
    assert body["sql"] == "SELECT * FROM public.reading" and body["rows"] == [] and "permission denied" in body["sql_error"]
    agent["ran"] = []                                                    # an answer that ran no SQL is not an answer (see the tests below)
    body = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).json()
    assert body["sql"] is None and body["rows"] == [] and body["answer"] is None and body["executed"] is False and body["unverified_text"]


def test_ask_failure_modes(client, agent):
    agent["call"] = aidb_features.AgentCall(None, None, "timed out after 90s", 90000)
    r = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER)
    assert r.status_code == 502 and "timed out" in r.json()["detail"]
    with connect() as conn:
        assert conn.execute("SELECT outcome FROM cp_ai.ask_log").fetchone()["outcome"] == "error"
    assert client.post("/api/ask", json={"question": "hi"}, headers=BEARER).status_code == 422
    assert client.post("/api/ask", json={"question": "x" * 501}, headers=BEARER).status_code == 422
    assert client.post("/api/ask", json={"question": QUESTION, "conversation_id": "'; drop table x;--"}, headers=BEARER).status_code == 422
    for _ in range(2):
        assert aidb_features._ask_slots.acquire(blocking=False)
    try:
        assert client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).status_code == 429
    finally:
        aidb_features._ask_slots.release()
        aidb_features._ask_slots.release()


def test_ask_needs_its_own_permission(client):
    r = client.post("/api/tokens", json={"name": "readonly", "scope": "read"}, headers={**BEARER, **H})
    assert r.status_code == 200, r.text
    read = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post("/api/ask", json={"question": QUESTION}, headers=read).status_code == 403
    assert client.post("/api/ask", json={"question": QUESTION}).status_code in (401, 403)
    assert "city:ask" in core.PERMISSIONS and core.ROLE_PERMISSIONS["operator"] >= {"city:ask"} and "city:ask" not in core.ROLE_PERMISSIONS["viewer"]
    assert "city:ask" not in core.SCOPE_PERMISSIONS["read"] and core.SCOPE_PERMISSIONS["full"] == core.ALL


def test_the_ask_ledger_is_pruned(client, agent):
    with connect() as conn:
        conn.execute("INSERT INTO cp_ai.ask_log (asked_at, actor, question_hash, question_chars, outcome) "
                     "VALUES (now() - interval '120 days', 'old', 'aaaaaaaaaaaa', 5, 'ok')")
        conn.commit()
    assert client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).status_code == 200
    with connect() as conn:
        assert [r["actor"] for r in conn.execute("SELECT actor FROM cp_ai.ask_log").fetchall()] != ["old"]
        assert conn.execute("SELECT count(*) AS n FROM cp_ai.ask_log WHERE actor = 'old'").fetchone()["n"] == 0


def test_the_actions_the_agent_took_are_read_back_from_aidbs_action_log(client):
    """`executed` reads AIDB's own log; here a stand-in table with the same columns."""
    with connect() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS aidb_internal")
        conn.execute("DROP TABLE IF EXISTS aidb_internal.action_log")
        conn.execute("CREATE TABLE aidb_internal.action_log (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, conversation_id uuid NOT NULL, "
                     "action_type text NOT NULL, request_payload jsonb, response_payload jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp())")
        cid = "11111111-2222-3333-4444-555555555555"
        for kind, req, resp in (
                ("user_prompt", {"contents": "q"}, None),
                ("tool_call", {"call_id": "c1", "tool_name": "semantic_kb_search", "arguments": {"query_text": "burst"}}, None),
                ("tool_response", None, {"call_id": "c1", "contents": "[]"}),
                ("tool_call", {"call_id": "c2", "tool_name": "run_sql_query", "arguments": {"query": "SELECT 1 AS one"}}, None),
                ("tool_response", None, {"call_id": "c2", "contents": '[{"one": 1}]'}),
                ("tool_call", {"call_id": "c3", "tool_name": "run_sql_query", "arguments": {"query": "SELECT * FROM public.reading"}}, None),
                ("tool_response", None, {"call_id": "c3", "error": "Tool error: permission denied for table reading\nmore"})):
            conn.execute("INSERT INTO aidb_internal.action_log (conversation_id, action_type, request_payload, response_payload) VALUES (%s, %s, %s, %s)",
                         (cid, kind, psycopg.types.json.Jsonb(req) if req else None, psycopg.types.json.Jsonb(resp) if resp else None))
        conn.execute("INSERT INTO aidb_internal.action_log (conversation_id, action_type, request_payload) VALUES (gen_random_uuid(), 'tool_call', %s)",
                     (psycopg.types.json.Jsonb({"call_id": "x", "tool_name": "run_sql_query", "arguments": {"query": "SELECT 'other conversation'"}}),))
        since = conn.execute("SELECT now() - interval '1 hour' AS t").fetchone()["t"]
        ran = aidb_features.executed(conn, cid, since)
        assert [(c["tool"], c["sql"], c["ok"], c["rows"], c["error"]) for c in ran] == [
            ("semantic_kb_search", None, True, None, ""), ("run_sql_query", "SELECT 1 AS one", True, [{"one": 1}], ""),
            ("run_sql_query", "SELECT * FROM public.reading", False, None, "Tool error: permission denied for table reading")]
        assert aidb_features.executed(conn, None, since) == []
        assert aidb_features.executed(conn, "99999999-2222-3333-4444-555555555555", since) == []
        conn.execute("DROP SCHEMA aidb_internal CASCADE")
        conn.commit()
        assert aidb_features.executed(conn, cid, since) == []            # no log to read (plain Postgres): nothing, not an error


# ---- the seal key (vault) -----------------------------------------------------------------------------------------------------------------
def test_the_vault_is_created_by_aidb_setup_with_the_platform_secret_and_reopened_without_a_prompt(monkeypatch):
    monkeypatch.setenv("CITYPULSE_ENCRYPTION_KEY", "p" * 48)
    with connect() as conn:
        assert not core.vault.is_initialized(conn)
        assert cli._unlock_vault(conn, create=False) is False and not core.vault.is_unlocked()
        assert cli._unlock_vault(conn, create=True) is True and core.vault.is_unlocked()
        wrapped = conn.execute("SELECT key FROM app_setting WHERE key LIKE 'vault%%' ORDER BY key").fetchall()
        assert {r["key"] for r in wrapped} >= {"vault_wrap_env", "vault_env_salt"} and "vault_wrap_pw" not in {r["key"] for r in wrapped}
        key_text = " ".join(r["value"] for r in conn.execute("SELECT value FROM app_setting WHERE key LIKE 'vault%%'").fetchall())
        assert "p" * 48 not in key_text, "the platform secret is not stored"
        core.vault.lock()
        assert cli._unlock_vault(conn, create=False, prompt=False) is True and core.vault.is_unlocked()
        mac = core.vault.row_mac(aidb.SEAL_TABLE, "x")
        core.vault.lock()
    monkeypatch.setenv("CITYPULSE_ENCRYPTION_KEY", "q" * 48)
    with connect() as conn:
        assert cli._unlock_vault(conn, create=False, prompt=False) is False, "another platform secret does not open it"
    monkeypatch.setenv("CITYPULSE_ENCRYPTION_KEY", "p" * 48)
    with connect() as conn:
        cli._unlock_vault(conn, create=False)
        assert core.vault.row_mac(aidb.SEAL_TABLE, "x") == mac, "the same key, the same MAC across restarts"


def test_the_vault_can_be_created_and_opened_with_a_passphrase_when_there_is_no_platform_secret(monkeypatch, capsys):
    import getpass
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["short", "a-sealed-boot-passphrase-1", "wrong passphrase", "a-sealed-boot-passphrase-1"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    with connect() as conn:
        assert cli._unlock_vault(conn, create=True) is False and "at least 12" in capsys.readouterr().err
        assert cli._unlock_vault(conn, create=True) is True and core.vault.is_unlocked()
        core.vault.lock()
        assert cli._unlock_vault(conn, create=False) is False and "does not open" in capsys.readouterr().err
        assert cli._unlock_vault(conn, create=False) is True and core.vault.is_unlocked()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    core.vault.lock()
    with connect() as conn:
        assert cli._unlock_vault(conn, create=False) is False, "no terminal and no platform secret: it does not prompt"


def test_unlocking_at_start_never_raises_and_does_nothing_while_off(monkeypatch, caplog):
    assert aidb_mode.unlock_vault_at_start() is False
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
    with caplog.at_level(logging.WARNING):
        assert aidb_mode.unlock_vault_at_start() is False
    assert "no seal key exists yet" in caplog.text
    monkeypatch.setenv("CITYPULSE_ENCRYPTION_KEY", "p" * 48)
    with connect() as conn:
        assert cli._unlock_vault(conn, create=True)
    core.vault.lock()
    assert aidb_mode.unlock_vault_at_start() is True and core.vault.is_unlocked()
    core.vault.lock()
    monkeypatch.setenv("CITYPULSE_ENCRYPTION_KEY", "z" * 48)
    with caplog.at_level(logging.WARNING):
        assert aidb_mode.unlock_vault_at_start() is False
    assert "vault stays locked" in caplog.text
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")      # an unreachable database: still no exception
    monkeypatch.setattr(core.db, "connect", lambda retries=1, delay=1.0: (_ for _ in ()).throw(RuntimeError("down")))
    assert aidb_mode.unlock_vault_at_start() is False


def test_the_sealed_boot_callback_opens_the_vault_only_when_it_exists_and_never_raises(monkeypatch, caplog):
    passphrase = "a-sealed-boot-passphrase-1"
    sealed_obj = sealing.Sealed(sealing.seal({"X": "y"}, passphrase))

    def unseal(p):
        sealed_obj.unseal_with(sealing.Protected(p))
    cb = aidb_mode.sealed_unlocker
    cb(None, sealing.Protected(passphrase))                          # switch off: nothing happens, nothing is created
    with connect() as conn:
        assert not core.vault.is_initialized(conn)
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
    with caplog.at_level(logging.WARNING):
        cb(None, sealing.Protected(passphrase))                      # on, but never set up: logged, nothing is created
    assert "no seal key exists yet" in caplog.text and not core.vault.is_unlocked()
    with connect() as conn:
        core.vault.initialize(conn, passphrase)
        conn.commit()
    core.vault.lock()
    cb(None, sealing.Protected(passphrase))
    assert core.vault.is_unlocked()
    core.vault.lock()
    with caplog.at_level(logging.WARNING):
        cb(None, sealing.Protected("some other passphrase"))         # does not open it: logged, the app still unseals
    assert not core.vault.is_unlocked() and "does not open the vault" in caplog.text
    monkeypatch.setattr(core.db, "connect", lambda retries=1, delay=1.0: (_ for _ in ()).throw(RuntimeError("down")))
    cb(None, sealing.Protected(passphrase))                          # a failure inside must not leave the app sealed


SEALED_SCRIPT = textwrap.dedent("""
    import json, os
    from fastapi.testclient import TestClient
    from citypulse import api, core
    out = {}
    with TestClient(api.app) as c:
        key = c.get("/_seal/key").json()["public_key"]
        from apgkit import sealed as sealing
        out["unseal"] = c.post("/_seal/unseal", json=sealing.make_envelope(key, os.environ["TEST_PASSPHRASE"])).status_code
        out["after"] = c.get("/api/status").status_code
        out["vault_unlocked"] = core.vault.is_unlocked()
        out["readyz"] = c.get("/readyz").json().get("aidb")
    print("RESULT" + json.dumps(out))
""")


def _sealed_run(passphrase_for_blob, vault_passphrase, backend):
    url = os.environ["TEST_DATABASE_URL"]
    with connect() as conn:
        conn.execute("DELETE FROM app_setting WHERE key LIKE 'vault%%'")
        conn.commit()
        if vault_passphrase:
            core.vault.initialize(conn, vault_passphrase)
            conn.commit()
        core.vault.lock()
    blob = sealing.seal({"DATABASE_URL": url}, passphrase_for_blob)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CITYPULSE_", "DATABASE_", "DB_EXTERNAL", "PSQL_", "APGKIT_", "TEST_DATABASE"))}
    env.update(APGKIT_SEALED=blob, TEST_PASSPHRASE=passphrase_for_blob, CITYPULSE_ROLE="api", CITYPULSE_API_KEY_FILE="/nonexistent/key")
    if backend:
        env["CITYPULSE_RETRIEVAL_BACKEND"] = backend
    proc = subprocess.run([sys.executable, "-c", SEALED_SCRIPT], env=env, capture_output=True, text=True, timeout=240, check=False)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    assert line, proc.stderr[-2000:]
    return json.loads(line[len("RESULT"):]), proc.stderr


def test_sealed_boot_opens_the_seal_key_with_the_unseal_passphrase_only_when_aidb_mode_is_on():
    p = "correct-horse-battery-staple-9"  # pragma: allowlist secret
    out, _ = _sealed_run(p, p, "aidb")
    assert out["unseal"] == 200 and out["after"] != 503 and out["vault_unlocked"] is True
    assert out["readyz"]["enabled"] is True and out["readyz"]["state"] in ("unavailable", "error", "ready")
    out, _ = _sealed_run(p, p, None)
    assert out["unseal"] == 200 and out["vault_unlocked"] is False and out["readyz"] == {"enabled": False}, "switch off: the vault is not touched"
    out, err = _sealed_run(p, "a-different-vault-passphrase-1", "aidb")
    assert out["unseal"] == 200 and out["after"] != 503, "a vault made with another passphrase never keeps the app sealed"
    assert out["vault_unlocked"] is False and "does not open the vault" in err


# ---- the operator commands ----------------------------------------------------------------------------------------------------------------
def test_aidb_status_on_plain_postgres_reports_unavailable_and_exits_1(capsys, monkeypatch):
    with pytest.raises(SystemExit) as e:
        cli.main(["aidb-status"])
    out = capsys.readouterr().out
    assert e.value.code == 1 and "not available" in out and "requested: retrieval_backend=app" in out
    with pytest.raises(SystemExit):
        cli.main(["aidb-status", "--json"])
    j = json.loads(capsys.readouterr().out)
    assert j["state"] == "unavailable" and j["requested"] == {"retrieval_backend": "app"} and j["governance"]["available"] is False


def test_aidb_setup_refuses_without_an_admin_dsn_and_never_echoes_one(monkeypatch, capsys):
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")
    with pytest.raises(SystemExit) as e:
        cli.main(["aidb-setup"])
    assert e.value.code == 2 and "no admin connection string" in capsys.readouterr().err
    secret = "sup3r-secret-admin-pw"  # pragma: allowlist secret
    monkeypatch.setenv("CITYPULSE_ENCRYPTION_KEY", "p" * 48)
    monkeypatch.setenv("CITYPULSE_AIDB_ADMIN_DSN", f"postgresql://admin:{secret}@127.0.0.1:1/none")
    with pytest.raises(SystemExit) as e:
        cli.main(["aidb-setup"])
    cap = capsys.readouterr()
    assert e.value.code == 1 and secret not in cap.out + cap.err and "could not connect" in cap.err
    # on a server without the extension the operator step stops with the reason, and the same admin string is never printed
    monkeypatch.setenv("CITYPULSE_AIDB_ADMIN_DSN", os.environ["TEST_DATABASE_URL"])
    with pytest.raises(SystemExit) as e:
        cli.main(["aidb-setup"])
    cap = capsys.readouterr()
    assert e.value.code == 1 and "AIDB is not available here" in cap.err and os.environ["TEST_DATABASE_URL"] not in cap.out + cap.err


def test_the_setup_requires_an_unlocked_vault_a_model_and_the_runtime_role(monkeypatch):
    class Admin:
        """Just enough of a connection for the checks that come before any change."""
    st = aidb.Status(True, installed=True, installable=True, version="7.9.0", can_use=True, can_create_roles=True)
    monkeypatch.setattr(aidb, "detect", lambda c: st)
    with connect() as conn:
        with pytest.raises(aidb_mode.SetupError, match="vault is locked"):
            aidb_mode.setup(Admin(), conn, "someone", vault=core.vault)
    monkeypatch.setattr(aidb, "detect", lambda c: aidb.Status(False, reason="the aidb extension is not installed on this server"))
    with connect() as conn, pytest.raises(aidb_mode.SetupError, match="not available"):
        aidb_mode.setup(Admin(), conn, "someone", vault=core.vault)
    monkeypatch.setattr(aidb, "detect", lambda c: aidb.Status(True, installed=True, version="7.9.0", can_use=True, can_create_roles=False))
    with connect() as conn, pytest.raises(aidb_mode.SetupError, match="CREATEROLE"):
        aidb_mode.setup(Admin(), conn, "someone", vault=core.vault)


def test_the_kit_is_pinned_to_the_commit_that_has_the_ddl_free_read_path():
    sha = "e7dd12403f85738bffea64e18efd5401d0c31b56"
    for name in ("kit-override.txt", "runtime.txt", "dev.txt"):
        assert sha in (ROOT / "requirements" / name).read_text(), name
    assert hasattr(aidb, "seal_table_exists") and "expect" in aidb.verify.__code__.co_varnames


def test_the_governance_view_is_narrow_and_names_no_sql_column():
    sql = aidb_mode.governance_view_sql()
    assert "security_barrier = true" in sql and aidb_objects.GOVERNANCE_VIEW in sql and "cp_ask_agent" in sql and "aidb_p_cp_ask" in sql
    head = sql.split("FROM (")[0]
    assert "db.query.text" not in sql and "payload_json AS" not in head and "principal" not in head and "sql" not in head.lower().replace("select", "")
    with connect() as conn:
        assert aidb_mode.recent_decisions(conn)["available"] is False, "no view on a database that never had AIDB set up"


def test_every_aidb_object_is_named_in_the_apps_own_namespace():
    names = [aidb_mode.PIPELINE, aidb_mode.SEMANTIC_KB, aidb_mode.MODEL, aidb_mode.CREDENTIAL, aidb_mode.PURPOSE, aidb_mode.AGENT]
    assert all(n.startswith("cp_") for n in names)
    assert aidb_mode._mine("agent", "cp_ask_agent") and aidb_mode._mine("sql_tool", "semkb_cp_ask_kb_search") and not aidb_mode._mine("agent", "pb_triage_agent")
    assert aidb.purpose_role_name(aidb_mode.PURPOSE) == "aidb_p_cp_ask"


# ---- questions: a model that does not run the query, or runs a wrong one ----------------------------------------------------------------------
GOOD_RAN = [{"tool": "run_sql_query", "sql": "SELECT district, peak_sensors_not_ok FROM cp_ask.burst_impact", "ok": True,
             "rows": [{"district": "d09", "peak_sensors_not_ok": 27}], "error": ""}]
BAD_RAN = [{"tool": "run_sql_query", "sql": "SELECT d.district_id FROM cp_ask.districts d", "ok": False, "rows": None,
            "error": 'Tool error: column d.district_id does not exist'}]
CID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def scripted(monkeypatch):
    """Each agent call takes the next (text, what the action log then shows); records the prompts."""
    monkeypatch.setenv("CITYPULSE_RETRIEVAL_BACKEND", "aidb")
    monkeypatch.setattr(aidb_mode, "usable", lambda feature, conn: (True, ""))
    box = {"steps": [], "prompts": [], "ran": []}

    def call(conn, name, prompt, conversation_id=None):
        box["prompts"].append((prompt, conversation_id))
        text, ran = box["steps"].pop(0)
        box["ran"] = ran
        return aidb_features.AgentCall(text, CID, "", 5)
    monkeypatch.setattr(aidb_features, "call_agent", call)
    monkeypatch.setattr(aidb_features, "executed", lambda conn, cid, since: box["ran"])
    return box


def test_ask_a_model_that_wrote_sql_instead_of_running_it_is_told_once_in_the_same_conversation(client, scripted):
    scripted["steps"] = [("SELECT district FROM cp_ask.districts;", []), ("d09 lost 27 sensors.", GOOD_RAN)]
    body = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).json()
    assert body["executed"] is True and body["retried"] is True and body["answer"] == "d09 lost 27 sensors." and body["rows"][0]["district"] == "d09"
    assert len(scripted["prompts"]) == 2 and scripted["prompts"][1][1] == CID and "run_sql_query" in scripted["prompts"][1][0]
    assert QUESTION not in scripted["prompts"][1][0], "a conversation is continued, the question is not repeated"


def test_ask_a_model_that_still_runs_nothing_has_no_answer_only_unverified_text(client, scripted):
    scripted["steps"] = [("SELECT 1;", []), ("Still just text: d09 lost 27.", [])]
    r = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER)
    body = r.json()
    assert r.status_code == 200 and body["executed"] is False and body["answer"] is None and body["rows"] == [] and body["sql"] is None
    assert body["unverified_text"] == "Still just text: d09 lost 27." and body["retried"] is True and len(scripted["prompts"]) == 2
    with connect() as conn:
        assert conn.execute("SELECT outcome FROM cp_ai.ask_log").fetchone()["outcome"] == "no_query"


def test_ask_a_query_with_an_unknown_column_gets_one_correction_round_with_the_error_and_the_real_columns(client, scripted):
    scripted["steps"] = [("I could not.", BAD_RAN), ("d09 lost 27 sensors.", BAD_RAN + GOOD_RAN)]
    body = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).json()
    assert body["executed"] is True and body["corrected"] is True and body["retried"] is False and body["answer"] == "d09 lost 27 sensors."
    assert body["rows"] == GOOD_RAN[0]["rows"] and not body["sql_error"] and len(scripted["prompts"]) == 2
    fix = scripted["prompts"][1][0]
    assert "d.district_id does not exist" in fix and "cp_ask.burst_impact(burst_id, kind, target, intensity, started_at, ends_at, city, district" in fix
    assert "cp_ask.sensors(sensor_id, city, district, sensor_type" in fix and "NO district_id" in fix
    assert len(fix) < 4500, "the schema hint is bounded"


def test_ask_gives_one_correction_round_only_and_none_for_a_refusal(client, scripted):
    scripted["steps"] = [("no.", BAD_RAN), ("still no.", BAD_RAN)]
    body = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).json()
    assert len(scripted["prompts"]) == 2 and body["corrected"] is True and "does not exist" in body["sql_error"] and body["rows"] == []
    scripted["prompts"].clear()
    denied = [{"tool": "run_sql_query", "sql": "SELECT * FROM public.reading", "ok": False, "rows": None, "error": "permission denied for table reading"}]
    scripted["steps"] = [("not allowed.", denied)]
    body = client.post("/api/ask", json={"question": QUESTION}, headers=BEARER).json()
    assert len(scripted["prompts"]) == 1 and body["corrected"] is False and "permission denied" in body["sql_error"]


def test_the_schema_hint_is_generated_from_the_catalog_and_names_every_view(client):
    with connect() as conn:
        hint = aidb_features.schema_hint(conn)
    for view, cols in aidb_objects.VIEWS.items():
        assert f"{view}({', '.join(cols)})" in hint, view
    assert len(hint) <= aidb_features.SCHEMA_HINT_MAX + len(aidb_features.KEYS_NOTE) + 1


def test_the_comments_state_the_keys_and_the_instructions_carry_the_burst_example(client):
    with connect() as conn:
        c = {r["name"]: r["note"] for r in conn.execute(
            "SELECT c.relname AS name, obj_description(c.oid, 'pg_class') AS note FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'cp_ask' AND c.relkind = 'v'").fetchall()}
        col = conn.execute("SELECT col_description('cp_ask.sensors'::regclass, a.attnum) AS note FROM pg_attribute a "
                           "WHERE a.attrelid = 'cp_ask.sensors'::regclass AND a.attname = 'district'").fetchone()["note"]
    for view in ("sensors", "district_health", "burst_impact"):
        assert "district_id" in c[view], view
    assert "(city, district)" in c["districts"] and "burst_id" in c["burst_impact"] and "peak_sensors_not_ok" in c["burst_impact"]
    assert "not district_id" in col
    assert "FROM cp_ask.burst_impact i WHERE i.burst_id = (SELECT burst_id FROM cp_ask.bursts ORDER BY started_at DESC LIMIT 1)" in aidb_mode.ASK_INSTRUCTIONS
