# CartoBlend

GIS toolkit for Blender — basemaps, OSM, DEM, GPX and more.

**Requires Blender 4.2 or newer.**

> Fork of [BlenderGIS](https://github.com/domlysz/BlenderGIS) by domlysz, updated for the Blender extension system and modern map tile providers.

---

## Features

**Interactive basemap viewer** — display live web maps directly in the 3D viewport (MapTiler, Mapbox, Thunderforest, Stadia, OpenStreetMap and more). Pan, zoom, search places, then export the visible area as a mesh.

**Elevation data** — fetch real terrain elevation from [OpenTopography](https://opentopography.org) (API key required, free registration).

**OpenStreetMap** — import buildings, roads, and other map features as 3D geometry.

**GIS file import:**
- Shapefile (`.shp`)
- Georeferenced raster / GeoTIFF
- OpenStreetMap (`.osm`)
- GeoJSON (`.geojson`)
- GPX track (`.gpx`)
- ESRI ASCII Grid (`.asc`)

**Export:** Shapefile (`.shp`)

**Mesh tools:** Delaunay triangulation, Voronoi diagram, Drop to Ground, longitude/latitude to sphere, Earth curvature correction.

**Camera tools:** Georeferenced render setup, geotagged photo camera setup (EXIF).

**Terrain analysis:** Generate shader node setups for slope, aspect, and reclassification.

---

## Installation

Download the latest release ZIP from the [Releases](https://github.com/oblivion8282-1337/cartoblend/releases) page.

In Blender: *Edit → Preferences → Extensions → Install from Disk*, then select the ZIP. The extension will appear under *User Default* extensions. Enable it and restart if prompted.

The release ZIP includes bundled wheels for **PyProj** and **Pillow** — no manual dependency installation needed. GDAL is optional; the addon falls back to its built-in raster handling when it is not present.

---

## API Keys

Some basemap providers require an API key. Enter them under *Edit → Preferences → Add-ons → CartoBlend*.

| Provider | Where to get a key |
|---|---|
| MapTiler | [maptiler.com](https://maptiler.com) |
| Mapbox | [mapbox.com](https://mapbox.com) |
| Thunderforest | [thunderforest.com](https://thunderforest.com) |
| Stadia Maps | [stadiamaps.com](https://stadiamaps.com) |
| OpenTopography | [opentopography.org](https://opentopography.org) |

---

## Map Viewer Shortcuts

| Key | Action |
|---|---|
| Scroll / `+` / `-` | Map zoom |
| `Ctrl` + Scroll | View zoom (no tile reload) |
| `Alt` + Scroll | Zoom ×10 |
| LMB / MMB drag | Pan map |
| `Ctrl` + Drag | Pan view only |
| Numpad 2/4/6/8 | Pan direction |
| `B` | Zoom box |
| `G` | Go to (search place) |
| `E` | Export as mesh |
| `Space` | Switch layer/source |
| `ESC` | Exit |

---

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
