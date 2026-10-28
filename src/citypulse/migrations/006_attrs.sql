-- Device metadata as JSON: `sensor.attrs` (vendor, firmware, install date, calibration, tags ...), sent optionally with a reading.
-- Nullable ADD COLUMN without a default is a metadata-only change. The size cap is a second line of defence behind the gateway's
-- validation; the GIN index uses jsonb_path_ops (smaller and faster than the default operator class, and it serves `@>` and
-- `@?`/`@@` jsonpath containment, which is all the API uses). The per-reading upsert never writes `attrs`, so those updates stay HOT.
ALTER TABLE sensor ADD COLUMN attrs jsonb;
ALTER TABLE sensor ADD CONSTRAINT sensor_attrs_shape CHECK (attrs IS NULL OR (jsonb_typeof(attrs) = 'object' AND pg_column_size(attrs) <= 4096));
CREATE INDEX sensor_attrs_gin ON sensor USING gin (attrs jsonb_path_ops);
