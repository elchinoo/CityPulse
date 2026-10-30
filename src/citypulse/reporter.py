"""Optional: push CityPulse's own health to PagerLens, so PagerLens can open and investigate incidents.

Enabled by PAGERLENS_URL + PAGERLENS_TOKEN (an ingest-scope PagerLens token, from the environment or a file). Instead of a
hand-copied token, CityPulse can ask the ecosystem identity provider for one (client_credentials, scope pagerlens.ingest) when
CITYPULSE_OIDC_ISSUER and CITYPULSE_SERVICE_CLIENT_SECRET[_FILE] are set; a static token wins if both are given.
Two services are reported, mirroring cause and effect:
  citypulse-ingest   the gateway: up, ingest rate, reject rate, p95 batch latency, overloaded rate
  citypulse-fleet    the sensors: health, offline / stale / fault / stuck shares (depends on citypulse-ingest)
Cities: `citypulse-fleet` carries the metrics of the whole fleet; the degradation alert is raised per city (one city or
many, no difference), and only for cities that have a problem: service `citypulse-fleet-<city>` (depends on citypulse-ingest),
the same metrics, alert labels carry `city`. When a city's verdict worsens an alert opens (or joins) an incident; it repeats
every 10 minutes while it lasts. At most MAX_CITY_SERVICES (the least healthy) cities are tracked at once; a city that
recovers gets a last healthy metric point and is dropped.
An alternative that needs no code here: run `pagerlens-agent` against /healthz and /metrics.
"""
import asyncio
import sys
import time

import httpx
from apgkit.auth import TokenSource
from apgkit.config import read_secret
from apgkit.netguard import NetGuard, UnsafeURL

from . import __version__, metrics, status
from .config import get_settings
from .db import connect

INTERVAL = 15
REALERT_SECONDS = 600
SEVERITY = {"degraded": "sev3", "partial_outage": "sev2", "major_outage": "sev1"}
STATES = ["operational", "degraded", "partial_outage", "major_outage"]
INGEST, FLEET = "citypulse-ingest", "citypulse-fleet"
MAX_CITY_SERVICES = 20


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


def city_service(city: str) -> str:
    return f"{FLEET}-{city}"


def _snapshot(tracked: frozenset = frozenset()) -> dict:
    """The fleet summary, plus a summary per city that has a problem or was reported before."""
    with connect(retries=1) as conn:
        overall = status.summary(conn)
        info = status.problem_cities(conn, MAX_CITY_SERVICES)
        cities = {city: status.summary(conn, city) for city in [*info["problems"], *sorted(tracked - set(info["problems"]))]}
        return {"overall": overall, "cities": cities}


# PagerLens's address is operator configuration, but it is still resolved once, checked and pinned (no second DNS lookup between
# the check and the connection). A private or local PagerLens needs CITYPULSE_ALLOW_PRIVATE_URLS=true.
guard = NetGuard("CITYPULSE_ALLOW_PRIVATE_URLS")


class Reporter:
    def __init__(self, url: str, token: str | None = None, source: TokenSource | None = None, transport=None):
        self.source = None if token else source
        self.url = url
        self.pin = transport is None             # tests inject a transport and need no DNS
        self.client = httpx.AsyncClient(base_url=url, timeout=15, follow_redirects=False, transport=transport,
                                        headers={"Authorization": f"Bearer {token}"} if token else {})
        self.rates = Rates()
        self.registered = False
        self.city_state: dict[str, str] = {}      # tracked cities (have a problem or just recovered) -> last verdict
        self.city_alert: dict[str, float] = {}
        self.city_registered: set[str] = set()

    async def post(self, path: str, body: dict) -> None:
        headers = await asyncio.to_thread(self.source.headers) if self.source else None   # cached; refreshed shortly before it expires
        if self.pin:
            try:
                target = await asyncio.to_thread(guard.resolve, self.url)
            except UnsafeURL as exc:
                raise RuntimeError(f"PagerLens URL refused: {exc}") from exc
            url, pinned, ext = target.request_args(path)
            r = await self.client.post(url, json=body, headers={**(headers or {}), **pinned}, extensions=ext)
        else:
            r = await self.client.post(path, json=body, headers=headers)
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

    async def register_city(self, city: str) -> None:
        if city not in self.city_registered:
            await self.post("/api/ingest/services", {"name": city_service(city), "tier": "critical", "owner": "city-platform",
                                                     "description": f"The sensors of city {city} as seen by CityPulse",
                                                     "depends_on": [INGEST]})
            self.city_registered.add(city)

    async def alert(self, service: str, s: dict, state: str, last_state: str, last_alert: float, city: str) -> float:
        """Open (or re-raise) the degradation alert when the verdict worsened or 10 minutes passed. Returns the alert time."""
        if state not in SEVERITY:
            return last_alert
        if STATES.index(state) > STATES.index(last_state) or time.monotonic() - last_alert > REALERT_SECONDS:
            labels = {"state": state, "health_pct": round(100 * s["health"], 1)}
            labels["city"] = city
            where = f"CityPulse city {city}"
            await self.post("/api/ingest/alerts", {
                "service": service, "name": "SensorFleetDegraded", "severity": SEVERITY[state], "source": "citypulse",
                "summary": f"{where} is {state.replace('_', ' ')}: " + "; ".join(s["reasons"][:3]), "labels": labels})
            return time.monotonic()
        return last_alert

    async def report(self) -> None:
        if not self.registered:
            await self.register()
        rates = self.rates.sample()
        snap = await asyncio.to_thread(_snapshot, frozenset(self.city_state))
        s = snap["overall"]
        points = [{"name": "up", "value": 1.0}] + [{"name": k, "value": round(v, 3)} for k, v in rates.items()]
        await self.post("/api/ingest/metrics", {"service": INGEST, "points": points})
        if s["total"]:
            await self.post("/api/ingest/metrics", {"service": FLEET, "points": fleet_points(s)})
        for city, cs in snap["cities"].items():    # one service per city that has (or just had) a problem
            await self.register_city(city)
            await self.post("/api/ingest/metrics", {"service": city_service(city), "points": fleet_points(cs)})
            cstate = cs["state"]
            self.city_alert[city] = await self.alert(city_service(city), cs, cstate, self.city_state.get(city, "operational"),
                                                     self.city_alert.get(city, 0.0), city)
            if cstate == "operational" and cs["counts"]["ok"] == cs["total"]:
                self.city_state.pop(city, None)     # recovered and reported once: stop tracking
                self.city_alert.pop(city, None)
            else:
                self.city_state[city] = cstate

    async def run(self) -> None:
        while True:
            try:
                await self.report()
            except Exception as exc:  # noqa: BLE001 - PagerLens being down must never hurt the platform
                print(f"reporter: {str(exc).splitlines()[0][:160]}", file=sys.stderr, flush=True)
            await asyncio.sleep(INTERVAL)


def _service_source(cfg) -> TokenSource | None:
    """A token source for PagerLens from the identity provider, if CityPulse's service client is configured."""
    if not (cfg.oidc_issuer and read_secret("CITYPULSE_SERVICE_CLIENT_SECRET")):
        return None
    return TokenSource(cfg.oidc_issuer, cfg.service_client_id, lambda: read_secret("CITYPULSE_SERVICE_CLIENT_SECRET"),
                       scope="pagerlens.ingest", audience="pagerlens")


def maybe_start() -> Reporter | None:
    cfg = get_settings()
    source = None if cfg.pagerlens_token else _service_source(cfg)
    if not (cfg.pagerlens_url and (cfg.pagerlens_token or source)):
        return None
    if cfg.pagerlens_url.startswith("http://") and not any(h in cfg.pagerlens_url for h in ("//localhost", "//127.0.0.1")):
        print("reporter: refusing to send a token over plain http to a non-local PagerLens", file=sys.stderr)
        return None
    return Reporter(cfg.pagerlens_url, cfg.pagerlens_token or None, source)
