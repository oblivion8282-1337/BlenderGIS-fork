# -*- coding:utf-8 -*-
import os, sys, time
import threading
import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator
import bmesh
import math
from mathutils import Vector

import logging
log = logging.getLogger(__name__)

from ..core.lib.shapefile import Reader as shpReader


def _read_cpg_encoding(shp_path):
	"""Return the encoding declared in a sidecar .cpg file, or None.

	Shapefiles bundle their DBF-encoding declaration in an ESRI .cpg file
	containing a single token (e.g. 'UTF-8', 'cp1252', 'ISO-8859-1', '936').
	Numeric values are interpreted as Windows code pages.
	"""
	cpg = os.path.splitext(shp_path)[0] + '.cpg'
	if not os.path.exists(cpg):
		return None
	try:
		with open(cpg, 'r', encoding='ascii', errors='ignore') as f:
			tok = f.read().strip()
	except OSError:
		return None
	if not tok:
		return None
	if tok.isdigit():
		return 'cp' + tok
	return tok

from ..geoscene import GeoScene, georefManagerLayout
from ..prefs import PredefCRS
from ..core import BBOX
from ..core.proj import Reproj
from ..core.utils import perf_clock

from .utils import adjust3Dview, getBBOX, DropToGround

PKG = __package__.rsplit('.', maxsplit=1)[0]  # bl_ext.user_default.cartoblend

featureType={
0:'Null',
1:'Point',
3:'PolyLine',
5:'Polygon',
8:'MultiPoint',
11:'PointZ',
13:'PolyLineZ',
15:'PolygonZ',
18:'MultiPointZ',
21:'PointM',
23:'PolyLineM',
25:'PolygonM',
28:'MultiPointM',
31:'MultiPatch'
}


# Module-level state for background shapefile parse/reproject phase.
# Phase A (file read + reproject + 3D geom assembly) runs in a worker thread.
# Phase C (bmesh / mesh / object creation, extrude_discrete_faces) runs on the
# main thread via bpy.app.timers, because bmesh.ops and bpy.data are not
# thread-safe. The DropToGround raycaster also calls into bpy, so when
# elevSource == 'OBJ' we fall back to synchronous (single-threaded) execution.
_shp_state_lock = threading.Lock()
_shp_thread = None
_shp_result = None       # dict, populated by worker; consumed by polling cb
_shp_context_args = None # operator-side parameters needed by polling cb


"""
dbf fields type:
	C is ASCII characters
	N is a double precision integer limited to around 18 characters in length
	D is for dates in the YYYYMMDD format, with no spaces or hyphens between the sections
	F is for floating point numbers with the same length limits as N
	L is for logical data which is stored in the shapefile's attribute table as a short integer as a 1 (true) or a 0 (false).
	The values it can receive are 1, 0, y, n, Y, N, T, F or the python builtins True and False
"""


class IMPORTGIS_OT_shapefile_file_dialog(Operator):
	"""Select shp file, loads the fields and start importgis.shapefile_props_dialog operator"""

	bl_idname = "importgis.shapefile_file_dialog"
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {'INTERNAL'}

	# Import dialog properties
	filepath: StringProperty(
		name="File Path",
		description="Filepath used for importing the file",
		maxlen=1024,
		subtype='FILE_PATH' )

	filename_ext = ".shp"

	filter_glob: StringProperty(
			default = "*.shp",
			options = {'HIDDEN'} )

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def draw(self, context):
		layout = self.layout
		layout.label(text="Options will be available")
		layout.label(text="after selecting a file")

	def execute(self, context):
		if os.path.exists(self.filepath):
			bpy.ops.importgis.shapefile_props_dialog('INVOKE_DEFAULT', filepath=self.filepath)
		else:
			self.report({'ERROR'}, "Invalid filepath")
		return{'FINISHED'}



class IMPORTGIS_OT_shapefile_props_dialog(Operator):
	"""Shapefile importer properties dialog"""

	bl_idname = "importgis.shapefile_props_dialog"
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {"INTERNAL"}

	filepath: StringProperty()

	#special function to auto redraw an operator popup called through invoke_props_dialog
	def check(self, context):
		return True

	def listFields(self, context):
		fieldsItems = []
		try:
			enc = _read_cpg_encoding(self.filepath)
			shp = shpReader(self.filepath, encoding=enc) if enc else shpReader(self.filepath)
		except Exception as e:
			log.warning("Unable to read shapefile fields", exc_info=True)
			return fieldsItems
		fields = [field for field in shp.fields if field[0] != 'DeletionFlag'] #ignore default DeletionFlag field
		for i, field in enumerate(fields):
			#put each item in a tuple (key, label, tooltip)
			fieldsItems.append( (field[0], field[0], '') )
		return fieldsItems

	# Shapefile CRS definition
	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	def listObjects(self, context):
		objs = []
		for index, object in enumerate(bpy.context.scene.objects):
			if object.type == 'MESH':
				#put each object in a tuple (key, label, tooltip) and add this to the objects list
				objs.append((object.name, object.name, "Object named " + object.name))
		return objs

	reprojection: BoolProperty(
			name="Specify shapefile CRS",
			description="Specify shapefile CRS if it's different from scene CRS",
			default=False )

	shpCRS: EnumProperty(
		name = "Shapefile CRS",
		description = "Choose a Coordinate Reference System",
		items = listPredefCRS)

	# Elevation source
	vertsElevSource: EnumProperty(
			name="Elevation source",
			description="Select the source of vertices z value",
			items=[
			('NONE', 'None', "Flat geometry"),
			('GEOM', 'Geometry', "Use z value from shape geometry if exists"),
			('FIELD', 'Field', "Extract z elevation value from an attribute field"),
			('OBJ', 'Object', "Get z elevation value from an existing ground mesh")
			],
			default='GEOM')

	# Elevation object
	objElevLst: EnumProperty(
		name="Elev. object",
		description="Choose the mesh from which extract z elevation",
		items=listObjects )

	# Elevation field
	'''
	useFieldElev: BoolProperty(
			name="Elevation from field",
			description="Extract z elevation value from an attribute field",
			default=False )
	'''
	fieldElevName: EnumProperty(
		name = "Elev. field",
		description = "Choose field",
		items = listFields )

	#Extrusion field
	useFieldExtrude: BoolProperty(
			name="Extrusion from field",
			description="Extract z extrusion value from an attribute field",
			default=False )

	fieldExtrudeName: EnumProperty(
		name = "Field",
		description = "Choose field",
		items = listFields )

	#Extrusion axis
	extrusionAxis: EnumProperty(
			name="Extrude along",
			description="Select extrusion axis",
			items=[ ('Z', 'z axis', "Extrude along Z axis"),
			('NORMAL', 'Normal', "Extrude along normal")] )

	#Create separate objects
	separateObjects: BoolProperty(
			name="Separate objects",
			description="Warning : can be very slow with lot of features",
			default=False )

	#Name objects from field
	useFieldName: BoolProperty(
			name="Object name from field",
			description="Extract name for created objects from an attribute field",
			default=False )
	fieldObjName: EnumProperty(
		name = "Field",
		description = "Choose field",
		items = listFields )


	def draw(self, context):
		#Function used by blender to draw the panel.
		scn = context.scene
		layout = self.layout

		#
		layout.prop(self, 'vertsElevSource')
		#
		#layout.prop(self, 'useFieldElev')
		if self.vertsElevSource == 'FIELD':
			layout.prop(self, 'fieldElevName')
		elif self.vertsElevSource == 'OBJ':
			layout.prop(self, 'objElevLst')
		#
		layout.prop(self, 'useFieldExtrude')
		if self.useFieldExtrude:
			layout.prop(self, 'fieldExtrudeName')
			layout.prop(self, 'extrusionAxis')
		#
		layout.prop(self, 'separateObjects')
		if self.separateObjects:
			layout.prop(self, 'useFieldName')
		else:
			self.useFieldName = False
		if self.separateObjects and self.useFieldName:
			layout.prop(self, 'fieldObjName')
		#
		geoscn = GeoScene()
		#geoscnPrefs = context.preferences.addons['geoscene'].preferences
		if geoscn.isPartiallyGeoref:
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self.shpCRSInputLayout(context)
			#
			georefManagerLayout(self, context)
		else:
			self.shpCRSInputLayout(context)


	def shpCRSInputLayout(self, context):
		layout = self.layout
		row = layout.row(align=True)
		#row.prop(self, "shpCRS", text='CRS')
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, "shpCRS", text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')


	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)

	def execute(self, context):

		#elevField = self.fieldElevName if self.useFieldElev else ""
		elevField = self.fieldElevName if self.vertsElevSource == 'FIELD' else ""
		extrudField = self.fieldExtrudeName if self.useFieldExtrude else ""
		nameField = self.fieldObjName if self.useFieldName else ""
		if self.vertsElevSource == 'OBJ':
			if not self.objElevLst:
				self.report({'ERROR'}, "No elevation object")
				return {'CANCELLED'}
			else:
				objElevName = self.objElevLst
		else:
			objElevName = '' #will not be used

		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		if geoscn.isGeoref:
			if self.reprojection:
				shpCRS = self.shpCRS
			else:
				shpCRS = geoscn.crs
		else:
			shpCRS = self.shpCRS

		try:
			bpy.ops.importgis.shapefile('INVOKE_DEFAULT', filepath=self.filepath, shpCRS=shpCRS, elevSource=self.vertsElevSource,
				fieldElevName=elevField, objElevName=objElevName, fieldExtrudeName=extrudField, fieldObjName=nameField,
				extrusionAxis=self.extrusionAxis, separateObjects=self.separateObjects)
		except Exception as e:
			log.error('Shapefile import fails', exc_info=True)
			self.report({'ERROR'}, 'Shapefile import fails, check logs.')
			return {'CANCELLED'}

		return{'FINISHED'}


def _shp_parse_thread(filepath, shpCRS, sceneCRS, elevSource, fieldElevName,
                      fieldExtrudeName, fieldObjName, separateObjects,
                      result_holder):
	"""Worker thread: read shapefile, reproject, build per-feature 3D geom.

	Pure file/CPU work — no bpy / bmesh access. Results are stashed in
	result_holder under the lock; the polling callback reads them out and
	performs all bpy / bmesh / mesh operations on the main thread.
	"""
	try:
		# --- Open shapefile (honour .cpg sidecar encoding) -----------------
		cpg_enc = _read_cpg_encoding(filepath)
		if cpg_enc:
			log.info("Using DBF encoding from .cpg sidecar: %s", cpg_enc)
			shp = shpReader(filepath, encoding=cpg_enc)
		else:
			shp = shpReader(filepath)

		shpType = featureType[shp.shapeType]
		log.info('Feature type : ' + shpType)
		if shpType not in ['Point','PolyLine','Polygon','PointZ','PolyLineZ','PolygonZ']:
			raise RuntimeError(
				"Cannot process multipoint, multipointZ, pointM, polylineM, "
				"polygonM and multipatch feature type")

		# --- Field index resolution ----------------------------------------
		fields = [f for f in shp.fields if f[0] != 'DeletionFlag']
		fieldsNames = [f[0] for f in fields]
		log.debug("DBF fields : " + str(fieldsNames))

		useDbf = bool(separateObjects or fieldElevName or fieldObjName or fieldExtrudeName)

		nameFieldIdx = None
		if fieldObjName and separateObjects:
			try:
				nameFieldIdx = fieldsNames.index(fieldObjName)
			except Exception:
				raise RuntimeError("Unable to find name field")

		zFieldIdx = None
		if fieldElevName:
			try:
				zFieldIdx = fieldsNames.index(fieldElevName)
			except Exception:
				raise RuntimeError("Unable to find elevation field")
			if fields[zFieldIdx][1] not in ['N', 'F', 'L']:
				raise RuntimeError("Elevation field do not contains numeric values")

		extrudeFieldIdx = None
		if fieldExtrudeName:
			try:
				extrudeFieldIdx = fieldsNames.index(fieldExtrudeName)
			except ValueError:
				raise RuntimeError("Unable to find extrusion field")
			if fields[extrudeFieldIdx][1] not in ['N', 'F', 'L']:
				raise RuntimeError("Extrusion field do not contains numeric values")

		# --- Reprojector ---------------------------------------------------
		rprj = None
		if sceneCRS != shpCRS:
			log.info("Data will be reprojected from {} to {}".format(shpCRS, sceneCRS))
			rprj = Reproj(shpCRS, sceneCRS)
			if rprj.iproj == 'EPSGIO' and shp.numRecords > 100:
				raise RuntimeError(
					"Reprojection through online epsg.io engine is limited "
					"to 100 features. \nPlease install GDAL or pyproj module.")

		# --- Bounding box (in scene CRS) -----------------------------------
		bbox = BBOX(shp.bbox)
		if rprj is not None:
			bbox = rprj.bbox(bbox)

		# --- Iterate features and assemble plain Python data ---------------
		# We do NOT yet shift by (dx, dy) because the scene origin may need to
		# be set on the main thread (GeoScene mutation is bpy state). The
		# polling callback picks (dx, dy) and shifts coords there.
		if useDbf:
			shpIter = shp.iterShapeRecords()
		else:
			shpIter = shp.iterShapes()
		nbFeats = shp.numRecords

		# parsed_features: list of dicts, one per feature, each with:
		#   'parts': list of list-of-(x,y,z) tuples (already reprojected, NOT shifted)
		#   'record': raw record (or None if !useDbf)
		#   'offset': float extrusion offset (or None)
		#   'name':   str name (or None)
		parsed_features = []

		for featIdx, feat in enumerate(shpIter):
			if useDbf:
				shape = feat.shape
				record = feat.record
			else:
				shape = feat
				record = None

			# Progress (worker-thread print is fine for large jobs)
			report_every = max(1, nbFeats // 10)
			if featIdx % report_every == 0:
				pourcent = round(((featIdx+1)*100)/nbFeats)
				if pourcent == 100:
					print(str(pourcent)+'%')
				else:
					print(str(pourcent), end="%, ")
				sys.stdout.flush()

			# Multipart handling
			if shpType == 'PointZ' or shpType == 'Point':
				partsIdx = [0]
			else:
				try:
					partsIdx = shape.parts
				except Exception as e:
					log.warning('Cannot access "parts" attribute for feature {} : {}'.format(featIdx, e))
					partsIdx = [0]
			nbParts = len(partsIdx)

			pts = shape.points
			nbPts = len(pts)

			if nbPts == 0:
				continue

			# Reproject (handle Z together if GEOM elevation on Z shape)
			zList = None
			useGeomZ = (shpType[-1] == 'Z' and elevSource == 'GEOM')
			if rprj is not None:
				if useGeomZ:
					zList = list(shape.z)
					reprojected = rprj.pts3D([(pt[0], pt[1], zList[k]) for k, pt in enumerate(pts)])
					pts = [(p[0], p[1]) for p in reprojected]
					zList = [p[2] for p in reprojected]
				else:
					pts = rprj.pts(pts)

			# Extrusion offset
			offset = None
			if fieldExtrudeName:
				try:
					offset = float(record[extrudeFieldIdx])
				except Exception as e:
					log.warning('Cannot extract extrusion value for feature {} : {}'.format(featIdx, e))
					offset = 0

			# Build per-part 3D point list (no dx/dy shift yet, no raycast yet)
			parts = []
			for j in range(nbParts):
				idx1 = partsIdx[j]
				idx2 = nbPts if j+1 == nbParts else partsIdx[j+1]

				geom = []
				for k, pt in enumerate(pts[idx1:idx2]):
					if elevSource == 'FIELD':
						try:
							z = float(record[zFieldIdx])
						except Exception as e:
							log.warning('Cannot extract elevation value for feature {} : {}'.format(featIdx, e))
							z = 0
					elif shpType[-1] == 'Z' and elevSource == 'GEOM':
						z = zList[idx1+k] if zList is not None else shape.z[idx1:idx2][k]
					else:
						# OBJ raycaster runs on main thread; placeholder z=0 for
						# now, polling cb will overwrite via raycaster
						z = 0
					geom.append((pt[0], pt[1], z))

				parts.append(geom)

			# Name extraction (decoded immediately so worker can hand off str)
			name = None
			if separateObjects and fieldObjName and record is not None and nameFieldIdx is not None:
				try:
					raw = record[nameFieldIdx]
				except Exception as e:
					log.warning('Cannot extract name value for feature {} : {}'.format(featIdx, e))
					raw = ''
				if isinstance(raw, bytes):
					name = ''
				else:
					name = str(raw) if raw is not None else ''

			parsed_features.append({
				'parts': parts,
				'record': record,
				'offset': offset,
				'name': name,
			})

		with _shp_state_lock:
			result_holder['ok'] = True
			result_holder['shp_fields'] = list(shp.fields)  # for attribute write-back
			result_holder['shpType'] = shpType
			result_holder['bbox'] = bbox
			result_holder['features'] = parsed_features
			result_holder['useDbf'] = useDbf
			result_holder['nameFieldIdx'] = nameFieldIdx
			result_holder['zFieldIdx'] = zFieldIdx
			result_holder['extrudeFieldIdx'] = extrudeFieldIdx

	except Exception as e:
		log.error('Shapefile parse worker failed', exc_info=True)
		with _shp_state_lock:
			result_holder['ok'] = False
			result_holder['error'] = str(e)


class IMPORTGIS_OT_shapefile(Operator):
	"""Import from ESRI shapefile file format (.shp)"""

	bl_idname = "importgis.shapefile" # important since its how bpy.ops.import.shapefile is constructed (allows calling operator from python console or another script)
	#bl_idname rules: must contain one '.' (dot) charactere, no capital letters, no reserved words (like 'import')
	bl_description = 'Import ESRI shapefile (.shp)'
	bl_label = "Import SHP"
	bl_options = {"UNDO"}

	filepath: StringProperty()

	shpCRS: StringProperty(name = "Shapefile CRS", description = "Coordinate Reference System")

	elevSource: StringProperty(name = "Elevation source", description = "Elevation source", default='GEOM') # [NONE, GEOM, OBJ, FIELD]
	objElevName: StringProperty(name = "Elevation object name", description = "")

	fieldElevName: StringProperty(name = "Elevation field", description = "Field name")
	fieldExtrudeName: StringProperty(name = "Extrusion field", description = "Field name")
	fieldObjName: StringProperty(name = "Objects names field", description = "Field name")

	#Extrusion axis
	extrusionAxis: EnumProperty(
			name="Extrude along",
			description="Select extrusion axis",
			items=[ ('Z', 'z axis', "Extrude along Z axis"),
			('NORMAL', 'Normal', "Extrude along normal")]
			)
	#Create separate objects
	separateObjects: BoolProperty(
			name="Separate objects",
			description="Import to separate objects instead one large object",
			default=False
			)

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def execute(self, context):
		# Validate scene georef on the main thread (touches bpy state).
		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		shpCRS = self.shpCRS
		if not geoscn.hasCRS:
			try:
				geoscn.crs = shpCRS
			except Exception:
				log.error("Cannot set scene crs", exc_info=True)
				self.report({'ERROR'}, "Cannot set scene crs, check logs for more infos")
				return {'CANCELLED'}

		# Double-click guard: refuse a second concurrent run.
		global _shp_thread, _shp_result, _shp_context_args
		with _shp_state_lock:
			if _shp_thread is not None and _shp_thread.is_alive():
				self.report({'INFO'}, "Shapefile import already running, please wait...")
				return {'CANCELLED'}

			_shp_result = {'ok': None, 'error': None}
			_shp_context_args = {
				'filepath': self.filepath,
				'shpCRS': shpCRS,
				'sceneCRS': geoscn.crs,
				'elevSource': self.elevSource,
				'objElevName': self.objElevName,
				'fieldElevName': self.fieldElevName,
				'fieldExtrudeName': self.fieldExtrudeName,
				'fieldObjName': self.fieldObjName,
				'extrusionAxis': self.extrusionAxis,
				'separateObjects': self.separateObjects,
				't0': perf_clock(),
			}
			_shp_thread = threading.Thread(
				target=_shp_parse_thread,
				args=(
					self.filepath,
					shpCRS,
					geoscn.crs,
					self.elevSource,
					self.fieldElevName,
					self.fieldExtrudeName,
					self.fieldObjName,
					self.separateObjects,
					_shp_result,
				),
				daemon=True,
			)
			_shp_thread.start()

		# Set wait cursor + deselect (main-thread bpy ops, safe here).
		w = context.window
		w.cursor_set('WAIT')
		bpy.ops.object.select_all(action='DESELECT')
		self.report({'INFO'}, "Reading shapefile in background, please wait...")

		bpy.app.timers.register(_poll_shp_thread, first_interval=0.5)
		return {'FINISHED'}


def _poll_shp_thread():
	"""Main-thread polling callback: drain worker, then build bmesh/objects.

	Phase C (mesh build, extrude_discrete_faces, object creation, optional
	raycast for elevSource=='OBJ') runs entirely here on the main thread.
	"""
	global _shp_thread, _shp_result, _shp_context_args
	with _shp_state_lock:
		if _shp_thread is None or _shp_thread.is_alive():
			return 0.5  # poll again
		# Worker finished — consume state under lock
		_shp_thread = None
		result = _shp_result
		args = _shp_context_args
		_shp_result = None
		_shp_context_args = None

	# Restore cursor at the very end no matter what
	def _restore_cursor():
		try:
			bpy.context.window.cursor_set('DEFAULT')
		except Exception:
			pass

	if not result or not result.get('ok'):
		err = result.get('error', 'Unknown error') if result else 'No result'
		log.error('Shapefile parse failed: %s', err)
		_restore_cursor()
		return None  # stop timer

	# --- Unpack worker result + execute params ------------------------------
	shpType = result['shpType']
	bbox = result['bbox']
	features = result['features']
	shp_fields = result['shp_fields']
	useDbf = result['useDbf']
	zFieldIdx = result['zFieldIdx']
	extrudeFieldIdx = result['extrudeFieldIdx']

	filepath = args['filepath']
	elevSource = args['elevSource']
	objElevName = args['objElevName']
	fieldElevName = args['fieldElevName']
	fieldExtrudeName = args['fieldExtrudeName']
	fieldObjName = args['fieldObjName']
	extrusionAxis = args['extrusionAxis']
	separateObjects = args['separateObjects']
	t0 = args['t0']

	context = bpy.context
	scn = context.scene
	geoscn = GeoScene()

	bm = None
	finalBm = None
	try:
		prefs = bpy.context.preferences.addons[PKG].preferences

		# Resolve raycaster on main thread (DropToGround uses bpy/BVHTree).
		rayCaster = None
		if elevSource == 'OBJ':
			try:
				elevObj = scn.objects[objElevName]
			except KeyError:
				log.error("Elevation object '{}' not found in scene".format(objElevName))
				return None
			# BVH method: ~5-10x faster than per-vertex obj.ray_cast on big shp.
			rayCaster = DropToGround(scn, elevObj, method='BVH')

		shpName = os.path.basename(filepath)[:-4]

		# Origin (dx, dy) — needs main thread because it may write to GeoScene.
		if not geoscn.isGeoref:
			dx, dy = bbox.center
			geoscn.setOriginPrj(dx, dy)
		else:
			dx, dy = geoscn.getOriginPrj()

		# --- BMesh build ----------------------------------------------------
		bm = bmesh.new()
		if not separateObjects and fieldExtrudeName:
			finalBm = bmesh.new()

		if separateObjects:
			layer = bpy.data.collections.new(shpName)
			context.scene.collection.children.link(layer)
			created_objects = []

		for featIdx, feat in enumerate(features):
			parts = feat['parts']
			record = feat['record']
			offset = feat['offset']

			for geom_part in parts:
				# Apply main-thread Z resolution for OBJ elevation source.
				if elevSource == 'OBJ' and rayCaster is not None:
					resolved = []
					for (x, y, _z) in geom_part:
						rcHit = rayCaster.rayCast(x=x-dx, y=y-dy)
						# DropToGround returns hit.loc in world coords; .z is the
						# vertical we want to attach to the (shifted) vertex.
						resolved.append((x, y, rcHit.loc.z))
					geom_part = resolved

				# Shift coords (relative to scene origin)
				if elevSource == 'OBJ':
					# Z is already the raycast hit z (world); only shift x/y.
					shifted = [(p[0]-dx, p[1]-dy, p[2]) for p in geom_part]
				else:
					shifted = [(p[0]-dx, p[1]-dy, p[2]) for p in geom_part]

				# POINTS
				if shpType == 'PointZ' or shpType == 'Point':
					vert = [bm.verts.new(pt) for pt in shifted]
					if fieldExtrudeName and offset and offset > 0:
						vect = (0, 0, offset)
						res = bmesh.ops.extrude_vert_indiv(bm, verts=vert)
						verts = res['verts']
						bmesh.ops.translate(bm, verts=verts, vec=vect)

				# LINES
				if shpType == 'PolyLine' or shpType == 'PolyLineZ':
					verts = [bm.verts.new(pt) for pt in shifted]
					edges = []
					for ev in range(len(shifted)-1):
						edge = bm.edges.new([verts[ev], verts[ev+1]])
						edges.append(edge)
					if fieldExtrudeName and offset and offset > 0:
						vect = (0, 0, offset)
						res = bmesh.ops.extrude_edge_only(bm, edges=edges)
						verts = [elem for elem in res['geom'] if isinstance(elem, bmesh.types.BMVert)]
						bmesh.ops.translate(bm, verts=verts, vec=vect)

				# NGONS
				if shpType == 'Polygon' or shpType == 'PolygonZ':
					# Polygons clockwise in shapefile spec; reverse for face-up.
					poly = list(shifted)
					poly.reverse()
					if poly:
						poly.pop()  # last == first
					if len(poly) >= 3:
						verts = [bm.verts.new(pt) for pt in poly]
						face = bm.faces.new(verts)
						face.normal_update()
						if face.normal.z < 0:
							pass  # polygon hole — bmesh can't represent
						if fieldExtrudeName and offset and offset > 0:
							if extrusionAxis == 'NORMAL':
								normal = face.normal
								vect = normal * offset
							elif extrusionAxis == 'Z':
								vect = (0, 0, offset)
							faces = bmesh.ops.extrude_discrete_faces(bm, faces=[face])
							verts = faces['faces'][0].verts
							if elevSource == 'OBJ':
								# Flat roof: lift all verts to max z + offset.
								z = max([v.co.z for v in verts]) + offset
								for v in verts:
									v.co.z = z
							else:
								bmesh.ops.translate(bm, verts=verts, vec=vect)

			# --- Per-feature finalisation when separateObjects=True ---------
			if separateObjects:
				if fieldObjName:
					name = feat['name'] if feat['name'] is not None else ''
				else:
					name = shpName

				_bbox = getBBOX.fromBmesh(bm)
				ox, oy, oz = _bbox.center
				oz = _bbox.zmin
				bmesh.ops.translate(bm, verts=bm.verts, vec=(-ox, -oy, -oz))

				mesh = bpy.data.meshes.new(name)
				bm.to_mesh(mesh)
				bm.clear()

				obj = bpy.data.objects.new(name, mesh)
				layer.objects.link(obj)
				obj.location = (ox, oy, oz)
				created_objects.append(obj)

				# Write attribute data as custom properties.
				for fi, field in enumerate(shp_fields):
					fieldName, fieldType, fieldLength, fieldDecLength = field
					if fieldName != 'DeletionFlag' and record is not None:
						if fieldType in ('N', 'F'):
							v = record[fi-1]
							if v is not None:
								obj[fieldName] = float(record[fi-1])
						else:
							obj[fieldName] = record[fi-1]

			elif fieldExtrudeName:
				# Periodic flush into final bmesh to keep extrude perf stable.
				_vert_offset = len(finalBm.verts)
				bm.verts.ensure_lookup_table()
				for v in bm.verts:
					finalBm.verts.new(v.co)
				finalBm.verts.ensure_lookup_table()
				for f in bm.faces:
					try:
						finalBm.faces.new([finalBm.verts[v.index + _vert_offset] for v in f.verts])
					except ValueError:
						pass
				for e in bm.edges:
					if not e.link_faces:
						try:
							finalBm.edges.new([finalBm.verts[v.index + _vert_offset] for v in e.verts])
						except ValueError:
							pass
				bm.clear()

		# Batch-apply selection for separate objects.
		if separateObjects and created_objects:
			context.view_layer.update()
			for obj in created_objects:
				obj.select_set(True)
			context.view_layer.objects.active = created_objects[-1]

		# Single-object path.
		if not separateObjects:
			mesh = bpy.data.meshes.new(shpName)
			if fieldExtrudeName:
				bm.free()
				bm = finalBm
				finalBm = None

			if prefs.mergeDoubles:
				bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
			bm.to_mesh(mesh)

			obj = bpy.data.objects.new(shpName, mesh)
			context.scene.collection.objects.link(obj)
			context.view_layer.objects.active = obj
			obj.select_set(True)
			bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')

		bm.free()
		bm = None

		t = perf_clock() - t0
		log.info('Build in %f seconds' % t)

		# Adjust grid size on main thread.
		if prefs.adjust3Dview:
			bbox.shift(-dx, -dy)
			adjust3Dview(context, bbox)

	except Exception:
		log.error('Shapefile build failed on main thread', exc_info=True)
	finally:
		if bm is not None:
			try:
				bm.free()
			except Exception:
				pass
		if finalBm is not None:
			try:
				finalBm.free()
			except Exception:
				pass
		_restore_cursor()

	return None  # stop timer


classes = [
	IMPORTGIS_OT_shapefile_file_dialog,
	IMPORTGIS_OT_shapefile_props_dialog,
	IMPORTGIS_OT_shapefile
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
