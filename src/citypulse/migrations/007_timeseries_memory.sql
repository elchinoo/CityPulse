-- Time series on core Postgres (no extension): per-district roll-ups of `reading`, and the episode table behind "have we seen this before?".
--
-- `reading` itself stays a plain table with a BRIN index on ts and retention by batched DELETE (GUIDE 7.13, "Why reading is not partitioned").
-- The roll-ups are NEW tables, so they can be partitioned declaratively without migrating any data: rollup_5m by month (the app creates the
-- partitions it needs before it writes and drops whole months past retention), rollup_1h unpartitioned (a year of it is small).
-- Roll-ups are tables the app refreshes incrementally rather than materialized views because `reading` is pruned after RETENTION_HOURS:
-- REFRESH MATERIALIZED VIEW recomputes everything from the rows that are left and would forget the history; an upsert of the last few buckets keeps it.
CREATE TABLE rollup_5m (
    city          text NOT NULL,
    district      text NOT NULL,
    type          text NOT NULL,
    bucket        timestamptz NOT NULL,          -- start of the bucket: date_bin('5 minutes', ts, TIMESTAMPTZ '2000-01-01')
    readings      integer NOT NULL,
    sensors       integer NOT NULL,              -- distinct sensors that reported in the bucket
    out_of_range  integer NOT NULL,              -- readings outside the physical limits of the type
    avg_value     double precision,
    min_value     double precision,
    max_value     double precision,
    PRIMARY KEY (city, district, type, bucket)
) PARTITION BY RANGE (bucket);

CREATE TABLE rollup_1h (
    city          text NOT NULL,
    district      text NOT NULL,
    type          text NOT NULL,
    bucket        timestamptz NOT NULL,
    readings      integer NOT NULL,
    sensors       integer NOT NULL,
    out_of_range  integer NOT NULL,
    avg_value     double precision,
    min_value     double precision,
    max_value     double precision,
    PRIMARY KEY (city, district, type, bucket)
);

-- Episode fingerprints: one fixed-size feature vector per district and window (docs: GUIDE "The fingerprint"). The vector is stored as a
-- plain real[] so a fingerprint is never lost on a database without pgvector; when the extension is there, the startup hook
-- (citypulse.memory.ensure) adds a GENERATED vector(16) column from it and an HNSW cosine index, and backfills nothing by hand.
CREATE TABLE episode (
    id             bigserial PRIMARY KEY,
    city           text NOT NULL,
    district       text NOT NULL,
    window_end     timestamptz NOT NULL,
    window_secs    integer NOT NULL,
    trigger        text NOT NULL CHECK (trigger IN ('schedule', 'verdict_change', 'seed')),
    verdict        text NOT NULL CHECK (verdict IN ('operational', 'degraded', 'partial_outage', 'major_outage')),
    health         real NOT NULL,
    sensors        integer NOT NULL,
    features       real[] NOT NULL CHECK (cardinality(features) = 16),
    -- outcome labels, filled in later by citypulse.memory.label():
    verdict_1h     text,                         -- the district's verdict one hour later
    episode_start  timestamptz,                  -- for a non-operational verdict: when this run of bad verdicts began
    ended_at       timestamptz,                  -- ... and when the district was operational again
    duration_min   real,                         -- ... so how long it lasted
    UNIQUE (city, district, window_end)
);
CREATE INDEX episode_unlabelled ON episode (window_end) WHERE verdict_1h IS NULL;
