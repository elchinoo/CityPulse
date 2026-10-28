"""Deterministic demo history for the pattern memory: `citypulse seed-history`.

A fresh install has no past, so "have we seen this before?" would answer nothing. This module writes several weeks of episode fingerprints
(one per district every 15 minutes, trigger `seed`) for built-in cities, then labels their outcomes with the same `memory.label()` the engine
uses. Nothing is random per run: every number is a pure function of (city, district, calendar day, window), through sha256 like the
geography, so two runs, two machines and two Python versions write the same history and a re-run adds nothing (ON CONFLICT DO NOTHING).

The model, kept small on purpose: each district has a fixed sensor count (200..299) and on about 30 % of the days one incident of one kind
(dropout, noise, stuck, flood or storm) with a severity and a length of 2..9 windows (30 to 135 minutes). An incident ramps up (first window half
strength), holds, and ramps down (last window half); it bends exactly the statistics the real fingerprint reads. Verdicts are then
whatever the district's ok share gives, through the same tiers as the live engine. It is demo data: realistic in shape, not a replay.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from . import memory
from .catalog import DISTRICTS
from .geo import CITIES

DEFAULT_CITIES = ("paris", "lyon")
DEFAULT_DAYS = 28
KINDS = ["dropout", "noise", "stuck", "flood", "storm"]
INCIDENT_DAY_SHARE = 0.30
STEP = timedelta(seconds=memory.WINDOW_SECS)
PER_DAY = 86400 // memory.WINDOW_SECS


def unit(*parts: object) -> float:
    """A stable number in [0, 1) from the parts (sha256, identical on every machine)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def sensors_in(city: str, district: str) -> int:
    return 200 + int(unit(city, district, "n") * 100)


def incident(city: str, district: str, day: int) -> dict | None:
    """The incident that starts on this calendar day (days since 1970-01-01 UTC), or None."""
    if unit(city, district, "inc", day) >= INCIDENT_DAY_SHARE:
        return None
    return {"start": int(unit(city, district, "start", day) * (PER_DAY - 12)),          # window index within the day; leaves room to end the same day
            "len": 2 + int(unit(city, district, "len", day) * 8),
            "kind": KINDS[int(unit(city, district, "kind", day) * len(KINDS))],
            "severity": 0.35 + 0.6 * unit(city, district, "sev", day)}


def strength(city: str, district: str, window_end: datetime) -> tuple[float, str | None]:
    """(how hard the incident bites this window, its kind): 0 outside an incident, severity in the middle, half of it at the first and last window."""
    index = int(window_end.timestamp()) // memory.WINDOW_SECS - 1            # the window ENDING at window_end is window index (end / 900) - 1
    day, slot = divmod(index, PER_DAY)
    inc = incident(city, district, day)
    if not inc or not (inc["start"] <= slot < inc["start"] + inc["len"]):
        return 0.0, None
    edge = slot in (inc["start"], inc["start"] + inc["len"] - 1)
    return inc["severity"] * (0.5 if edge else 1.0), inc["kind"]


def synth_stats(city: str, district: str, window_end: datetime) -> tuple[dict, float]:
    """(raw statistics, overload rate) of a district for the window ending at `window_end`: the inputs of `memory.to_vector`."""
    f, kind = strength(city, district, window_end)
    total = sensors_in(city, district)
    j = lambda tag: unit(city, district, tag, int(window_end.timestamp()))        # noqa: E731 - per-window jitter, in [0, 1)
    delta = 0.004 + 0.003 * j("d") + 0.0008 * f
    shares = {"stale": 0.004 * j("s"), "offline": 0.002 * j("o"), "fault": 0.003 * j("f"), "stuck": 0.002 * j("k")}
    rate = 1.0 + 0.04 * (j("r") - 0.5)
    silent = shares["stale"] + shares["offline"]
    delta_p95, overload = delta * 3.2, 0.0
    if kind in ("dropout", "storm"):
        w = 1.0 if kind == "dropout" else 0.55
        shares["offline"] += w * 0.55 * f
        shares["stale"] += w * 0.25 * f
        silent += w * 0.8 * f
        rate -= w * 0.75 * f
    if kind in ("noise", "storm"):
        w = 1.0 if kind == "noise" else 0.6
        shares["fault"] += w * 0.4 * f
        delta += w * 0.05 * f
        delta_p95 += w * 0.18 * f
    if kind in ("stuck", "storm"):
        w = 1.0 if kind == "stuck" else 0.5
        shares["stuck"] += w * 0.45 * f
        delta *= 1 - 0.7 * w * f
    if kind in ("flood", "storm"):
        w = 1.0 if kind == "flood" else 0.5
        rate += w * 1.2 * f
        overload = w * 0.5 * f
    bad = sum(shares.values())
    if bad > 0.97:
        shares = {k: v * 0.97 / bad for k, v in shares.items()}
    counts = {k: int(round(v * total)) for k, v in shares.items()}
    ok = total - sum(counts.values())
    stats = {"city": city, "district": district, "total": total, "ok": ok, **counts,
             "delta_mean": max(0.0, delta), "delta_p95": max(0.0, delta_p95), "burst_share": 1.0 if f > 0 else 0.0,
             "silent": min(total, int(round(silent * total))), "rate_ratio": max(0.0, rate)}
    return stats, overload


def window_ends(now: datetime, days: int) -> list[datetime]:
    """Every quarter-hour boundary from `days` ago up to (and not after) the last one before `now`, oldest first."""
    last = memory.floor_window(now)
    return [last - STEP * i for i in range(days * PER_DAY - 1, -1, -1)]


def seed(conn, cities=DEFAULT_CITIES, days: int = DEFAULT_DAYS, now: datetime | None = None, reset: bool = False) -> dict:
    """Write the demo history (trigger `seed`) for the built-in cities, label the outcomes, and report what happened."""
    cities = tuple(cities)
    unknown = [c for c in cities if c not in CITIES]
    if unknown:
        raise ValueError(f"no built-in geography for {', '.join(unknown)}; choose from {', '.join(sorted(CITIES))}")
    if not 1 <= days <= 120:
        raise ValueError("days must be between 1 and 120")
    now = now or datetime.now(UTC)
    removed = conn.execute("DELETE FROM episode WHERE trigger = 'seed'").rowcount if reset else 0
    ends = window_ends(now, days)
    inserted = 0
    for city in cities:
        batch: list[tuple] = []
        for district in DISTRICTS:
            for end in ends:
                stats, over = synth_stats(city, district, end)
                batch.append(memory.row_for(stats, end, "seed", over))
                if len(batch) >= 5000:
                    inserted += _flush(conn, batch)
                    batch = []
        inserted += _flush(conn, batch)
    labelled = memory.label(conn, since=f"{days + 2} days")
    conn.commit()
    ok, reason = memory.status(conn, ttl=0)
    return {"cities": list(cities), "days": days, "windows": len(ends), "districts": len(DISTRICTS), "removed": removed, "inserted": inserted,
            "labelled": labelled, "similarity_search": ok, "note": "" if ok else reason}


def _flush(conn, batch: list[tuple]) -> int:
    if not batch:
        return 0
    with conn.cursor() as cur:
        cur.executemany(memory.INSERT, batch)
        return max(0, cur.rowcount)           # psycopg 3: after executemany, the total over all rows
