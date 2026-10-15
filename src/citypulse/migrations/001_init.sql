-- CityPulse schema: auth, sensors with their latest state, readings, status history and burst (chaos) controls.
CREATE TABLE app_setting (
    key   text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE api_token (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    token_hash    text NOT NULL UNIQUE,          -- sha256 of the token; the token itself is shown once
    prefix        text NOT NULL,
    scope         text NOT NULL CHECK (scope IN ('read', 'ingest', 'metrics', 'full')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);

CREATE TABLE web_session (
    token_hash    text PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL
);

-- One row per sensor: identity plus the latest reading and the judged status.
-- Low fillfactor and no index on the columns that change on every reading keep those updates HOT.
CREATE TABLE sensor (
    id            text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'),
    type          text NOT NULL,
    district      text NOT NULL CHECK (district ~ '^[a-z0-9][a-z0-9_-]{0,23}$'),
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_ts       timestamptz NOT NULL,          -- event time of the newest reading
    last_value    double precision NOT NULL,
    last_received timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'stale', 'offline', 'fault', 'stuck')),
    reason        text,
    status_since  timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 70);
CREATE INDEX sensor_district_type ON sensor (district, type);

CREATE TABLE reading (
    sensor_id  text NOT NULL,
    ts         timestamptz NOT NULL,
    value      double precision NOT NULL
);
CREATE INDEX reading_sensor_ts ON reading (sensor_id, ts DESC);
CREATE INDEX reading_ts_brin ON reading USING brin (ts);

-- Periodic roll-up of the status engine: scope is 'overall', 'district:<id>' or 'type:<key>'.
CREATE TABLE status_history (
    ts     timestamptz NOT NULL,
    scope  text NOT NULL,
    total  integer NOT NULL,
    ok     integer NOT NULL,
    PRIMARY KEY (scope, ts)
);

-- Burst controls. A row is active while until > now(); the simulator and the gateway both read it.
CREATE TABLE chaos (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('flood', 'noise', 'dropout', 'stuck', 'late', 'corrupt', 'slow_db')),
    target      text NOT NULL DEFAULT 'all' CHECK (target ~ '^(all|district:[a-z0-9_-]{1,24}|type:[a-z0-9_]{1,32})$'),
    intensity   smallint NOT NULL CHECK (intensity BETWEEN 1 AND 5),
    started_at  timestamptz NOT NULL DEFAULT now(),
    until       timestamptz NOT NULL
);
CREATE INDEX chaos_until ON chaos (until);
