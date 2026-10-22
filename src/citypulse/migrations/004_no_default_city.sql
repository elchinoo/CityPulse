-- Cities are mandatory: a reading without one is rejected by the gateway, so nothing may invent a city any more.
-- This only removes the column DEFAULT, a catalogue change that touches no row and no data: every existing sensor keeps
-- the city it has, including the legacy city 'default' that the pre-004 gateway assigned to readings without one.
-- Those legacy sensors stay readable (the API and the dashboard flag them as "legacy, no city reported", and the status
-- engine never decommissions them on its own) until an admin deletes that city: DELETE /api/cities/default.
-- The CHECK constraint on the column is unchanged, so 'default' is still a storable value for those rows.
ALTER TABLE sensor ALTER COLUMN city DROP DEFAULT;
