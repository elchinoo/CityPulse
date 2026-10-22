-- Multi-city: every sensor belongs to one city, fixed by its first reading. Existing sensors (and senders that never
-- send a city) land in the city 'default', so an upgraded deployment keeps working unchanged.
-- The constant default makes the ADD COLUMN a metadata-only change on PostgreSQL 11+ (no table rewrite).
ALTER TABLE sensor ADD COLUMN city text NOT NULL DEFAULT 'default' CHECK (city ~ '^[a-z0-9][a-z0-9_-]{0,31}$');
-- Serves city / city+district / city+district+type lookups and city prefix search (text_pattern_ops also supports =).
-- The columns are immutable after the first reading, so per-reading updates stay HOT.
CREATE INDEX sensor_city_district_type ON sensor (city text_pattern_ops, district, type);

-- Burst targets may now name a city: 'city:<id>'.
ALTER TABLE chaos DROP CONSTRAINT chaos_target_check;
ALTER TABLE chaos ADD CONSTRAINT chaos_target_check
    CHECK (target ~ '^(all|district:[a-z0-9_-]{1,24}|type:[a-z0-9_]{1,32}|city:[a-z0-9][a-z0-9_-]{0,31})$');
