import os
import time
import json
import random

import logging
log = logging.getLogger(__name__)

import bpy
import bmesh
from bpy.types import Operator, Panel, AddonPreferences
from bpy.props import StringProperty, IntProperty, FloatProperty, BoolProperty, EnumProperty, FloatVectorProperty

from .lib.osm import overpy

from ..geoscene import GeoScene
from .utils import adjust3Dview, getBBOX, DropToGround, isTopView

from ..core.proj import Reproj, reprojBbox, reprojPt, utm
from ..core.utils import perf_clock

from ..core import settings
USER_AGENT = settings.user_agent

PKG, SUBPKG = __package__.split('.', maxsplit=1)

#WARNING: There is a known bug with using an enum property with a callback, Python must keep a reference to the strings returned
#https://developer.blender.org/T48873
#https://developer.blender.org/T38489
def getTags():
	prefs = bpy.context.preferences.addons[PKG].preferences
	tags = json.loads(prefs.osmTagsJson)
	return tags

#Global variable that will be seed by getTags() at each operator invoke
#then callback of dynamic enum will use this global variable
OSMTAGS = []



closedWaysArePolygons = ['aeroway', 'amenity', 'boundary', 'building', 'craft', 'geological', 'historic', 'landuse', 'leisure', 'military', 'natural', 'office', 'place', 'shop' , 'sport', 'tourism']
closedWaysAreExtruded = ['building']

#Street width by highway type (meters)
HIGHWAY_WIDTHS = {
	'motorway': 12, 'motorway_link': 8,
	'trunk': 10, 'trunk_link': 7,
	'primary': 8, 'primary_link': 6,
	'secondary': 7, 'secondary_link': 5,
	'tertiary': 6, 'tertiary_link': 5,
	'residential': 5, 'living_street': 4,
	'service': 3, 'unclassified': 5,
	'pedestrian': 3, 'footway': 2, 'path': 1.5,
	'cycleway': 2, 'track': 3, 'steps': 2,
}
DEFAULT_STREET_WIDTH = 4


def queryBuilder(bbox, tags=['building', 'highway'], types=['node', 'way', 'relation'], format='json'):

		'''
		QL template syntax :
		[out:json][bbox:ymin,xmin,ymax,xmax];(node[tag1];node[tag2];((way[tag1];way[tag2];);>;);relation;);out;
		'''

		#s,w,n,e <--> ymin,xmin,ymax,xmax
		bboxStr = ','.join(map(str, bbox.toLatlon()))

		if not types:
			#if no type filter is defined then just select all kind of type
			types = ['node', 'way', 'relation']

		head = "[out:"+format+"][bbox:"+bboxStr+"];"

		union = '('
		#all tagged nodes
		if 'node' in types:
			if tags:
				union += ';'.join( ['node['+tag+']' for tag in tags] ) + ';'
			else:
				union += 'node;'
		#all tagged ways with all their nodes (recurse down)
		if 'way' in types:
			union += '(('
			if tags:
				union += ';'.join( ['way['+tag+']' for tag in tags] ) + ';);'
			else:
				union += 'way;);'
			union += '>;);'
		#all relations (no filter tag applied)
		if 'relation' in types or 'rel' in types:
			union += 'relation;'
		union += ')'

		output = ';out;'
		qry = head + union + output

		return qry





########################

def _get_or_create_building_geonodes():
	"""Create a Geometry Nodes group for building extrusion from 'height' attribute.
	Stores a per-building random ID before extrusion (for consistent shader variation),
	and assigns material index 1 to extruded side faces."""
	name = 'OSM Building Extrusion'
	if name in bpy.data.node_groups:
		return bpy.data.node_groups[name]

	group = bpy.data.node_groups.new(name, 'GeometryNodeTree')

	# Interface sockets
	group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
	s_mult = group.interface.new_socket(name='Height Multiplier', in_out='INPUT', socket_type='NodeSocketFloat')
	s_mult.default_value = 1.0
	s_mult.min_value = 0.0
	s_mult.max_value = 10.0
	group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')

	nodes = group.nodes
	links = group.links

	# Group Input / Output
	n_in = nodes.new('NodeGroupInput')
	n_in.location = (-600, 0)
	n_out = nodes.new('NodeGroupOutput')
	n_out.location = (600, 0)

	# --- Store per-building random ID (before extrusion, each face = one building) ---
	n_random = nodes.new('FunctionNodeRandomValue')
	n_random.data_type = 'FLOAT'
	n_random.location = (-450, -350)
	n_random.inputs[8].default_value = 42  # Seed

	n_store_id = nodes.new('GeometryNodeStoreNamedAttribute')
	n_store_id.data_type = 'FLOAT'
	n_store_id.domain = 'FACE'
	n_store_id.location = (-250, 0)
	n_store_id.inputs[2].default_value = "building_id"  # Name
	links.new(n_in.outputs['Geometry'], n_store_id.inputs[0])  # Geometry
	links.new(n_random.outputs[1], n_store_id.inputs[3])  # Value (float output)

	# Named Attribute → read "height" per face
	n_attr = nodes.new('GeometryNodeInputNamedAttribute')
	n_attr.data_type = 'FLOAT'
	n_attr.inputs['Name'].default_value = 'height'
	n_attr.location = (-600, -200)

	# Multiply height by multiplier
	n_mult = nodes.new('ShaderNodeMath')
	n_mult.operation = 'MULTIPLY'
	n_mult.location = (-100, -150)
	links.new(n_attr.outputs['Attribute'], n_mult.inputs[0])
	links.new(n_in.outputs['Height Multiplier'], n_mult.inputs[1])

	# Combine XYZ → offset vector (0, 0, height)
	n_xyz = nodes.new('ShaderNodeCombineXYZ')
	n_xyz.location = (50, -150)
	links.new(n_mult.outputs[0], n_xyz.inputs['Z'])

	# Selection: only extrude faces where height > 0
	n_gt = nodes.new('FunctionNodeCompare')
	n_gt.data_type = 'FLOAT'
	n_gt.operation = 'GREATER_THAN'
	n_gt.location = (-100, -300)
	links.new(n_attr.outputs['Attribute'], n_gt.inputs['A'])
	n_gt.inputs['B'].default_value = 0.0

	# Extrude Mesh (Individual Faces)
	n_ext = nodes.new('GeometryNodeExtrudeMesh')
	n_ext.mode = 'FACES'
	n_ext.location = (200, 0)
	n_ext.inputs['Individual'].default_value = True
	links.new(n_store_id.outputs[0], n_ext.inputs['Mesh'])  # From Store Named Attribute
	links.new(n_gt.outputs['Result'], n_ext.inputs['Selection'])
	links.new(n_xyz.outputs['Vector'], n_ext.inputs['Offset'])

	# Set Material Index = 1 on side faces
	n_setmat = nodes.new('GeometryNodeSetMaterialIndex')
	n_setmat.location = (400, 0)
	links.new(n_ext.outputs['Mesh'], n_setmat.inputs['Geometry'])
	links.new(n_ext.outputs['Side'], n_setmat.inputs['Selection'])
	n_setmat.inputs['Material Index'].default_value = 1

	# --- Roof shape extrusion ---
	# Read Named Attribute "roof_shape" (INT)
	n_roof_shape = nodes.new('GeometryNodeInputNamedAttribute')
	n_roof_shape.data_type = 'INT'
	n_roof_shape.inputs['Name'].default_value = 'roof_shape'
	n_roof_shape.location = (400, -400)

	# Read Named Attribute "roof_height" (FLOAT)
	n_roof_height = nodes.new('GeometryNodeInputNamedAttribute')
	n_roof_height.data_type = 'FLOAT'
	n_roof_height.inputs['Name'].default_value = 'roof_height'
	n_roof_height.location = (400, -550)

	# Selection: roof_shape > 0 AND Top face from first extrude
	n_rs_gt = nodes.new('FunctionNodeCompare')
	n_rs_gt.data_type = 'INT'
	n_rs_gt.operation = 'GREATER_THAN'
	n_rs_gt.location = (600, -400)
	links.new(n_roof_shape.outputs['Attribute'], n_rs_gt.inputs[2])  # A (INT)
	n_rs_gt.inputs[3].default_value = 0  # B (INT)

	# AND: roof_shape > 0 AND Top (from first extrude)
	n_roof_and = nodes.new('FunctionNodeBooleanMath')
	n_roof_and.operation = 'AND'
	n_roof_and.location = (600, -250)
	links.new(n_rs_gt.outputs['Result'], n_roof_and.inputs[0])
	links.new(n_ext.outputs['Top'], n_roof_and.inputs[1])

	# Combine XYZ for roof offset (0, 0, roof_height)
	n_roof_xyz = nodes.new('ShaderNodeCombineXYZ')
	n_roof_xyz.location = (600, -550)
	links.new(n_roof_height.outputs['Attribute'], n_roof_xyz.inputs['Z'])

	# Second Extrude: push top faces up by roof_height
	n_ext_roof = nodes.new('GeometryNodeExtrudeMesh')
	n_ext_roof.mode = 'FACES'
	n_ext_roof.location = (800, 0)
	n_ext_roof.inputs['Individual'].default_value = True
	links.new(n_setmat.outputs['Geometry'], n_ext_roof.inputs['Mesh'])
	links.new(n_roof_and.outputs['Result'], n_ext_roof.inputs['Selection'])
	links.new(n_roof_xyz.outputs['Vector'], n_ext_roof.inputs['Offset'])

	# Scale the new roof-top faces inward to create a peak
	n_roof_scale = nodes.new('GeometryNodeScaleElements')
	n_roof_scale.domain = 'FACE'
	n_roof_scale.location = (1000, 0)
	links.new(n_ext_roof.outputs['Mesh'], n_roof_scale.inputs['Geometry'])
	links.new(n_ext_roof.outputs['Top'], n_roof_scale.inputs['Selection'])
	n_roof_scale.inputs['Scale'].default_value = 0.1

	# Set Material Index = 2 on roof side faces (optional: distinguish roof sides)
	n_setmat_roof = nodes.new('GeometryNodeSetMaterialIndex')
	n_setmat_roof.location = (1200, 0)
	links.new(n_roof_scale.outputs['Geometry'], n_setmat_roof.inputs['Geometry'])
	links.new(n_ext_roof.outputs['Side'], n_setmat_roof.inputs['Selection'])
	n_setmat_roof.inputs['Material Index'].default_value = 0  # Roof sides get rooftop material

	# Move output node further right
	n_out.location = (1400, 0)

	# Output
	links.new(n_setmat_roof.outputs['Geometry'], n_out.inputs['Geometry'])

	return group


def _get_or_create_rooftop_material():
	"""Create a simple default rooftop material (slot 0).
	Can be replaced later with satellite texture projection."""
	name = 'OSM_Rooftop_Satellite'
	if name in bpy.data.materials:
		return bpy.data.materials[name]

	mat = bpy.data.materials.new(name)
	mat.use_nodes = True
	tree = mat.node_tree
	bsdf = tree.nodes['Principled BSDF']
	bsdf.inputs['Base Color'].default_value = (0.5, 0.5, 0.5, 1.0)
	bsdf.inputs['Roughness'].default_value = 0.9
	return mat


def _get_or_create_facade_material():
	"""Create the procedural facade shader (slot 1) with tangent-projected window grid.
	Uses True Normal for correct projection on all wall orientations,
	and FLOORED_MODULO to handle negative coordinate values."""
	name = 'OSM_Facade_Procedural'
	if name in bpy.data.materials:
		return bpy.data.materials[name]

	mat = bpy.data.materials.new(name)
	mat.use_nodes = True
	tree = mat.node_tree
	tree.nodes.clear()

	# --- Core nodes ---
	n_output = tree.nodes.new('ShaderNodeOutputMaterial')
	n_output.location = (1100, 0)

	n_bsdf = tree.nodes.new('ShaderNodeBsdfPrincipled')
	n_bsdf.location = (900, 0)
	tree.links.new(n_bsdf.outputs['BSDF'], n_output.inputs['Surface'])

	# Geometry → True Normal
	n_geom = tree.nodes.new('ShaderNodeNewGeometry')
	n_geom.location = (-800, 200)

	n_sep_normal = tree.nodes.new('ShaderNodeSeparateXYZ')
	n_sep_normal.location = (-600, 200)
	tree.links.new(n_geom.outputs['True Normal'], n_sep_normal.inputs['Vector'])

	# Texture Coordinate → Object position
	n_texcoord = tree.nodes.new('ShaderNodeTexCoord')
	n_texcoord.location = (-800, -100)

	n_sep_pos = tree.nodes.new('ShaderNodeSeparateXYZ')
	n_sep_pos.location = (-600, -100)
	tree.links.new(n_texcoord.outputs['Object'], n_sep_pos.inputs['Vector'])

	# --- Tangent projection: h = Y*nx - X*ny ---
	n_y_nx = tree.nodes.new('ShaderNodeMath')
	n_y_nx.operation = 'MULTIPLY'
	n_y_nx.location = (-400, 100)
	tree.links.new(n_sep_pos.outputs['Y'], n_y_nx.inputs[0])
	tree.links.new(n_sep_normal.outputs['X'], n_y_nx.inputs[1])

	n_x_ny = tree.nodes.new('ShaderNodeMath')
	n_x_ny.operation = 'MULTIPLY'
	n_x_ny.location = (-400, -50)
	tree.links.new(n_sep_pos.outputs['X'], n_x_ny.inputs[0])
	tree.links.new(n_sep_normal.outputs['Y'], n_x_ny.inputs[1])

	n_h = tree.nodes.new('ShaderNodeMath')
	n_h.operation = 'SUBTRACT'
	n_h.location = (-200, 50)
	tree.links.new(n_y_nx.outputs[0], n_h.inputs[0])
	tree.links.new(n_x_ny.outputs[0], n_h.inputs[1])

	# --- Horizontal window grid: h_frac = FLOORED_MODULO(h, 2.5) / 2.5 ---
	n_h_mod = tree.nodes.new('ShaderNodeMath')
	n_h_mod.operation = 'FLOORED_MODULO'
	n_h_mod.location = (-50, 100)
	n_h_mod.inputs[1].default_value = 2.5
	tree.links.new(n_h.outputs[0], n_h_mod.inputs[0])

	n_h_frac = tree.nodes.new('ShaderNodeMath')
	n_h_frac.operation = 'DIVIDE'
	n_h_frac.location = (100, 100)
	n_h_frac.inputs[1].default_value = 2.5
	tree.links.new(n_h_mod.outputs[0], n_h_frac.inputs[0])

	# --- Vertical window grid: z_frac = FLOORED_MODULO(Z, 3.0) / 3.0 ---
	n_z_mod = tree.nodes.new('ShaderNodeMath')
	n_z_mod.operation = 'FLOORED_MODULO'
	n_z_mod.location = (-50, -100)
	n_z_mod.inputs[1].default_value = 3.0
	tree.links.new(n_sep_pos.outputs['Z'], n_z_mod.inputs[0])

	n_z_frac = tree.nodes.new('ShaderNodeMath')
	n_z_frac.operation = 'DIVIDE'
	n_z_frac.location = (100, -100)
	n_z_frac.inputs[1].default_value = 3.0
	tree.links.new(n_z_mod.outputs[0], n_z_frac.inputs[0])

	# --- Window mask: horizontal (0.15 .. 0.85) × vertical (0.1 .. 0.9) ---
	n_h_gt = tree.nodes.new('ShaderNodeMath')
	n_h_gt.operation = 'GREATER_THAN'
	n_h_gt.location = (250, 150)
	n_h_gt.inputs[1].default_value = 0.15
	tree.links.new(n_h_frac.outputs[0], n_h_gt.inputs[0])

	n_h_lt = tree.nodes.new('ShaderNodeMath')
	n_h_lt.operation = 'LESS_THAN'
	n_h_lt.location = (250, 50)
	n_h_lt.inputs[1].default_value = 0.85
	tree.links.new(n_h_frac.outputs[0], n_h_lt.inputs[0])

	n_h_mask = tree.nodes.new('ShaderNodeMath')
	n_h_mask.operation = 'MULTIPLY'
	n_h_mask.location = (400, 100)
	tree.links.new(n_h_gt.outputs[0], n_h_mask.inputs[0])
	tree.links.new(n_h_lt.outputs[0], n_h_mask.inputs[1])

	n_z_gt = tree.nodes.new('ShaderNodeMath')
	n_z_gt.operation = 'GREATER_THAN'
	n_z_gt.location = (250, -50)
	n_z_gt.inputs[1].default_value = 0.1
	tree.links.new(n_z_frac.outputs[0], n_z_gt.inputs[0])

	n_z_lt = tree.nodes.new('ShaderNodeMath')
	n_z_lt.operation = 'LESS_THAN'
	n_z_lt.location = (250, -150)
	n_z_lt.inputs[1].default_value = 0.9
	tree.links.new(n_z_frac.outputs[0], n_z_lt.inputs[0])

	n_z_mask = tree.nodes.new('ShaderNodeMath')
	n_z_mask.operation = 'MULTIPLY'
	n_z_mask.location = (400, -100)
	tree.links.new(n_z_gt.outputs[0], n_z_mask.inputs[0])
	tree.links.new(n_z_lt.outputs[0], n_z_mask.inputs[1])

	n_window = tree.nodes.new('ShaderNodeMath')
	n_window.operation = 'MULTIPLY'
	n_window.location = (550, 0)
	tree.links.new(n_h_mask.outputs[0], n_window.inputs[0])
	tree.links.new(n_z_mask.outputs[0], n_window.inputs[1])

	# --- Output: wall/window color mix ---
	n_mix_color = tree.nodes.new('ShaderNodeMix')
	n_mix_color.data_type = 'RGBA'
	n_mix_color.location = (700, 100)
	n_mix_color.inputs['A'].default_value = (0.75, 0.70, 0.62, 1.0)  # Wall color
	n_mix_color.inputs['B'].default_value = (0.05, 0.07, 0.12, 1.0)  # Window color
	tree.links.new(n_window.outputs[0], n_mix_color.inputs['Factor'])
	tree.links.new(n_mix_color.outputs['Result'], n_bsdf.inputs['Base Color'])

	# Roughness: wall=0.8, window=0.1
	n_mix_rough = tree.nodes.new('ShaderNodeMix')
	n_mix_rough.data_type = 'FLOAT'
	n_mix_rough.location = (700, -100)
	n_mix_rough.inputs['A'].default_value = 0.8
	n_mix_rough.inputs['B'].default_value = 0.1
	tree.links.new(n_window.outputs[0], n_mix_rough.inputs['Factor'])
	tree.links.new(n_mix_rough.outputs['Result'], n_bsdf.inputs['Roughness'])

	return mat


def _apply_building_geonodes(obj):
	"""Add the building extrusion Geometry Nodes modifier and materials to an object."""
	group = _get_or_create_building_geonodes()
	mod = obj.modifiers.new('Building Extrusion', type='NODES')
	mod.node_group = group

	# Assign materials: slot 0 = rooftop, slot 1 = facade
	mat_roof = _get_or_create_rooftop_material()
	mat_facade = _get_or_create_facade_material()
	if mat_roof.name not in [s.material.name for s in obj.material_slots if s.material]:
		obj.data.materials.append(mat_roof)
	if mat_facade.name not in [s.material.name for s in obj.material_slots if s.material]:
		obj.data.materials.append(mat_facade)


def _get_or_create_street_geonodes():
	"""Create a Geometry Nodes group for street width from 'width' attribute."""
	name = 'OSM Street Width'
	if name in bpy.data.node_groups:
		return bpy.data.node_groups[name]

	group = bpy.data.node_groups.new(name, 'GeometryNodeTree')

	# Interface sockets
	group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
	s_mult = group.interface.new_socket(name='Width Multiplier', in_out='INPUT', socket_type='NodeSocketFloat')
	s_mult.default_value = 1.0
	s_mult.min_value = 0.0
	s_mult.max_value = 10.0
	s_merge = group.interface.new_socket(name='Merge Distance', in_out='INPUT', socket_type='NodeSocketFloat')
	s_merge.default_value = 0.0
	s_merge.min_value = 0.0
	s_merge.max_value = 1.0
	group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')

	nodes = group.nodes
	links = group.links

	# Group Input / Output
	n_in = nodes.new('NodeGroupInput')
	n_in.location = (-700, 0)
	n_out = nodes.new('NodeGroupOutput')
	n_out.location = (500, 0)

	# Named Attribute → read "width" per vertex
	n_attr = nodes.new('GeometryNodeInputNamedAttribute')
	n_attr.data_type = 'FLOAT'
	n_attr.inputs['Name'].default_value = 'width'
	n_attr.location = (-700, -200)

	# width * multiplier * 0.5 (profile spans -1 to +1 = 2 units)
	n_mult = nodes.new('ShaderNodeMath')
	n_mult.operation = 'MULTIPLY'
	n_mult.location = (-450, -200)
	links.new(n_attr.outputs[0], n_mult.inputs[0])
	links.new(n_in.outputs[1], n_mult.inputs[1])  # Width Multiplier

	n_half = nodes.new('ShaderNodeMath')
	n_half.operation = 'MULTIPLY'
	n_half.inputs[1].default_value = 0.5
	n_half.location = (-250, -200)
	links.new(n_mult.outputs[0], n_half.inputs[0])

	# Mesh to Curve
	n_m2c = nodes.new('GeometryNodeMeshToCurve')
	n_m2c.location = (-300, 0)
	links.new(n_in.outputs[0], n_m2c.inputs[0])  # Geometry

	# Profile: line from (-1, 0, 0) to (1, 0, 0)
	n_line = nodes.new('GeometryNodeCurvePrimitiveLine')
	n_line.location = (-100, -300)
	n_line.inputs['Start'].default_value = (-1.0, 0.0, 0.0)
	n_line.inputs['End'].default_value = (1.0, 0.0, 0.0)

	# Curve to Mesh — use Scale input for width
	n_c2m = nodes.new('GeometryNodeCurveToMesh')
	n_c2m.location = (50, 0)
	links.new(n_m2c.outputs[0], n_c2m.inputs[0])   # Curve
	links.new(n_line.outputs[0], n_c2m.inputs[1])   # Profile Curve
	links.new(n_half.outputs[0], n_c2m.inputs[2])   # Scale

	# Merge by Distance
	n_merge = nodes.new('GeometryNodeMergeByDistance')
	n_merge.location = (300, 0)
	links.new(n_c2m.outputs[0], n_merge.inputs[0])  # Geometry
	links.new(n_in.outputs[2], n_merge.inputs[2])    # Merge Distance

	# Output
	links.new(n_merge.outputs[0], n_out.inputs[0])

	return group


def _apply_street_geonodes(obj):
	"""Add the street width Geometry Nodes modifier to an object."""
	group = _get_or_create_street_geonodes()
	mod = obj.modifiers.new('Street Width', type='NODES')
	mod.node_group = group


def _get_or_create_terrain_snap_geonodes():
	"""Create a Geometry Nodes group that snaps vertices onto a terrain mesh via raycast.
	Vertices that miss the raycast (outside terrain extent) use the mean Z of hit vertices
	as fallback to prevent dangling geometry. Per-face Z averaging ensures flat roofs
	on sloped terrain (Evaluate on Domain → Face)."""
	name = 'OSM Snap to Terrain'
	if name in bpy.data.node_groups:
		return bpy.data.node_groups[name]

	group = bpy.data.node_groups.new(name, 'GeometryNodeTree')

	# Interface
	group.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
	group.interface.new_socket(name='Terrain', in_out='INPUT', socket_type='NodeSocketObject')
	s_off = group.interface.new_socket(name='Z Offset', in_out='INPUT', socket_type='NodeSocketFloat')
	s_off.default_value = 0.1
	s_off.min_value = -100.0
	s_off.max_value = 100.0
	group.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')

	nodes = group.nodes
	links = group.links

	n_in = nodes.new('NodeGroupInput'); n_in.location = (-900, 0)
	n_out = nodes.new('NodeGroupOutput'); n_out.location = (800, 0)

	# Object Info → terrain geometry
	n_objinfo = nodes.new('GeometryNodeObjectInfo')
	n_objinfo.transform_space = 'RELATIVE'
	n_objinfo.location = (-700, -300)
	links.new(n_in.outputs[1], n_objinfo.inputs['Object'])

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
	links.new(n_in.outputs[0], n_stat.inputs['Geometry'])
	links.new(n_ray.outputs['Is Hit'], n_stat.inputs['Selection'])
	links.new(n_hit_sep.outputs['Z'], n_stat.inputs[2])  # Attribute

	# Switch: hit → hit_Z, miss → mean_Z
	n_switch = nodes.new('GeometryNodeSwitch')
	n_switch.input_type = 'FLOAT'
	n_switch.location = (350, -300)
	links.new(n_ray.outputs['Is Hit'], n_switch.inputs[0])
	links.new(n_stat.outputs['Mean'], n_switch.inputs[1])   # False: mean Z
	links.new(n_hit_sep.outputs['Z'], n_switch.inputs[2])   # True: hit Z

	# Evaluate on Domain (Face) → all verts of a face get same Z (flat roofs)
	n_eod = nodes.new('GeometryNodeFieldOnDomain')
	n_eod.domain = 'FACE'
	n_eod.location = (500, -300)
	links.new(n_switch.outputs[0], n_eod.inputs[0])

	# Add Z offset
	n_add = nodes.new('ShaderNodeMath')
	n_add.operation = 'ADD'
	n_add.location = (600, -200)
	links.new(n_eod.outputs[0], n_add.inputs[0])
	links.new(n_in.outputs[2], n_add.inputs[1])

	# New position (orig X, orig Y, final Z)
	n_new_pos = nodes.new('ShaderNodeCombineXYZ')
	n_new_pos.location = (600, -50)
	links.new(n_sep.outputs['X'], n_new_pos.inputs['X'])
	links.new(n_sep.outputs['Y'], n_new_pos.inputs['Y'])
	links.new(n_add.outputs[0], n_new_pos.inputs['Z'])

	# Set Position on ALL vertices
	n_setpos = nodes.new('GeometryNodeSetPosition')
	n_setpos.location = (750, 100)
	links.new(n_in.outputs[0], n_setpos.inputs['Geometry'])
	links.new(n_new_pos.outputs[0], n_setpos.inputs['Position'])

	links.new(n_setpos.outputs[0], n_out.inputs[0])
	return group


def _apply_terrain_snap(obj, terrain_obj):
	"""Add Snap to Terrain modifier as first modifier on an object."""
	group = _get_or_create_terrain_snap_geonodes()
	mod = obj.modifiers.new('Snap to Terrain', type='NODES')
	mod.node_group = group
	# Set terrain object
	for item in group.interface.items_tree:
		if item.name == 'Terrain':
			mod[item.identifier] = terrain_obj
	# Move to top of modifier stack
	while obj.modifiers.find(mod.name) > 0:
		bpy.ops.object.modifier_move_up({'object': obj}, modifier=mod.name)


########################
_join_buffer = None

def joinBmesh(src_bm, dest_bm):
	'''
	Hack to join a bmesh to another
	TODO: replace this function by bmesh.ops.duplicate when 'dest' argument will be implemented
	'''
	global _join_buffer
	if _join_buffer is None or _join_buffer.name not in bpy.data.meshes:
		_join_buffer = bpy.data.meshes.new(".temp")
	src_bm.to_mesh(_join_buffer)
	dest_bm.from_mesh(_join_buffer)
	_join_buffer.clear_geometry()





class OSM_IMPORT():
	"""Import from Open Street Map"""

	def enumTags(self, context):
		items = []
		##prefs = context.preferences.addons[PKG].preferences
		##osmTags = json.loads(prefs.osmTagsJson)
		#we need to use a global variable as workaround to enum callback bug (T48873, T38489)
		for tag in OSMTAGS:
			#put each item in a tuple (key, label, tooltip)
			items.append( (tag, tag, tag) )
		return items

	filterTags: EnumProperty(
			name = "Tags",
			description = "Select tags to include",
			items = enumTags,
			options = {"ENUM_FLAG"})

	featureType: EnumProperty(
			name = "Type",
			description = "Select types to include",
			items = [
				('node', 'Nodes', 'Request all nodes'),
				('way', 'Ways', 'Request all ways'),
				('relation', 'Relations', 'Request all relations')
			],
			default = {'way'},
			options = {"ENUM_FLAG"}
			)

	# Elevation object
	def listObjects(self, context):
		objs = []
		for index, object in enumerate(bpy.context.scene.objects):
			if object.type == 'MESH':
				#put each object in a tuple (key, label, tooltip) and add this to the objects list
				objs.append((str(index), object.name, "Object named " + object.name))
		return objs

	objElevLst: EnumProperty(
		name="Elev. object",
		description="Choose the mesh from which extract z elevation",
		items=listObjects )

	useElevObj: BoolProperty(
			name="Elevation from object",
			description="Get z elevation value from an existing ground mesh",
			default=False )

	separate: BoolProperty(name='Separate objects', description='Warning : can be very slow with lot of features', default=False)

	buildingsExtrusion: BoolProperty(name='Buildings extrusion', description='', default=True)
	defaultHeight: FloatProperty(name='Default Height', description='Set the height value using for extrude building when the tag is missing', default=20)
	levelHeight: FloatProperty(name='Level height', description='Set a height for a building level, using for compute extrude height based on number of levels', default=3)
	randomHeightThreshold: IntProperty(name='Random height threshold', description='Threshold value for randomize default height', default=0)

	def draw(self, context):
		layout = self.layout
		row = layout.row()
		row.prop(self, "featureType", expand=True)
		row = layout.row()
		col = row.column()
		col.prop(self, "filterTags", expand=True)
		layout.prop(self, 'useElevObj')
		if self.useElevObj:
			layout.prop(self, 'objElevLst')
		layout.prop(self, 'buildingsExtrusion')
		if self.buildingsExtrusion:
			layout.prop(self, 'defaultHeight')
			layout.prop(self, 'randomHeightThreshold')
			layout.prop(self, 'levelHeight')
		layout.prop(self, 'separate')


	def build(self, context, result, dstCRS):
		prefs = context.preferences.addons[PKG].preferences
		scn = context.scene
		geoscn = GeoScene(scn)
		scale = geoscn.scale #TODO

		#Init reprojector class
		try:
			rprj = Reproj(4326, dstCRS)
		except Exception as e:
			log.error('Unable to reproject data', exc_info=True)
			self.report({'ERROR'}, "Unable to reproject data, check logs for more infos")
			return {'CANCELLED'}

		if self.useElevObj:
			if not self.objElevLst:
				log.error('There is no elevation object in the scene to get elevation from')
				self.report({'ERROR'}, "There is no elevation object in the scene to get elevation from")
				return {'CANCELLED'}
			elevObj = scn.objects[int(self.objElevLst)]
			rayCaster = DropToGround(scn, elevObj)

		bmeshes = {}
		vgroupsObj = {}

		#######
		def seed(id, tags, pts):
			'''
			Sub funtion :
				1. create a bmesh from [pts]
				2. seed a global bmesh or create a new object
			'''
			if len(pts) > 1:
				if pts[0] == pts[-1] and any(tag in closedWaysArePolygons for tag in tags):
					type = 'Areas'
					closed = True
					pts.pop() #exclude last duplicate node
				else:
					type = 'Ways'
					closed = False
			else:
				type = 'Nodes'
				closed = False

			#reproj and shift coords
			pts = rprj.pts(pts)
			dx, dy = geoscn.crsx, geoscn.crsy

			if self.useElevObj:
				#pts = [rayCaster.rayCast(v[0]-dx, v[1]-dy).loc for v in pts]
				pts = [rayCaster.rayCast(v[0]-dx, v[1]-dy) for v in pts]
				hits = [pt.hit for pt in pts]
				if not all(hits) and any(hits):
					zs = [p.loc.z for p in pts if p.hit]
					meanZ = sum(zs) / len(zs)
					for v in pts:
						if not v.hit:
							v.loc.z = meanZ
				pts = [pt.loc for pt in pts]
			else:
				pts = [ (v[0]-dx, v[1]-dy, 0) for v in pts]

			#Create a new bmesh
			#>using an intermediate bmesh object allows some extra operation like extrusion
			bm = bmesh.new()

			#Pre-create attribute layers before adding geometry (adding layers invalidates element refs)
			is_building = closed and self.buildingsExtrusion and any(tag in closedWaysAreExtruded for tag in tags)
			is_street = not closed and 'highway' in tags
			if is_building:
				height_layer = bm.faces.layers.float.new('height')
				roof_shape_layer = bm.faces.layers.int.new('roof_shape')
				roof_height_layer = bm.faces.layers.float.new('roof_height')
			if is_street:
				width_layer = bm.verts.layers.float.new('width')

			if len(pts) == 1:
				verts = [bm.verts.new(pt) for pt in pts]

			elif closed: #faces
				verts = [bm.verts.new(pt) for pt in pts]
				face = bm.faces.new(verts)
				#ensure face is up (anticlockwise order)
				#because in OSM there is no particular order for closed ways
				face.normal_update()
				if face.normal.z < 0:
					face.normal_flip()

				#Store height as face attribute for Geometry Nodes extrusion
				if is_building:
					offset = None
					if "height" in tags:
							htag = tags["height"]
							htag = htag.replace(',', '.')
							try:
								offset = int(htag)
							except ValueError:
								try:
									offset = float(htag)
								except ValueError:
									for i, c in enumerate(htag):
										if not c.isdigit():
											try:
												offset, unit = float(htag[:i]), htag[i:].strip()
											except ValueError:
												offset = None
					elif "building:levels" in tags:
						try:
							offset = int(tags["building:levels"]) * self.levelHeight
						except ValueError as e:
							offset = None

					if offset is None:
						minH = self.defaultHeight - self.randomHeightThreshold
						if minH < 0 :
							minH = 0
						maxH = self.defaultHeight + self.randomHeightThreshold
						offset = random.randint(int(minH), int(maxH))

					face[height_layer] = float(offset)

					# --- Roof shape ---
					_roof_shape_map = {
						'flat': 0,
						'gabled': 1,
						'hipped': 2,
						'pyramidal': 3,
						'skillion': 4,
					}
					_rs_tag = tags.get('roof:shape', 'flat')
					_rs_int = _roof_shape_map.get(_rs_tag, 0)
					face[roof_shape_layer] = _rs_int

					# --- Roof height ---
					_rh = None
					if 'roof:height' in tags:
						try:
							_rh = float(tags['roof:height'].replace(',', '.').replace('m', '').strip())
						except ValueError:
							_rh = None
					if _rh is None:
						# Default: 30% of building height for non-flat roofs, 0 for flat
						_rh = float(offset) * 0.3 if _rs_int > 0 else 0.0
					face[roof_height_layer] = _rh


			elif len(pts) > 1: #edge
				verts = [bm.verts.new(pt) for pt in pts]
				for i in range(len(pts)-1):
					edge = bm.edges.new( [verts[i], verts[i+1] ])
				#Store street width as vertex attribute for Geometry Nodes
				if is_street:
					hw_type = tags.get('highway', '')
					street_w = HIGHWAY_WIDTHS.get(hw_type, DEFAULT_STREET_WIDTH)
					#OSM width tag overrides default
					if 'width' in tags:
						try:
							street_w = float(tags['width'].replace('m','').replace(',','.').strip())
						except ValueError:
							pass
					for v in verts:
						v[width_layer] = street_w


			if self.separate:

				name = tags.get('name', str(id))

				mesh = bpy.data.meshes.new(name)
				bm.to_mesh(mesh)
				mesh.update()
				mesh.validate()

				obj = bpy.data.objects.new(name, mesh)

				#Add Geometry Nodes modifiers
				if self.buildingsExtrusion and any(tag in closedWaysAreExtruded for tag in tags):
					_apply_building_geonodes(obj)
				if 'highway' in tags:
					_apply_street_geonodes(obj)

				#Assign tags to custom props
				obj['id'] = str(id) #cast to str to avoid overflow error "Python int too large to convert to C int"
				for key in tags.keys():
					obj[key] = tags[key]

				#Put object in right collection
				if self.filterTags:
					tagsList = self.filterTags
				else:
					tagsList = OSMTAGS
				if any(tag in tagsList for tag in tags):
					for k in tagsList:
						if k in tags:
							try:
								tagCollec = layer.children[k]
							except KeyError:
								tagCollec = bpy.data.collections.new(k)
								layer.children.link(tagCollec)
							tagCollec.objects.link(obj)
							break
				else:
					layer.objects.link(obj)

				obj.select_set(True)


			else:
				#Grouping

				bm.verts.index_update()
				#bm.edges.index_update()
				#bm.faces.index_update()

				if self.filterTags:

					#group by tags (there could be some duplicates)
					for k in self.filterTags:

						if k in extags: #
							objName = type + ':' + k
							kbm = bmeshes.setdefault(objName, bmesh.new())
							offset = len(kbm.verts)
							joinBmesh(bm, kbm)

				else:
					#group all into one unique mesh
					objName = type
					_bm = bmeshes.setdefault(objName, bmesh.new())
					offset = len(_bm.verts)
					joinBmesh(bm, _bm)


				#vertex group
				name = tags.get('name', None)
				vidx = [v.index + offset for v in bm.verts]
				vgroups = vgroupsObj.setdefault(objName, {})

				for tag in extags:
					#if tag in osmTags:#filter
					if not tag.startswith('name'):
						vgroup = vgroups.setdefault('Tag:'+tag, [])
						vgroup.extend(vidx)

				if name is not None:
					#vgroup['Name:'+name] = [vidx]
					vgroup = vgroups.setdefault('Name:'+name, [])
					vgroup.extend(vidx)

				if 'relation' in self.featureType:
					for rel in result.relations:
						name = rel.tags.get('name', str(rel.id))
						for member in rel.members:
							#todo: remove duplicate members
							if id == member.ref:
								vgroup = vgroups.setdefault('Relation:'+name, [])
								vgroup.extend(vidx)



			bm.free()


		######

		if self.separate:
			layer = bpy.data.collections.new('OSM')
			context.scene.collection.children.link(layer)

		#Build mesh
		waysNodesId = [node.id for way in result.ways for node in way.nodes]

		if 'node' in self.featureType:

			for node in result.nodes:

				#extended tags list
				extags = list(node.tags.keys()) + [k + '=' + v for k, v in node.tags.items()]

				if node.id in waysNodesId:
					continue

				if self.filterTags and not any(tag in self.filterTags for tag in extags):
					continue

				pt = (float(node.lon), float(node.lat))
				seed(node.id, node.tags, [pt])


		if 'way' in self.featureType:

			for way in result.ways:

				extags = list(way.tags.keys()) + [k + '=' + v for k, v in way.tags.items()]

				if self.filterTags and not any(tag in self.filterTags for tag in extags):
					continue

				pts = [(float(node.lon), float(node.lat)) for node in way.nodes]
				seed(way.id, way.tags, pts)



		if not self.separate:

			for name, bm in bmeshes.items():
				if prefs.mergeDoubles:
					bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
				mesh = bpy.data.meshes.new(name)
				bm.to_mesh(mesh)
				bm.free()

				mesh.update()#calc_edges=True)
				mesh.validate()
				obj = bpy.data.objects.new(name, mesh)
				scn.collection.objects.link(obj)
				obj.select_set(True)

				#Add Geometry Nodes modifiers
				if self.buildingsExtrusion and 'building' in name.lower():
					_apply_building_geonodes(obj)
				if 'highway' in name.lower():
					_apply_street_geonodes(obj)

				vgroups = vgroupsObj.get(name, None)
				if vgroups is not None:
					#for vgroupName, vgroupIdx in vgroups.items():
					for vgroupName in sorted(vgroups.keys()):
						vgroupIdx = vgroups[vgroupName]
						g = obj.vertex_groups.new(name=vgroupName)
						g.add(vgroupIdx, weight=1, type='ADD')


		elif 'relation' in self.featureType:

			relations = bpy.data.collections.new('Relations')
			bpy.data.collections['OSM'].children.link(relations)
			importedObjects = bpy.data.collections['OSM'].objects

			for rel in result.relations:

				name = rel.tags.get('name', str(rel.id))
				try:
					relation = relations.children[name] #or bpy.data.collections[name]
				except KeyError:
					relation = bpy.data.collections.new(name)
					relations.children.link(relation)

				for member in rel.members:

					#todo: remove duplicate members

					for obj in importedObjects:
						#id = int(obj.get('id', -1))
						try:
							id = int(obj['id'])
						except (ValueError, KeyError):
							id = None
						if id == member.ref:
							try:
								relation.objects.link(obj)
							except Exception as e:
								log.error('Object {} already in group {}'.format(obj.name, name), exc_info=True)

				#cleanup
				if not relation.objects:
					bpy.data.collections.remove(relation)





#######################

class IMPORTGIS_OT_osm_file(Operator, OSM_IMPORT):

	bl_idname = "importgis.osm_file"
	bl_description = 'Select and import osm xml file'
	bl_label = "Import OSM"
	bl_options = {"UNDO"}

	# Import dialog properties
	filepath: StringProperty(
		name="File Path",
		description="Filepath used for importing the file",
		maxlen=1024,
		subtype='FILE_PATH' )

	filename_ext = ".osm"

	filter_glob: StringProperty(
			default = "*.osm",
			options = {'HIDDEN'} )

	def invoke(self, context, event):
		#workaround to enum callback bug (T48873, T38489)
		global OSMTAGS
		OSMTAGS = getTags()
		#open file browser
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def execute(self, context):

		scn = context.scene

		if not os.path.exists(self.filepath):
			self.report({'ERROR'}, "Invalid file")
			return {'CANCELLED'}

		try:
			bpy.ops.object.mode_set(mode='OBJECT')
		except RuntimeError:
			pass
		bpy.ops.object.select_all(action='DESELECT')

		#Set cursor representation to 'loading' icon
		w = context.window
		w.cursor_set('WAIT')

		#Spatial ref system
		geoscn = GeoScene(scn)
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		#Parse file
		t0 = perf_clock()
		api = overpy.Overpass()
		#with open(self.filepath, "r", encoding"utf-8") as f:
		#	result = api.parse_xml(f.read()) #WARNING read() load all the file into memory
		result = api.parse_xml(self.filepath)
		t = perf_clock() - t0
		log.info('File parsed in {} seconds'.format(round(t, 2)))

		#Get bbox
		bounds = result.bounds
		lon = (bounds["minlon"] + bounds["maxlon"])/2
		lat = (bounds["minlat"] + bounds["maxlat"])/2
		#Set CRS
		if not geoscn.hasCRS:
			try:
				geoscn.crs = utm.lonlat_to_epsg(lon, lat)
			except Exception as e:
				log.error("Cannot set UTM CRS", exc_info=True)
				self.report({'ERROR'}, "Cannot set UTM CRS, check logs for more infos")
				return {'CANCELLED'}
		#Set scene origin georef
		if not geoscn.hasOriginPrj:
			x, y = reprojPt(4326, geoscn.crs, lon, lat)
			geoscn.setOriginPrj(x, y)

		#Build meshes
		t0 = perf_clock()
		self.build(context, result, geoscn.crs)
		t = perf_clock() - t0
		log.info('Mesh build in {} seconds'.format(round(t, 2)))

		bbox = getBBOX.fromScn(scn)
		adjust3Dview(context, bbox)

		return {'FINISHED'}




########################

# Category definitions: user-friendly name → (OSM tags, feature types)
OSM_CATEGORIES = {
	'buildings': {
		'label': 'Buildings',
		'tags': ['building'],
		'types': ['way', 'relation'],
		'default': True,
	},
	'streets': {
		'label': 'Streets',
		'tags': ['highway'],
		'types': ['way'],
		'default': False,
	},
	'green': {
		'label': 'Green Areas / Parks',
		'tags': ['landuse', 'leisure'],
		'types': ['way', 'relation'],
		'default': False,
	},
	'water': {
		'label': 'Water',
		'tags': ['waterway', 'natural'],
		'types': ['way', 'relation'],
		'default': False,
	},
	'railway': {
		'label': 'Railway',
		'tags': ['railway'],
		'types': ['way'],
		'default': False,
	},
}


class IMPORTGIS_OT_osm_query(Operator, OSM_IMPORT):
	"""Import from Open Street Map"""

	bl_idname = "importgis.osm_query"
	bl_description = 'Query for Open Street Map data covering the current view3d area'
	bl_label = "Get OSM"
	bl_options = {"UNDO"}

	# User-friendly category checkboxes
	cat_buildings: BoolProperty(name='Buildings', description='Import building footprints (extruded)', default=True)
	cat_streets: BoolProperty(name='Streets', description='Import streets and roads', default=False)
	cat_green: BoolProperty(name='Green Areas / Parks', description='Import parks, gardens and green spaces', default=False)
	cat_water: BoolProperty(name='Water', description='Import rivers, lakes and waterways', default=False)
	cat_railway: BoolProperty(name='Railway', description='Import railway lines', default=False)

	#special function to auto redraw an operator popup called through invoke_props_dialog
	def check(self, context):
		return True

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def invoke(self, context, event):
		#workaround to enum callback bug (T48873, T38489)
		global OSMTAGS
		OSMTAGS = getTags()
		return context.window_manager.invoke_props_dialog(self)

	def draw(self, context):
		layout = self.layout
		# Category checkboxes
		layout.label(text="What to import:")
		col = layout.column(align=True)
		col.prop(self, 'cat_buildings', icon='HOME')
		col.prop(self, 'cat_streets', icon='CURVE_PATH')
		col.prop(self, 'cat_green', icon='OUTLINER_OB_FORCE_FIELD')
		col.prop(self, 'cat_water', icon='MOD_FLUIDSIM')
		col.prop(self, 'cat_railway', icon='GP_MULTIFRAME_EDITING')
		# Building options
		if self.cat_buildings:
			layout.separator()
			box = layout.box()
			box.label(text="Building Options:", icon='HOME')
			box.prop(self, 'buildingsExtrusion')
			if self.buildingsExtrusion:
				box.prop(self, 'defaultHeight')
				box.prop(self, 'randomHeightThreshold')
				box.prop(self, 'levelHeight')
		# General options
		layout.separator()
		layout.prop(self, 'useElevObj')
		if self.useElevObj:
			layout.prop(self, 'objElevLst')
		layout.prop(self, 'separate')

	def execute(self, context):

		prefs = bpy.context.preferences.addons[PKG].preferences
		scn = context.scene
		geoscn = GeoScene(scn)
		objs = context.selected_objects
		aObj = context.active_object

		if not geoscn.isGeoref:
				self.report({'ERROR'}, "Scene is not georef")
				return {'CANCELLED'}
		elif geoscn.isBroken:
				self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
				return {'CANCELLED'}

		if len(objs) == 1 and aObj.type == 'MESH':
			bbox = getBBOX.fromObj(aObj).toGeo(geoscn)
		elif isTopView(context):
			bbox = getBBOX.fromTopView(context).toGeo(geoscn)
		else:
			self.report({'ERROR'}, "Please define the query extent in orthographic top view or by selecting a reference object")
			return {'CANCELLED'}

		if bbox.dimensions.x > 20000 or bbox.dimensions.y > 20000:
			self.report({'ERROR'}, "Too large extent")
			return {'CANCELLED'}

		#Build tags and types from selected categories
		tags = set()
		types = set()
		for key, cat in OSM_CATEGORIES.items():
			if getattr(self, 'cat_' + key, False):
				tags.update(cat['tags'])
				types.update(cat['types'])

		if not tags:
			self.report({'ERROR'}, "Please select at least one category")
			return {'CANCELLED'}

		#Set the inherited filterTags and featureType so build() works correctly
		self.filterTags = tags
		self.featureType = types

		#Get view3d bbox in lonlat
		bbox = reprojBbox(geoscn.crs, 4326, bbox)

		#Set cursor representation to 'loading' icon
		w = context.window
		w.cursor_set('WAIT')

		#Download from overpass api
		log.debug('Requests overpass server : {}'.format(prefs.overpassServer))
		api = overpy.Overpass(overpass_server=prefs.overpassServer, user_agent=USER_AGENT)
		query = queryBuilder(bbox, tags=list(tags), types=list(types), format='xml')
		log.debug('Overpass query : {}'.format(query)) # can fails with non utf8 chars

		try:
			result = api.query(query)
		except Exception as e:
			log.error("Overpass query failed", exc_info=True)
			self.report({'ERROR'}, "Overpass query failed, check logs for more infos.")
			return {'CANCELLED'}
		else:
			log.info('Overpass query successful')

		self.build(context, result, geoscn.crs)

		bbox = getBBOX.fromScn(scn)
		adjust3Dview(context, bbox, zoomToSelect=False)

		return {'FINISHED'}

def _find_basemap_mesh_and_image():
	"""Find the basemap terrain mesh and its satellite image texture.
	Returns (mesh_object, image) or (None, None)."""
	for obj in bpy.data.objects:
		if obj.type != 'MESH' or not obj.name.startswith('EXPORT_'):
			continue
		for slot in obj.material_slots:
			mat = slot.material
			if not mat or not mat.use_nodes:
				continue
			for node in mat.node_tree.nodes:
				if node.type == 'TEX_IMAGE' and node.image:
					return obj, node.image
	return None, None


def _get_building_objects():
	"""Return all mesh objects that have the Building Extrusion modifier."""
	return [obj for obj in bpy.data.objects
			if obj.type == 'MESH'
			and any(m.type == 'NODES' and m.node_group and m.node_group.name == 'OSM Building Extrusion'
					for m in obj.modifiers)]


class IMPORTGIS_OT_apply_rooftop_texture(Operator):
	"""Project basemap satellite texture onto building rooftops"""
	bl_idname = "importgis.apply_rooftop_texture"
	bl_label = "Apply Satellite Rooftop"
	bl_description = "Project the basemap satellite texture onto building rooftops via Object Coordinates"
	bl_options = {"UNDO"}

	@classmethod
	def poll(cls, context):
		_, img = _find_basemap_mesh_and_image()
		return img is not None

	def execute(self, context):
		terrain_obj, basemap_img = _find_basemap_mesh_and_image()
		if not terrain_obj or not basemap_img:
			self.report({'ERROR'}, "No basemap terrain mesh with image texture found")
			return {'CANCELLED'}

		# Compute mapping from terrain mesh bounds
		# Object coords → UV: Scale = 1/extent, Location = -min/extent
		depsgraph = context.evaluated_depsgraph_get()
		eval_obj = terrain_obj.evaluated_get(depsgraph)
		eval_mesh = eval_obj.to_mesh()

		xs = [v.co.x for v in eval_mesh.vertices]
		ys = [v.co.y for v in eval_mesh.vertices]
		min_x, max_x = min(xs), max(xs)
		min_y, max_y = min(ys), max(ys)
		eval_obj.to_mesh_clear()

		extent_x = max_x - min_x
		extent_y = max_y - min_y
		if extent_x == 0 or extent_y == 0:
			self.report({'ERROR'}, "Terrain mesh has zero extent")
			return {'CANCELLED'}

		scale_x = 1.0 / extent_x
		scale_y = 1.0 / extent_y
		loc_x = -min_x * scale_x
		loc_y = -min_y * scale_y

		# Create/update the rooftop material
		name = 'OSM_Rooftop_Satellite'
		mat = bpy.data.materials.get(name)
		if not mat:
			mat = bpy.data.materials.new(name)
		mat.use_nodes = True
		tree = mat.node_tree
		tree.nodes.clear()

		n_output = tree.nodes.new('ShaderNodeOutputMaterial')
		n_output.location = (600, 0)

		n_bsdf = tree.nodes.new('ShaderNodeBsdfPrincipled')
		n_bsdf.location = (400, 0)
		n_bsdf.inputs['Roughness'].default_value = 0.9
		tree.links.new(n_bsdf.outputs['BSDF'], n_output.inputs['Surface'])

		n_texcoord = tree.nodes.new('ShaderNodeTexCoord')
		n_texcoord.location = (-200, 0)

		n_mapping = tree.nodes.new('ShaderNodeMapping')
		n_mapping.location = (0, 0)
		n_mapping.inputs['Location'].default_value = (loc_x, loc_y, 0.0)
		n_mapping.inputs['Scale'].default_value = (scale_x, scale_y, 1.0)
		tree.links.new(n_texcoord.outputs['Object'], n_mapping.inputs['Vector'])

		n_img = tree.nodes.new('ShaderNodeTexImage')
		n_img.location = (200, 0)
		n_img.image = basemap_img
		tree.links.new(n_mapping.outputs['Vector'], n_img.inputs['Vector'])
		tree.links.new(n_img.outputs['Color'], n_bsdf.inputs['Base Color'])

		# Apply to building objects
		buildings = _get_building_objects()
		count = 0
		for obj in buildings:
			existing = [s.material.name for s in obj.material_slots if s.material]
			if name not in existing:
				if len(obj.material_slots) == 0:
					obj.data.materials.append(mat)
				else:
					obj.material_slots[0].material = mat
			else:
				# Update existing slot
				for i, slot in enumerate(obj.material_slots):
					if slot.material and slot.material.name == name:
						slot.material = mat
			count += 1

		self.report({'INFO'}, f"Satellite rooftop applied to {count} building(s)")
		return {'FINISHED'}


class IMPORTGIS_OT_apply_facade_shader(Operator):
	"""Apply procedural window facade shader to building walls"""
	bl_idname = "importgis.apply_facade_shader"
	bl_label = "Apply Facade Shader"
	bl_description = "Apply the procedural facade shader with tangent-projected windows to building side faces"
	bl_options = {"UNDO"}

	@classmethod
	def poll(cls, context):
		return len(_get_building_objects()) > 0

	def execute(self, context):
		mat_facade = _get_or_create_facade_material()
		mat_roof = _get_or_create_rooftop_material()

		buildings = _get_building_objects()
		count = 0
		for obj in buildings:
			existing = [s.material.name for s in obj.material_slots if s.material]
			# Ensure slot 0 exists (rooftop)
			if len(obj.material_slots) == 0:
				obj.data.materials.append(mat_roof)
			# Ensure slot 1 exists (facade)
			if mat_facade.name not in existing:
				if len(obj.material_slots) < 2:
					obj.data.materials.append(mat_facade)
				else:
					obj.material_slots[1].material = mat_facade
			count += 1

		self.report({'INFO'}, f"Facade shader applied to {count} building(s)")
		return {'FINISHED'}


class IMPORTGIS_PT_building_materials(Panel):
	"""Building material tools in the GIS sidebar"""
	bl_label = "Building Materials"
	bl_idname = "IMPORTGIS_PT_building_materials"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_parent_id = "VIEW3D_PT_gis_scene"

	def draw_header(self, context):
		self.layout.label(icon='HOME')

	def draw(self, context):
		layout = self.layout
		buildings = _get_building_objects()
		if not buildings:
			layout.label(text="No buildings in scene", icon='INFO')
			return

		layout.label(text=f"{len(buildings)} building object(s)", icon='HOME')
		layout.separator()
		layout.operator("importgis.apply_rooftop_texture", icon='IMAGE_DATA')
		layout.operator("importgis.apply_facade_shader", icon='MOD_BUILD')


classes = [
	IMPORTGIS_OT_osm_file,
	IMPORTGIS_OT_osm_query,
	IMPORTGIS_OT_apply_rooftop_texture,
	IMPORTGIS_OT_apply_facade_shader,
	IMPORTGIS_PT_building_materials,
]

def register():
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError as e:
			log.warning('{} is already registered, now unregister and retry... '.format(cls))
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)

def unregister():
	for cls in classes:
		bpy.utils.unregister_class(cls)
