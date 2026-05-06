import os
import json
import logging
import threading
log = logging.getLogger(__name__)

import bpy
import bmesh
from bpy.types import Operator
from bpy.props import StringProperty, BoolProperty, FloatProperty

from ..geoscene import GeoScene
from ..core.proj import Reproj, reprojPt, utm
from .utils import adjust3Dview, getBBOX

from .io_import_osm import _apply_building_geonodes

PKG = __package__.rsplit('.', maxsplit=1)[0]  # bl_ext.user_default.cartoblend

# Default building height when no property is available
DEFAULT_BUILDING_HEIGHT = 15.0
# Meters per building level
LEVEL_HEIGHT = 3.0


# ---------------------------------------------------------------------------
# Module-level state for background GeoJSON parsing (async pattern)
# ---------------------------------------------------------------------------

_geojson_state_lock = threading.Lock()
_geojson_thread = None
# Result dict set by worker thread, consumed by polling timer in main thread.
# Keys on success: 'ok' True, 'parsed' dict with all data needed for Phase C.
# Keys on error:   'ok' False, 'error' str.
_geojson_result = None
# Operator options captured before the thread starts (bpy is not thread-safe).
_geojson_context_args = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _joinBmesh(src_bm, dest_bm):
	"""Join src_bm into dest_bm using direct bmesh vertex/face/edge copying."""
	vert_map = {}
	for v in src_bm.verts:
		new_v = dest_bm.verts.new(v.co)
		vert_map[v.index] = new_v
	dest_bm.verts.ensure_lookup_table()
	# Build face layer mapping (float and int layers)
	face_float_map = []
	for src_layer in src_bm.faces.layers.float:
		dst_layer = dest_bm.faces.layers.float.get(src_layer.name)
		if dst_layer is not None:
			face_float_map.append((src_layer, dst_layer))
	face_int_map = []
	for src_layer in src_bm.faces.layers.int:
		dst_layer = dest_bm.faces.layers.int.get(src_layer.name)
		if dst_layer is not None:
			face_int_map.append((src_layer, dst_layer))
	for f in src_bm.faces:
		try:
			new_face = dest_bm.faces.new([vert_map[v.index] for v in f.verts])
			new_face.material_index = f.material_index
			for src_layer, dst_layer in face_float_map:
				new_face[dst_layer] = f[src_layer]
			for src_layer, dst_layer in face_int_map:
				new_face[dst_layer] = f[src_layer]
		except ValueError:
			pass
	for e in src_bm.edges:
		if not e.link_faces:
			try:
				dest_bm.edges.new([vert_map[v.index] for v in e.verts])
			except ValueError:
				pass


def _iter_geometries(geojson):
	"""Yield (geometry_dict, properties_dict) from any valid GeoJSON structure.

	Handles FeatureCollection, Feature, and bare Geometry objects.
	Multi* types are exploded into their single-geometry counterparts so that
	each yielded geometry is one of Point, LineString, Polygon.
	"""
	gtype = geojson.get("type", "")

	if gtype == "FeatureCollection":
		for feature in geojson.get("features", []):
			yield from _iter_geometries(feature)

	elif gtype == "Feature":
		geom = geojson.get("geometry")
		props = geojson.get("properties") or {}
		if geom is None:
			return
		gtype2 = geom.get("type", "")
		coords = geom.get("coordinates")
		if coords is None:
			return
		# Explode multi-types
		if gtype2 == "MultiPoint":
			for pt in coords:
				yield {"type": "Point", "coordinates": pt}, props
		elif gtype2 == "MultiLineString":
			for line in coords:
				yield {"type": "LineString", "coordinates": line}, props
		elif gtype2 == "MultiPolygon":
			for poly in coords:
				yield {"type": "Polygon", "coordinates": poly}, props
		elif gtype2 == "GeometryCollection":
			for sub_geom in geom.get("geometries", []):
				yield from _iter_geometries({"type": "Feature", "geometry": sub_geom, "properties": props})
		else:
			yield geom, props

	elif gtype in ("Point", "MultiPoint", "LineString", "MultiLineString",
					"Polygon", "MultiPolygon", "GeometryCollection"):
		# Bare geometry – wrap as feature and recurse
		yield from _iter_geometries({"type": "Feature", "geometry": geojson, "properties": {}})


def _get_height_from_props(props, default_height, level_height):
	"""Extract a building height from feature properties.

	Looks for common keys: 'height', 'building:height', 'building:levels',
	'levels'.  Returns *None* if nothing relevant is found, signalling that
	the feature is not a building.  Returns a float otherwise.
	"""
	for key in ("height", "building:height", "Height", "HEIGHT"):
		val = props.get(key)
		if val is not None:
			try:
				return float(str(val).replace(",", ".").split()[0])
			except (ValueError, IndexError):
				pass

	for key in ("building:levels", "levels", "building_levels"):
		val = props.get(key)
		if val is not None:
			try:
				return int(float(str(val))) * level_height
			except (ValueError, TypeError):
				pass

	# Check if any key hints at "building"
	if any(k.startswith("building") for k in props):
		return default_height

	return None


def _first_coord(geojson):
	"""Return the first [lon, lat] coordinate found in the GeoJSON, or None."""
	for geom, _props in _iter_geometries(geojson):
		gtype = geom.get("type", "")
		coords = geom.get("coordinates")
		if coords is None:
			continue
		if gtype == "Point":
			return coords[:2]
		elif gtype == "LineString":
			if coords:
				return coords[0][:2]
		elif gtype == "Polygon":
			if coords and coords[0]:
				return coords[0][0][:2]
	return None


# ---------------------------------------------------------------------------
# Phase A + B worker — runs in a background thread (NO bpy calls!)
# ---------------------------------------------------------------------------

def _geojson_parse_thread(filepath, dx, dy, dstCRS, buildingsExtrusion,
                           defaultHeight, levelHeight, separate, result_holder):
	"""Parse GeoJSON and reproject all features.  No bpy calls allowed here.

	Produces pure-Python data structures that Phase C (main thread) turns into
	Blender objects:

	  parsed['features_separate']:
	    list of dicts, one per feature (used when separate=True):
	      {
	        'cat': str,
	        'feat_name': str,
	        'gtype': str,
	        'pts_3d': list of (x, y, z) tuples,
	        'is_building': bool,
	        'height_val': float | None,
	        'props': dict,
	      }

	  parsed['features_merged']:
	    dict  cat -> list of same dicts (used when separate=False)

	  parsed['feat_count']: int
	  parsed['skip_count']: int
	  parsed['separate']:   bool  (echoed so Phase C knows which path to take)
	"""
	try:
		# --- Phase A: parse JSON ---------------------------------------------------
		with open(filepath, 'r', encoding='utf-8') as f:
			geojson = json.load(f)

		# --- Phase B: iterate + reproject -----------------------------------------
		rprj = Reproj(4326, dstCRS)

		features_separate = []    # used when separate=True
		features_merged = {}      # cat -> list of feature dicts, used when separate=False

		feat_count = 0
		skip_count = 0

		for geom, props in _iter_geometries(geojson):
			gtype = geom.get("type", "")
			coords = geom.get("coordinates")
			if coords is None:
				skip_count += 1
				continue

			feat_count += 1
			feat_name = (props.get("name") or props.get("Name") or
			             props.get("NAME") or str(feat_count))

			# ----- Point ------------------------------------------------------------
			if gtype == "Point":
				cat = "Points"
				pts_raw = [coords[:2]]

			# ----- LineString -------------------------------------------------------
			elif gtype == "LineString":
				cat = "Lines"
				if len(coords) < 2:
					skip_count += 1
					continue
				pts_raw = [c[:2] for c in coords]

			# ----- Polygon ----------------------------------------------------------
			elif gtype == "Polygon":
				cat = "Polygons"
				ring = coords[0] if coords else []
				if len(ring) < 3:
					skip_count += 1
					continue
				if ring[0] == ring[-1]:
					ring = ring[:-1]
				if len(ring) < 3:
					skip_count += 1
					continue
				pts_raw = [c[:2] for c in ring]

			else:
				skip_count += 1
				continue

			# Reproject
			try:
				pts_prj = rprj.pts(pts_raw)
			except Exception:
				log.warning("Reprojection failed for feature %s", feat_name, exc_info=True)
				skip_count += 1
				continue

			# Shift to scene origin
			pts_3d = [(p[0] - dx, p[1] - dy, 0.0) for p in pts_prj]

			is_building = False
			height_val = None
			if gtype == "Polygon" and buildingsExtrusion:
				height_val = _get_height_from_props(props, defaultHeight, levelHeight)
				if height_val is not None:
					is_building = True

			feat_data = {
				'cat': cat,
				'feat_name': feat_name,
				'gtype': gtype,
				'pts_3d': pts_3d,
				'is_building': is_building,
				'height_val': height_val,
				'props': props,
			}

			if separate:
				features_separate.append(feat_data)
			else:
				bucket = features_merged.setdefault(cat, [])
				bucket.append(feat_data)

		with _geojson_state_lock:
			result_holder['ok'] = True
			result_holder['parsed'] = {
				'features_separate': features_separate,
				'features_merged': features_merged,
				'feat_count': feat_count,
				'skip_count': skip_count,
				'separate': separate,
			}

	except Exception as exc:
		with _geojson_state_lock:
			result_holder['ok'] = False
			result_holder['error'] = str(exc)
		log.error("GeoJSON background parse failed", exc_info=True)


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_geojson_file(Operator):
	"""Import a GeoJSON file into the scene"""

	bl_idname = "importgis.geojson_file"
	bl_description = "Select and import a GeoJSON file (.geojson / .json)"
	bl_label = "Import GeoJSON"
	bl_options = {"UNDO"}

	# File browser properties
	filepath: StringProperty(
		name="File Path",
		description="Path to the GeoJSON file",
		maxlen=1024,
		subtype='FILE_PATH',
	)

	filename_ext = ".geojson"

	filter_glob: StringProperty(
		default="*.geojson;*.json",
		options={'HIDDEN'},
	)

	# --- User options -----------------------------------------------------------

	separate: BoolProperty(
		name="Separate objects",
		description="Create a separate Blender object for every feature (can be slow with many features)",
		default=False,
	)

	buildingsExtrusion: BoolProperty(
		name="Buildings extrusion",
		description="Apply Geometry-Nodes building extrusion when height data is present",
		default=True,
	)

	defaultHeight: FloatProperty(
		name="Default height",
		description="Fallback building height when the property is missing",
		default=DEFAULT_BUILDING_HEIGHT,
		min=0,
	)

	levelHeight: FloatProperty(
		name="Level height",
		description="Height per building level (used when 'building:levels' is present)",
		default=LEVEL_HEIGHT,
		min=0,
	)

	# ---------------------------------------------------------------------------

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def draw(self, context):
		layout = self.layout
		layout.prop(self, 'separate')
		layout.prop(self, 'buildingsExtrusion')
		if self.buildingsExtrusion:
			layout.prop(self, 'defaultHeight')
			layout.prop(self, 'levelHeight')

	# ---------------------------------------------------------------------------

	def execute(self, context):
		if not os.path.isfile(self.filepath):
			self.report({'ERROR'}, "File not found: " + self.filepath)
			return {'CANCELLED'}

		# Switch to object mode if needed
		try:
			bpy.ops.object.mode_set(mode='OBJECT')
		except RuntimeError:
			pass
		bpy.ops.object.select_all(action='DESELECT')

		# --- Scene CRS / origin --------------------------------------------------
		scn = context.scene
		geoscn = GeoScene(scn)

		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		# Auto-set UTM CRS / origin from first coordinate — must happen in main
		# thread because _first_coord does json.load again (small but unavoidable
		# duplication here; only runs when the scene has no CRS/origin yet).
		if not geoscn.hasCRS or not geoscn.hasOriginPrj:
			try:
				with open(self.filepath, 'r', encoding='utf-8') as f:
					_geojson_probe = json.load(f)
			except Exception as e:
				self.report({'ERROR'}, "Failed to parse GeoJSON: " + str(e))
				return {'CANCELLED'}

			_cached_first_coord = _first_coord(_geojson_probe)

			if not geoscn.hasCRS:
				first = _cached_first_coord
				if first is None:
					self.report({'ERROR'}, "GeoJSON contains no usable coordinates")
					return {'CANCELLED'}
				lon, lat = first
				try:
					geoscn.crs = utm.lonlat_to_epsg(lon, lat)
				except Exception:
					log.error("Cannot auto-set UTM CRS", exc_info=True)
					self.report({'ERROR'}, "Cannot auto-set UTM CRS from first coordinate")
					return {'CANCELLED'}
				log.info("Auto-set scene CRS to %s", geoscn.crs)

			if not geoscn.hasOriginPrj:
				first = _cached_first_coord
				if first is not None:
					lon, lat = first
					x, y = reprojPt(4326, geoscn.crs, lon, lat)
					geoscn.setOriginPrj(x, y)

		dstCRS = geoscn.crs
		dx, dy = geoscn.crsx, geoscn.crsy

		# Validate reprojector can be constructed before spawning thread
		try:
			Reproj(4326, dstCRS)
		except Exception:
			log.error("Unable to initialise reprojection", exc_info=True)
			self.report({'ERROR'}, "Unable to reproject data – check logs")
			return {'CANCELLED'}

		w = context.window
		w.cursor_set('WAIT')

		# --- Capture scene reference for Phase C ---------------------------------
		# Store scene name (string) so the timer callback can look it up via
		# bpy.data.scenes (avoids holding a live bpy reference across threads).
		scene_name = scn.name

		# --- Doppelklick-Guard + thread start ------------------------------------
		global _geojson_thread, _geojson_result, _geojson_context_args
		with _geojson_state_lock:
			if _geojson_thread is not None and _geojson_thread.is_alive():
				self.report({'INFO'}, "GeoJSON import already running, please wait...")
				return {'CANCELLED'}

			_geojson_result = {'ok': None, 'error': None}
			_geojson_context_args = {
				'filepath': self.filepath,
				'scene_name': scene_name,
				'buildingsExtrusion': self.buildingsExtrusion,
				'separate': self.separate,
			}

			_geojson_thread = threading.Thread(
				target=_geojson_parse_thread,
				args=(
					self.filepath, dx, dy, dstCRS,
					self.buildingsExtrusion, self.defaultHeight, self.levelHeight,
					self.separate, _geojson_result,
				),
				daemon=True,
			)
			_geojson_thread.start()

		self.report({'INFO'}, "Parsing GeoJSON in background, please wait...")

		# --- Phase C polling callback (main thread) -------------------------------
		def _poll_geojson_thread():
			global _geojson_thread, _geojson_result, _geojson_context_args

			with _geojson_state_lock:
				if _geojson_thread is None or _geojson_thread.is_alive():
					return 0.5  # poll again in 0.5 s

				# Thread finished — consume state under lock
				_geojson_thread = None
				result = _geojson_result
				ctx_args = _geojson_context_args
				_geojson_result = None
				_geojson_context_args = None

			# --- Error path -------------------------------------------------------
			def _reset_cursor():
				try:
					bpy.context.window.cursor_set('DEFAULT')
				except Exception:
					pass

			if not result or not result.get('ok'):
				err = result.get('error', 'Unknown error') if result else 'No result'
				log.error("GeoJSON import background error: %s", err)
				_reset_cursor()
				return None  # stop timer

			# --- Success: build Blender objects (Phase C) -------------------------
			parsed = result['parsed']
			feat_count = parsed['feat_count']
			skip_count = parsed['skip_count']
			separate = parsed['separate']
			buildingsExtrusion = ctx_args['buildingsExtrusion']

			scn = bpy.data.scenes.get(ctx_args['scene_name'])
			if scn is None:
				log.error("Scene '%s' no longer exists", ctx_args['scene_name'])
				_reset_cursor()
				return None

			try:
				if separate:
					_build_separate(scn, parsed, buildingsExtrusion)
				else:
					_build_merged(scn, parsed, buildingsExtrusion)

				bbox = getBBOX.fromScn(scn)
				adjust3Dview(bpy.context, bbox)

				msg = "Imported {} feature(s)".format(feat_count)
				if skip_count:
					msg += " ({} skipped)".format(skip_count)
				log.info(msg)
				# INFO reports via operator self are unavailable in a timer callback;
				# write to the log and print so the user sees it in the system console.
				print("[cartoblend] " + msg)

			except Exception:
				log.error("GeoJSON Phase C (mesh build) failed", exc_info=True)
			finally:
				_reset_cursor()

			return None  # stop timer

		bpy.app.timers.register(_poll_geojson_thread, first_interval=0.5)

		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Phase C helpers — called from main thread only
# ---------------------------------------------------------------------------

def _build_separate(scn, parsed, buildingsExtrusion):
	"""Create one Blender object per feature (separate=True path)."""
	layer = bpy.data.collections.new('GeoJSON')
	scn.collection.children.link(layer)

	for feat in parsed['features_separate']:
		cat = feat['cat']
		feat_name = feat['feat_name']
		gtype = feat['gtype']
		pts_3d = feat['pts_3d']
		is_building = feat['is_building']
		height_val = feat['height_val']
		props = feat['props']

		bm = bmesh.new()
		height_layer = None
		if is_building:
			height_layer = bm.faces.layers.float.new('height')

		_fill_bmesh(bm, gtype, pts_3d, is_building, height_val, height_layer)

		mesh = bpy.data.meshes.new(feat_name)
		bm.to_mesh(mesh)
		bm.free()
		mesh.update()

		obj = bpy.data.objects.new(feat_name, mesh)

		if is_building and buildingsExtrusion:
			_apply_building_geonodes(obj)

		# Store properties as custom props
		for k, v in props.items():
			if isinstance(v, (dict, list, tuple)):
				continue
			try:
				if isinstance(v, str) and len(v) > 1024:
					v = v[:1024]
				obj[k] = v
			except Exception:
				obj[k] = str(v)[:1024]

		# Link into collection, organised by category
		try:
			cat_col = layer.children[cat]
		except KeyError:
			cat_col = bpy.data.collections.new(cat)
			layer.children.link(cat_col)
		cat_col.objects.link(obj)
		obj.select_set(True)


def _build_merged(scn, parsed, buildingsExtrusion):
	"""Merge all features per category into a single object (separate=False path)."""
	bmeshes = {}              # cat -> bmesh
	vgroupsObj = {}           # cat -> {group_name: [vertex indices]}
	building_categories = set()
	_bm_has_height_layer = set()

	for cat, feats in parsed['features_merged'].items():
		for feat in feats:
			gtype = feat['gtype']
			pts_3d = feat['pts_3d']
			is_building = feat['is_building']
			height_val = feat['height_val']
			props = feat['props']

			# Per-feature bmesh (temporary)
			bm = bmesh.new()
			height_layer = None
			if is_building:
				height_layer = bm.faces.layers.float.new('height')

			degenerate = _fill_bmesh(bm, gtype, pts_3d, is_building, height_val, height_layer)
			if degenerate:
				bm.free()
				continue

			# Get or create destination bmesh for this category
			dest_bm = bmeshes.get(cat)
			if dest_bm is None:
				dest_bm = bmesh.new()
				if is_building:
					dest_bm.faces.layers.float.new('height')
					_bm_has_height_layer.add(cat)
				bmeshes[cat] = dest_bm

			# Ensure 'height' layer exists if we encounter buildings later
			if is_building and cat not in _bm_has_height_layer:
				dest_bm.faces.layers.float.new('height')
				_bm_has_height_layer.add(cat)
			if is_building:
				building_categories.add(cat)

			bm.verts.index_update()
			offset = len(dest_bm.verts)
			_joinBmesh(bm, dest_bm)

			# Vertex groups for properties
			vgroups = vgroupsObj.setdefault(cat, {})
			vidx = list(range(offset, offset + len(bm.verts)))

			feat_label = props.get("name") or props.get("Name") or props.get("NAME")
			if feat_label:
				vg = vgroups.setdefault("Name:" + str(feat_label), [])
				vg.extend(vidx)

			for tag_key in ("type", "class", "category", "landuse", "building"):
				tag_val = props.get(tag_key)
				if tag_val:
					vg = vgroups.setdefault("Tag:" + tag_key + "=" + str(tag_val), [])
					vg.extend(vidx)

			bm.free()

	# Finalise merged bmeshes
	for name, bm in bmeshes.items():
		bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
		mesh = bpy.data.meshes.new(name)
		bm.to_mesh(mesh)
		bm.free()
		mesh.update()

		obj = bpy.data.objects.new(name, mesh)
		scn.collection.objects.link(obj)
		obj.select_set(True)

		if (buildingsExtrusion
				and name in building_categories
				and 'height' in [attr.name for attr in mesh.attributes]):
			_apply_building_geonodes(obj)

		vgroups = vgroupsObj.get(name)
		if vgroups:
			for vgName in sorted(vgroups.keys()):
				vgIdx = vgroups[vgName]
				g = obj.vertex_groups.new(name=vgName)
				g.add(vgIdx, weight=1, type='ADD')


def _fill_bmesh(bm, gtype, pts_3d, is_building, height_val, height_layer):
	"""Fill *bm* with geometry for one feature.

	Returns True if the feature was degenerate and should be skipped, else False.
	"""
	if gtype == "Point":
		for pt in pts_3d:
			bm.verts.new(pt)

	elif gtype == "LineString":
		verts = [bm.verts.new(pt) for pt in pts_3d]
		for i in range(len(verts) - 1):
			bm.edges.new([verts[i], verts[i + 1]])

	elif gtype == "Polygon":
		verts = [bm.verts.new(pt) for pt in pts_3d]
		try:
			face = bm.faces.new(verts)
		except ValueError:
			log.warning("Degenerate polygon – skipped face creation")
			return True  # degenerate

		face.normal_update()
		if face.normal.z < 0:
			face.normal_flip()

		if is_building and height_val is not None and height_layer is not None:
			face[height_layer] = float(height_val)

	return False  # ok


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
	IMPORTGIS_OT_geojson_file,
]


def register():
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError:
			log.warning('%s is already registered, now unregister and retry...', cls)
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)


def unregister():
	for cls in classes:
		bpy.utils.unregister_class(cls)
