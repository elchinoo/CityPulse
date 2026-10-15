"""Prometheus metrics. The shared names (http_requests_total, http_request_duration_seconds, db_up, app_info,
process_*) match TrustDesk and PagerLens, so one pagerlens-agent preset reads every app in the family."""
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.process_collector import ProcessCollector

from . import __version__

APP = "citypulse"
REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)
REQUESTS = Counter("http_requests_total", "HTTP requests handled", ["app", "method", "route", "status"], registry=REGISTRY)
LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["app", "route"], registry=REGISTRY,
                    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
INFO = Gauge("app_info", "Build information", ["app", "version"], registry=REGISTRY)
INFO.labels(APP, __version__).set(1)

READINGS = Counter("citypulse_readings_total", "Readings by outcome", ["result"], registry=REGISTRY)
BY_PROTOCOL = Counter("citypulse_readings_by_protocol_total", "Readings offered, by collection protocol", ["protocol"], registry=REGISTRY)
BATCHES = Counter("citypulse_ingest_batches_total", "Ingest batches by outcome", ["result"], registry=REGISTRY)
BATCH_SECONDS = Histogram("citypulse_ingest_batch_seconds", "Time to accept a batch", registry=REGISTRY,
                          buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
INFLIGHT = Gauge("citypulse_ingest_inflight", "Batches being written right now", registry=REGISTRY)
SIM_BUFFER = Gauge("citypulse_sim_buffer_readings", "Readings the simulator holds for retry", registry=REGISTRY)
SIM_SENT = Counter("citypulse_sim_sent_total", "Readings the simulator delivered", registry=REGISTRY)
SIM_DROPPED = Counter("citypulse_sim_dropped_total", "Readings the simulator dropped (buffer full)", registry=REGISTRY)
SIM_SENSORS = Gauge("citypulse_sim_sensors", "Sensors the simulator runs", registry=REGISTRY)

class FleetCollector:
    """Evaluated at scrape time from the database, so every replica reports the same fleet."""

    def collect(self):
        from . import status
        from .db import connect

        g = GaugeMetricFamily("citypulse_sensors", "Sensors by judged status", labels=["status"])
        r = GaugeMetricFamily("citypulse_fleet_health_ratio", "Share of sensors that are ok (0..1)")
        d = GaugeMetricFamily("citypulse_district_health_ratio", "Share of a district's sensors that are ok", labels=["district"])
        try:
            with connect(retries=1) as conn:
                s = status.summary(conn)
            for name, n in s["counts"].items():
                g.add_metric([name], n)
            if s["total"]:
                r.add_metric([], s["health"])
            for row in s["districts"]:
                if row["total"]:
                    d.add_metric([row["key"]], row["ok"] / row["total"])
        except Exception:  # noqa: BLE001 - the database being down is reported by db_up, not by failing the scrape
            pass
        yield g
        yield r
        yield d


class DatabaseCollector:
    def collect(self):
        from .db import connect

        up = 0
        try:
            with connect(retries=1) as conn:
                conn.execute("SELECT 1")
            up = 1
        except Exception:  # noqa: BLE001 - a scrape must never fail because the database is down; that is the signal
            pass
        g = GaugeMetricFamily("db_up", "1 if the database answers", labels=["app"])
        g.add_metric([APP], up)
        yield g


REGISTRY.register(FleetCollector())
REGISTRY.register(DatabaseCollector())


def observe(method: str, route: str, status: int, started: float) -> None:
    REQUESTS.labels(APP, method, route, str(status)).inc()
    LATENCY.labels(APP, route).observe(time.perf_counter() - started)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get() if labels else counter._value.get()
