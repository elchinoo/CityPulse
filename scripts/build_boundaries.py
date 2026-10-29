#!/usr/bin/env python3
"""Download real district boundaries and a few context layers, simplify them and write the small GeoJSON files CityPulse ships.

    python3 scripts/build_boundaries.py [--cities brussels,vienna] [--raw-dir build/geodata-raw] [--refresh]

Needs network access and `shapely` >= 2.1 (GEOS >= 3.12, for `coverage_simplify`); the APP needs neither (the files are vendored in
`src/citypulse/geodata/`, read at start, no request at runtime). Raw downloads are cached in --raw-dir (not committed); delete it or
pass --refresh to fetch again. Afterwards run `python3 scripts/sync_geodata.py` in the CityPulseSimulator repo to copy the result.

What is written for each city (`src/citypulse/geodata/`):
    <city>.districts.geojson   one feature per district: `id` (the district id senders use: lowercase ASCII slug), `name`, optional `name_nl`
    <city>.context.geojson     base-map layers, property `kind`: water (polygon), waterway (line), park (polygon), road_major (motorway and
                               trunk), road_main (primary and secondary)
    cities.json                centre, attribution text and licence of each city (shown in the Map footer; ATTRIBUTION.md is the long form)

Sources and licences (also in src/citypulse/geodata/ATTRIBUTION.md):
    Brussels  communes: OpenStreetMap, boundary=administrative admin_level=8 inside the Brussels-Capital Region relation 54094 (ODbL 1.0)
    Vienna    districts: Stadt Wien, WFS ogdwien:BEZIRKSGRENZEOGD (data.wien.gv.at; the service states CC BY 3.0 AT)
    both      context layers: OpenStreetMap via the Overpass API (ODbL 1.0)
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import shapely
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import linemerge, polygonize, unary_union

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "citypulse" / "geodata"
UA = "CityPulse-boundary-builder/1.0 (https://github.com/elchinoo/CityPulse)"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]
WFS_VIENNA = ("https://data.wien.gv.at/daten/geo?service=WFS&request=GetFeature&version=1.1.0&typeName=ogdwien:BEZIRKSGRENZEOGD"
              "&srsName=EPSG:4326&outputFormat=json")
OSM = "© OpenStreetMap contributors (ODbL)"

# tolerances in degrees (0.0001 is about 11 m of latitude); district coordinates are rounded to 5 decimals (about 1 m)
DISTRICT_TOL, AREA_TOL, LINE_TOL = 0.0001, 0.0003, 0.0003
MIN_PARK_KM2, MIN_WATER_KM2 = 0.3, 0.05
MIN_MAIN_ROAD_DEG = 0.003          # drop main-road pieces shorter than this (about 330 m): they only add bytes
DECIMALS = 5                # districts
CTX_DECIMALS = 4            # base-map layers: about 11 m, a third of a pixel on the map

CITIES = {
    "brussels": {
        "name": "Brussels", "country": "BE", "centre": (50.8467, 4.3525), "region_rel": 54094, "districts": "osm-communes",
        "attribution": OSM, "licence": "ODbL 1.0 (https://opendatacommons.org/licenses/odbl/1-0/)",
        "districts_source": "OpenStreetMap, administrative boundaries (admin_level 8) of the Brussels-Capital Region, via the Overpass API",
    },
    "vienna": {
        "name": "Vienna", "country": "AT", "centre": (48.2082, 16.3738), "region_rel": 109166, "districts": "wien-wfs",
        "attribution": "Datenquelle: Stadt Wien – data.wien.gv.at (CC BY 3.0 AT); base map " + OSM,
        "licence": "Districts: CC BY 3.0 AT (https://creativecommons.org/licenses/by/3.0/at/deed.de), as stated by the WFS; base map: ODbL 1.0",
        "districts_source": "Stadt Wien, WFS ogdwien:BEZIRKSGRENZEOGD (https://data.wien.gv.at)",
    },
}


def slug(text: str) -> str:
    t = text.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", t.lower())).strip("-")


def http(url: str, data: bytes | None = None, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoints above
        return r.read()


def overpass(query: str, cache: Path, refresh: bool) -> dict:
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in OVERPASS:
        for _attempt in range(2):
            try:
                raw = http(url, body)
                data = json.loads(raw)
                if "elements" in data:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_bytes(raw)
                    print(f"  overpass {url}: {len(raw) / 1024:.0f} KB -> {cache.name}")
                    return data
            except Exception as exc:  # noqa: BLE001 - try the next endpoint
                last = exc
                time.sleep(3)
    raise SystemExit(f"Overpass failed on every endpoint: {last}")


# ---- OSM element -> shapely --------------------------------------------------------------------------------------------------
def _line(geometry) -> LineString | None:
    pts = [(p["lon"], p["lat"]) for p in geometry]
    return LineString(pts) if len(pts) >= 2 else None


def _ring_union(lines: list[LineString]):
    merged = unary_union(lines) if lines else None
    if merged is None:
        return None
    polys = list(polygonize(linemerge(merged) if merged.geom_type != "LineString" else [merged]))
    return unary_union(polys) if polys else None


def element_geometry(el: dict):
    """A polygon (or multipolygon) from an Overpass `out geom` way or relation; None when it does not close."""
    if el["type"] == "way":
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
        return Polygon(pts).buffer(0) if len(pts) >= 4 and pts[0] == pts[-1] else None
    outer, inner = [], []
    for m in el.get("members", []):
        if m["type"] == "way" and m.get("geometry"):
            ln = _line(m["geometry"])
            if ln is not None:
                (inner if m.get("role") == "inner" else outer).append(ln)
    shell = _ring_union(outer)
    if shell is None:
        return None
    holes = _ring_union(inner)
    return shell.difference(holes) if holes is not None else shell


def km2(geom) -> float:
    """Rough area in km2 (equirectangular around the geometry's own latitude)."""
    lat = geom.centroid.y
    k = math.cos(math.radians(lat))
    return geom.area * (111.32 * k) * 111.32


# ---- districts ---------------------------------------------------------------------------------------------------------------
def brussels_districts(raw: Path, refresh: bool) -> list[dict]:
    q = ('[out:json][timeout:90];area(3600054094)->.a;rel(area.a)["boundary"="administrative"]["admin_level"="8"];out geom qt;')
    data = overpass(q, raw / "brussels-communes.json", refresh)
    out = []
    for el in data["elements"]:
        t = el["tags"]
        name = t.get("name:fr") or t["name"]
        g = element_geometry(el)
        if g is None:
            raise SystemExit(f"commune {name} did not close")
        out.append({"id": slug(name), "name": name, "name_nl": t.get("name:nl"), "ref": t.get("ref:INS"), "geom": g})
    return out


def vienna_districts(raw: Path, refresh: bool) -> list[dict]:
    cache = raw / "vienna-bezirke.json"
    if not cache.exists() or refresh:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(http(WFS_VIENNA))
        print(f"  wfs: {cache.stat().st_size / 1024:.0f} KB -> {cache.name}")
    data = json.loads(cache.read_text())
    out = []
    for f in data["features"]:
        p = f["properties"]
        out.append({"id": slug(p["NAMEK"]), "name": p["NAMEK"], "ref": str(p["BEZNR"]), "geom": shape(f["geometry"])})
    out.sort(key=lambda d: int(d["ref"]))
    return out


def _clean(g):
    g = shapely.make_valid(g) if not g.is_valid else g
    if g.geom_type == "GeometryCollection":
        polys = [x for x in g.geoms if x.geom_type in ("Polygon", "MultiPolygon")]
        g = unary_union(polys)
    return g


def _round(g, decimals: int = DECIMALS):
    return shapely.set_precision(g, 10 ** -decimals)


def simplify_districts(items: list[dict]) -> None:
    geoms = [_clean(d["geom"]) for d in items]
    # coverage_simplify keeps shared edges identical on both sides, so neighbouring districts still tile without gaps
    simp = shapely.coverage_simplify(geoms, DISTRICT_TOL, simplify_boundary=True)
    for d, g in zip(items, simp):
        d["geom"] = _clean(_round(g))


# ---- context layers ----------------------------------------------------------------------------------------------------------
def context(city: str, region, rel: int, raw: Path, refresh: bool) -> list[dict]:
    area = f"area({3600000000 + rel})->.a;"
    feats: list[dict] = []

    def add(kind: str, geom, tol: float):
        if geom is None or geom.is_empty:
            return
        geom = geom.intersection(region)
        if geom.is_empty:
            return
        geom = _round(geom.simplify(tol, preserve_topology=True), CTX_DECIMALS)
        parts = [g for g in getattr(geom, "geoms", [geom]) if not g.is_empty]
        for g in parts:
            if kind == "road_main" and g.geom_type == "LineString" and g.length < MIN_MAIN_ROAD_DEG:
                continue
            if g.geom_type in ("Polygon", "LineString") and (g.geom_type == "LineString" or g.area > 0):
                feats.append({"type": "Feature", "properties": {"kind": kind}, "geometry": mapping(g)})

    roads = overpass(f'[out:json][timeout:120];{area}way(area.a)["highway"~"^(motorway|trunk|primary|secondary)$"];out geom qt;',
                     raw / f"{city}-roads.json", refresh)
    major, main = [], []
    for el in roads["elements"]:
        ln = _line(el.get("geometry", []))
        if ln is None:
            continue
        (major if el["tags"].get("highway") in ("motorway", "trunk") else main).append(ln)
    for kind, lines in (("road_major", major), ("road_main", main)):
        if lines:
            merged = linemerge(unary_union(lines))
            add(kind, merged, LINE_TOL)

    water = overpass(f'[out:json][timeout:120];{area}(way(area.a)["natural"="water"];rel(area.a)["natural"="water"];'
                     f'way(area.a)["waterway"~"^(river|canal)$"]["name"];);out geom qt;', raw / f"{city}-water.json", refresh)
    lines = []
    for el in water["elements"]:
        if el["type"] == "way" and "waterway" in el["tags"] and "natural" not in el["tags"]:
            ln = _line(el.get("geometry", []))
            if ln is not None:
                lines.append(ln)
            continue
        g = element_geometry(el)
        if g is not None and not g.is_empty and km2(g) >= MIN_WATER_KM2:
            add("water", g, AREA_TOL)
    if lines:
        add("waterway", linemerge(unary_union(lines)), LINE_TOL)

    parks = overpass(f'[out:json][timeout:120];{area}(way(area.a)["leisure"="park"];rel(area.a)["leisure"="park"];'
                     f'way(area.a)["landuse"="forest"];rel(area.a)["landuse"="forest"];way(area.a)["natural"="wood"];rel(area.a)["natural"="wood"];);'
                     f'out geom qt;', raw / f"{city}-parks.json", refresh)
    pg = []
    for el in parks["elements"]:
        g = element_geometry(el)
        if g is not None and not g.is_empty:
            pg.append(g)
    merged = unary_union(pg) if pg else None
    if merged is not None:
        for g in getattr(merged, "geoms", [merged]):
            if km2(g) >= MIN_PARK_KM2:
                add("park", g, 2 * AREA_TOL)
    return feats


def write(path: Path, obj: dict) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    return len(text) + 1


def build(city: str, raw: Path, refresh: bool) -> dict:
    spec = CITIES[city]
    print(f"{city}:")
    items = brussels_districts(raw, refresh) if spec["districts"] == "osm-communes" else vienna_districts(raw, refresh)
    ids = [d["id"] for d in items]
    assert len(set(ids)) == len(ids) and all(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,23}", i) for i in ids), ids
    region_full = unary_union([d["geom"] for d in items])
    simplify_districts(items)
    region = unary_union([d["geom"] for d in items]).buffer(0)
    feats = []
    for d in items:
        props = {"id": d["id"], "name": d["name"]}
        if d.get("name_nl") and d["name_nl"] != d["name"]:
            props["name_nl"] = d["name_nl"]
        feats.append({"type": "Feature", "properties": props, "geometry": mapping(d["geom"])})
    n1 = write(OUT / f"{city}.districts.geojson", {"type": "FeatureCollection", "features": feats})
    ctx = context(city, region_full.buffer(0), spec["region_rel"], raw, refresh)
    n2 = write(OUT / f"{city}.context.geojson", {"type": "FeatureCollection", "features": ctx})
    print(f"  {len(items)} districts: {n1 / 1024:.0f} KB; context {len(ctx)} features: {n2 / 1024:.0f} KB; total {(n1 + n2) / 1024:.0f} KB")
    for d in items:
        assert d["geom"].is_valid and not d["geom"].is_empty, d["id"]
    gaps = region.area - sum(d["geom"].area for d in items)
    print(f"  overlap/gap check: area union - sum = {gaps:.3e} (about 0 when the districts tile)")
    return {"name": spec["name"], "country": spec["country"], "centre": list(spec["centre"]), "attribution": spec["attribution"],
            "licence": spec["licence"], "districts_source": spec["districts_source"], "districts": len(items),
            "bbox": [round(v, DECIMALS) for v in region.bounds]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cities", default=",".join(CITIES))
    ap.add_argument("--raw-dir", default=str(ROOT / "build" / "geodata-raw"))
    ap.add_argument("--refresh", action="store_true", help="download again even when cached")
    a = ap.parse_args()
    meta = {}
    existing = OUT / "cities.json"
    if existing.exists():
        meta = json.loads(existing.read_text())
    for city in [c.strip() for c in a.cities.split(",") if c.strip()]:
        if city not in CITIES:
            sys.exit(f"unknown city {city}; choose from {', '.join(CITIES)}")
        meta[city] = build(city, Path(a.raw_dir), a.refresh)
    write(existing, dict(sorted(meta.items())))


if __name__ == "__main__":
    main()
