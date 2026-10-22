"""The sensor catalogue: 27 types a smart city runs, with plausible behaviour and hard physical limits.

`lo`/`hi` are the hard limits: a reading outside them is a sensor fault, not weather. The simulator draws normal
values as `mean + amp * cos(2*pi*(hour - peak)/24) + noise`, clipped to the limits.
"""
import math
import re
from dataclasses import dataclass

DISTRICTS: dict[str, str] = {
    "d01": "Harbor", "d02": "Old Town", "d03": "Financial Quarter", "d04": "University Hill",
    "d05": "Riverside", "d06": "Airport Zone", "d07": "Northgate", "d08": "Tech Park",
    "d09": "Green Valley", "d10": "Eastside", "d11": "Market District", "d12": "Southfield",
}

SENSOR_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")
DISTRICT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
CITY_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Before cities were mandatory, readings without a city were stored under this id. It is no longer accepted anywhere:
# it only survives so that sensors of an upgraded database stay readable (and are flagged "legacy") until an admin deletes them.
LEGACY_CITY = "default"


@dataclass(frozen=True)
class SensorType:
    key: str
    label: str
    unit: str
    group: str
    lo: float
    hi: float
    mean: float
    amp: float          # diurnal swing
    peak: float         # hour of day of the maximum
    sigma: float        # measurement noise
    interval: int       # seconds between reports
    weight: int         # share of the fleet
    stuckable: bool = True    # False for discrete or naturally constant signals

    def normal(self, hour: float) -> float:
        return self.mean + self.amp * math.cos(2 * math.pi * (hour - self.peak) / 24)


# The seven the project started with, then twenty more.
_T = [
    # weather
    SensorType("rain", "Rain gauge", "mm/h", "weather", 0, 300, 0.6, 0.4, 15, 0.3, 30, 4),
    SensorType("light", "Ambient light", "lux", "weather", 0, 120000, 30000, 30000, 13, 800, 30, 4),
    SensorType("humidity", "Humidity", "%", "weather", 0, 100, 60, 15, 5, 1.5, 30, 6),
    SensorType("temperature", "Temperature", "°C", "weather", -50, 65, 18, 7, 15, 0.4, 30, 8),
    SensorType("wind_speed", "Wind speed", "m/s", "weather", 0, 80, 4, 2, 14, 1.2, 30, 3),
    SensorType("pressure", "Barometric pressure", "hPa", "weather", 870, 1085, 1013, 2, 10, 0.8, 60, 2),
    SensorType("uv_index", "UV index", "idx", "weather", 0, 16, 3.5, 3.5, 13, 0.3, 60, 2),
    # traffic and mobility
    SensorType("traffic_camera", "Traffic camera", "veh/min", "traffic", 0, 400, 35, 20, 17, 4, 15, 8),
    SensorType("traffic_light", "Traffic light", "state", "traffic", 0, 3, 1, 1, 0, 0, 10, 9, False),
    SensorType("radar", "Speed radar", "km/h", "traffic", 0, 250, 48, 8, 13, 5, 15, 5),
    SensorType("parking", "Parking bay", "occupied", "traffic", 0, 1, 0.5, 0.35, 14, 0, 30, 10, False),
    SensorType("pedestrian_counter", "Pedestrian counter", "people/min", "traffic", 0, 2000, 18, 14, 17, 3, 30, 4),
    SensorType("bike_share", "Bike-share dock", "bikes", "traffic", 0, 60, 14, 6, 8, 2, 60, 3),
    SensorType("ev_charger", "EV charger", "kW", "traffic", 0, 350, 22, 12, 19, 4, 30, 3),
    # environment
    SensorType("air_quality", "Air quality PM2.5", "µg/m³", "environment", 0, 1000, 14, 6, 8, 2, 60, 5),
    SensorType("co2", "CO₂", "ppm", "environment", 300, 5000, 430, 40, 8, 12, 60, 3),
    SensorType("noise", "Noise level", "dB", "environment", 20, 140, 58, 10, 18, 2.5, 30, 4),
    SensorType("soil_moisture", "Soil moisture", "%", "environment", 0, 100, 38, 6, 6, 1.2, 120, 2),
    # water and utilities
    SensorType("water_level", "Water level", "cm", "utilities", 0, 800, 120, 10, 6, 3, 60, 3),
    SensorType("water_flow", "Water flow", "L/min", "utilities", 0, 5000, 600, 220, 7, 30, 30, 3),
    SensorType("water_pressure", "Water pressure", "bar", "utilities", 0, 16, 4.2, 0.4, 7, 0.1, 30, 3),
    SensorType("smart_meter", "Smart power meter", "kW", "utilities", 0, 2000, 120, 70, 19, 8, 30, 6),
    SensorType("street_light", "Street light", "W", "utilities", 0, 400, 80, 80, 0, 3, 60, 6),
    SensorType("gas_leak", "Gas leak detector", "ppm", "utilities", 0, 10000, 2, 1, 12, 0.5, 30, 2, False),
    SensorType("waste_bin", "Waste bin fill", "%", "utilities", 0, 100, 45, 25, 20, 1.5, 120, 3),
    # structures and safety
    SensorType("bridge_vibration", "Bridge vibration", "mm/s", "structures", 0, 100, 1.2, 0.6, 17, 0.25, 15, 1),
    SensorType("seismic", "Seismic (PGA)", "mg", "structures", 0, 2000, 0.4, 0.1, 0, 0.15, 15, 1),
]

TYPES: dict[str, SensorType] = {t.key: t for t in _T}
assert len(TYPES) == 27

GROUPS = ["weather", "traffic", "environment", "utilities", "structures"]


def build_fleet(n: int, city: str) -> list[tuple[str, str, str]]:
    """Deterministic fleet of `n` sensors as (id, type, district), spread by weight and round-robin over districts.
    Sensor ids are global, so the city is part of the id (`<city>.<type>-<district>-<n>`, at most 64 characters)."""
    total = sum(t.weight for t in _T)
    counts = {t.key: max(1, round(n * t.weight / total)) for t in _T}
    districts = list(DISTRICTS)
    fleet: list[tuple[str, str, str]] = []
    for t in _T:
        for i in range(counts[t.key]):
            d = districts[(i + hash_str(t.key)) % len(districts)]
            sid = f"{t.key.replace('_', '-')}-{d}-{i:04d}"
            fleet.append((f"{city}.{sid}", t.key, d))
    return fleet


def hash_str(s: str) -> int:
    """Stable across processes (Python's hash() is salted)."""
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) % 1_000_003
    return h
