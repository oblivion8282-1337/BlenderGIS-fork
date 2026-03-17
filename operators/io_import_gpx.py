import os
import logging
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

import bpy
import bmesh
from bpy.types import Operator
from bpy.props import StringProperty, BoolProperty, FloatProperty, EnumProperty

from ..geoscene import GeoScene
from ..core.proj import Reproj, reprojPt, utm
from .utils import adjust3Dview, getBBOX

PKG, SUBPKG = __package__.split('.', maxsplit=1)

# GPX XML namespace
GPX_NS_10 = '{http://www.topografix.com/GPX/1/0}'
GPX_NS_11 = '{http://www.topografix.com/GPX/1/1}'


# ---------------------------------------------------------------------------
# GPX Parser
# ---------------------------------------------------------------------------

def _detect_ns(root):
	"""Detect GPX namespace from root element tag."""
	tag = root.tag
	if tag.startswith(GPX_NS_11):
		return GPX_NS_11
	if tag.startswith(GPX_NS_10):
		return GPX_NS_10
	# No namespace
	return ''


def _parse_gpx(filepath):
	"""Parse a GPX file and return structured data.

	Returns dict with keys:
	  'waypoints': [(lon, lat, ele, name), ...]
	  'routes':    [{'name': str, 'points': [(lon, lat, ele), ...]}, ...]
	  'tracks':    [{'name': str, 'segments': [[(lon, lat, ele), ...], ...]}, ...]
	"""
	tree = ET.parse(filepath)
	root = tree.getroot()
	ns = _detect_ns(root)

	result = {'waypoints': [], 'routes': [], 'tracks': []}

	# --- Waypoints ---
	for wpt in root.findall(ns + 'wpt'):
		lat = float(wpt.get('lat'))
		lon = float(wpt.get('lon'))
		ele_el = wpt.find(ns + 'ele')
		ele = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
		name_el = wpt.find(ns + 'name')
		name = name_el.text if name_el is not None and name_el.text else ''
		result['waypoints'].append((lon, lat, ele, name))

	# --- Routes ---
	for rte in root.findall(ns + 'rte'):
		name_el = rte.find(ns + 'name')
		rte_name = name_el.text if name_el is not None and name_el.text else ''
		points = []
		for rtept in rte.findall(ns + 'rtept'):
			lat = float(rtept.get('lat'))
			lon = float(rtept.get('lon'))
			ele_el = rtept.find(ns + 'ele')
			ele = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
			points.append((lon, lat, ele))
		if points:
			result['routes'].append({'name': rte_name, 'points': points})

	# --- Tracks ---
	for trk in root.findall(ns + 'trk'):
		name_el = trk.find(ns + 'name')
		trk_name = name_el.text if name_el is not None and name_el.text else ''
		segments = []
		for trkseg in trk.findall(ns + 'trkseg'):
			seg_points = []
			for trkpt in trkseg.findall(ns + 'trkpt'):
				lat = float(trkpt.get('lat'))
				lon = float(trkpt.get('lon'))
				ele_el = trkpt.find(ns + 'ele')
				ele = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
				seg_points.append((lon, lat, ele))
			if seg_points:
				segments.append(seg_points)
		if segments:
			result['tracks'].append({'name': trk_name, 'segments': segments})

	return result


def _first_coord_gpx(gpx_data):
	"""Return first (lon, lat) from parsed GPX data, or None."""
	for wpt in gpx_data['waypoints']:
		return (wpt[0], wpt[1])
	for rte in gpx_data['routes']:
		if rte['points']:
			return (rte['points'][0][0], rte['points'][0][1])
	for trk in gpx_data['tracks']:
		if trk['segments'] and trk['segments'][0]:
			p = trk['segments'][0][0]
			return (p[0], p[1])
	return None


# ---------------------------------------------------------------------------
# Route material
# ---------------------------------------------------------------------------

def _get_or_create_route_material(color_name='route'):
	"""Get or create a simple colored material for routes."""
	mat_name = f'GPX Route ({color_name})'
	mat = bpy.data.materials.get(mat_name)
	if mat is not None:
		return mat

	mat = bpy.data.materials.new(mat_name)
	mat.use_nodes = True
	nodes = mat.node_tree.nodes
	bsdf = nodes.get('Principled BSDF')
	if bsdf:
		# Bright red/orange + emission for visibility on terrain
		bsdf.inputs['Base Color'].default_value = (1.0, 0.15, 0.0, 1.0)
		bsdf.inputs['Roughness'].default_value = 0.5
		bsdf.inputs['Emission Color'].default_value = (1.0, 0.2, 0.0, 1.0)
		bsdf.inputs['Emission Strength'].default_value = 2.0
	return mat


# ---------------------------------------------------------------------------
# Geometry Nodes: GPX Snap to Terrain (vertex-based, no face domain)
# ---------------------------------------------------------------------------

def _get_or_create_gpx_snap_geonodes():
	"""Snap vertices to terrain via raycast. Unlike the OSM version this works
	on edges/vertices (no Face domain evaluation) so it's suitable for lines."""
	ng_name = 'GPX Snap to Terrain'
	ng = bpy.data.node_groups.get(ng_name)
	if ng is not None:
		return ng

	ng = bpy.data.node_groups.new(ng_name, 'GeometryNodeTree')

	# Interface
	ng.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
	ng.interface.new_socket('Terrain', in_out='INPUT', socket_type='NodeSocketObject')
	s_off = ng.interface.new_socket('Z Offset', in_out='INPUT', socket_type='NodeSocketFloat')
	s_off.default_value = 3.0
	s_off.min_value = -100.0
	s_off.max_value = 100.0
	ng.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')

	nodes = ng.nodes
	links = ng.links

	n_in = nodes.new('NodeGroupInput'); n_in.location = (-900, 0)
	n_out = nodes.new('NodeGroupOutput'); n_out.location = (800, 0)

	# Object Info → terrain geometry
	n_objinfo = nodes.new('GeometryNodeObjectInfo')
	n_objinfo.transform_space = 'RELATIVE'
	n_objinfo.location = (-700, -300)
	links.new(n_in.outputs['Terrain'], n_objinfo.inputs['Object'])

	# Current vertex position
	n_pos = nodes.new('GeometryNodeInputPosition')
	n_pos.location = (-700, -100)

	n_sep = nodes.new('ShaderNodeSeparateXYZ')
	n_sep.location = (-500, -100)
	links.new(n_pos.outputs[0], n_sep.inputs[0])

	# Ray source: (x, y, 10000) — cast from high above
	n_src = nodes.new('ShaderNodeCombineXYZ')
	n_src.location = (-300, -100)
	n_src.inputs['Z'].default_value = 10000.0
	links.new(n_sep.outputs['X'], n_src.inputs['X'])
	links.new(n_sep.outputs['Y'], n_src.inputs['Y'])

	# Ray direction: straight down
	n_dir = nodes.new('ShaderNodeCombineXYZ')
	n_dir.location = (-300, -300)
	n_dir.inputs['Z'].default_value = -1.0

	# Raycast
	n_ray = nodes.new('GeometryNodeRaycast')
	n_ray.location = (-50, -200)
	links.new(n_objinfo.outputs['Geometry'], n_ray.inputs['Target Geometry'])
	links.new(n_src.outputs[0], n_ray.inputs['Source Position'])
	links.new(n_dir.outputs[0], n_ray.inputs['Ray Direction'])
	n_ray.inputs['Ray Length'].default_value = 20000.0

	# Get hit Z
	n_hit_sep = nodes.new('ShaderNodeSeparateXYZ')
	n_hit_sep.location = (150, -250)
	links.new(n_ray.outputs['Hit Position'], n_hit_sep.inputs[0])

	# Mean Z of all hit vertices (fallback for non-hit)
	n_stat = nodes.new('GeometryNodeAttributeStatistic')
	n_stat.data_type = 'FLOAT'
	n_stat.location = (150, -450)
	links.new(n_in.outputs['Geometry'], n_stat.inputs['Geometry'])
	links.new(n_ray.outputs['Is Hit'], n_stat.inputs['Selection'])
	links.new(n_hit_sep.outputs['Z'], n_stat.inputs[2])

	# Switch: hit → hit_Z, miss → mean_Z  (no Face domain — direct per-vertex)
	n_switch = nodes.new('GeometryNodeSwitch')
	n_switch.input_type = 'FLOAT'
	n_switch.location = (350, -300)
	links.new(n_ray.outputs['Is Hit'], n_switch.inputs[0])
	links.new(n_stat.outputs['Mean'], n_switch.inputs[1])   # False: mean Z
	links.new(n_hit_sep.outputs['Z'], n_switch.inputs[2])   # True: hit Z

	# Add Z offset
	n_add = nodes.new('ShaderNodeMath')
	n_add.operation = 'ADD'
	n_add.location = (500, -200)
	links.new(n_switch.outputs[0], n_add.inputs[0])
	links.new(n_in.outputs['Z Offset'], n_add.inputs[1])

	# New position (orig X, orig Y, final Z)
	n_new_pos = nodes.new('ShaderNodeCombineXYZ')
	n_new_pos.location = (600, -50)
	links.new(n_sep.outputs['X'], n_new_pos.inputs['X'])
	links.new(n_sep.outputs['Y'], n_new_pos.inputs['Y'])
	links.new(n_add.outputs[0], n_new_pos.inputs['Z'])

	# Set Position on ALL vertices
	n_setpos = nodes.new('GeometryNodeSetPosition')
	n_setpos.location = (750, 100)
	links.new(n_in.outputs['Geometry'], n_setpos.inputs['Geometry'])
	links.new(n_new_pos.outputs[0], n_setpos.inputs['Position'])

	links.new(n_setpos.outputs[0], n_out.inputs[0])
	return ng


# ---------------------------------------------------------------------------
# Geometry Nodes: GPX Route Width
# ---------------------------------------------------------------------------

def _get_or_create_route_geonodes():
	"""Create or return a Geometry Nodes group for giving routes a visible width.
	Supports Flat Band and Tube profiles, with curve subdivision smoothing.
	Profile input: 0 = Flat Band, 1 = Tube."""
	ng_name = 'GPX Route Width'
	ng = bpy.data.node_groups.get(ng_name)
	if ng is not None:
		return ng

	ng = bpy.data.node_groups.new(ng_name, 'GeometryNodeTree')

	# Interface
	ng.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
	s_w = ng.interface.new_socket('Width', in_out='INPUT', socket_type='NodeSocketFloat')
	s_w.default_value = 3.0
	s_w.min_value = 0.1
	s_res = ng.interface.new_socket('Smoothing', in_out='INPUT', socket_type='NodeSocketFloat')
	s_res.default_value = 2.0
	s_res.min_value = 0.0
	s_res.max_value = 10.0
	s_res.description = "Subdivision cuts per segment – higher = smoother curve"
	s_prof = ng.interface.new_socket('Profile', in_out='INPUT', socket_type='NodeSocketInt')
	s_prof.default_value = 0
	s_prof.min_value = 0
	s_prof.max_value = 1
	s_prof.description = "0 = Flat Band, 1 = Tube"
	s_merge = ng.interface.new_socket('Merge Dist', in_out='INPUT', socket_type='NodeSocketFloat')
	s_merge.default_value = 2.5
	s_merge.min_value = 0.0
	s_merge.max_value = 50.0
	s_merge.description = "Merge overlapping vertices at corners (half-width recommended)"
	ng.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')

	nodes = ng.nodes
	links = ng.links

	# Group Input / Output
	inp = nodes.new('NodeGroupInput')
	inp.location = (-800, 0)
	out = nodes.new('NodeGroupOutput')
	out.location = (800, 0)

	# Mesh to Curve
	m2c = nodes.new('GeometryNodeMeshToCurve')
	m2c.location = (-600, 0)
	links.new(inp.outputs['Geometry'], m2c.inputs['Mesh'])

	# Subdivide Curve for smoothing (resolution = number of cuts)
	subdiv = nodes.new('GeometryNodeSubdivideCurve')
	subdiv.location = (-400, 0)
	links.new(m2c.outputs['Curve'], subdiv.inputs['Curve'])

	# Smoothing → integer cuts (float to int via floor)
	f2i = nodes.new('ShaderNodeMath')
	f2i.location = (-600, -150)
	f2i.operation = 'FLOOR'
	links.new(inp.outputs['Smoothing'], f2i.inputs[0])
	links.new(f2i.outputs[0], subdiv.inputs['Cuts'])

	# Width * 0.5 for half-width/radius
	mult = nodes.new('ShaderNodeMath')
	mult.location = (-400, -300)
	mult.operation = 'MULTIPLY'
	mult.inputs[1].default_value = 0.5
	links.new(inp.outputs['Width'], mult.inputs[0])

	# --- Profile A: Flat Band (Line from -hw to +hw) ---
	line = nodes.new('GeometryNodeCurvePrimitiveLine')
	line.location = (-200, -400)
	line.mode = 'POINTS'
	# Start: (-1, 0, 0), End: (1, 0, 0) — scaled by width later via Scale input
	line.inputs['Start'].default_value = (-1.0, 0.0, 0.0)
	line.inputs['End'].default_value = (1.0, 0.0, 0.0)

	# --- Profile B: Tube (Circle) ---
	circle = nodes.new('GeometryNodeCurvePrimitiveCircle')
	circle.location = (-200, -200)
	circle.mode = 'RADIUS'
	circle.inputs['Resolution'].default_value = 8
	circle.inputs['Radius'].default_value = 1.0

	# Switch between profiles: Profile == 0 → Flat, Profile >= 1 → Tube
	prof_cmp = nodes.new('FunctionNodeCompare')
	prof_cmp.location = (-200, -550)
	prof_cmp.data_type = 'INT'
	prof_cmp.operation = 'GREATER_EQUAL'
	prof_cmp.inputs[2].default_value = 1  # compare value
	links.new(inp.outputs['Profile'], prof_cmp.inputs[2])  # A = Profile
	# Actually: A >= 1 → Tube
	# Inputs for INT compare: inputs[2] = A (INT), inputs[3] = B (INT)
	links.new(inp.outputs['Profile'], prof_cmp.inputs[2])
	prof_cmp.inputs[3].default_value = 1

	prof_switch = nodes.new('GeometryNodeSwitch')
	prof_switch.input_type = 'GEOMETRY'
	prof_switch.location = (0, -300)
	links.new(prof_cmp.outputs[0], prof_switch.inputs[0])         # Switch condition
	links.new(line.outputs['Curve'], prof_switch.inputs[1])       # False (0): Flat
	links.new(circle.outputs['Curve'], prof_switch.inputs[2])     # True (1): Tube

	# Curve to Mesh with selected profile, scaled by half-width
	c2m = nodes.new('GeometryNodeCurveToMesh')
	c2m.location = (200, 0)
	links.new(subdiv.outputs['Curve'], c2m.inputs['Curve'])
	links.new(prof_switch.outputs[0], c2m.inputs['Profile Curve'])

	# Scale profile by half-width (Curve to Mesh doesn't have a scale input,
	# so we scale the profile curves directly)
	# Actually Curve to Mesh does NOT have a Scale input in Blender 5.x
	# So we need to scale the profile before feeding it in
	# → Transform the profile curve

	# Better approach: scale profile points by half-width
	# We'll use Set Position to scale the profile before the switch

	# Scale line profile
	line_pos = nodes.new('GeometryNodeInputPosition')
	line_pos.location = (-400, -450)

	line_scale = nodes.new('ShaderNodeVectorMath')
	line_scale.location = (-300, -450)
	line_scale.operation = 'SCALE'
	links.new(line_pos.outputs[0], line_scale.inputs[0])
	links.new(mult.outputs[0], line_scale.inputs['Scale'])

	line_setpos = nodes.new('GeometryNodeSetPosition')
	line_setpos.location = (-150, -400)
	links.new(line.outputs['Curve'], line_setpos.inputs['Geometry'])
	links.new(line_scale.outputs[0], line_setpos.inputs['Position'])

	# Scale circle profile
	circ_scale = nodes.new('ShaderNodeVectorMath')
	circ_scale.location = (-300, -250)
	circ_scale.operation = 'SCALE'
	links.new(line_pos.outputs[0], circ_scale.inputs[0])
	links.new(mult.outputs[0], circ_scale.inputs['Scale'])

	circ_setpos = nodes.new('GeometryNodeSetPosition')
	circ_setpos.location = (-150, -200)
	links.new(circle.outputs['Curve'], circ_setpos.inputs['Geometry'])
	links.new(circ_scale.outputs[0], circ_setpos.inputs['Position'])

	# Re-link switch to use scaled profiles
	links.new(line_setpos.outputs['Geometry'], prof_switch.inputs[1])   # False: Flat
	links.new(circ_setpos.outputs['Geometry'], prof_switch.inputs[2])   # True: Tube

	# Set Shade Smooth
	smooth = nodes.new('GeometryNodeSetShadeSmooth')
	smooth.location = (400, 0)
	links.new(c2m.outputs['Mesh'], smooth.inputs['Geometry'])

	# Merge by Distance — clean up overlapping faces at corners
	merge = nodes.new('GeometryNodeMergeByDistance')
	merge.location = (600, 0)
	links.new(smooth.outputs['Geometry'], merge.inputs['Geometry'])
	links.new(inp.outputs['Merge Dist'], merge.inputs['Distance'])

	links.new(merge.outputs['Geometry'], out.inputs['Geometry'])

	return ng


def _apply_route_geonodes(obj, width=3.0, resolution=2.0, profile=0, terrain_obj=None):
	"""Add terrain snap (if requested) + route width GN modifiers + material.
	profile: 0 = Flat Band, 1 = Tube."""
	# Snap to terrain FIRST (before width conversion, so raycast hits terrain)
	if terrain_obj is not None:
		snap_ng = _get_or_create_gpx_snap_geonodes()
		snap_mod = obj.modifiers.new('Snap to Terrain', 'NODES')
		snap_mod.node_group = snap_ng
		for item in snap_ng.interface.items_tree:
			if item.name == 'Terrain' and hasattr(item, 'identifier'):
				snap_mod[item.identifier] = terrain_obj
				break

	# Route width + smoothing + profile
	ng = _get_or_create_route_geonodes()
	mod = obj.modifiers.new('GPX Route Width', 'NODES')
	mod.node_group = ng

	# Set inputs
	for item in ng.interface.items_tree:
		if not hasattr(item, 'identifier'):
			continue
		if item.name == 'Width':
			mod[item.identifier] = width
		elif item.name == 'Smoothing':
			mod[item.identifier] = resolution
		elif item.name == 'Profile':
			mod[item.identifier] = profile
		elif item.name == 'Merge Dist':
			mod[item.identifier] = width * 0.5

	# Material
	mat = _get_or_create_route_material()
	if mat.name not in [m.name for m in obj.data.materials]:
		obj.data.materials.append(mat)


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_gpx_file(Operator):
	"""Import a GPX file (tracks, routes, waypoints) into the scene"""

	bl_idname = "importgis.gpx_file"
	bl_description = "Select and import a GPX file (.gpx)"
	bl_label = "Import GPX"
	bl_options = {"UNDO"}

	# File browser properties
	filepath: StringProperty(
		name="File Path",
		description="Path to the GPX file",
		maxlen=1024,
		subtype='FILE_PATH',
	)

	filename_ext = ".gpx"

	filter_glob: StringProperty(
		default="*.gpx",
		options={'HIDDEN'},
	)

	# --- User options ---

	importTracks: BoolProperty(
		name="Tracks",
		description="Import track segments",
		default=True,
	)

	importRoutes: BoolProperty(
		name="Routes",
		description="Import route elements",
		default=True,
	)

	importWaypoints: BoolProperty(
		name="Waypoints",
		description="Import waypoint markers",
		default=True,
	)

	useElevation: BoolProperty(
		name="Use elevation",
		description="Use GPX elevation data for Z coordinate (otherwise flat at Z=0)",
		default=True,
	)

	separate: BoolProperty(
		name="Separate objects",
		description="Create a separate object for each track/route",
		default=True,
	)

	routeProfile: EnumProperty(
		name="Profile",
		description="Shape of the route geometry",
		items=[
			('FLAT', "Flat Band", "Flat ribbon on the surface"),
			('TUBE', "Tube", "Round tube/pipe"),
		],
		default='FLAT',
	)

	routeWidth: FloatProperty(
		name="Route width (m)",
		description="Width of the route geometry in meters (0 = edges only, no mesh conversion)",
		default=5.0,
		min=0.0,
		max=100.0,
	)

	curveResolution: FloatProperty(
		name="Curve smoothing",
		description="Subdivision cuts per segment — higher = smoother curve (0 = no smoothing)",
		default=2.0,
		min=0.0,
		max=10.0,
	)

	snapToTerrain: BoolProperty(
		name="Snap to terrain",
		description="Snap route to the terrain mesh in the scene (exported basemap)",
		default=True,
	)

	# ---------------------------------------------------------------------------

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def draw(self, context):
		layout = self.layout
		layout.label(text="Elements:")
		row = layout.row(align=True)
		row.prop(self, 'importTracks', toggle=True)
		row.prop(self, 'importRoutes', toggle=True)
		row.prop(self, 'importWaypoints', toggle=True)
		layout.separator()
		layout.prop(self, 'useElevation')
		layout.prop(self, 'separate')
		layout.prop(self, 'routeProfile')
		layout.prop(self, 'routeWidth')
		layout.prop(self, 'curveResolution')
		layout.prop(self, 'snapToTerrain')

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

		# --- Parse GPX ----------------------------------------------------------
		try:
			gpx_data = _parse_gpx(self.filepath)
		except Exception as e:
			log.error("Failed to parse GPX", exc_info=True)
			self.report({'ERROR'}, "Failed to parse GPX: " + str(e))
			return {'CANCELLED'}

		n_tracks = len(gpx_data['tracks'])
		n_routes = len(gpx_data['routes'])
		n_wpts = len(gpx_data['waypoints'])
		log.info("GPX: %d tracks, %d routes, %d waypoints", n_tracks, n_routes, n_wpts)

		if n_tracks == 0 and n_routes == 0 and n_wpts == 0:
			self.report({'WARNING'}, "GPX file contains no data")
			return {'CANCELLED'}

		# --- Scene CRS / origin -------------------------------------------------
		scn = context.scene
		geoscn = GeoScene(scn)

		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		# Auto-set CRS from first coordinate
		# Use EPSG:3857 (Web Mercator) instead of UTM to stay compatible with
		# basemap tiles — avoids the need for GDAL raster reprojection.
		first = _first_coord_gpx(gpx_data)
		if first is None:
			self.report({'ERROR'}, "GPX contains no usable coordinates")
			return {'CANCELLED'}

		if not geoscn.hasCRS:
			geoscn.crs = 'EPSG:3857'
			log.info("Auto-set scene CRS to EPSG:3857 (Web Mercator)")

		if not geoscn.hasOriginPrj:
			lon, lat = first
			x, y = reprojPt(4326, geoscn.crs, lon, lat)
			geoscn.setOriginPrj(x, y)

		dstCRS = geoscn.crs

		# Init reprojector (EPSG:4326 -> scene CRS)
		try:
			rprj = Reproj(4326, dstCRS)
		except Exception:
			log.error("Unable to initialise reprojection", exc_info=True)
			self.report({'ERROR'}, "Unable to reproject data – check logs")
			return {'CANCELLED'}

		dx, dy = geoscn.crsx, geoscn.crsy

		# --- Collection ---------------------------------------------------------
		gpx_name = os.path.splitext(os.path.basename(self.filepath))[0]
		collection = bpy.data.collections.new(gpx_name)
		scn.collection.children.link(collection)

		created_objects = []

		# --- Profile enum → int -------------------------------------------------
		profile_int = 1 if self.routeProfile == 'TUBE' else 0

		# --- Find terrain mesh for snap -----------------------------------------
		terrain_obj = None
		if self.snapToTerrain:
			for obj in scn.objects:
				if obj.type == 'MESH' and obj.name.startswith('EXPORT_'):
					terrain_obj = obj
					break
			if terrain_obj is None:
				for obj in scn.objects:
					if obj.type == 'MESH' and any(k in obj.name.lower() for k in ('terrain', 'dem', 'srtm', 'elevation')):
						terrain_obj = obj
						break
			if terrain_obj:
				log.info("Will snap GPX to terrain: %s", terrain_obj.name)
			else:
				log.info("No terrain mesh found for snap")

		# Helper: reproject and shift a list of (lon, lat, ele) points
		def reproject_points(points):
			pts_raw = [(p[0], p[1]) for p in points]
			pts_prj = rprj.pts(pts_raw)
			if self.useElevation:
				return [(p[0] - dx, p[1] - dy, points[i][2]) for i, p in enumerate(pts_prj)]
			else:
				return [(p[0] - dx, p[1] - dy, 0.0) for p in pts_prj]

		# Helper: build a line object from 3D points
		def make_line_object(name, pts_3d, parent_collection):
			bm = bmesh.new()
			verts = [bm.verts.new(pt) for pt in pts_3d]
			for i in range(len(verts) - 1):
				bm.edges.new([verts[i], verts[i + 1]])

			mesh = bpy.data.meshes.new(name)
			bm.to_mesh(mesh)
			bm.free()
			mesh.update()

			obj = bpy.data.objects.new(name, mesh)
			parent_collection.objects.link(obj)
			obj.select_set(True)

			# Apply terrain snap + route width GN
			if self.routeWidth > 0:
				_apply_route_geonodes(obj, self.routeWidth, self.curveResolution, profile_int, terrain_obj)
			elif terrain_obj is not None:
				_apply_route_geonodes(obj, 0, self.curveResolution, profile_int, terrain_obj)

			return obj

		# --- Import Tracks ------------------------------------------------------
		if self.importTracks and n_tracks > 0:
			if self.separate:
				trk_col = bpy.data.collections.new('Tracks')
				collection.children.link(trk_col)
			else:
				trk_col = collection

			merged_bm = None if self.separate else bmesh.new()
			track_idx = 0

			for trk in gpx_data['tracks']:
				trk_name = trk['name'] or f"Track {track_idx + 1}"
				track_idx += 1

				for seg_idx, seg_pts in enumerate(trk['segments']):
					if len(seg_pts) < 2:
						continue

					try:
						pts_3d = reproject_points(seg_pts)
					except Exception:
						log.warning("Reprojection failed for track %s seg %d", trk_name, seg_idx)
						continue

					if self.separate:
						seg_name = trk_name if len(trk['segments']) == 1 else f"{trk_name} seg{seg_idx + 1}"
						obj = make_line_object(seg_name, pts_3d, trk_col)
						obj['gpx_type'] = 'track'
						obj['gpx_name'] = trk_name
						created_objects.append(obj)
					else:
						# Accumulate into merged bmesh
						verts = [merged_bm.verts.new(pt) for pt in pts_3d]
						for i in range(len(verts) - 1):
							merged_bm.edges.new([verts[i], verts[i + 1]])

			# Finalise merged tracks
			if not self.separate and merged_bm is not None:
				if len(merged_bm.verts) > 0:
					mesh = bpy.data.meshes.new('Tracks')
					merged_bm.to_mesh(mesh)
					mesh.update()
					obj = bpy.data.objects.new('Tracks', mesh)
					trk_col.objects.link(obj)
					obj.select_set(True)
					if self.routeWidth > 0:
						_apply_route_geonodes(obj, self.routeWidth, self.curveResolution, profile_int, terrain_obj)
					elif terrain_obj is not None:
						_apply_route_geonodes(obj, 0, self.curveResolution, profile_int, terrain_obj)
					created_objects.append(obj)
				merged_bm.free()

		# --- Import Routes ------------------------------------------------------
		if self.importRoutes and n_routes > 0:
			if self.separate:
				rte_col = bpy.data.collections.new('Routes')
				collection.children.link(rte_col)
			else:
				rte_col = collection

			merged_bm = None if self.separate else bmesh.new()

			for rte_idx, rte in enumerate(gpx_data['routes']):
				rte_name = rte['name'] or f"Route {rte_idx + 1}"
				if len(rte['points']) < 2:
					continue

				try:
					pts_3d = reproject_points(rte['points'])
				except Exception:
					log.warning("Reprojection failed for route %s", rte_name)
					continue

				if self.separate:
					obj = make_line_object(rte_name, pts_3d, rte_col)
					obj['gpx_type'] = 'route'
					obj['gpx_name'] = rte_name
					created_objects.append(obj)
				else:
					verts = [merged_bm.verts.new(pt) for pt in pts_3d]
					for i in range(len(verts) - 1):
						merged_bm.edges.new([verts[i], verts[i + 1]])

			if not self.separate and merged_bm is not None:
				if len(merged_bm.verts) > 0:
					mesh = bpy.data.meshes.new('Routes')
					merged_bm.to_mesh(mesh)
					mesh.update()
					obj = bpy.data.objects.new('Routes', mesh)
					rte_col.objects.link(obj)
					obj.select_set(True)
					if self.routeWidth > 0:
						_apply_route_geonodes(obj, self.routeWidth, self.curveResolution, profile_int, terrain_obj)
					elif terrain_obj is not None:
						_apply_route_geonodes(obj, 0, self.curveResolution, profile_int, terrain_obj)
					created_objects.append(obj)
				merged_bm.free()

		# --- Import Waypoints ---------------------------------------------------
		if self.importWaypoints and n_wpts > 0:
			wpt_col = bpy.data.collections.new('Waypoints')
			collection.children.link(wpt_col)

			pts_raw = [(w[0], w[1]) for w in gpx_data['waypoints']]
			try:
				pts_prj = rprj.pts(pts_raw)
			except Exception:
				log.warning("Reprojection failed for waypoints")
				pts_prj = []

			for i, p in enumerate(pts_prj):
				wpt = gpx_data['waypoints'][i]
				wpt_name = wpt[3] or f"WPT {i + 1}"
				ele = wpt[2] if self.useElevation else 0.0

				# Create empty as waypoint marker
				empty = bpy.data.objects.new(wpt_name, None)
				empty.location = (p[0] - dx, p[1] - dy, ele)
				empty.empty_display_type = 'PLAIN_AXES'
				empty.empty_display_size = 10.0
				empty['gpx_type'] = 'waypoint'
				empty['gpx_name'] = wpt_name
				empty['gpx_ele'] = ele
				wpt_col.objects.link(empty)
				empty.select_set(True)
				created_objects.append(empty)

		# --- Finish -------------------------------------------------------------
		total = len(created_objects)
		if total == 0:
			self.report({'WARNING'}, "No geometry imported from GPX file")
			# Clean up empty collection
			bpy.data.collections.remove(collection)
			return {'CANCELLED'}

		bbox = getBBOX.fromScn(scn)
		adjust3Dview(context, bbox)

		msg = f"Imported GPX: {n_tracks} track(s), {n_routes} route(s), {n_wpts} waypoint(s)"
		self.report({'INFO'}, msg)
		log.info(msg)

		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
	IMPORTGIS_OT_gpx_file,
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
