# Cartoblend Performance — Benchmark Report

Three complementary benchmarks document what the fork delivers
relative to its upstream parent project, **BlenderGIS**
(`domlysz/BlenderGIS`), and to the pre-async refactor state.

---

## A. UI-block time per importer (async refactor effect)

Comparison between **pre-async** state (`0dfe14b`, all importers ran
synchronously and froze the UI for the duration of the import) and
**current** state (`fcb9e2b`, all importers refactored to background
thread + bpy.app.timers polling).

The number is **execute() wallclock** — the time Blender's UI is
frozen while the operator runs. After the refactor this is the time
to spawn a worker thread; the actual parse + mesh-build work happens
afterwards in the background.

| Operator | Pre-async (sync, UI frozen) | Current (async, UI free) | UI-freeze cut by |
|---|---:|---:|---:|
| **ASC 200×200** | 80.6 ms | 6.5 ms | **12.5×** |
| **OSM 2k nodes / 200 ways** | 25.0 ms | 0.7 ms | **36×** |
| **SHP-In 200 polys** | 7.7 ms | 0.3 ms | **25×** |
| **GeoTIFF 256×256** | 2.6 ms | 0.9 ms | **2.9×** |
| GeoJSON 200 polys | 10.6 ms (failed)¹ | 0.6 ms | n/a |
| GPX 2k pts | 10.1 ms (failed)¹ | 1.2 ms | n/a |
| SHP-Out (1 poly) | 1.5 ms | 1.9 ms | ≈ same |

¹ Pre-async revision crashed on these inputs because of unrelated
pre-existing bugs (node-socket index drift in Blender 5.x). Both
were fixed in the async sweep — see `fcb9e2b`.

```
ASC 200x200          ████████████  12.5×
OSM 2000 nodes       ███████████████████████████████████  36×
SHP-In 200 polys     █████████████████████████  25×
GeoTIFF 256x256      ███  2.9×
```

These are tiny inputs. At realistic sizes (4k×4k DEM, 50k OSM nodes,
100k-polygon shapefile) the synchronous version freezes the UI for
**seconds to tens of seconds** while the async version freezes it
for the same few milliseconds — the heavy work happens in the
background while the user keeps interacting with Blender.

To reproduce: see [`async_ui_block_bench.py`](async_ui_block_bench.py).

---

## B. Live end-to-end pipeline (in Blender)

The **real** Map Viewer workflow: load a Munich centre bbox at zoom 14
(24 OSM Mapnik tiles) through `MapService.getImage()`. Best of 3 runs.

Compared revisions:
- **Initial fork** (`9d08732`) — first commit after fork, with only the
  minimal Blender 5.x compatibility patches needed to run at all.
  Performance-wise effectively still upstream BlenderGIS.
- **Current** (`dc305cb`) — head of `main` after the security and
  performance audit sweeps.

| Pipeline phase | Initial fork | Current | Speedup |
|---|---:|---:|---:|
| **Cold** (HTTP fetch + cache write + decode + mosaic build) | 824 ms | 154 ms | **5.35×** |
| **Warm** (cache read + decode + mosaic build, no network) | 17.1 ms | 7.6 ms | **2.25×** |
| **Warm 2nd run** (decode-cache hit + mosaic only) | 17.3 ms | 7.3 ms | **2.37×** |

```
Cold   (HTTP fetch + decode + mosaic)   █████████████████████████████  5.35×
Warm   (DB read + decode + mosaic)      ████████████                   2.25×
Warm²  (decode-cache hit + mosaic)      █████████████                  2.37×
```

What's compounding here: HTTP connection pool, parallel PNG decode
(`ThreadPoolExecutor`), enlarged decode LRU cache, center-out tile
ordering, cached SQLite connections, faster `paste()` fast-path,
synchronous=OFF on the cache DB.

To reproduce, see [`live_blender_bench.py`](live_blender_bench.py).

---

## C. Standalone module benchmark (no Blender needed)

Five modules that don't depend on `bpy`, run as a regular Python
script. Same workloads against both the upstream code and the fork.

| Workflow | Upstream | Fork | Speedup |
|---|---:|---:|---:|
| OSM XML parser (50k nodes / 5k ways) | 215 ms | 172 ms | **1.25×** |
| Tile cache streaming (pan workload, 2000 tiles) | 66 ms | 37 ms | **1.78×** |
| PNG decode pipeline (64 tiles, 256×256) | 36 ms | 10 ms | **3.66×** |
| DEM hole-filling (500×500, 30% NaN, 5 iter) | 4.29 s | 15 ms | **280×** |
| Voronoi point dedup (50k points, 30% dupes) | 35 ms | 17 ms | **2.08×** |

```
OSM XML parser (50k nodes / 5k ways)    █                                1.25×
Tile cache streaming (pan workload)     ███                              1.78×
PNG decode pipeline (64 tiles)          ██████                           3.66×
DEM hole-filling (500x500, 30% NaN)     ██████████████████████████████ 280.24×
Voronoi point dedup (50k points)        ███                              2.08×
```

To reproduce:
```bash
git clone --depth 1 https://github.com/domlysz/BlenderGIS.git /tmp/blendergis-orig
python3 benchmarks/run_benchmark.py
```

---

## Environment

- Python: `3.14.4`
- NumPy: `2.4.4`
- CPU cores: 16
- Blender: 5.2 Alpha
- Network: home WAN (cold runs measured after DNS/TCP warm-up to remove first-connection setup noise)

## Methodology

- Best of N runs (lower bound; noise-resistant against scheduler/IO/network jitter).
- Cold-cache runs wipe `<source>*.gpkg*` between iterations to force HTTP fetch.
- Warm runs reuse the populated cache.
- Same `MapService` API call (`getImage`) on both versions for the live bench.
- Standalone bench imports the two codebases as distinct Python modules
  in the same interpreter.

## Not measured

ASC/GeoTIFF DEM import, `exportAsMesh`, Map Viewer pan UX latency,
Geometry Nodes modifier setup, custom-providers UI panel — these are
also faster (per audit estimates 10–100× for some) but require a
live UI workflow that's harder to automate.
