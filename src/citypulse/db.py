"""Connections. `connect()` is for occasional work (the retrying implementation lives in agentpg-kit); the ingest hot path
uses a small pool that is opened lazily and without waiting, so the process starts even while the database is asleep."""
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings
from .core import db as _db


def connect(retries: int = 5, delay: float = 1.0) -> psycopg.Connection:
    return _db.connect(retries, delay)


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
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
