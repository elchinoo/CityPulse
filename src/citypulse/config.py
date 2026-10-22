import os
from dataclasses import dataclass

from apgkit.config import database_url, read_secret


@dataclass(frozen=True)
class Settings:
    database_url: str
    role: str                 # api | sim | all
    sim_sensors: int
    sim_city: str             # the city the built-in simulator's sensors belong to; '' = not set (the simulator then refuses to start)
    sim_speed: float          # 1.0 = real intervals; 5.0 = every sensor reports 5x as often
    ingest_url: str           # sim role: where to POST readings (api role is reached over HTTP)
    ingest_token: str
    retention_hours: float
    max_sensors: int
    ingest_concurrency: int
    pagerlens_url: str
    pagerlens_token: str
    oidc_issuer: str          # identity provider; with the service client below it lets CityPulse mint its PagerLens token
    service_client_id: str


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


def _city() -> str:
    """CITYPULSE_CITY has no default: '' when unset, SystemExit when set to something that is not a usable city id."""
    from .catalog import CITY_ID, LEGACY_CITY
    city = os.environ.get("CITYPULSE_CITY", "").strip()
    if city and not CITY_ID.match(city):
        raise SystemExit("CITYPULSE_CITY must match [a-z0-9][a-z0-9_-]{0,31}")
    if city == LEGACY_CITY:
        raise SystemExit(f"CITYPULSE_CITY={LEGACY_CITY} is the retired legacy city and is rejected by the gateway; choose a real city id")
    return city


def require_sim_city(cfg: "Settings") -> None:
    """The built-in simulator (role sim or all) must be told which city it simulates."""
    if not cfg.sim_city:
        raise SystemExit("the built-in simulator (CITYPULSE_ROLE=sim or all) needs an explicit CITYPULSE_CITY, "
                         "e.g. CITYPULSE_CITY=paris (a-z, 0-9, _ and -, up to 32 characters); "
                         "use CITYPULSE_ROLE=api to run without a simulator")


def get_settings() -> Settings:
    role = os.environ.get("CITYPULSE_ROLE", "all").lower()
    if role not in {"api", "sim", "all"}:
        raise SystemExit("CITYPULSE_ROLE must be api, sim or all")
    return Settings(
        database_url=database_url(),
        role=role,
        sim_sensors=_int("SIM_SENSORS", 3000, 10, 50_000),
        sim_city=_city(),
        sim_speed=_float("SIM_SPEED", 1.0, 1.0, 20.0),
        ingest_url=os.environ.get("CITYPULSE_INGEST_URL", "").rstrip("/"),
        ingest_token=read_secret("CITYPULSE_INGEST_TOKEN", "/etc/citypulse/ingest_token"),
        retention_hours=_float("RETENTION_HOURS", 6.0, 0.1, 168.0),
        max_sensors=_int("MAX_SENSORS", 50_000, 100, 1_000_000),
        ingest_concurrency=_int("INGEST_CONCURRENCY", 8, 1, 64),
        pagerlens_url=os.environ.get("PAGERLENS_URL", "").rstrip("/"),
        pagerlens_token=read_secret("PAGERLENS_TOKEN", "/etc/citypulse/pagerlens_token"),
        oidc_issuer=os.environ.get("CITYPULSE_OIDC_ISSUER", "").strip(),
        service_client_id=os.environ.get("CITYPULSE_SERVICE_CLIENT_ID", "citypulse-service"),
    )
