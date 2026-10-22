"""Burst controls. A burst is a row in `chaos` that expires on its own; the simulator and the gateway read it."""
import random
import time
from dataclasses import dataclass

from .catalog import CITY_ID, DISTRICTS, TYPES

KINDS = {
    "flood": "Sensors report many times more often: stresses the ingest gateway.",
    "noise": "Measurement noise grows and some readings spike past physical limits: faulty values.",
    "dropout": "Sensors go silent: stale, then offline.",
    "stuck": "A share of sensors repeats the same value forever.",
    "late": "Readings arrive minutes late, as if a link were congested.",
    "corrupt": "A share of payload items is malformed: rejected by validation.",
    "slow_db": "The ingest gateway slows down as if the database were struggling (gateway-side).",
}
MAX_SECONDS = 1800
MAX_ACTIVE = 50


class ChaosError(ValueError):
    pass


@dataclass(frozen=True)
class Burst:
    kind: str
    target: str
    intensity: int

    def matches(self, sensor_type: str, district: str, city: str = "") -> bool:
        if self.target == "all":
            return True
        scope, _, key = self.target.partition(":")
        return ((scope == "district" and key == district) or (scope == "type" and key == sensor_type)
                or (scope == "city" and key == city))


def check_target(target: str) -> str:
    if target == "all":
        return target
    scope, _, key = target.partition(":")
    if (scope == "district" and key in DISTRICTS) or (scope == "type" and key in TYPES) or (scope == "city" and CITY_ID.match(key)):
        return target
    raise ChaosError("target must be 'all', 'district:<id>', 'type:<key>' or 'city:<id>' with a valid id")


def start(conn, kind: str, target: str = "all", intensity: int = 3, seconds: int = 120) -> int:
    if kind not in KINDS:
        raise ChaosError(f"unknown burst kind {kind!r}")
    target = check_target(target)
    if target.startswith("city:") and not conn.execute("SELECT 1 FROM sensor WHERE city = %s LIMIT 1", (target[5:],)).fetchone():
        raise ChaosError(f"no sensor reports for city {target[5:]!r}")
    if not 1 <= intensity <= 5:
        raise ChaosError("intensity must be 1 to 5")
    if not 5 <= seconds <= MAX_SECONDS:
        raise ChaosError(f"duration must be 5 to {MAX_SECONDS} seconds")
    active = conn.execute("SELECT count(*) AS n FROM chaos WHERE until > now()").fetchone()["n"]
    if active >= MAX_ACTIVE:
        raise ChaosError("too many active bursts; stop some first")
    row = conn.execute(
        "INSERT INTO chaos (kind, target, intensity, until) VALUES (%s,%s,%s, now() + make_interval(secs => %s)) RETURNING id",
        (kind, target, intensity, seconds)).fetchone()
    conn.commit()
    return row["id"]


def storm(conn, seconds: int = 180, rng: random.Random | None = None, city: str | None = None) -> list[int]:
    """The big red button: several faults at once, so the platform has a real bad day.
    With `city`, the faults (and the dark district) are confined to that city's sensors; slow_db stays gateway-wide."""
    rng = rng or random.Random()
    hit = rng.choice(list(DISTRICTS))
    scope = f"city:{city}" if city else "all"
    plan = [("flood", scope, 3), ("slow_db", "all", 3), ("corrupt", scope, 2), ("noise", scope, 2),
            ("dropout", scope if city else f"district:{hit}", 5), ("stuck", scope, 2), ("late", scope, 2)]
    return [start(conn, k, t, i, seconds) for k, t, i in plan]


def stop_all(conn) -> int:
    n = conn.execute("UPDATE chaos SET until = now() WHERE until > now()").rowcount
    conn.commit()
    return n


def active(conn, city: str | None = None) -> list[dict]:
    """Active bursts. With `city`, only those that can touch it: everything except bursts aimed at other cities."""
    return conn.execute(
        "SELECT id, kind, target, intensity, started_at, until, "
        "greatest(0, extract(epoch FROM until - now()))::int AS seconds_left "
        "FROM chaos WHERE until > now() AND (%s::text IS NULL OR target NOT LIKE 'city:%%' OR target = 'city:' || %s::text) "
        "ORDER BY id", (city, city)).fetchall()


def prune(conn) -> None:
    conn.execute("DELETE FROM chaos WHERE until < now() - interval '1 hour'")


class Cache:
    """Short-lived cache of the active bursts, so the hot paths do not query on every batch."""

    def __init__(self, loader, ttl: float = 2.0):
        self._loader, self._ttl = loader, ttl
        self._at, self._bursts = 0.0, []

    def get(self) -> list[Burst]:
        now = time.monotonic()
        if now - self._at > self._ttl:
            self._at = now
            try:
                self._bursts = [Burst(r["kind"], r["target"], r["intensity"]) for r in self._loader()]
            except Exception:  # noqa: BLE001 - keep the last known bursts if the database blips
                pass
        return self._bursts
