"""Async-Sweep-Benchmark: misst execute()-Wallclock pro Importer/Exporter.

Vor dem Async-Refactor: execute() = total work time (UI blockiert).
Nach dem Async-Refactor: execute() << total time (Worker im Background).

Ergebnis: pro Operator wird die wallclock-time von bpy.ops.foo() gemessen.
Diese Zahl ist die UI-Freeze-Dauer — der User-spürbare Unterschied.

Funktioniert auf beiden Versionen — die initial-fork-Version blockiert
naturgemäß bis fertig, die aktuelle Version returnt sofort.
"""
import bpy, os, sys, tempfile, time, json, numpy as np

PKG = 'bl_ext.user_default.cartoblend'
sys.path.insert(0, '/home/michael/.config/blender/5.2/extensions/user_default/cartoblend/core/lib')

# --- Setup ---
from bl_ext.user_default.cartoblend.geoscene import GeoScene
g = GeoScene(bpy.context.scene)
if not g.hasCRS: g.crs = 'EPSG:4326'
if not g.hasOriginPrj: g.setOriginPrj(0, 0)


def make_asc(path, n=200):
    """Synthetic ASC with n×n grid."""
    with open(path, 'w') as f:
        f.write(f"ncols {n}\nnrows {n}\nxllcorner 0.0\nyllcorner 0.0\n"
                f"cellsize 0.0001\nNODATA_value -9999\n")
        for r in range(n):
            f.write(' '.join(str(r * c % 1000) for c in range(n)) + '\n')


def make_geojson(path, n_features=200):
    feats = []
    for i in range(n_features):
        x = (i % 20) * 0.0005
        y = (i // 20) * 0.0005
        feats.append({"type": "Feature", "properties": {"name": f"f{i}", "height": 5.0 + (i % 20)},
                      "geometry": {"type": "Polygon",
                                   "coordinates": [[[x, y], [x+0.0003, y], [x+0.0003, y+0.0003], [x, y+0.0003], [x, y]]]}})
    with open(path, 'w') as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)


def make_gpx(path, n_pts=2000):
    with open(path, 'w') as f:
        f.write('<?xml version="1.0"?>\n<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">\n')
        f.write('  <trk><name>BigTrack</name><trkseg>\n')
        for i in range(n_pts):
            lat = 48.137 + (i * 0.00002)
            lon = 11.575 + (i * 0.00003)
            f.write(f'    <trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>{500+i*0.1:.1f}</ele></trkpt>\n')
        f.write('  </trkseg></trk>\n</gpx>\n')


def make_osm(path, n_nodes=2000, n_ways=200):
    with open(path, 'w') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">\n')
        f.write('  <bounds minlat="48.13" minlon="11.57" maxlat="48.20" maxlon="11.65"/>\n')
        for i in range(1, n_nodes + 1):
            lat = 48.13 + (i % 100) * 0.0007
            lon = 11.57 + ((i // 100) % 80) * 0.0007
            f.write(f'  <node id="{i}" lat="{lat:.6f}" lon="{lon:.6f}"/>\n')
        for w in range(1, n_ways + 1):
            f.write(f'  <way id="{1000+w}">\n')
            for k in range(5):
                nid = ((w * 5 + k) % n_nodes) + 1
                f.write(f'    <nd ref="{nid}"/>\n')
            nid_close = ((w * 5) % n_nodes) + 1
            f.write(f'    <nd ref="{nid_close}"/>\n')
            f.write('    <tag k="building" v="yes"/>\n')
            f.write('    <tag k="height" v="10"/>\n')
            f.write('  </way>\n')
        f.write('</osm>\n')


def make_shp(path_no_ext, n_polys=200):
    sys.path.insert(0, '/home/michael/.config/blender/5.2/extensions/user_default/cartoblend/operators/lib/shapefile')
    try:
        from bl_ext.user_default.cartoblend.operators.lib.shapefile import shapefile
    except ImportError:
        import shapefile
    w = shapefile.Writer(path_no_ext, shapeType=shapefile.POLYGON)
    w.field('name', 'C')
    w.field('height', 'N', 8, 2)
    for i in range(n_polys):
        x = (i % 20) * 0.0005
        y = (i // 20) * 0.0005
        w.poly([[(x, y), (x+0.0003, y), (x+0.0003, y+0.0003), (x, y+0.0003), (x, y)]])
        w.record(f'p{i}', 5.0 + i % 20)
    w.close()


def make_geotiff(path):
    import imageio
    n = 256
    data = (np.indices((n, n)).sum(axis=0)).astype(np.uint8)
    imageio.imwrite(path, data)
    wld = os.path.splitext(path)[0] + '.wld'
    with open(wld, 'w') as f:
        f.write('0.0001\n0.0\n0.0\n-0.0001\n11.575\n48.138\n')


def cleanup_new_state(snap_objs, snap_meshes, snap_imgs, snap_curves):
    for o in [o for o in bpy.data.objects if o.name not in snap_objs]:
        try: bpy.data.objects.remove(o, do_unlink=True)
        except: pass
    for m in [m for m in bpy.data.meshes if m.name not in snap_meshes and m.users == 0]:
        try: bpy.data.meshes.remove(m)
        except: pass
    for i in [i for i in bpy.data.images if i.name not in snap_imgs and i.users == 0]:
        try: bpy.data.images.remove(i)
        except: pass
    for c in [c for c in bpy.data.curves if c.name not in snap_curves and c.users == 0]:
        try: bpy.data.curves.remove(c)
        except: pass
    for ng in list(bpy.data.node_groups):
        if 'building' in ng.name.lower() or 'OSM Building' in ng.name:
            try: bpy.data.node_groups.remove(ng)
            except: pass


def time_op(label, op_call):
    snap_objs = set(bpy.data.objects.keys())
    snap_meshes = set(bpy.data.meshes.keys())
    snap_imgs = set(bpy.data.images.keys())
    snap_curves = set(bpy.data.curves.keys())
    t0 = time.perf_counter()
    try:
        ret = op_call()
        ok = (ret == {'FINISHED'} or ret == 'FINISHED' or 'FINISHED' in str(ret))
    except Exception as e:
        ok = False
        ret = f'EXCEPTION: {type(e).__name__}: {e}'
    elapsed_ms = (time.perf_counter() - t0) * 1000
    cleanup_new_state(snap_objs, snap_meshes, snap_imgs, snap_curves)
    return {'op': label, 'execute_ms': round(elapsed_ms, 2), 'ok': ok, 'ret': str(ret)}


# === Run all ===
results = []
tmp = tempfile.mkdtemp(prefix='cb_bench_')

# 1) ASC
asc_path = os.path.join(tmp, 'test.asc')
make_asc(asc_path, n=200)
results.append(time_op('ASC 200x200',
    lambda: bpy.ops.importgis.asc_file(filepath=asc_path, importMode='MESH', step=1)))

# 2) GeoJSON
gj_path = os.path.join(tmp, 'test.geojson')
make_geojson(gj_path, n_features=200)
results.append(time_op('GeoJSON 200 polys',
    lambda: bpy.ops.importgis.geojson_file(filepath=gj_path)))

# 3) GPX
gpx_path = os.path.join(tmp, 'test.gpx')
make_gpx(gpx_path, n_pts=2000)
results.append(time_op('GPX 2000 pts',
    lambda: bpy.ops.importgis.gpx_file(filepath=gpx_path)))

# 4) OSM
osm_path = os.path.join(tmp, 'test.osm')
make_osm(osm_path, n_nodes=2000, n_ways=200)
results.append(time_op('OSM 2000 nodes / 200 ways',
    lambda: bpy.ops.importgis.osm_file(filepath=osm_path)))

# 5) SHP-Import
shp_no_ext = os.path.join(tmp, 'test_shp')
make_shp(shp_no_ext, n_polys=200)
results.append(time_op('SHP-In 200 polys',
    lambda: bpy.ops.importgis.shapefile(filepath=shp_no_ext + '.shp',
                                        shpCRS='EPSG:4326', separateObjects=False)))

# 6) GeoTIFF
tif_path = os.path.join(tmp, 'test.tif')
make_geotiff(tif_path)
results.append(time_op('GeoTIFF 256x256',
    lambda: bpy.ops.importgis.georaster(filepath=tif_path, importMode='PLANE', rastCRS='EPSG:4326')))

# 7) SHP-Export — first build a small mesh
import bmesh
me = bpy.data.meshes.new('export_bench')
bm = bmesh.new()
verts = [bm.verts.new((x, y, 0)) for x, y in [(0,0),(0.001,0),(0.001,0.001),(0,0.001)]]
bm.faces.new(verts)
bm.to_mesh(me); bm.free()
obj = bpy.data.objects.new('export_bench', me)
bpy.context.scene.collection.objects.link(obj)
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
shp_out = os.path.join(tmp, 'export.shp')
results.append(time_op('SHP-Out (1 poly)',
    lambda: bpy.ops.exportgis.shapefile(filepath=shp_out)))
# cleanup the export mesh
bpy.data.objects.remove(obj, do_unlink=True)
if me.users == 0: bpy.data.meshes.remove(me)

# Cleanup tmp
import shutil
try: shutil.rmtree(tmp)
except: pass

result = {'results': results}
print(result)
