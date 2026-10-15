"""Optional: push CityPulse's own health to PagerLens, so PagerLens can open and investigate incidents.

Enabled by PAGERLENS_URL + PAGERLENS_TOKEN (an ingest-scope PagerLens token, from the environment or a file).
Two services are reported, mirroring cause and effect:
  citypulse-ingest   the gateway: up, ingest rate, reject rate, p95 batch latency, overloaded rate
  citypulse-fleet    the sensors: health, offline / stale / fault / stuck shares (depends on citypulse-ingest)
When the fleet verdict worsens, an alert opens (or joins) an incident; it repeats every 10 minutes while it lasts.
An alternative that needs no code here: run `pagerlens-agent` against /healthz and /metrics.
"""
import asyncio
import sys
import time

import httpx

from . import __version__, metrics, status
from .config import get_settings
from .db import connect

INTERVAL = 15
REALERT_SECONDS = 600
SEVERITY = {"degraded": "sev3", "partial_outage": "sev2", "major_outage": "sev1"}
STATES = ["operational", "degraded", "partial_outage", "major_outage"]
INGEST, FLEET = "citypulse-ingest", "citypulse-fleet"


def _val(counter, **labels) -> float:
    return metrics.counter_value(counter, **labels)


def p95_ms() -> float:
    """Rough p95 of the batch-latency histogram (prometheus_client keeps per-bucket counts)."""
    h = metrics.BATCH_SECONDS
    counts = [b.get() for b in h._buckets]
    total = sum(counts)
    if not total:
        return 0.0
    seen = 0.0
    for bound, n in zip(h._upper_bounds, counts):
        seen += n
        if seen >= 0.95 * total:
            return 10_000.0 if bound == float("inf") else float(bound) * 1000
    return 0.0


class Rates:
    """Per-second rates from cumulative counters."""

    def __init__(self):
        self.prev: dict[str, float] = {}
        self.at = 0.0

    def sample(self) -> dict[str, float]:
        now = time.monotonic()
        cur = {"accepted": _val(metrics.READINGS, result="accepted"), "rejected": _val(metrics.READINGS, result="rejected"),
               "ok": _val(metrics.BATCHES, result="ok"), "overloaded": _val(metrics.BATCHES, result="overloaded"),
               "error": _val(metrics.BATCHES, result="error")}
        out: dict[str, float] = {}
        if self.prev and now > self.at:
            dt = now - self.at
            d = {k: max(0.0, cur[k] - self.prev[k]) for k in cur}
            readings = d["accepted"] + d["rejected"]
            batches = d["ok"] + d["overloaded"] + d["error"]
            out["ingest_rate"] = d["accepted"] / dt
            out["reject_pct"] = 100 * d["rejected"] / readings if readings else 0.0
            out["overloaded_pct"] = 100 * d["overloaded"] / batches if batches else 0.0
        out["latency_p95_ms"] = p95_ms()
        self.prev, self.at = cur, now
        return out


def fleet_points(s: dict) -> list[dict]:
    total = s["total"] or 1
    c = s["counts"]
    sized = [d["ok"] / d["total"] for d in s["districts"] if d["total"] >= 20]
    worst = round(100 * min(sized), 2) if sized else round(100 * s["health"], 2)
    return [
        {"name": "worst_district_pct", "value": worst},
        {"name": "fleet_health_pct", "value": round(100 * s["health"], 2)},
        {"name": "offline_pct", "value": round(100 * c["offline"] / total, 2)},
        {"name": "stale_pct", "value": round(100 * c["stale"] / total, 2)},
        {"name": "fault_pct", "value": round(100 * c["fault"] / total, 2)},
        {"name": "stuck_pct", "value": round(100 * c["stuck"] / total, 2)},
    ]


def _summary() -> dict:
    with connect(retries=1) as conn:
        return status.summary(conn)


class Reporter:
    def __init__(self, url: str, token: str):
        self.client = httpx.AsyncClient(base_url=url, timeout=15, follow_redirects=False,
                                        headers={"Authorization": f"Bearer {token}"})
        self.rates = Rates()
        self.registered = False
        self.last_state = "operational"
        self.last_alert = 0.0

    async def post(self, path: str, body: dict) -> None:
        r = await self.client.post(path, json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"PagerLens answered HTTP {r.status_code} on {path}")

    async def register(self) -> None:
        await self.post("/api/ingest/services", {"name": INGEST, "tier": "critical", "owner": "city-platform",
                                                 "description": "CityPulse ingest gateway: validates and stores sensor readings"})
        await self.post("/api/ingest/services", {"name": FLEET, "tier": "critical", "owner": "city-platform",
                                                 "description": "The city's sensors as seen by CityPulse",
                                                 "depends_on": [INGEST]})
        await self.post("/api/ingest/deploys", {"service": INGEST, "version": __version__, "summary": "CityPulse started"})
        self.registered = True

    async def report(self) -> None:
        if not self.registered:
            await self.register()
        rates = self.rates.sample()
        s = await asyncio.to_thread(_summary)
        points = [{"name": "up", "value": 1.0}] + [{"name": k, "value": round(v, 3)} for k, v in rates.items()]
        await self.post("/api/ingest/metrics", {"service": INGEST, "points": points})
        if s["total"]:
            await self.post("/api/ingest/metrics", {"service": FLEET, "points": fleet_points(s)})
        state = s["state"]
        if state in SEVERITY:
            worse = STATES.index(state) > STATES.index(self.last_state)
            if worse or time.monotonic() - self.last_alert > REALERT_SECONDS:
                self.last_alert = time.monotonic()
                await self.post("/api/ingest/alerts", {
                    "service": FLEET, "name": "SensorFleetDegraded", "severity": SEVERITY[state], "source": "citypulse",
                    "summary": f"CityPulse fleet is {state.replace('_', ' ')}: " + "; ".join(s["reasons"][:3]),
                    "labels": {"state": state, "health_pct": round(100 * s["health"], 1)}})
        self.last_state = state

    async def run(self) -> None:
        while True:
            try:
                await self.report()
            except Exception as exc:  # noqa: BLE001 - PagerLens being down must never hurt the platform
                print(f"reporter: {str(exc).splitlines()[0][:160]}", file=sys.stderr, flush=True)
            await asyncio.sleep(INTERVAL)


def maybe_start() -> Reporter | None:
    cfg = get_settings()
    if not (cfg.pagerlens_url and cfg.pagerlens_token):
        return None
    if cfg.pagerlens_url.startswith("http://") and not any(h in cfg.pagerlens_url for h in ("//localhost", "//127.0.0.1")):
        print("reporter: refusing to send a token over plain http to a non-local PagerLens", file=sys.stderr)
        return None
    return Reporter(cfg.pagerlens_url, cfg.pagerlens_token)
