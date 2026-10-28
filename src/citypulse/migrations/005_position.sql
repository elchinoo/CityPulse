-- Optional sensor position, in decimal degrees (WGS 84). Plain columns, so a position is stored even on a database without PostGIS;
-- the geography column, its indexes and the district polygons are created at startup by citypulse.geo.ensure() when the PostGIS
-- extension can be installed (see GUIDE 7.12). Both columns are NULL or both are set. Nullable adds are metadata-only changes.
ALTER TABLE sensor ADD COLUMN lat double precision CHECK (lat BETWEEN -90 AND 90);
ALTER TABLE sensor ADD COLUMN lon double precision CHECK (lon BETWEEN -180 AND 180);
ALTER TABLE sensor ADD CONSTRAINT sensor_position_together CHECK ((lat IS NULL) = (lon IS NULL));
