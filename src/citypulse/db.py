"""Connections. `connect()` is for occasional work; the ingest hot path uses a small pool."""
import sys
import threading
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings


def connect(retries: int = 5, delay: float = 1.0) -> psycopg.Connection:
    url = get_settings().database_url
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
        except psycopg.OperationalError as exc:  # cold start / waking database
            last = exc
            if retries > 1:
                reason = str(exc).strip().splitlines()[0][:200]
                print(f"database not reachable (attempt {attempt + 1}/{retries}): {reason}", file=sys.stderr, flush=True)
            time.sleep(delay * (2**attempt))
    raise RuntimeError(f"database unavailable after {retries} attempts: {last}")


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
    """Lazily opened so the process starts even while the database is asleep."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=10, open=False,
                                   kwargs={"row_factory": dict_row, "connect_timeout": 10}, timeout=5)
            _pool.open(wait=False)
        return _pool


def reset_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
