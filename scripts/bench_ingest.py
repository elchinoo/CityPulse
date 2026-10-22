"""Ingest throughput of the gateway against a scratch Postgres (not a capacity benchmark).

    DATABASE_URL=postgresql://.../scratch_db python scripts/bench_ingest.py [--cities N] [--readings N]

Drops and recreates the `public` schema of that database, so point it at a disposable one.
"""
import argparse
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("CITYPULSE_ROLE", "api")

from citypulse import db, gateway, migrate  # noqa: E402
from citypulse.db import connect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readings", type=int, default=200_000)
    ap.add_argument("--sensors", type=int, default=5000)
    ap.add_argument("--cities", type=int, default=1, help="how many cities the sensors are spread over (a city is required)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    rates = []
    for _ in range(a.runs):
        with connect() as c:
            c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            c.commit()
        migrate.run()
        db.reset_pool()
        gateway._known.update(count=0, at=0.0)
        batches = []
        for b in range(a.readings // 1000):
            items = []
            for i in range(1000):
                n = (b * 1000 + i) % a.sensors
                items.append({"sensor": f"temperature-c{n % a.cities}-{n:05d}", "type": "temperature", "district": f"d{n % 12:02d}",
                              "city": f"city{n % a.cities}", "value": 20.0 + (i % 7) / 10})
            batches.append(items)
        t = time.perf_counter()
        with ThreadPoolExecutor(a.threads) as ex:
            res = list(ex.map(gateway.accept, batches))
        dt = time.perf_counter() - t
        assert sum(r["accepted"] for r in res) == len(batches) * 1000, res[0]
        rates.append(a.readings / dt)
        print(f"run: {a.readings / dt:,.0f} readings/s", flush=True)
    print(f"median {statistics.median(rates):,.0f} readings/s (cities={a.cities}, sensors={a.sensors})")


if __name__ == "__main__":
    main()
