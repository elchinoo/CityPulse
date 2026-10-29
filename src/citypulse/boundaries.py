"""Real district boundaries and base-map layers, vendored as small GeoJSON files in `geodata/` (no network, no PostGIS needed to read).

`scripts/build_boundaries.py` downloads them (OpenStreetMap, Stadt Wien open data), simplifies them and writes:

    geodata/cities.json                 per city: name, centre, attribution, licence, sources
    geodata/<city>.districts.geojson    one feature per real district: properties `id` (the district id senders use), `name`
    geodata/<city>.context.geojson      water, waterways, parks and main roads for the base map (property `kind`)

`geo.ensure` stores the districts in PostGIS (table district_area, source 'boundary'); the Map draws the context layers from
`GET /api/geo/context` (served from these files, so it needs no database). The simulator (cpsim) carries the same files and places
every sensor inside the polygon of its district, so position and polygon agree. Attribution text is shown in the Map footer.
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path

DIR = Path(__file__).parent / "geodata"
KINDS = ("water", "waterway", "park", "road_major", "road_main")


@cache
def meta() -> dict[str, dict]:
    path = DIR / "cities.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def cities() -> list[str]:
    """Ids of the cities that have real district boundaries (their files exist)."""
    return sorted(c for c in meta() if (DIR / f"{c}.districts.geojson").exists())


def has_boundaries(city: str | None) -> bool:
    return bool(city) and city in cities()


@cache
def districts(city: str) -> tuple[dict, ...]:
    """The real districts of `city` as ({id, name, name_nl?, geometry}, ...) with GeoJSON geometry (Polygon or MultiPolygon)."""
    path = DIR / f"{city}.districts.geojson"
    if city not in meta() or not path.exists():
        return ()
    feats = json.loads(path.read_text(encoding="utf-8"))["features"]
    return tuple({**f["properties"], "geometry": f["geometry"]} for f in feats)


@cache
def names() -> dict[str, str]:
    """Display name of every real district id ('molenbeek-saint-jean' -> 'Molenbeek-Saint-Jean'); the first city wins a shared id."""
    out: dict[str, str] = {}
    for c in cities():
        for d in districts(c):
            out.setdefault(d["id"], d["name"])
    return out


@cache
def context(city: str) -> dict:
    """Base-map layers of `city` as a GeoJSON FeatureCollection, plus the attribution to show. Empty for a city without them."""
    path = DIR / f"{city}.context.geojson"
    m = meta().get(city)
    if m is None or not path.exists():
        return {"type": "FeatureCollection", "city": city, "features": [], "attribution": None, "licence": None}
    feats = [f for f in json.loads(path.read_text(encoding="utf-8"))["features"] if f["properties"].get("kind") in KINDS]
    return {"type": "FeatureCollection", "city": city, "features": feats, "attribution": m["attribution"], "licence": m["licence"],
            "name": m["name"], "bbox": m.get("bbox")}


def _ring(ring) -> str:
    return "(" + ", ".join(f"{x:.6f} {y:.6f}" for x, y in ring) + ")"


def wkt(geometry: dict) -> str:
    """WKT of a GeoJSON Polygon or MultiPolygon (what ST_GeomFromText takes)."""
    if geometry["type"] == "Polygon":
        return "POLYGON(" + ", ".join(_ring(r) for r in geometry["coordinates"]) + ")"
    if geometry["type"] == "MultiPolygon":
        return "MULTIPOLYGON(" + ", ".join("(" + ", ".join(_ring(r) for r in poly) + ")" for poly in geometry["coordinates"]) + ")"
    raise ValueError(f"not a polygon: {geometry['type']}")


def boundary_areas() -> list[tuple[str, str, str]]:
    """(city, district, WKT) for every real district of every bundled city."""
    return [(c, d["id"], wkt(d["geometry"])) for c in cities() for d in districts(c)]
