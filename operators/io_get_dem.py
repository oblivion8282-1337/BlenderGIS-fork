import os
import time
import threading

import logging
log = logging.getLogger(__name__)

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import bpy
import bmesh
from bpy.types import Operator, Panel, AddonPreferences
from bpy.props import StringProperty, IntProperty, FloatProperty, BoolProperty, EnumProperty, FloatVectorProperty

from ..geoscene import GeoScene
from .utils import adjust3Dview, getBBOX, isTopView
from ..core.proj import SRS, reprojBbox

from ..core import settings
USER_AGENT = settings.user_agent

PKG = __package__.rsplit('.', maxsplit=1)[0]  # bl_ext.user_default.cartoblend

TIMEOUT = 120

# Module-level state for background DEM download
_dem_thread = None
_dem_result = None   # dict with keys: 'filepath', 'onMesh', 'objectsLst', or 'error'
_dem_context_args = None  # tuple of (filePath, onMesh, objectsLst) for the timer callback


def _dem_download_thread(url, filePath, result_holder):
	"""Run in a background thread: download DEM file and store result."""
	rq = Request(url, headers={'User-Agent': result_holder['user_agent']})
	try:
		with urlopen(rq, timeout=TIMEOUT) as response, open(filePath, 'wb') as outFile:
			data = response.read()
			outFile.write(data)
		result_holder['ok'] = True
	except (URLError, HTTPError) as err:
		result_holder['ok'] = False
		result_holder['error'] = 'Http request fails url:{}, code:{}, error:{}'.format(
			url, getattr(err, 'code', None), err.reason)
	except TimeoutError:
		result_holder['ok'] = False
		result_holder['error'] = 'Http request timed out. url:{}'.format(url)


class IMPORTGIS_OT_dem_query(Operator):
	"""Import elevation data from a web service"""

	bl_idname = "importgis.dem_query"
	bl_description = 'Query for elevation data from a web service'
	bl_label = "Get elevation (SRTM)"
	bl_options = {"UNDO"}

	def invoke(self, context, event):

		#check georef
		geoscn = GeoScene(context.scene)
		if not geoscn.isGeoref:
				self.report({'ERROR'}, "Scene is not georef")
				return {'CANCELLED'}
		if geoscn.isBroken:
				self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
				return {'CANCELLED'}

		#return self.execute(context)
		return context.window_manager.invoke_props_dialog(self)#, width=350)

	def draw(self,context):
		prefs = context.preferences.addons[PKG].preferences
		layout = self.layout
		row = layout.row(align=True)
		row.prop(prefs, "demServer", text='Server')
		if 'opentopography' in prefs.demServer:
			row = layout.row(align=True)
			row.prop(prefs, "opentopography_api_key", text='Api Key')

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def execute(self, context):

		prefs = bpy.context.preferences.addons[PKG].preferences
		scn = context.scene
		geoscn = GeoScene(scn)
		crs = SRS(geoscn.crs)

		#Validate selection
		objs = bpy.context.selected_objects
		aObj = context.active_object
		if len(objs) == 1 and aObj.type == 'MESH':
			onMesh = True
			bbox = getBBOX.fromObj(aObj).toGeo(geoscn)
		elif isTopView(context):
			onMesh = False
			bbox = getBBOX.fromTopView(context).toGeo(geoscn)
		else:
			self.report({'ERROR'}, "Please define the query extent in orthographic top view or by selecting a reference object")
			return {'CANCELLED'}

		if bbox.dimensions.x > 1000000 or bbox.dimensions.y > 1000000:
			self.report({'ERROR'}, "Too large extent")
			return {'CANCELLED'}

		bbox = reprojBbox(geoscn.crs, 4326, bbox)

		if 'SRTM' in prefs.demServer:
			if bbox.ymin > 60:
				self.report({'ERROR'}, "SRTM is not available beyond 60 degrees north")
				return {'CANCELLED'}
			if bbox.ymax < -56:
				self.report({'ERROR'}, "SRTM is not available below 56 degrees south")
				return {'CANCELLED'}

		if 'opentopography' in prefs.demServer:
			if not prefs.opentopography_api_key:
				self.report({'ERROR'}, "Please register to opentopography.org and request for an API key")
				return {'CANCELLED'}

		#Set cursor representation to 'loading' icon
		w = context.window
		w.cursor_set('WAIT')

		#url template
		#http://opentopo.sdsc.edu/otr/getdem?demtype=SRTMGL3&west=-120.168457&south=36.738884&east=-118.465576&north=38.091337&outputFormat=GTiff
		e = 0.002 #opentopo service does not always respect the entire bbox, so request for a little more
		xmin, xmax = bbox.xmin - e, bbox.xmax + e
		ymin, ymax = bbox.ymin - e, bbox.ymax + e

		url = prefs.demServer.format(W=xmin, E=xmax, S=ymin, N=ymax, API_KEY=prefs.opentopography_api_key)
		log.debug(url)

		# Download the file from url and save it locally
		# opentopo return a geotiff object in wgs84
		if bpy.data.is_saved:
			filePath = os.path.join(os.path.dirname(bpy.data.filepath), 'srtm.tif')
		else:
			filePath = os.path.join(bpy.app.tempdir, 'srtm.tif')

		# Resolve objectsLst before starting thread (bpy API is not thread-safe)
		if onMesh:
			objectsLst = next(
				(str(i) for i, obj in enumerate(scn.collection.all_objects) if obj.name == context.active_object.name),
				None)
			if objectsLst is None:
				self.report({'ERROR'}, "Active object not found in scene collection")
				return {'CANCELLED'}
		else:
			objectsLst = None

		# Start background download thread
		global _dem_thread, _dem_result, _dem_context_args
		_dem_result = {'ok': None, 'error': None, 'user_agent': USER_AGENT}
		_dem_context_args = (filePath, onMesh, objectsLst)
		_dem_thread = threading.Thread(
			target=_dem_download_thread,
			args=(url, filePath, _dem_result),
			daemon=True)
		_dem_thread.start()

		self.report({'INFO'}, "Downloading DEM in background, please wait...")

		# Register a timer to poll thread completion and trigger import
		def _poll_dem_thread():
			global _dem_thread, _dem_result, _dem_context_args
			if _dem_thread is None or _dem_thread.is_alive():
				return 0.5  # poll again in 0.5 s
			# Thread finished
			_dem_thread = None
			result = _dem_result
			args = _dem_context_args
			_dem_result = None
			_dem_context_args = None

			if not result or not result.get('ok'):
				err = result.get('error', 'Unknown error') if result else 'No result'
				log.error(err)
				try:
					bpy.context.window.cursor_set('DEFAULT')
				except Exception:
					pass
				return None  # stop timer

			filePath, onMesh, objectsLst = args
			try:
				if not onMesh:
					bpy.ops.importgis.georaster(
						'EXEC_DEFAULT',
						filepath=filePath,
						reprojection=True,
						rastCRS='EPSG:4326',
						importMode='DEM',
						subdivision='subsurf',
						demInterpolation=True)
				else:
					bpy.ops.importgis.georaster(
						'EXEC_DEFAULT',
						filepath=filePath,
						reprojection=True,
						rastCRS='EPSG:4326',
						importMode='DEM',
						subdivision='subsurf',
						demInterpolation=True,
						demOnMesh=True,
						objectsLst=objectsLst,
						clip=False,
						fillNodata=False)
				scn = bpy.context.scene
				bbox2 = getBBOX.fromScn(scn)
				adjust3Dview(bpy.context, bbox2, zoomToSelect=False)
			except Exception as e:
				log.error('DEM import failed after download', exc_info=True)
			finally:
				try:
					bpy.context.window.cursor_set('DEFAULT')
				except Exception:
					pass
			return None  # stop timer

		bpy.app.timers.register(_poll_dem_thread, first_interval=0.5)

		return {'FINISHED'}


def register():
	try:
		bpy.utils.register_class(IMPORTGIS_OT_dem_query)
	except ValueError as e:
		log.warning('{} is already registered, now unregister and retry... '.format(IMPORTGIS_OT_dem_query))
		unregister()
		bpy.utils.register_class(IMPORTGIS_OT_dem_query)

def unregister():
	bpy.utils.unregister_class(IMPORTGIS_OT_dem_query)
