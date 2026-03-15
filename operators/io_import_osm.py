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
	"""Create a Geometry Nodes group for building extrusion from 'height' attribute."""
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
	n_out.location = (400, 0)

	# Named Attribute → read "height" per face
	n_attr = nodes.new('GeometryNodeInputNamedAttribute')
	n_attr.data_type = 'FLOAT'
	n_attr.inputs['Name'].default_value = 'height'
	n_attr.location = (-600, -200)

	# Multiply height by multiplier
	n_mult = nodes.new('ShaderNodeMath')
	n_mult.operation = 'MULTIPLY'
	n_mult.location = (-300, -150)
	links.new(n_attr.outputs['Attribute'], n_mult.inputs[0])
	links.new(n_in.outputs['Height Multiplier'], n_mult.inputs[1])

	# Combine XYZ → offset vector (0, 0, height)
	n_xyz = nodes.new('ShaderNodeCombineXYZ')
	n_xyz.location = (-100, -150)
	links.new(n_mult.outputs[0], n_xyz.inputs['Z'])

	# Selection: only extrude faces where height > 0
	n_gt = nodes.new('FunctionNodeCompare')
	n_gt.data_type = 'FLOAT'
	n_gt.operation = 'GREATER_THAN'
	n_gt.location = (-300, -300)
	links.new(n_attr.outputs['Attribute'], n_gt.inputs['A'])
	n_gt.inputs['B'].default_value = 0.0

	# Extrude Mesh (Individual Faces)
	n_ext = nodes.new('GeometryNodeExtrudeMesh')
	n_ext.mode = 'FACES'
	n_ext.location = (100, 0)
	n_ext.inputs['Individual'].default_value = True
	links.new(n_in.outputs['Geometry'], n_ext.inputs['Mesh'])
	links.new(n_gt.outputs['Result'], n_ext.inputs['Selection'])
	links.new(n_xyz.outputs['Vector'], n_ext.inputs['Offset'])

	# Output
	links.new(n_ext.outputs['Mesh'], n_out.inputs['Geometry'])

	return group


def _apply_building_geonodes(obj):
	"""Add the building extrusion Geometry Nodes modifier to an object."""
	group = _get_or_create_building_geonodes()
	mod = obj.modifiers.new('Building Extrusion', type='NODES')
	mod.node_group = group


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

classes = [
	IMPORTGIS_OT_osm_file,
	IMPORTGIS_OT_osm_query
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
