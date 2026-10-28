"""Delete a city and everything that belongs to it, in small transactions so a large city never locks the database.

Order matters for safety: burst targets first (stop faults aimed at the city), then readings, then the city's status
history, and the sensor rows last. A city that is half deleted (the process died) therefore still lists its remaining
sensors and the same call finishes the job. Each batch is its own transaction of at most CHUNK rows.
"""
from .db import connect

CHUNK = 5000
LOCK_ID = 727403          # advisory lock: one city deletion at a time (the first two are the engine's and the migrations')


class NoSuchCity(LookupError):
    pass


class Busy(RuntimeError):
    pass


def _drain(conn, sql: str, args: tuple, chunk: int) -> tuple[int, int]:
    """Run a `... LIMIT chunk` delete until it removes fewer rows than that, committing after every batch."""
    total = batches = 0
    while True:
        n = conn.execute(sql, (*args, chunk)).rowcount
        conn.commit()
        total += n
        batches += 1 if n else 0
        if n < chunk:
            return total, batches


def delete_city(city: str, chunk: int = CHUNK) -> dict:
    """Returns {"city", "deleted": {"readings", "sensors", "status_history", "chaos", "rollups", "rollups_1h", "episodes"}, "batches": n}. Raises NoSuchCity or Busy."""
    with connect(retries=1) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_ID,)).fetchone()["ok"]:
            raise Busy("another city deletion is running; try again when it has finished")
        try:
            if not conn.execute("SELECT 1 FROM sensor WHERE city = %s LIMIT 1", (city,)).fetchone():
                raise NoSuchCity(f"no sensor reports for city {city!r}")
            conn.commit()
            steps = [
                ("chaos", "DELETE FROM chaos WHERE ctid IN (SELECT ctid FROM chaos WHERE target = %s LIMIT %s)", (f"city:{city}",)),
                ("readings", "DELETE FROM reading WHERE ctid IN (SELECT r.ctid FROM reading r JOIN sensor s ON s.id = r.sensor_id "
                             "WHERE s.city = %s LIMIT %s)", (city,)),
                ("status_history", "DELETE FROM status_history WHERE ctid IN (SELECT ctid FROM status_history WHERE scope = %s LIMIT %s)",
                 (f"city:{city}",)),
                # rollup_5m is partitioned (a ctid is only unique inside one partition), so it is addressed by its primary key
                ("rollups", "DELETE FROM rollup_5m WHERE (city, district, type, bucket) IN "
                            "(SELECT city, district, type, bucket FROM rollup_5m WHERE city = %s LIMIT %s)", (city,)),
                ("rollups_1h", "DELETE FROM rollup_1h WHERE ctid IN (SELECT ctid FROM rollup_1h WHERE city = %s LIMIT %s)", (city,)),
                ("episodes", "DELETE FROM episode WHERE ctid IN (SELECT ctid FROM episode WHERE city = %s LIMIT %s)", (city,)),
                ("sensors", "DELETE FROM sensor WHERE ctid IN (SELECT ctid FROM sensor WHERE city = %s LIMIT %s)", (city,)),
            ]
            deleted, batches = {}, 0
            for name, sql, args in steps:
                deleted[name], n = _drain(conn, sql, args, chunk)
                batches += n
            return {"city": city, "deleted": deleted, "batches": batches}
        finally:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
            conn.commit()
