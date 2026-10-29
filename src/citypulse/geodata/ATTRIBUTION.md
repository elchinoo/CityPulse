# Attribution and licences of the map data

The files in this folder are small, simplified copies of open data, vendored so the app needs no network at run time. They are rebuilt
by `scripts/build_boundaries.py` (see its docstring); coordinates are WGS 84, rounded, and the geometry is simplified (districts to
about 11 m, base-map layers to about 33 m), so they are fit for a map of a city and for sample data, not for surveying.

## Brussels (`brussels.*.geojson`)

* **Districts**: the 19 communes of the Brussels-Capital Region, from OpenStreetMap (`boundary=administrative`, `admin_level=8`, inside
  relation 54094), read through the Overpass API on 29 Oct 2026. Names are the French ones (`name:fr`), the Dutch ones are kept as `name_nl`.
* **Base map**: water, waterways, parks and forests (at least 0.3 km2), and motorways, trunk, primary and secondary roads, from
  OpenStreetMap through the Overpass API, clipped to the region.
* **Licence**: Open Database Licence (ODbL) 1.0, <https://opendatacommons.org/licenses/odbl/1-0/>.
* **Attribution to show**: © OpenStreetMap contributors, <https://www.openstreetmap.org/copyright>. The Map tab shows
  "© OpenStreetMap contributors (ODbL)" under the map.

## Vienna (`vienna.*.geojson`)

* **Districts**: the 23 Bezirke of Vienna, from the City of Vienna's open data service, WFS layer `ogdwien:BEZIRKSGRENZEOGD`
  (<https://data.wien.gv.at>), read on 29 Oct 2026.
* **Licence of the districts**: Creative Commons Attribution 3.0 Austria (CC BY 3.0 AT),
  <https://creativecommons.org/licenses/by/3.0/at/deed.de>; this is the licence the WFS service itself states in its capabilities document
  (its terms of use are at <https://data.wien.gv.at/nutzungsbedingungen>).
* **Attribution to show**: "Datenquelle: Stadt Wien – data.wien.gv.at". The data were simplified (geometry) for this app.
* **Base map**: as for Brussels (OpenStreetMap through the Overpass API, ODbL 1.0), clipped to the city. The Map tab shows
  "Datenquelle: Stadt Wien – data.wien.gv.at (CC BY 3.0 AT); base map © OpenStreetMap contributors (ODbL)".

## Derived database (ODbL)

The base-map files are a derived database of OpenStreetMap. Whoever redistributes them must keep this notice and offer them under the ODbL;
the rest of the repository stays under its own licence (Apache-2.0).

## Cities that are not here

The built-in demo cities (Paris, Lyon and the others in `geo.CITIES`) have generated outlines and no base map; a city a fleet reports
without any file here gets outlines derived from its sensors' positions. Neither uses third-party data.
