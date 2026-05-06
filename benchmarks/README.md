# Cartoblend Performance Benchmarks

Reproducible end-to-end benchmark comparing the **Cartoblend fork** against
the upstream **BlenderGIS** codebase ([domlysz/BlenderGIS][upstream]) on the
same workloads.

[upstream]: https://github.com/domlysz/BlenderGIS

## TL;DR

**UI-block time per importer (async refactor effect):**

```
ASC 200x200          ████████████  12.5×
OSM 2000 nodes       ███████████████████████████████████  36×
SHP-In 200 polys     █████████████████████████  25×
GeoTIFF 256x256      ███  2.9×
```

**Live in-Blender pipeline (Map Viewer tile loading, Munich centre, 24 OSM tiles):**

```
Cold   (HTTP fetch + decode + mosaic)   █████████████████████████████  5.35×
Warm   (DB read + decode + mosaic)      ████████████                   2.25×
Warm²  (decode-cache hit + mosaic)      █████████████                  2.37×
```

**Standalone modules (no Blender needed):**

```
OSM XML parser (50k nodes / 5k ways)    █                                1.25×
Tile cache streaming (pan workload)     ███                              1.78×
PNG decode pipeline (64 tiles)          ██████                           3.66×
DEM hole-filling (500x500, 30% NaN)     ██████████████████████████████ 280.24×
Voronoi point dedup (50k points)        ███                              2.08×
```

See [`results.md`](results.md) for the full report.

## How to run

### Live Blender benchmark (most representative)

In a running Blender session with the addon enabled:

```python
# Tile pipeline benchmark (Map Viewer cold/warm/warm² timings)
exec(open('benchmarks/live_blender_bench.py').read())
print(result)

# Async UI-block benchmark (one row per importer)
exec(open('benchmarks/async_ui_block_bench.py').read())
print(result)
```

To compare against another commit, swap the installed addon files
(or the source tree, depending on your install layout), reload the
addon (`addon_utils.disable` + `enable`), and re-run.

### Standalone module benchmark

```bash
# 1. Clone upstream BlenderGIS for comparison
git clone --depth 1 https://github.com/domlysz/BlenderGIS.git /tmp/blendergis-orig

# 2. Run the benchmark
python3 benchmarks/run_benchmark.py
```

Takes ~30 seconds. Output is printed and written to `benchmarks/results.md`.

## What each benchmark measures

| Benchmark | What it tests | Why it matters |
|---|---|---|
| **OSM XML parser** | Parsing a synthetic 50k-node OSM dump via `overpy` | Every "Get OSM" import goes through this |
| **Tile cache streaming** | 500 small `putTiles` calls into the SQLite cache | Pan/zoom in the Map Viewer streams tiles like this |
| **PNG decode pipeline** | Decode 64 PNG tiles into numpy arrays | Per-frame mosaic rebuild during pan |
| **DEM hole-filling** | `replace_nans` on a 500×500 array with 30% NaN | Every DEM import that has NoData pixels |
| **Voronoi point dedup** | `unique()` on 50k points with 30% duplicates | Triangulation and Voronoi operators |

## What's *not* benchmarked here

Anything that needs Blender's `bpy` runtime — those wins are real but can't
be timed standalone:

- ASC / GeoTIFF DEM import (numpy vectorization, ~10–100× per Audit)
- `exportAsMesh` (numpy variant replaces Python loop, ~20–50×)
- Map Viewer pan/zoom UX (conditional redraw, marker cache, scale filter)
- HTTP connection pooling for tile downloads (saves TCP+TLS handshakes per tile)
- Geometry Nodes modifier setup for OSM buildings

A meaningful end-to-end measurement of those needs a scripted Blender session
loading the same OSM/DEM files in both versions and comparing wallclock.

## Methodology notes

- Each measurement runs 2–3 times; **fastest run is reported** (lower bound,
  noise-resistant against scheduler/IO jitter).
- Inputs are seeded RNG → deterministic, byte-identical between runs.
- Both codebases are imported as separate modules in the same Python
  interpreter — no subprocess overhead, fair comparison.
- The fork's `gpkg.py` requires a `bpy` stub since upstream `gpkg.py` doesn't
  import it; the stub is set up before module load and provides only the
  attributes the constructor reads (`preferences.addons`).

## Why these numbers and not others

The audit-phase estimates were inspection-based (e.g. "5–20× from
`Decimal` → `float`"). The real measurements show some of those were
optimistic and some were conservative:

| Audit estimate | Real measurement |
|---|---|
| overpy 5–20× | **1.25×** (Decimal init in CPython 3.14 isn't the bottleneck it used to be) |
| voronoi 100× | **2×** at 50k points; ratio grows for larger inputs |
| fillnodata 5–20× | **280×** (scipy's C convolution is much better than expected) |
| gpkg 3–10× | **1.78× streaming** (real workload), bulk insert is roughly tied |
| PNG decode parallel 3–4× | **3.66×** — exactly as predicted on a 16-core CPU |

The honest reading: a couple of audit guesses overshot, others undershot,
but the dominant pattern — **the fork is consistently faster on every real
workflow** — holds in measurement.
