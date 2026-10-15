"""The sensor simulator: thousands of sensors across 12 districts, reporting through the real collection path.

Each sensor follows a daily curve with its own bias and noise. Bursts (see chaos.py) bend that behaviour: flood,
noise, dropout, stuck, late, corrupt. Delivery behaves like a real edge collector: batches of 100, a bounded retry
buffer, exponential backoff when the gateway answers "overloaded", and original timestamps kept on retry (so
delayed data arrives late instead of being rewritten).
"""
import asyncio
import json
import random
import sys
import time
from collections import deque
from datetime import datetime

import httpx

from . import chaos, gateway, metrics
from . import settings_store as store
from .catalog import TYPES, build_fleet, hash_str
from .config import get_settings
from .db import connect

BATCH = 100
MAX_SENDS_PER_TICK = 8
BUFFER_MAX = 100_000
HEARTBEAT_KEY = "sim_heartbeat"


class Retry(Exception):
    def __init__(self, after: float = 0.0):
        super().__init__("retry")
        self.after = after


class Fatal(Exception):
    """The sink refuses us for good (bad token): back off hard and say so."""


class DirectSink:
    """Same process as the gateway: call it directly (still validated, still back-pressured)."""

    async def send(self, readings: list[dict]) -> None:
        try:
            await asyncio.to_thread(gateway.accept, readings)
        except (gateway.Overloaded, gateway.Unavailable) as exc:
            raise Retry() from exc


class HttpSink:
    """Separate process: POST to the ingest API with an ingest-scope token, like any real collector."""

    def __init__(self, url: str, token: str):
        self.client = httpx.AsyncClient(base_url=url, timeout=20, follow_redirects=False,
                                        headers={"Authorization": f"Bearer {token}"})

    async def send(self, readings: list[dict]) -> None:
        try:
            r = await self.client.post("/api/readings", json={"readings": readings})
        except httpx.HTTPError as exc:
            raise Retry() from exc
        if r.status_code in (401, 403):
            raise Fatal(f"ingest refused the token (HTTP {r.status_code})")
        if r.status_code in (429, 502, 503, 504):
            try:
                after = float(r.headers.get("retry-after", "0") or 0)
            except ValueError:
                after = 0.0
            raise Retry(after)
        if r.status_code >= 300:
            raise Retry()  # unexpected: keep the data and try again later


class Sensor:
    __slots__ = ("id", "type", "district", "bias", "due", "state", "frozen", "member")

    def __init__(self, sid: str, kind: str, district: str, rng: random.Random):
        t = TYPES[kind]
        self.id, self.type, self.district = sid, kind, district
        self.bias = rng.gauss(0, t.sigma * 0.5) if t.sigma else 0.0
        self.due = time.time() + rng.uniform(0, t.interval)   # spread first reports over one interval
        self.state = rng.random() < 0.5                       # parking occupancy
        self.frozen: float | None = None
        self.member = hash_str(sid) % 100                     # stable position for "a share of sensors" effects


def _reflect(v: float, lo: float, hi: float) -> float:
    """Keep a value inside [lo, hi] by bouncing off the edges (clipping would repeat the edge value)."""
    if v < lo:
        v = lo + (lo - v)
    if v > hi:
        v = hi - (v - hi)
    return min(max(v, lo), hi)


class Simulator:
    def __init__(self, n: int, speed: float, sink, bursts, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.speed = speed
        self.sink = sink
        self.bursts = bursts            # callable -> list[chaos.Burst]
        self.sensors = [Sensor(*f, self.rng) for f in build_fleet(n)]
        self.buffer: deque[dict] = deque()
        self.backoff = 0.0
        self.resume_at = 0.0
        self.sent = 0
        self.dropped = 0
        self.fatal = ""
        metrics.SIM_SENSORS.set(len(self.sensors))

    # ---- one sensor, one moment ---------------------------------------------------------------
    def value(self, s: Sensor, now: float, noise_x: float, spike: float) -> float:
        t = TYPES[s.type]
        local = datetime.fromtimestamp(now)
        hour = local.hour + local.minute / 60
        if s.type == "traffic_light":  # 90 s cycle: red 45, green 40, amber 5 (0, 1, 2)
            phase = (now + s.member * 0.9) % 90
            return 0.0 if phase < 45 else 1.0 if phase < 85 else 2.0
        if s.type == "parking":
            target = _reflect(t.normal(hour), 0.05, 0.95)
            if self.rng.random() < 0.04:
                s.state = self.rng.random() < target
            return 1.0 if s.state else 0.0
        if spike and self.rng.random() < spike:  # a wild reading, past the sensor's physical limits
            if self.rng.random() < 0.7:
                return round(t.hi * (1.1 + self.rng.random() * 0.6) + 1, 3)
            return round(t.lo - 1 - self.rng.random() * 5, 3)
        v = t.normal(hour) + s.bias + self.rng.gauss(0, t.sigma * noise_x)
        return round(_reflect(v, t.lo, t.hi), 3)

    def read(self, s: Sensor, now: float, active: list) -> dict | None:
        """The payload item for this sensor now, or None if it stays silent. Moves its next due time."""
        t = TYPES[s.type]
        mine = [b for b in active if b.matches(s.type, s.district)]

        def level(kind: str) -> int:
            return max((b.intensity for b in mine if b.kind == kind), default=0)

        s.due = now + t.interval / self.speed / (1 + 2 * level("flood")) * self.rng.uniform(0.9, 1.1)
        if level("dropout"):
            return None
        noise, stuck, late, corrupt = level("noise"), level("stuck"), level("late"), level("corrupt")
        if stuck and t.stuckable and s.member < min(90, 15 * stuck):
            if s.frozen is None:
                s.frozen = self.value(s, now, 1, 0)
            v = s.frozen
        else:
            s.frozen = None
            v = self.value(s, now, 1 + 2 * noise, 0.04 * noise)
        item = {"sensor": s.id, "type": s.type, "district": s.district, "value": v,
                "ts": round(now - 90 * late, 3)}
        if corrupt and self.rng.random() < 0.08 * corrupt:
            item = self._corrupt(item)
        return item

    def _corrupt(self, item: dict) -> dict:
        kind = self.rng.choice(["type", "value", "nan", "sensor", "ts"])
        if kind == "type":
            item["type"] = "teleporter"
        elif kind == "value":
            item["value"] = "n/a"
        elif kind == "nan":
            item["value"] = None
        elif kind == "sensor":
            item["sensor"] = "!!"
        else:
            item["ts"] = "yesterday-ish"
        return item

    # ---- delivery -----------------------------------------------------------------------------
    def due_readings(self, now: float, active: list) -> list[dict]:
        out = []
        for s in self.sensors:
            if s.due <= now:
                item = self.read(s, now, active)
                if item is not None:
                    out.append(item)
        return out

    def hold(self, readings: list[dict]) -> None:
        self.buffer.extend(readings)
        over = len(self.buffer) - BUFFER_MAX
        if over > 0:
            for _ in range(over):
                self.buffer.popleft()
            self.dropped += over
            metrics.SIM_DROPPED.inc(over)
        metrics.SIM_BUFFER.set(len(self.buffer))

    async def deliver(self, now: float) -> None:
        """Send what is buffered, oldest first, a few batches per tick; back off if the sink pushes back."""
        if now < self.resume_at:
            return
        chunks = []
        while self.buffer and len(chunks) < MAX_SENDS_PER_TICK:
            chunks.append([self.buffer.popleft() for _ in range(min(BATCH, len(self.buffer)))])
        if not chunks:
            return
        results = await asyncio.gather(*(self.sink.send(c) for c in chunks), return_exceptions=True)
        failed, wait = [], 0.0
        for chunk, res in zip(chunks, results):
            if res is None:
                self.sent += len(chunk)
                metrics.SIM_SENT.inc(len(chunk))
            elif isinstance(res, Retry):
                failed.append(chunk)
                wait = max(wait, res.after)
            elif isinstance(res, Fatal):
                failed.append(chunk)
                self.fatal = str(res)
                wait = max(wait, 30.0)
            else:
                failed.append(chunk)
                print(f"sim: unexpected send error: {res!r}", file=sys.stderr, flush=True)
        for chunk in reversed(failed):          # put back at the front, keeping their original order
            self.buffer.extendleft(reversed(chunk))
        if failed:
            self.backoff = min(20.0, max(self.backoff * 2, 1.0))
            self.resume_at = now + max(self.backoff, wait) * self.rng.uniform(0.8, 1.2)
        else:
            self.backoff, self.fatal = 0.0, ""
        metrics.SIM_BUFFER.set(len(self.buffer))

    async def tick(self) -> None:
        now = time.time()
        active = await asyncio.to_thread(self.bursts)
        self.hold(self.due_readings(now, active))
        await self.deliver(now)

    def heartbeat(self) -> dict:
        return {"at": round(time.time()), "sensors": len(self.sensors), "sent": self.sent,
                "buffer": len(self.buffer), "dropped": self.dropped, "backoff": round(self.backoff, 1),
                "problem": self.fatal}

    async def run(self) -> None:
        last_beat = 0.0
        while True:
            started = time.monotonic()
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - the simulator must outlive any one failure
                print(f"sim: tick failed: {str(exc).splitlines()[0][:160]}", file=sys.stderr, flush=True)
            if time.monotonic() - last_beat > 5:
                last_beat = time.monotonic()
                await asyncio.to_thread(_write_heartbeat, self.heartbeat())
            await asyncio.sleep(max(0.05, 1.0 - (time.monotonic() - started)))


def _write_heartbeat(beat: dict) -> None:
    try:
        with connect(retries=1) as conn:
            store.put(conn, HEARTBEAT_KEY, json.dumps(beat))
            conn.commit()
    except Exception:  # noqa: BLE001 - heartbeat is informational
        pass


def heartbeat(conn) -> dict | None:
    raw = store.get(conn, HEARTBEAT_KEY)
    try:
        beat = json.loads(raw) if raw else None
    except ValueError:
        return None
    if beat:
        beat["age"] = max(0, round(time.time() - beat.get("at", 0)))
        beat["running"] = beat["age"] < 20
    return beat


def make(direct: bool) -> Simulator:
    cfg = get_settings()
    if direct:
        sink = DirectSink()
    else:
        if not cfg.ingest_url or not cfg.ingest_token:
            raise SystemExit("role=sim needs CITYPULSE_INGEST_URL and CITYPULSE_INGEST_TOKEN (an ingest-scope token)")
        sink = HttpSink(cfg.ingest_url, cfg.ingest_token)

    def load():
        with connect(retries=1) as conn:
            return chaos.active(conn)

    cache = chaos.Cache(lambda: load(), ttl=3.0)
    return Simulator(cfg.sim_sensors, cfg.sim_speed, sink, cache.get)
