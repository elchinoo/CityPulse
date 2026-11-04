"""The database objects AIDB mode reads: a schema of derived views for questions (`cp_ask`), a table of incident texts and a ledger of questions
(`cp_ai`). Plain SQL, no AIDB needed, so they are created on every install (a hook after the migrations, like hardening.py) and cost nothing while
AIDB mode is off.

    cp_ask   what the QUESTION agent may read, and the only schema its semantic knowledge base indexes:
               districts        one row per district now: sensors and how many are ok / stale / offline / fault / stuck, the verdict
               sensors          one row per sensor: type, protocol, status, since when, newest reading time and value
               readings_5m      the 5-minute roll-ups per district and sensor type (count, min, avg, max): aggregates, never raw readings
               district_health  one row per district and fingerprint window (15 minutes): verdict, health, sensors not ok
               incidents        the windows whose verdict was not operational, with the shares that explain them and the outcome once known
               bursts           the faults injected through /api/chaos: kind, target, intensity, start, end
               burst_impact     per burst and district: the worst health and the most sensors not ok while the burst (and 30 minutes after) ran
    cp_ai    what no agent reads: `incident_text` (a short derived text per incident, the source of the incident-similarity pipeline), the
             ledger `ask_log` of questions asked (hash and length only), and the governance view `governance_decisions_v` that `aidb-setup` adds.

What the views leave out is the point: no raw reading (the `reading` table is not reachable from cp_ask), no sensor position, no device
attributes (`sensor.attrs` is free-form vendor metadata), no API token, no session, no setting. The views are owned by the application role and
run with its rights (they are not `security_invoker`), which is what lets the purpose role read them without any privilege on the base tables.
Run as the schema owner: the privileges are granted by `citypulse aidb-setup`.
"""
from __future__ import annotations

import logging

import psycopg

log = logging.getLogger("citypulse.aidb_objects")

SCHEMA_ASK, SCHEMA_AI = "cp_ask", "cp_ai"
NARRATIVE_TABLE = "cp_ai.incident_text"
ASK_LOG_TABLE = "cp_ai.ask_log"
GOVERNANCE_VIEW = "cp_ai.governance_decisions_v"

# Columns of the views, for the documentation, the tests and `citypulse aidb-status`.
VIEWS = {
    "cp_ask.districts": ("city", "district", "sensors", "sensors_ok", "sensors_stale", "sensors_offline", "sensors_fault", "sensors_stuck",
                         "health_pct", "verdict", "last_reading_at"),
    "cp_ask.sensors": ("sensor_id", "city", "district", "sensor_type", "protocol", "status", "status_reason", "status_since", "last_reading_at",
                       "last_value"),
    "cp_ask.readings_5m": ("city", "district", "sensor_type", "bucket_start", "readings", "sensors_reporting", "out_of_range", "avg_value",
                           "min_value", "max_value"),
    "cp_ask.district_health": ("episode_id", "city", "district", "window_end", "verdict", "health_pct", "sensors", "sensors_not_ok", "trigger"),
    "cp_ask.incidents": ("episode_id", "city", "district", "window_end", "verdict", "health_pct", "sensors", "sensors_not_ok", "stale_pct",
                         "offline_pct", "fault_pct", "stuck_pct", "burst_active_pct", "verdict_1h", "episode_start", "ended_at", "duration_min"),
    "cp_ask.bursts": ("burst_id", "kind", "target", "intensity", "started_at", "ends_at"),
    "cp_ask.burst_impact": ("burst_id", "kind", "target", "intensity", "started_at", "ends_at", "city", "district", "sensors",
                            "peak_sensors_not_ok", "worst_health_pct", "worst_verdict"),
}
# Creation order: a view built on another comes after it.
VIEW_ORDER = ("cp_ask.districts", "cp_ask.sensors", "cp_ask.readings_5m", "cp_ask.district_health", "cp_ask.incidents", "cp_ask.bursts",
              "cp_ask.burst_impact")

# Joins the operator setup records in the semantic knowledge base: (left, right, columns, predicate, description).
RELATIONSHIPS = (
    ("cp_ask.sensors", "cp_ask.districts", ("city", "district"), "belongs to", "A sensor belongs to one district of one city."),
    ("cp_ask.readings_5m", "cp_ask.districts", ("city", "district"), "summarises", "A roll-up row summarises the readings of one sensor type in one district."),
    ("cp_ask.district_health", "cp_ask.districts", ("city", "district"), "measures", "A health window is one fingerprint of one district."),
    ("cp_ask.incidents", "cp_ask.districts", ("city", "district"), "happened in", "An incident window belongs to one district."),
    ("cp_ask.burst_impact", "cp_ask.districts", ("city", "district"), "hit", "A burst impact row is one burst's effect on one district."),
    ("cp_ask.burst_impact", "cp_ask.bursts", ("burst_id",), "is the impact of", "Each impact row belongs to one burst."),
)

SQL = """
CREATE SCHEMA IF NOT EXISTS cp_ask;
CREATE SCHEMA IF NOT EXISTS cp_ai;

CREATE OR REPLACE VIEW cp_ask.districts AS
SELECT s.city, s.district, count(*)::int AS sensors,
       count(*) FILTER (WHERE s.status = 'ok')::int AS sensors_ok,
       count(*) FILTER (WHERE s.status = 'stale')::int AS sensors_stale,
       count(*) FILTER (WHERE s.status = 'offline')::int AS sensors_offline,
       count(*) FILTER (WHERE s.status = 'fault')::int AS sensors_fault,
       count(*) FILTER (WHERE s.status = 'stuck')::int AS sensors_stuck,
       round((100.0 * count(*) FILTER (WHERE s.status = 'ok') / count(*))::numeric, 1) AS health_pct,
       CASE WHEN count(*) FILTER (WHERE s.status = 'ok')::float8 / count(*) >= 0.97 THEN 'operational'
            WHEN count(*) FILTER (WHERE s.status = 'ok')::float8 / count(*) >= 0.85 THEN 'degraded'
            WHEN count(*) FILTER (WHERE s.status = 'ok')::float8 / count(*) >= 0.5 THEN 'partial_outage'
            ELSE 'major_outage' END AS verdict,
       max(s.last_ts) AS last_reading_at
FROM sensor s GROUP BY s.city, s.district;

CREATE OR REPLACE VIEW cp_ask.sensors AS
SELECT s.id AS sensor_id, s.city, s.district, s.type AS sensor_type, s.protocol, s.status, s.reason AS status_reason, s.status_since,
       s.last_ts AS last_reading_at, s.last_value
FROM sensor s;

CREATE OR REPLACE VIEW cp_ask.readings_5m AS
SELECT r.city, r.district, r.type AS sensor_type, r.bucket AS bucket_start, r.readings, r.sensors AS sensors_reporting, r.out_of_range,
       r.avg_value, r.min_value, r.max_value
FROM rollup_5m r;

CREATE OR REPLACE VIEW cp_ask.district_health AS
SELECT e.id AS episode_id, e.city, e.district, e.window_end, e.verdict, round((100.0 * e.health)::numeric, 1) AS health_pct, e.sensors,
       (e.sensors - round(e.health * e.sensors))::int AS sensors_not_ok, e.trigger
FROM episode e;

-- features[] is the pattern-memory fingerprint (memory.py): 1-based index 2 stale, 3 offline, 4 fault, 5 stuck (each the square root of
-- the share of sensors), 8 the share of the window a burst was active.
CREATE OR REPLACE VIEW cp_ask.incidents AS
SELECT e.id AS episode_id, e.city, e.district, e.window_end, e.verdict, round((100.0 * e.health)::numeric, 1) AS health_pct, e.sensors,
       (e.sensors - round(e.health * e.sensors))::int AS sensors_not_ok,
       round((100.0 * e.features[2] * e.features[2])::numeric, 1) AS stale_pct,
       round((100.0 * e.features[3] * e.features[3])::numeric, 1) AS offline_pct,
       round((100.0 * e.features[4] * e.features[4])::numeric, 1) AS fault_pct,
       round((100.0 * e.features[5] * e.features[5])::numeric, 1) AS stuck_pct,
       round((100.0 * e.features[8])::numeric, 1) AS burst_active_pct,
       e.verdict_1h, e.episode_start, e.ended_at, round(e.duration_min::numeric, 1) AS duration_min
FROM episode e WHERE e.verdict <> 'operational';

CREATE OR REPLACE VIEW cp_ask.bursts AS
SELECT c.id AS burst_id, c.kind, c.target, c.intensity, c.started_at, c.until AS ends_at FROM chaos c;

CREATE OR REPLACE VIEW cp_ask.burst_impact AS
SELECT c.id AS burst_id, c.kind, c.target, c.intensity, c.started_at, c.until AS ends_at, h.city, h.district,
       max(h.sensors)::int AS sensors, max(h.sensors_not_ok)::int AS peak_sensors_not_ok, min(h.health_pct) AS worst_health_pct,
       (array_agg(h.verdict ORDER BY h.health_pct, h.window_end))[1] AS worst_verdict
FROM chaos c JOIN cp_ask.district_health h
  ON h.window_end >= c.started_at AND h.window_end <= c.until + interval '30 minutes'
 AND (c.target = 'all' OR c.target LIKE 'type:%' OR c.target = 'city:' || h.city OR c.target = 'district:' || h.district)
GROUP BY c.id, c.kind, c.target, c.intensity, c.started_at, c.until, h.city, h.district;

COMMENT ON SCHEMA cp_ask IS 'Derived read-only views of a smart-city sensor platform: districts, sensors, 5-minute roll-ups, health windows, incidents and bursts. No raw readings.';

COMMENT ON VIEW cp_ask.districts IS 'One row per district of a city right now: how many sensors it has and how many are ok, stale, offline, in fault or stuck, its health percentage and its verdict (operational, degraded, partial_outage, major_outage).';
COMMENT ON COLUMN cp_ask.districts.city IS 'City id, for example paris.';
COMMENT ON COLUMN cp_ask.districts.district IS 'District id within the city, for example d03 or molenbeek-saint-jean.';
COMMENT ON COLUMN cp_ask.districts.sensors IS 'Number of sensors installed in the district.';
COMMENT ON COLUMN cp_ask.districts.sensors_ok IS 'Sensors reporting normally right now.';
COMMENT ON COLUMN cp_ask.districts.sensors_stale IS 'Sensors whose last reading is late.';
COMMENT ON COLUMN cp_ask.districts.sensors_offline IS 'Sensors that stopped reporting.';
COMMENT ON COLUMN cp_ask.districts.sensors_fault IS 'Sensors reporting values outside the physical limits of their type.';
COMMENT ON COLUMN cp_ask.districts.sensors_stuck IS 'Sensors repeating the same value.';
COMMENT ON COLUMN cp_ask.districts.health_pct IS 'Share of the district''s sensors that are ok, 0 to 100.';
COMMENT ON COLUMN cp_ask.districts.verdict IS 'operational (97 percent ok or more), degraded (85 to 97), partial_outage (50 to 85), major_outage (below 50).';
COMMENT ON COLUMN cp_ask.districts.last_reading_at IS 'When the district''s newest reading was taken.';

COMMENT ON VIEW cp_ask.sensors IS 'One row per sensor: where it is (city, district), what it measures, how it reports, its judged status and the newest reading.';
COMMENT ON COLUMN cp_ask.sensors.sensor_id IS 'Sensor identifier.';
COMMENT ON COLUMN cp_ask.sensors.city IS 'City id of the sensor; joins to cp_ask.districts.city.';
COMMENT ON COLUMN cp_ask.sensors.district IS 'District id of the sensor; joins to cp_ask.districts.district.';
COMMENT ON COLUMN cp_ask.sensors.sensor_type IS 'What the sensor measures, for example temperature, air_quality_pm25, traffic_count or water_level.';
COMMENT ON COLUMN cp_ask.sensors.protocol IS 'How the sensor reports: http, otlp or an industrial protocol such as modbus_tcp.';
COMMENT ON COLUMN cp_ask.sensors.status IS 'ok, stale, offline, fault or stuck.';
COMMENT ON COLUMN cp_ask.sensors.status_reason IS 'Why the status engine judged the sensor not ok.';
COMMENT ON COLUMN cp_ask.sensors.status_since IS 'When the sensor entered its current status.';
COMMENT ON COLUMN cp_ask.sensors.last_reading_at IS 'Event time of the sensor''s newest reading.';
COMMENT ON COLUMN cp_ask.sensors.last_value IS 'Value of the newest reading, in the unit of the sensor type.';

COMMENT ON VIEW cp_ask.readings_5m IS 'Five-minute roll-ups of the readings per district and sensor type: how many, how many sensors reported, how many were out of range, and the minimum, average and maximum value. Aggregates only.';
COMMENT ON COLUMN cp_ask.readings_5m.city IS 'City id.';
COMMENT ON COLUMN cp_ask.readings_5m.district IS 'District id.';
COMMENT ON COLUMN cp_ask.readings_5m.sensor_type IS 'Sensor type measured.';
COMMENT ON COLUMN cp_ask.readings_5m.bucket_start IS 'Start of the five-minute bucket.';
COMMENT ON COLUMN cp_ask.readings_5m.readings IS 'Readings received in the bucket.';
COMMENT ON COLUMN cp_ask.readings_5m.sensors_reporting IS 'Distinct sensors that reported in the bucket.';
COMMENT ON COLUMN cp_ask.readings_5m.out_of_range IS 'Readings outside the physical limits of the type.';
COMMENT ON COLUMN cp_ask.readings_5m.avg_value IS 'Average value in the bucket.';
COMMENT ON COLUMN cp_ask.readings_5m.min_value IS 'Minimum value in the bucket.';
COMMENT ON COLUMN cp_ask.readings_5m.max_value IS 'Maximum value in the bucket.';

COMMENT ON VIEW cp_ask.district_health IS 'The health history of every district: one row per fingerprint window (every 15 minutes and at every verdict change) with the verdict, the health percentage and how many sensors were not ok.';
COMMENT ON COLUMN cp_ask.district_health.episode_id IS 'Identifier of the fingerprint window.';
COMMENT ON COLUMN cp_ask.district_health.city IS 'City id.';
COMMENT ON COLUMN cp_ask.district_health.district IS 'District id.';
COMMENT ON COLUMN cp_ask.district_health.window_end IS 'When the 15-minute window ended.';
COMMENT ON COLUMN cp_ask.district_health.verdict IS 'The district''s verdict at the end of the window.';
COMMENT ON COLUMN cp_ask.district_health.health_pct IS 'Share of the district''s sensors that were ok, 0 to 100.';
COMMENT ON COLUMN cp_ask.district_health.sensors IS 'Sensors in the district at the time.';
COMMENT ON COLUMN cp_ask.district_health.sensors_not_ok IS 'Sensors that were stale, offline, in fault or stuck: the sensors lost.';
COMMENT ON COLUMN cp_ask.district_health.trigger IS 'schedule (every 15 minutes), verdict_change or seed (demo history).';

COMMENT ON VIEW cp_ask.incidents IS 'Incidents: the health windows whose verdict was not operational, with the share of sensors stale, offline, in fault or stuck, how much of the window a burst was active, and the outcome once it is known (the verdict an hour later, when the stretch began and ended, how long it lasted).';
COMMENT ON COLUMN cp_ask.incidents.episode_id IS 'Identifier of the incident window.';
COMMENT ON COLUMN cp_ask.incidents.city IS 'City id.';
COMMENT ON COLUMN cp_ask.incidents.district IS 'District id.';
COMMENT ON COLUMN cp_ask.incidents.window_end IS 'When the incident window ended.';
COMMENT ON COLUMN cp_ask.incidents.verdict IS 'degraded, partial_outage or major_outage.';
COMMENT ON COLUMN cp_ask.incidents.health_pct IS 'Share of the district''s sensors that were ok.';
COMMENT ON COLUMN cp_ask.incidents.sensors IS 'Sensors in the district.';
COMMENT ON COLUMN cp_ask.incidents.sensors_not_ok IS 'Sensors lost: stale, offline, in fault or stuck.';
COMMENT ON COLUMN cp_ask.incidents.stale_pct IS 'Percent of sensors stale.';
COMMENT ON COLUMN cp_ask.incidents.offline_pct IS 'Percent of sensors offline.';
COMMENT ON COLUMN cp_ask.incidents.fault_pct IS 'Percent of sensors in fault.';
COMMENT ON COLUMN cp_ask.incidents.stuck_pct IS 'Percent of sensors stuck.';
COMMENT ON COLUMN cp_ask.incidents.burst_active_pct IS 'Percent of the window during which a burst aimed at this district, its city or everything was active.';
COMMENT ON COLUMN cp_ask.incidents.verdict_1h IS 'The district''s verdict one hour later, once known.';
COMMENT ON COLUMN cp_ask.incidents.episode_start IS 'When this run of bad verdicts began, once it has ended.';
COMMENT ON COLUMN cp_ask.incidents.ended_at IS 'When the district was operational again.';
COMMENT ON COLUMN cp_ask.incidents.duration_min IS 'How long the incident lasted, in minutes.';

COMMENT ON VIEW cp_ask.bursts IS 'Bursts: the faults injected through the burst button (flood, noise, dropout, stuck, late, corrupt, slow_db) with their target, intensity, start and end.';
COMMENT ON COLUMN cp_ask.bursts.burst_id IS 'Burst identifier.';
COMMENT ON COLUMN cp_ask.bursts.kind IS 'flood, noise, dropout, stuck, late, corrupt or slow_db.';
COMMENT ON COLUMN cp_ask.bursts.target IS 'all, city:<id>, district:<id> or type:<sensor type>.';
COMMENT ON COLUMN cp_ask.bursts.intensity IS 'Strength from 1 (light) to 5 (heavy).';
COMMENT ON COLUMN cp_ask.bursts.started_at IS 'When the burst began.';
COMMENT ON COLUMN cp_ask.bursts.ends_at IS 'When the burst ended or will end.';

COMMENT ON VIEW cp_ask.burst_impact IS 'What each burst did to each district: the most sensors not ok and the worst health seen from the start of the burst until 30 minutes after its end. Use it for questions such as which districts lost the most sensors during the last burst (order by bursts.started_at desc).';
COMMENT ON COLUMN cp_ask.burst_impact.burst_id IS 'Burst identifier; joins to cp_ask.bursts.burst_id.';
COMMENT ON COLUMN cp_ask.burst_impact.kind IS 'Burst kind.';
COMMENT ON COLUMN cp_ask.burst_impact.target IS 'Burst target.';
COMMENT ON COLUMN cp_ask.burst_impact.intensity IS 'Burst intensity 1 to 5.';
COMMENT ON COLUMN cp_ask.burst_impact.started_at IS 'When the burst began.';
COMMENT ON COLUMN cp_ask.burst_impact.ends_at IS 'When the burst ended.';
COMMENT ON COLUMN cp_ask.burst_impact.city IS 'City id of the affected district.';
COMMENT ON COLUMN cp_ask.burst_impact.district IS 'District id of the affected district.';
COMMENT ON COLUMN cp_ask.burst_impact.sensors IS 'Sensors in the district.';
COMMENT ON COLUMN cp_ask.burst_impact.peak_sensors_not_ok IS 'The most sensors lost (not ok) in any window of the burst: the sensors the burst cost the district.';
COMMENT ON COLUMN cp_ask.burst_impact.worst_health_pct IS 'The lowest health percentage seen during the burst.';
COMMENT ON COLUMN cp_ask.burst_impact.worst_verdict IS 'The district''s worst verdict during the burst.';

-- A short derived text per incident (built by citypulse.aidb_features from cp_ask.incidents, never from raw readings): the source of the
-- incident-similarity pipeline. Deleting an episode (city deletion, retention) removes its text.
CREATE TABLE IF NOT EXISTS cp_ai.incident_text (
    episode_id bigint PRIMARY KEY REFERENCES episode(id) ON DELETE CASCADE,
    narrative  text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Every question asked of /api/ask: when, who, a short hash and the length of the question (never its text), how it ended.
CREATE TABLE IF NOT EXISTS cp_ai.ask_log (
    id           bigserial PRIMARY KEY,
    asked_at     timestamptz NOT NULL DEFAULT now(),
    actor        text NOT NULL,
    question_hash text NOT NULL,
    question_chars integer NOT NULL,
    outcome      text NOT NULL,
    steps        integer NOT NULL DEFAULT 0,
    rows_returned integer NOT NULL DEFAULT 0,
    ms           integer NOT NULL DEFAULT 0,
    conversation_id text
);
"""

# (`SQL` has a literal `%` in LIKE 'type:%': it is executed with no parameters, and psycopg only parses `%` when parameters are given.)

# The schema hook creates the table with its foreign key; a schema rebuilt underneath it (tests drop and recreate `public`) can leave the table
# without the key, so make sure it is there.
_FK = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'cp_ai.incident_text'::regclass AND contype = 'f') THEN
        DELETE FROM cp_ai.incident_text WHERE episode_id NOT IN (SELECT id FROM episode);      -- orphans of a rebuilt schema (derived data)
        ALTER TABLE cp_ai.incident_text ADD FOREIGN KEY (episode_id) REFERENCES episode(id) ON DELETE CASCADE;
    END IF;
END $$;
"""


def refresh(conn: psycopg.Connection) -> str:
    """Create or refresh the objects from this code. Returns `refreshed`, `as found` (this role may not create or replace them, but they exist from
    an earlier privileged run: they are used as they are, and their seals say whether they were changed) or `missing`. Never raises: a role that may
    not create schemas leaves AIDB mode unavailable (it is off by default) and must not stop the app; that is logged."""
    try:
        with conn.transaction():
            conn.execute(SQL)
            conn.execute(_FK)
        return "refreshed"
    except psycopg.errors.InsufficientPrivilege as exc:
        have = present(conn)
        log.warning("AIDB views not %s (%s); %s", "refreshed" if have else "created", str(exc).splitlines()[0][:160],
                    "they exist from an earlier run and are used as they are" if have else
                    "AIDB mode stays unavailable until a role that may create schemas runs `citypulse migrate`")
        return "as found" if have else "missing"
    except psycopg.Error as exc:
        log.warning("AIDB views not created: %s", str(exc).splitlines()[0][:200])
    return "missing"


def apply(conn: psycopg.Connection) -> bool:
    """The after-migrate hook body. Idempotent. Returns whether the objects are in place (see `refresh`)."""
    return refresh(conn) != "missing"


def hook(conn: psycopg.Connection) -> None:
    """The `after_migrate` hook (see apgkit.migrate.run): never raises."""
    apply(conn)


def present(conn: psycopg.Connection) -> bool:
    try:
        row = conn.execute("SELECT to_regclass('cp_ask.districts') IS NOT NULL AND to_regclass('cp_ask.burst_impact') IS NOT NULL "
                           "AND to_regclass('cp_ai.incident_text') IS NOT NULL AND to_regclass('cp_ai.ask_log') IS NOT NULL AS ok").fetchone()
    except psycopg.errors.InsufficientPrivilege:          # a role with no USAGE on the schemas cannot see them: for it they are not there
        conn.rollback()
        return False
    return bool(row and row["ok"])


def view_definition(conn: psycopg.Connection, name: str) -> list[str] | None:
    """What the catalog says about one view right now (its SQL, owner and options), as the fields a seal covers. None if it does not exist."""
    schema, _, rel = name.partition(".")
    r = conn.execute("SELECT pg_get_viewdef(c.oid, true) AS body, pg_get_userbyid(c.relowner) AS owner, COALESCE(c.reloptions::text, '') AS options "
                     "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'v'",
                     (schema, rel)).fetchone()
    return None if r is None else ["cp_view", name, r["body"], r["owner"], r["options"]]
