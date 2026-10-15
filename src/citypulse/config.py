import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    role: str                 # api | sim | all
    api_key: str
    allow_anonymous: bool
    sim_sensors: int
    sim_speed: float          # 1.0 = real intervals; 5.0 = every sensor reports 5x as often
    ingest_url: str           # sim role: where to POST readings (api role is reached over HTTP)
    ingest_token: str
    retention_hours: float
    max_sensors: int
    ingest_concurrency: int
    pagerlens_url: str
    pagerlens_token: str


def read_secret(env: str, default_file: str = "") -> str:
    """Secret from the environment variable, else the first line of the file named by <ENV>_FILE."""
    value = os.environ.get(env, "")
    if value:
        return value
    path = os.environ.get(f"{env}_FILE", default_file)
    if not path:
        return ""
    try:
        with open(path) as fh:
            return fh.readline().strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        print(f"WARNING: cannot read {env} file {path}: {exc.strerror}", file=sys.stderr)
        return ""


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return max(lo, min(hi, value))


def _float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return max(lo, min(hi, value))


def get_settings() -> Settings:
    role = os.environ.get("CITYPULSE_ROLE", "all").lower()
    if role not in {"api", "sim", "all"}:
        raise SystemExit("CITYPULSE_ROLE must be api, sim or all")
    return Settings(
        database_url=os.environ.get("DATABASE_URL")
        or os.environ.get("DB_EXTERNAL_DSN")
        or os.environ.get("PSQL_CONNECTION_STRING")
        or "",
        role=role,
        api_key=read_secret("CITYPULSE_API_KEY", "/etc/citypulse/api_key"),
        allow_anonymous=os.environ.get("CITYPULSE_ALLOW_ANONYMOUS", "").lower() in {"1", "true", "yes"},
        sim_sensors=_int("SIM_SENSORS", 3000, 10, 50_000),
        sim_speed=_float("SIM_SPEED", 1.0, 1.0, 20.0),
        ingest_url=os.environ.get("CITYPULSE_INGEST_URL", "").rstrip("/"),
        ingest_token=read_secret("CITYPULSE_INGEST_TOKEN", "/etc/citypulse/ingest_token"),
        retention_hours=_float("RETENTION_HOURS", 6.0, 0.1, 168.0),
        max_sensors=_int("MAX_SENSORS", 50_000, 100, 1_000_000),
        ingest_concurrency=_int("INGEST_CONCURRENCY", 8, 1, 64),
        pagerlens_url=os.environ.get("PAGERLENS_URL", "").rstrip("/"),
        pagerlens_token=read_secret("PAGERLENS_TOKEN", "/etc/citypulse/pagerlens_token"),
    )
