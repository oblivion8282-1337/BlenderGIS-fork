# -*- coding:utf-8 -*-

#  ***** GPL LICENSE BLOCK *****
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
#  All rights reserved.
#  ***** GPL LICENSE BLOCK *****

import bpy

bl_info = {
	'name': 'CartoBlend',
	'description': 'GIS toolkit for Blender — basemaps, OSM, DEM, GPX and more',
	'author': 'domlysz, oblivion',
	'license': 'GPL',
	'deps': '',
	'version': (3, 0, 0),
	'blender': (4, 2, 0),
	'location': 'View3D > Sidebar > GIS',
	'warning': '',
	'wiki_url': 'https://github.com/oblivion8282-1337/cartoblend/wiki',
	'tracker_url': 'https://github.com/oblivion8282-1337/cartoblend/issues',
	'link': '',
	'support': 'COMMUNITY',
	'category': '3D View'
	}

class BlenderVersionError(Exception):
	pass

if bl_info['blender'] > bpy.app.version:
	raise BlenderVersionError(f"This addon requires Blender >= {bl_info['blender']}")
#if bpy.app.version[0] > 5: #prevent breaking changes on major release
#	raise BlenderVersionError(f"This addon is not tested against Blender {bpy.app.version[0]}.x breaking changes")


#Modules
CAM_GEOPHOTO = True
CAM_GEOREF = True
EXPORT_SHP = True
GET_DEM = True
IMPORT_GEORASTER = True
IMPORT_OSM = True
IMPORT_SHP = True
IMPORT_ASC = True
IMPORT_GEOJSON = True
IMPORT_GPX = True
DELAUNAY = True
TERRAIN_NODES = True
TERRAIN_RECLASS = True
BASEMAPS = True
DROP = True
EARTH_SPHERE = True

import os, sys, tempfile
from datetime import datetime

def getAppData():
	home = os.path.expanduser('~')
	loc = os.path.join(home, '.bgis')
	if not os.path.exists(loc):
		os.mkdir(loc)
	return loc

APP_DATA = getAppData()

import logging
from logging.handlers import RotatingFileHandler
#temporary set log level, will be overriden reading addon prefs
#logsFormat = "%(levelname)s:%(name)s:%(lineno)d:%(message)s"
logsFormat = '{levelname}:{name}:{lineno}:{message}'
logsFileName = 'bgis.log'
try:
	#logsFilePath = os.path.join(os.path.dirname(__file__), logsFileName)
	logsFilePath = os.path.join(APP_DATA, logsFileName)
	#logging.basicConfig(level=logging.getLevelName('DEBUG'), format=logsFormat, style='{', filename=logsFilePath, filemode='w')
	logHandler = RotatingFileHandler(logsFilePath, mode='a', maxBytes=512000, backupCount=1)
except PermissionError:
	#logsFilePath = os.path.join(bpy.app.tempdir, logsFileName)
	logsFilePath = os.path.join(tempfile.gettempdir(), logsFileName)
	logHandler = RotatingFileHandler(logsFilePath, mode='a', maxBytes=512000, backupCount=1)
logHandler.setFormatter(logging.Formatter(logsFormat, style='{'))
logger = logging.getLogger(__name__)
logger.addHandler(logHandler)
logger.setLevel(logging.DEBUG)
logger.info('###### Starting new Blender session : {}'.format(datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

def _excepthook(exc_type, exc_value, exc_traceback):
	if 'cartoblend' in exc_traceback.tb_frame.f_code.co_filename or 'CartoBlend' in exc_traceback.tb_frame.f_code.co_filename:
		logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
	sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = _excepthook #warn, this is a global variable, can be overrided by another addon

####
'''
Workaround for `sys.excepthook` thread
https://stackoverflow.com/questions/1643327/sys-excepthook-and-threading
'''
import threading

init_original = threading.Thread.__init__

def init(self, *args, **kwargs):

	init_original(self, *args, **kwargs)
	run_original = self.run

	def run_with_except_hook(*args2, **kwargs2):
		try:
			run_original(*args2, **kwargs2)
		except Exception:
			sys.excepthook(*sys.exc_info())

	self.run = run_with_except_hook

threading.Thread.__init__ = init

####


import ssl
if (not os.environ.get('PYTHONHTTPSVERIFY', '') and
	getattr(ssl, '_create_unverified_context', None)):
	ssl._create_default_https_context = ssl._create_unverified_context

#from .core.checkdeps import HAS_GDAL, HAS_PYPROJ, HAS_PIL, HAS_IMGIO
from .core.settings import settings

#Import all modules which contains classes that must be registed (classes derived from bpy.types.*)
from . import prefs
from . import geoscene

if CAM_GEOPHOTO:
	from .operators import add_camera_exif
if CAM_GEOREF:
	from .operators import add_camera_georef
if EXPORT_SHP:
	from .operators import io_export_shp
if GET_DEM:
	from .operators import io_get_dem
if IMPORT_GEORASTER:
	from .operators import io_import_georaster
if IMPORT_OSM:
	from .operators import io_import_osm
if IMPORT_SHP:
	from .operators import io_import_shp
if IMPORT_ASC:
	from .operators import io_import_asc
if IMPORT_GEOJSON:
	from .operators import io_import_geojson
if IMPORT_GPX:
	from .operators import io_import_gpx
if DELAUNAY:
	from .operators import mesh_delaunay_voronoi
if TERRAIN_NODES:
	from .operators import nodes_terrain_analysis_builder
if TERRAIN_RECLASS:
	from .operators import nodes_terrain_analysis_reclassify
if BASEMAPS:
	from .operators import view3d_mapviewer
if DROP:
	from .operators import object_drop
if EARTH_SPHERE:
	from .operators import mesh_earth_sphere


import bpy.utils.previews as iconsLib
icons_dict = {}


class BGIS_OT_logs(bpy.types.Operator):
	bl_idname = "bgis.logs"
	bl_description = 'Display CartoBlend logs'
	bl_label = "Logs"

	def execute(self, context):
		if logsFileName in bpy.data.texts:
			logs = bpy.data.texts[logsFileName]
		else:
			logs = bpy.data.texts.load(logsFilePath)
		bpy.ops.screen.area_split(direction='VERTICAL', factor=0.5)
		area = bpy.context.area
		area.type = 'TEXT_EDITOR'
		area.spaces[0].text = logs
		bpy.ops.text.reload()
		return {'FINISHED'}


####################################
# N-Panel Sidebar
####################################

# ─── Parent: Map ──────────────────────────────────────────

class VIEW3D_PT_gis_map(bpy.types.Panel):
	bl_label = "Map"
	bl_idname = "VIEW3D_PT_gis_map"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_order = 0

	def draw_header(self, context):
		self.layout.label(icon='WORLD')

	def draw(self, context):
		layout = self.layout
		if BASEMAPS:
			layout.prop(context.scene.gis_basemap, 'src', text='Source')
			layout.prop(context.scene.gis_basemap, 'lay', text='Layer')
			# Search
			row = layout.row(align=True)
			row.prop(context.scene, 'gis_goto_query', text='')
			row.operator("view3d.map_goto", icon='VIEWZOOM', text="")
			if context.scene.gis_goto_result:
				box = layout.box()
				col = box.column(align=True)
				name = context.scene.gis_goto_result
				parts = [p.strip() for p in name.split(',')]
				if parts:
					col.label(text=parts[0], icon='PINNED')
				if len(parts) > 1:
					col.label(text=', '.join(parts[1:]))
			import sys
			_mv = sys.modules.get(__package__ + '.operators.view3d_mapviewer')
			if _mv:
				_hist = getattr(_mv, '_search_history', [])
				if _hist:
					col = layout.column(align=True)
					col.label(text="Recent:", icon='TIME')
					for i, q in enumerate(_hist[:5]):
						op = col.operator("view3d.map_goto_history", text=q, icon='DOT')
						op.index = i
			# Start / Resume / Export — context-dependent
			import sys
			_mv2 = sys.modules.get(__package__ + '.operators.view3d_mapviewer')
			viewer_active = _mv2 and getattr(_mv2, '_map_viewer_active', False)
			if viewer_active:
				layout.operator("view3d.map_export", icon='CHECKMARK', text="Export as Mesh")
				row = layout.row(align=True)
				_locked = _mv2 and getattr(_mv2, '_zoom_locked', False)
				row.operator("view3d.map_lock_zoom", icon='LOCKED' if _locked else 'UNLOCKED', text="Lock Zoom", depress=_locked)
				row.operator("view3d.map_exit", icon='PANEL_CLOSE', text="Exit")
			else:
				row = layout.row(align=True)
				row.operator("view3d.map_start", icon_value=icons_dict["layers"].icon_id, text="Start")
				sub = row.row(align=True)
				sub.operator("view3d.map_resume", icon='LOOP_FORWARDS', text="Resume")
				sub.enabled = bpy.ops.view3d.map_resume.poll() if hasattr(bpy.ops.view3d, 'map_resume') else False

# ─── Scene ────────────────────────────────────────────────

class VIEW3D_PT_gis_scene(bpy.types.Panel):
	bl_label = "Scene"
	bl_idname = "VIEW3D_PT_gis_scene"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_order = 1

	def draw_header(self, context):
		self.layout.label(icon='SCENE_DATA')

	def draw(self, context):
		layout = self.layout
		row = layout.row(align=True)
		if IMPORT_OSM:
			row.operator("importgis.osm_query", icon_value=icons_dict["osm"].icon_id, text="Get OSM")
		if GET_DEM:
			row.operator("importgis.dem_query", icon_value=icons_dict["raster"].icon_id, text="Get DEM")

# Building Materials sub-panel is registered via io_import_osm.py (bl_parent_id = VIEW3D_PT_gis_scene)

# ─── Import ───────────────────────────────────────────────

class VIEW3D_PT_gis_import(bpy.types.Panel):
	bl_label = "Import"
	bl_idname = "VIEW3D_PT_gis_import"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_order = 2
	bl_options = {'DEFAULT_CLOSED'}

	def draw_header(self, context):
		self.layout.label(icon='IMPORT')

	def draw(self, context):
		layout = self.layout
		col = layout.column(align=True)
		if IMPORT_SHP:
			col.operator("importgis.shapefile_file_dialog", icon_value=icons_dict["shp"].icon_id, text='Shapefile (.shp)')
		if IMPORT_GEORASTER:
			col.operator("importgis.georaster", icon_value=icons_dict["raster"].icon_id, text="Georeferenced raster")
		if IMPORT_OSM:
			col.operator("importgis.osm_file", icon_value=icons_dict["osm"].icon_id, text="OpenStreetMap (.osm)")
		if IMPORT_GEOJSON:
			col.operator("importgis.geojson_file", icon='FILE', text="GeoJSON (.geojson)")
		if IMPORT_GPX:
			row = col.row(align=True)
			row.operator("importgis.gpx_file", icon='CURVE_PATH', text="GPX Track (.gpx)")
			import sys
			_gpx = sys.modules.get(__package__ + '.operators.io_import_gpx')
			overlay_active = _gpx and _gpx._draw_handler is not None
			row.operator("importgis.gpx_overlay_toggle", icon='OVERLAY' if overlay_active else 'GHOST_DISABLED', text="")
		if IMPORT_ASC:
			col.operator('importgis.asc_file', icon_value=icons_dict["asc"].icon_id, text="ESRI ASCII Grid (.asc)")

# ─── Export ───────────────────────────────────────────────

class VIEW3D_PT_gis_export(bpy.types.Panel):
	bl_label = "Export"
	bl_idname = "VIEW3D_PT_gis_export"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_order = 3
	bl_options = {'DEFAULT_CLOSED'}

	def draw_header(self, context):
		self.layout.label(icon='EXPORT')

	def draw(self, context):
		layout = self.layout
		if EXPORT_SHP:
			layout.operator('exportgis.shapefile', text="Shapefile (.shp)", icon_value=icons_dict["shp"].icon_id)

# ─── Parent: Tools ────────────────────────────────────────

class VIEW3D_PT_gis_tools(bpy.types.Panel):
	bl_label = "Tools"
	bl_idname = "VIEW3D_PT_gis_tools"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_order = 4
	bl_options = {'DEFAULT_CLOSED'}

	def draw_header(self, context):
		self.layout.label(icon='TOOL_SETTINGS')

	def draw(self, context):
		pass

class VIEW3D_PT_gis_mesh(bpy.types.Panel):
	bl_label = "Mesh"
	bl_idname = "VIEW3D_PT_gis_mesh"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_parent_id = "VIEW3D_PT_gis_tools"

	def draw(self, context):
		layout = self.layout
		if DELAUNAY:
			layout.operator("tesselation.delaunay", icon_value=icons_dict["delaunay"].icon_id, text='Delaunay')
			layout.operator("tesselation.voronoi", icon_value=icons_dict["voronoi"].icon_id, text='Voronoi')
		if DROP:
			layout.operator("object.drop", icon_value=icons_dict["drop"].icon_id, text='Drop to Ground')
		if EARTH_SPHERE:
			layout.operator("earth.sphere", icon="WORLD", text='lonlat to sphere')
			layout.operator("earth.curvature", icon_value=icons_dict["curve"].icon_id, text='Earth curvature correction')

class VIEW3D_PT_gis_camera(bpy.types.Panel):
	bl_label = "Camera"
	bl_idname = "VIEW3D_PT_gis_camera"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_parent_id = "VIEW3D_PT_gis_tools"
	bl_options = {'DEFAULT_CLOSED'}

	def draw(self, context):
		layout = self.layout
		if CAM_GEOREF:
			layout.operator("camera.georender", icon_value=icons_dict["georefCam"].icon_id, text='Georender')
		if CAM_GEOPHOTO:
			layout.operator("camera.geophotos", icon_value=icons_dict["exifCam"].icon_id, text='Geophotos')
			layout.operator("camera.geophotos_setactive", icon='FILE_REFRESH')

class VIEW3D_PT_gis_analysis(bpy.types.Panel):
	bl_label = "Analysis"
	bl_idname = "VIEW3D_PT_gis_analysis"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_parent_id = "VIEW3D_PT_gis_tools"

	def draw(self, context):
		layout = self.layout
		if TERRAIN_NODES:
			layout.operator("analysis.nodes", icon_value=icons_dict["terrain"].icon_id, text='Terrain analysis')

class VIEW3D_PT_gis_settings(bpy.types.Panel):
	bl_label = "Settings"
	bl_idname = "VIEW3D_PT_gis_settings"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_parent_id = "VIEW3D_PT_gis_tools"
	bl_options = {'DEFAULT_CLOSED'}

	def draw(self, context):
		layout = self.layout
		layout.operator("bgis.pref_show", icon='PREFERENCES', text='Preferences')
		layout.operator("bgis.logs", icon='TEXT', text='Show Logs')

# ─── Shortcuts (standalone, bottom) ───────────────────────

class VIEW3D_PT_gis_shortcuts(bpy.types.Panel):
	bl_label = "Map Viewer Shortcuts"
	bl_idname = "VIEW3D_PT_gis_shortcuts"
	bl_space_type = 'VIEW_3D'
	bl_region_type = 'UI'
	bl_category = 'CartoBlend'
	bl_order = 99
	bl_options = {'DEFAULT_CLOSED'}

	def draw_header(self, context):
		self.layout.label(icon='INFO')

	def draw(self, context):
		layout = self.layout
		shortcuts = [
			("Scroll / +/-", "Map zoom"),
			("Ctrl + Scroll", "View zoom (no tile change)"),
			("Alt + Scroll", "Scale x10"),
			("LMB / MMB Drag", "Pan map"),
			("Ctrl + Drag", "Pan view only"),
			("Numpad 2/4/6/8", "Pan direction"),
			("B", "Zoom box"),
			("L", "Lock/unlock zoom level"),
			("G", "Go to (search place)"),
			("O", "Options"),
			("E", "Export as mesh"),
			("Space", "Switch layer/source"),
			("ESC", "Exit"),
		]
		col = layout.column(align=True)
		for key, desc in shortcuts:
			row = col.row()
			row.label(text=key)
			row.label(text=desc)

panels = [
	VIEW3D_PT_gis_map,
	VIEW3D_PT_gis_scene,
	VIEW3D_PT_gis_import,
	VIEW3D_PT_gis_export,
	VIEW3D_PT_gis_tools,
	VIEW3D_PT_gis_mesh,
	VIEW3D_PT_gis_camera,
	VIEW3D_PT_gis_analysis,
	VIEW3D_PT_gis_settings,
	VIEW3D_PT_gis_shortcuts,
]


def register():
	#icons
	global icons_dict
	icons_dict = iconsLib.new()
	icons_dir = os.path.join(os.path.dirname(__file__), "icons")
	for icon in os.listdir(icons_dir):
		name, ext = os.path.splitext(icon)
		icons_dict.load(name, os.path.join(icons_dir, icon), 'IMAGE')

	#operators
	prefs.register()

	for panel in panels:
		try:
			bpy.utils.register_class(panel)
		except ValueError as e:
			logger.warning('{} is already registered, now unregister and retry... '.format(panel))
			bpy.utils.unregister_class(panel)
			bpy.utils.register_class(panel)

	geoscene.register()

	bpy.utils.register_class(BGIS_OT_logs)

	if BASEMAPS:
		view3d_mapviewer.register()
	if IMPORT_GEORASTER:
		io_import_georaster.register()
	if IMPORT_SHP:
		io_import_shp.register()
	if EXPORT_SHP:
		io_export_shp.register()
	if IMPORT_OSM:
		io_import_osm.register()
	if IMPORT_ASC:
		io_import_asc.register()
	if IMPORT_GEOJSON:
		io_import_geojson.register()
	if IMPORT_GPX:
		io_import_gpx.register()
	if DELAUNAY:
		mesh_delaunay_voronoi.register()
	if DROP:
		object_drop.register()
	if GET_DEM:
		io_get_dem.register()
	if CAM_GEOPHOTO:
		add_camera_exif.register()
	if CAM_GEOREF:
		add_camera_georef.register()
	if TERRAIN_NODES:
		nodes_terrain_analysis_builder.register()
	if TERRAIN_RECLASS:
		nodes_terrain_analysis_reclassify.register()
	if EARTH_SPHERE:
		mesh_earth_sphere.register()

	#N-panel is registered via panel classes, no header menu needed

	#shortcuts
	if not bpy.app.background: #no ui when running as background
		wm = bpy.context.window_manager
		kc =  wm.keyconfigs.active
		if '3D View' in kc.keymaps:
			km = kc.keymaps['3D View']
			if BASEMAPS:
				kmi = km.keymap_items.new(idname='view3d.map_start', type='NUMPAD_ASTERIX', value='PRESS')

	#Setup prefs
	preferences = bpy.context.preferences.addons[__package__].preferences
	logger.setLevel(logging.getLevelName(preferences.logLevel)) #will affect all child logger

	#update core settings according to addon prefs
	settings.proj_engine = preferences.projEngine
	settings.img_engine = preferences.imgEngine
	settings.maptiler_api_key = preferences.maptiler_api_key

def unregister():

	global icons_dict
	iconsLib.remove(icons_dict)

	if not bpy.app.background: #no ui when running as background
		wm = bpy.context.window_manager
		if '3D View' in  wm.keyconfigs.active.keymaps:
			km = wm.keyconfigs.active.keymaps['3D View']
			if BASEMAPS:
				if 'view3d.map_start' in km.keymap_items:
					kmi = km.keymap_items.remove(km.keymap_items['view3d.map_start'])

	geoscene.unregister()

	for panel in panels:
		bpy.utils.unregister_class(panel)

	bpy.utils.unregister_class(BGIS_OT_logs)

	prefs.unregister()
	if BASEMAPS:
		view3d_mapviewer.unregister()
	if IMPORT_GEORASTER:
		io_import_georaster.unregister()
	if IMPORT_SHP:
		io_import_shp.unregister()
	if EXPORT_SHP:
		io_export_shp.unregister()
	if IMPORT_OSM:
		io_import_osm.unregister()
	if IMPORT_ASC:
		io_import_asc.unregister()
	if IMPORT_GEOJSON:
		io_import_geojson.unregister()
	if IMPORT_GPX:
		io_import_gpx.unregister()
	if DELAUNAY:
		mesh_delaunay_voronoi.unregister()
	if DROP:
		object_drop.unregister()
	if GET_DEM:
		io_get_dem.unregister()
	if CAM_GEOPHOTO:
		add_camera_exif.unregister()
	if CAM_GEOREF:
		add_camera_georef.unregister()
	if TERRAIN_NODES:
		nodes_terrain_analysis_builder.unregister()
	if TERRAIN_RECLASS:
		nodes_terrain_analysis_reclassify.unregister()
	if EARTH_SPHERE:
		mesh_earth_sphere.unregister()

if __name__ == "__main__":
	register()
