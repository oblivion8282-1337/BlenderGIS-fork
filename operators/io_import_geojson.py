import os
import json
import logging
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
# Helpers
# ---------------------------------------------------------------------------

def _joinBmesh(src_bm, dest_bm):
	"""Join src_bm into dest_bm using direct bmesh vertex/face/edge copying."""
	vert_map = {}
	for v in src_bm.verts:
		new_v = dest_bm.verts.new(v.co)
		vert_map[v.index] = new_v
	dest_bm.verts.ensure_lookup_table()
	for f in src_bm.faces:
		try:
			new_face = dest_bm.faces.new([vert_map[v.index] for v in f.verts])
			new_face.material_index = f.material_index
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

		w = context.window
		w.cursor_set('WAIT')

		# --- Parse GeoJSON ------------------------------------------------------
		try:
			with open(self.filepath, 'r', encoding='utf-8') as f:
				geojson = json.load(f)
		except Exception as e:
			log.error("Failed to parse GeoJSON", exc_info=True)
			self.report({'ERROR'}, "Failed to parse GeoJSON: " + str(e))
			return {'CANCELLED'}

		# --- Scene CRS / origin --------------------------------------------------
		scn = context.scene
		geoscn = GeoScene(scn)

		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		# Auto-set UTM CRS from first coordinate if scene has none
		if not geoscn.hasCRS:
			first = _first_coord(geojson)
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
			first = _first_coord(geojson)
			if first is not None:
				lon, lat = first
				x, y = reprojPt(4326, geoscn.crs, lon, lat)
				geoscn.setOriginPrj(x, y)

		dstCRS = geoscn.crs

		# Init reprojector  (EPSG:4326 -> scene CRS)
		try:
			rprj = Reproj(4326, dstCRS)
		except Exception:
			log.error("Unable to initialise reprojection", exc_info=True)
			self.report({'ERROR'}, "Unable to reproject data – check logs")
			return {'CANCELLED'}

		dx, dy = geoscn.crsx, geoscn.crsy

		# --- Build geometry -------------------------------------------------------

		# Accumulators for merged mode
		bmeshes = {}       # name -> bmesh
		vgroupsObj = {}    # name -> {group_name: [vertex indices]}

		# Collection for separate mode
		layer = None
		if self.separate:
			layer = bpy.data.collections.new('GeoJSON')
			scn.collection.children.link(layer)

		feat_count = 0
		skip_count = 0

		for geom, props in _iter_geometries(geojson):
			gtype = geom.get("type", "")
			coords = geom.get("coordinates")
			if coords is None:
				skip_count += 1
				continue

			feat_count += 1
			feat_name = props.get("name") or props.get("Name") or props.get("NAME") or str(feat_count)

			# ----- Point --------------------------------------------------------
			if gtype == "Point":
				cat = "Points"
				pts_raw = [coords[:2]]  # [[lon, lat]]

			# ----- LineString ---------------------------------------------------
			elif gtype == "LineString":
				cat = "Lines"
				if len(coords) < 2:
					skip_count += 1
					continue
				pts_raw = [c[:2] for c in coords]

			# ----- Polygon ------------------------------------------------------
			elif gtype == "Polygon":
				cat = "Polygons"
				ring = coords[0] if coords else []
				if len(ring) < 3:
					skip_count += 1
					continue
				# GeoJSON polygons have the first coord repeated as last – drop it
				if ring[0] == ring[-1]:
					ring = ring[:-1]
				if len(ring) < 3:
					skip_count += 1
					continue
				pts_raw = [c[:2] for c in ring]

			else:
				skip_count += 1
				continue

			# Reproject  (lon/lat tuples)
			try:
				pts_prj = rprj.pts(pts_raw)
			except Exception:
				log.warning("Reprojection failed for feature %s", feat_name, exc_info=True)
				skip_count += 1
				continue

			# Shift to scene origin
			pts_3d = [(p[0] - dx, p[1] - dy, 0.0) for p in pts_prj]

			# --- Build bmesh for this feature -----------------------------------
			bm = bmesh.new()

			is_polygon = (gtype == "Polygon")
			is_building = False
			height_val = None

			if is_polygon and self.buildingsExtrusion:
				height_val = _get_height_from_props(props, self.defaultHeight, self.levelHeight)
				if height_val is not None:
					is_building = True

			# Pre-create attribute layers
			if is_building:
				height_layer = bm.faces.layers.float.new('height')

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
					# Degenerate polygon (e.g. duplicate verts)
					log.warning("Degenerate polygon for feature %s – skipped face creation", feat_name)
					bm.free()
					skip_count += 1
					continue

				face.normal_update()
				if face.normal.z < 0:
					face.normal_flip()

				if is_building and height_val is not None:
					face[height_layer] = float(height_val)

			# --- Separate mode: create object immediately -----------------------
			if self.separate:
				obj_name = feat_name
				mesh = bpy.data.meshes.new(obj_name)
				bm.to_mesh(mesh)
				mesh.update()

				obj = bpy.data.objects.new(obj_name, mesh)

				# Building GN
				if is_building:
					_apply_building_geonodes(obj)

				# Store properties as custom props
				for k, v in props.items():
					try:
						obj[k] = v
					except Exception:
						obj[k] = str(v)

				# Link into collection, organised by category
				try:
					cat_col = layer.children[cat]
				except KeyError:
					cat_col = bpy.data.collections.new(cat)
					layer.children.link(cat_col)
				cat_col.objects.link(obj)
				obj.select_set(True)

			# --- Merged mode: accumulate into per-category bmeshes ---------------
			else:
				objName = cat
				dest_bm = bmeshes.get(objName)
				if dest_bm is None:
					dest_bm = bmesh.new()
					# Pre-create layers on dest so they survive joins
					if is_building:
						dest_bm.faces.layers.float.new('height')
					bmeshes[objName] = dest_bm

				# Ensure 'height' layer exists on dest even if first features weren't buildings
				if is_building and 'height' not in [l.name for l in dest_bm.faces.layers.float]:
					dest_bm.faces.layers.float.new('height')

				bm.verts.index_update()
				offset = len(dest_bm.verts)
				_joinBmesh(bm, dest_bm)

				# Vertex groups for properties
				vgroups = vgroupsObj.setdefault(objName, {})
				vidx = list(range(offset, offset + len(bm.verts)))

				feat_label = props.get("name") or props.get("Name") or props.get("NAME")
				if feat_label:
					vg = vgroups.setdefault("Name:" + str(feat_label), [])
					vg.extend(vidx)

				# Group by a few common classification keys
				for tag_key in ("type", "class", "category", "landuse", "building"):
					tag_val = props.get(tag_key)
					if tag_val:
						vg = vgroups.setdefault("Tag:" + tag_key + "=" + str(tag_val), [])
						vg.extend(vidx)

			bm.free()

		# --- Finalise merged bmeshes -------------------------------------------
		if not self.separate:
			for name, bm in bmeshes.items():
				bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
				mesh = bpy.data.meshes.new(name)
				bm.to_mesh(mesh)
				bm.free()
				mesh.update()

				obj = bpy.data.objects.new(name, mesh)
				scn.collection.objects.link(obj)
				obj.select_set(True)

				# Building GN for polygon objects that carry height data
				if self.buildingsExtrusion and name == "Polygons":
					# Check if any face actually has a height > 0
					has_height = False
					for poly in mesh.polygons:
						# Named attribute stored via bmesh – read via attribute API
						pass
					# Safer: just apply if the layer exists
					if 'height' in [attr.name for attr in mesh.attributes]:
						_apply_building_geonodes(obj)

				# Vertex groups
				vgroups = vgroupsObj.get(name)
				if vgroups:
					for vgName in sorted(vgroups.keys()):
						vgIdx = vgroups[vgName]
						g = obj.vertex_groups.new(name=vgName)
						g.add(vgIdx, weight=1, type='ADD')

		# --- Finish ---------------------------------------------------------------
		if feat_count == 0:
			self.report({'WARNING'}, "No geometry found in GeoJSON file")
			return {'CANCELLED'}

		bbox = getBBOX.fromScn(scn)
		adjust3Dview(context, bbox)

		msg = "Imported {} feature(s)".format(feat_count)
		if skip_count:
			msg += " ({} skipped)".format(skip_count)
		self.report({'INFO'}, msg)
		log.info(msg)

		return {'FINISHED'}


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
