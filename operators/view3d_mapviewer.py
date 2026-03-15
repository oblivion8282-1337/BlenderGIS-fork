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

#built-in imports
import math
import os
import threading
import logging
log = logging.getLogger(__name__)

#bpy imports
import bpy
from mathutils import Vector
from bpy.types import Operator, Panel, AddonPreferences
from bpy.props import StringProperty, IntProperty, FloatProperty, BoolProperty, EnumProperty, FloatVectorProperty
import addon_utils
import gpu
from gpu_extras.batch import batch_for_shader
import blf

#core imports
from ..core import HAS_GDAL, HAS_PIL, HAS_IMGIO
from ..core.proj import reprojPt, reprojBbox, dd2meters, meters2dd
from ..core.basemaps import GRIDS, SOURCES, MapService

from ..core import settings
USER_AGENT = settings.user_agent

#bgis imports
from ..geoscene import GeoScene, SK, georefManagerLayout
from ..prefs import PredefCRS

#utilities
from .utils import getBBOX, mouseTo3d
from .utils import placeObj, adjust3Dview, showTextures, rasterExtentToMesh, geoRastUVmap, addTexture #for export to mesh tool

#OSM Nominatim API module
#https://github.com/damianbraun/nominatim
from .lib.osm.nominatim import nominatimQuery

PKG, SUBPKG = __package__.split('.', maxsplit=1) #blendergis.basemaps

#Nominatim search results cache
_nominatim_results = []
_search_result_items = [] #enum items cache (prevent garbage collection)

####################

class BaseMap(GeoScene):

	"""Handle a map as background image in Blender"""

	def __init__(self, context, srckey, laykey, grdkey=None):

		#Get context
		self.context = context
		self.scn = context.scene
		GeoScene.__init__(self, self.scn)
		self.area = context.area
		self.area3d = [r for r in self.area.regions if r.type == 'WINDOW'][0]
		self.view3d = self.area.spaces.active
		self.reg3d = self.view3d.region_3d

		#Get cache destination folder in addon preferences
		prefs = context.preferences.addons[PKG].preferences
		cacheFolder = prefs.cacheFolder

		self.synchOrj = prefs.synchOrj

		#Get resampling algo preference and set the constant
		MapService.RESAMP_ALG = prefs.resamplAlg

		#Init MapService class
		self.srv = MapService(srckey, cacheFolder)
		self.name = srckey + '_' + laykey + '_' + grdkey

		#Set destination tile matrix
		if grdkey is None:
			grdkey = self.srv.srcGridKey
		if grdkey == self.srv.srcGridKey:
			self.tm = self.srv.srcTms
		else:
			#Define destination grid in map service
			self.srv.setDstGrid(grdkey)
			self.tm = self.srv.dstTms

		#Init some geoscene props if needed
		if not self.hasCRS:
			self.crs = self.tm.CRS
		if not self.hasOriginPrj:
			self.setOriginPrj(0, 0, self.synchOrj)
		if not self.hasScale:
			self.scale = 1
		if not self.hasZoom:
			self.zoom = 0

		self.lockedZoom = None

		#Set path to tiles mosaic used as background image in Blender
		#We need a format that support transparency so jpg is exclude
		#Writing to tif is generally faster than writing to png
		if bpy.data.is_saved:
			folder = os.path.dirname(bpy.data.filepath) + os.sep
			##folder = bpy.path.abspath("//"))
		else:
			##folder = bpy.context.preferences.filepaths.temporary_directory
			#Blender crease a sub-directory within the temp directory, for each session, which is cleared on exit
			folder = bpy.app.tempdir
		self.imgPath = folder + self.name + ".tif"

		#Get layer def obj
		self.layer = self.srv.layers[laykey]

		#map keys
		self.srckey = srckey
		self.laykey = laykey
		self.grdkey = grdkey

		#Thread attributes
		self.thread = None
		#Background image attributes
		self.img = None #bpy image
		self.bkg = None #empty image obj
		self.viewDstZ = None #view 3d z distance
		#Store previous request
		#TODO


	def get(self):
		'''Launch run() function in a new thread'''
		self.stop()
		#Capture bpy state before starting thread (bpy API is not thread-safe)
		self._req_area_width = self.area.width
		self._req_area_height = self.area.height
		self._req_view_location = tuple(self.reg3d.view_location)
		self.srv.start()
		self.thread = threading.Thread(target=self.run)
		self.thread.start()

	def stop(self):
		'''Stop actual thread'''
		if self.srv.running:
			self.srv.stop()
			if self.thread is not None:
				self.thread.join()

	def run(self):
		"""thread method"""
		self.mosaic = self.request()
		if self.srv.running and self.mosaic is not None:
			#save image
			self.mosaic.save(self.imgPath)
		needsPlace = self.srv.running and self.mosaic is not None
		self.srv.stop()
		if needsPlace:
			#Defer place() to main thread (bpy API is not thread-safe)
			bpy.app.timers.register(self._place_on_main_thread)

	def moveOrigin(self, dx, dy, useScale=True, updObjLoc=True):
		'''Move scene origin and update props'''
		self.moveOriginPrj(dx, dy, useScale, updObjLoc, self.synchOrj) #geoscene function

	def request(self):
		'''Request map service to build a mosaic of required tiles to cover view3d area'''
		#Get area dimension (use cached values from get() for thread safety)
		w, h = self._req_area_width, self._req_area_height

		#Get area bbox coords in destination tile matrix crs (map origin is bottom left)

		#Method 1 : Get bbox coords in scene crs and then reproject the bbox if needed
		z = self.lockedZoom if self.lockedZoom is not None else self.zoom
		res = self.tm.getRes(z)
		if self.crs == 'EPSG:4326':
			res = meters2dd(res)
		dx, dy, dz = self._req_view_location
		ox = self.crsx + (dx * self.scale)
		oy = self.crsy + (dy * self.scale)
		xmin = ox - w/2 * res * self.scale
		ymax = oy + h/2 * res * self.scale
		xmax = ox + w/2 * res * self.scale
		ymin = oy - h/2 * res * self.scale
		bbox = (xmin, ymin, xmax, ymax)
		#reproj bbox to destination grid crs if scene crs is different
		if self.crs != self.tm.CRS:
			bbox = reprojBbox(self.crs, self.tm.CRS, bbox)

		'''
		#Method 2
		bbox = getBBOX.fromTopView(self.context) #ERROR context is None ????
		bbox = bbox.toGeo(geoscn=self)
		if self.crs != self.tm.CRS:
			bbox = reprojBbox(self.crs, self.tm.CRS, bbox)
		'''

		log.debug('Bounding box request : {}'.format(bbox))

		#Stop thread if the request is same as previous
		#TODO

		if self.srv.srcGridKey == self.grdkey:
			toDstGrid = False
		else:
			toDstGrid = True

		mosaic = self.srv.getImage(self.laykey, bbox, self.zoom, toDstGrid=toDstGrid, outCRS=self.crs)

		return mosaic


	def place(self):
		'''Set map as background image'''

		#Get or load bpy image
		try:
			self.img = [img for img in bpy.data.images if img.filepath == self.imgPath and len(img.packed_files) == 0][0]
		except IndexError:
			self.img = bpy.data.images.load(self.imgPath)

		#Get or load background image
		empties = [obj for obj in self.scn.objects if obj.type == 'EMPTY']
		bkgs = [obj for obj in empties if obj.empty_display_type == 'IMAGE']
		for bkg in bkgs:
			bkg.hide_viewport = True
		try:
			self.bkg = [bkg for bkg in bkgs if bkg.data.filepath == self.imgPath and len(bkg.data.packed_files) == 0][0]
		except IndexError:
			self.bkg = bpy.data.objects.new(self.name, None) #None will create an empty
			self.bkg.empty_display_type = 'IMAGE'
			self.bkg.empty_image_depth = 'BACK'
			self.bkg.data = self.img
			self.scn.collection.objects.link(self.bkg)
		else:
			self.bkg.hide_viewport = False

		#Get some image props
		img_ox, img_oy = self.mosaic.center
		img_w, img_h = self.mosaic.size
		res = self.mosaic.pxSize.x
		#res = self.tm.getRes(self.zoom)

		#Set background size
		sizex = img_w * res / self.scale
		sizey = img_h * res / self.scale
		size = max([sizex, sizey])
		#self.bkg.empty_display_size = sizex #limited to 1000
		self.bkg.empty_display_size = 1 #a size of 1 means image width=1bu
		self.bkg.scale = (size, size, 1)

		#Set background offset (image origin does not match scene origin)
		dx = (self.crsx - img_ox) / self.scale
		dy = (self.crsy - img_oy) / self.scale
		#self.bkg.empty_image_offset = [-0.5, -0.5] #in image unit space
		self.bkg.location = (-dx, -dy, 0)
		#ratio = img_w / img_h
		#self.bkg.offset_y = -dy * ratio #https://developer.blender.org/T48034

		#Get 3d area's number of pixels and resulting size at the requested zoom level resolution
		#dst =  max( [self.area3d.width, self.area3d.height] ) #WARN return [1,1] !!!!????
		dst =  max( [self.area.width, self.area.height] )
		z = self.lockedZoom if self.lockedZoom is not None else self.zoom
		res = self.tm.getRes(z)
		dst = dst * res / self.scale

		#Compute 3dview FOV and needed z distance to see the maximum extent that
		#can be draw at full res (area 3d needs enough pixels otherwise the image will appears downgraded)
		#WARN seems these formulas does not works properly in Blender2.8
		view3D_aperture = 36 #Blender constant (see source code)
		view3D_zoom = 2 #Blender constant (see source code)
		fov = 2 * math.atan(view3D_aperture / (self.view3d.lens*2) ) #fov equation
		fov = math.atan(math.tan(fov/2) * view3D_zoom) * 2 #zoom correction (see source code)
		zdst = (dst/2) / math.tan(fov/2) #trigo
		zdst = math.floor(zdst) #make sure no downgrade
		self.reg3d.view_distance = zdst
		self.viewDstZ = zdst

		#Update image drawing
		self.bkg.data.reload()

	def _place_on_main_thread(self):
		'''Timer callback to execute place() safely on the main thread'''
		self.place()
		return None #don't repeat




####################################
def drawInfosText(self, context):
	#Get contexts
	scn = context.scene
	area = context.area
	area3d = [reg for reg in area.regions if reg.type == 'WINDOW'][0]
	view3d = area.spaces.active
	reg3d = view3d.region_3d
	#Get map props stored in scene
	geoscn = GeoScene(scn)
	zoom = geoscn.zoom
	scale = geoscn.scale
	#
	txt = "Map view : "
	txt += "Zoom " + str(zoom)
	if self.map.lockedZoom is not None:
		txt += " (Locked)"
	txt += " - Scale 1:" + str(int(scale))
	'''
	# view3d distance
	dst = reg3d.view_distance
	if dst > 1000:
		dst /= 1000
		unit = 'km'
	else:
		unit = 'm'
	txt += ' 3D View distance ' + str(int(dst)) + ' ' + unit
	'''
	# cursor crs coords
	txt += ' ' + str((int(self.posx), int(self.posy)))
	# progress
	txt += ' ' + self.progress
	context.area.header_text_set(txt)


def drawZoomBox(self, context):
	if self.zoomBoxMode and not self.zoomBoxDrag:
		# before selection starts draw infinite cross
		px, py = self.zb_xmax, self.zb_ymax
		p1 = (0, py, 0)
		p2 = (context.area.width, py, 0)
		p3 = (px, 0, 0)
		p4 = (px, context.area.height, 0)
		coords = [p1, p2, p3, p4]
		shader = gpu.shader.from_builtin('UNIFORM_COLOR')
		batch = batch_for_shader(shader, 'LINES', {"pos": coords})
		shader.bind()
		shader.uniform_float("color", (0, 0, 0, 1))
		batch.draw(shader)

	elif self.zoomBoxMode and self.zoomBoxDrag:
		p1 = (self.zb_xmin, self.zb_ymin, 0)
		p2 = (self.zb_xmin, self.zb_ymax, 0)
		p3 = (self.zb_xmax, self.zb_ymax, 0)
		p4 = (self.zb_xmax, self.zb_ymin, 0)
		coords = [p1, p2, p2, p3, p3, p4, p4, p1]
		shader = gpu.shader.from_builtin('UNIFORM_COLOR')
		batch = batch_for_shader(shader, 'LINES', {"pos": coords})
		shader.bind()
		shader.uniform_float("color", (0, 0, 0, 1))
		batch.draw(shader)


# Help overlay persistent state — lives at module level, independent of operator lifecycle
_help_visible = False
_help_mouse_x = 0
_help_mouse_y = 0
_map_viewer_active = False
_help_draw_handler = None

# Help button
HELP_BTN_H = 38
HELP_BTN_MARGIN = 16
HELP_BTN_PAD_X = 20
HELP_BTN_LABEL = "?  Shortcuts"

def getHelpBtnRect(context):
	"""Return (x, y, w, h) of the help button in bottom-right corner"""
	font_id = 0
	blf.size(font_id, 15)
	tw = blf.dimensions(font_id, HELP_BTN_LABEL)[0]
	btn_w = int(tw + HELP_BTN_PAD_X * 2)
	x = context.area.width - btn_w - HELP_BTN_MARGIN
	y = HELP_BTN_MARGIN
	return x, y, btn_w, HELP_BTN_H

def isInsideRect(mx, my, x, y, w, h):
	return x <= mx <= x + w and y <= my <= y + h

def drawRoundedRect(x, y, w, h, color, radius=6):
	"""Draw a rectangle with rounded corners"""
	shader = gpu.shader.from_builtin('UNIFORM_COLOR')
	# Simple approach: draw center rect + edge rects (good enough for small radius)
	verts = [
		(x + radius, y, 0), (x + w - radius, y, 0),
		(x + radius, y + h, 0), (x + w - radius, y + h, 0),
	]
	batch = batch_for_shader(shader, 'TRI_STRIP', {"pos": verts})
	shader.bind()
	shader.uniform_float("color", color)
	batch.draw(shader)
	# left edge
	verts = [
		(x, y + radius, 0), (x + radius, y + radius, 0),
		(x, y + h - radius, 0), (x + radius, y + h - radius, 0),
	]
	batch = batch_for_shader(shader, 'TRI_STRIP', {"pos": verts})
	batch.draw(shader)
	# right edge
	verts = [
		(x + w - radius, y + radius, 0), (x + w, y + radius, 0),
		(x + w - radius, y + h - radius, 0), (x + w, y + h - radius, 0),
	]
	batch = batch_for_shader(shader, 'TRI_STRIP', {"pos": verts})
	batch.draw(shader)
	# corners (small squares to approximate rounding)
	for cx, cy in [(x, y), (x + w - radius, y), (x, y + h - radius), (x + w - radius, y + h - radius)]:
		verts = [
			(cx + 1, cy, 0), (cx + radius, cy, 0),
			(cx, cy + 1, 0), (cx + radius, cy + radius, 0),
		]
		batch = batch_for_shader(shader, 'TRI_STRIP', {"pos": verts})
		batch.draw(shader)

def _drawHelpPersistent():
	"""Persistent draw callback — registered once at addon load, independent of operator"""
	global _help_visible, _help_mouse_x, _help_mouse_y, _map_viewer_active
	if not _map_viewer_active:
		return

	context = bpy.context
	if context.area is None or context.area.type != 'VIEW_3D':
		return

	gpu.state.blend_set('ALPHA')

	# -- Draw help button --
	bx, by, bw, bh = getHelpBtnRect(context)
	hover = isInsideRect(_help_mouse_x, _help_mouse_y, bx, by, bw, bh)
	if _help_visible:
		btn_color = (0.25, 0.55, 0.85, 0.92)
	elif hover:
		btn_color = (0.30, 0.30, 0.30, 0.88)
	else:
		btn_color = (0.20, 0.20, 0.20, 0.78)

	drawRoundedRect(bx, by, bw, bh, btn_color)

	# Button label
	font_id = 0
	blf.size(font_id, 15)
	tw = blf.dimensions(font_id, HELP_BTN_LABEL)[0]
	th = blf.dimensions(font_id, HELP_BTN_LABEL)[1]
	blf.position(font_id, bx + (bw - tw) / 2, by + (bh - th) / 2 + 1, 0)
	blf.color(font_id, 1.0, 1.0, 1.0, 0.95)
	blf.draw(font_id, HELP_BTN_LABEL)

	# -- Draw shortcuts panel if toggled --
	if not _help_visible:
		gpu.state.blend_set('NONE')
		return

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

	font_id = 0
	blf.size(font_id, 13)
	line_h = 22
	pad_x, pad_y = 16, 12
	key_col_w = 160

	max_desc_w = 0
	for key, desc in shortcuts:
		w = blf.dimensions(font_id, desc)[0]
		if w > max_desc_w:
			max_desc_w = w

	panel_w = key_col_w + max_desc_w + pad_x * 3
	panel_h = len(shortcuts) * line_h + pad_y * 2 + 30

	# position panel: right-aligned, opening upward above button
	px = context.area.width - panel_w - HELP_BTN_MARGIN
	py = by + bh + 10

	# panel background
	drawRoundedRect(px, py, panel_w, panel_h, (0.12, 0.12, 0.12, 0.90))

	# panel border
	shader = gpu.shader.from_builtin('UNIFORM_COLOR')
	border = [
		(px, py, 0), (px + panel_w, py, 0),
		(px + panel_w, py, 0), (px + panel_w, py + panel_h, 0),
		(px + panel_w, py + panel_h, 0), (px, py + panel_h, 0),
		(px, py + panel_h, 0), (px, py, 0)
	]
	batch = batch_for_shader(shader, 'LINES', {"pos": border})
	shader.bind()
	shader.uniform_float("color", (0.35, 0.35, 0.35, 0.5))
	batch.draw(shader)

	# title
	blf.size(font_id, 14)
	ty = py + panel_h - pad_y - 14
	blf.position(font_id, px + pad_x, ty, 0)
	blf.color(font_id, 0.9, 0.9, 0.9, 1.0)
	blf.draw(font_id, "Map Viewer Shortcuts")

	# separator line
	sep_y = ty - 8
	sep_verts = [(px + pad_x, sep_y, 0), (px + panel_w - pad_x, sep_y, 0)]
	batch = batch_for_shader(shader, 'LINES', {"pos": sep_verts})
	shader.bind()
	shader.uniform_float("color", (0.5, 0.5, 0.5, 0.5))
	batch.draw(shader)

	# shortcut lines
	blf.size(font_id, 13)
	cy = sep_y - line_h
	for key, desc in shortcuts:
		blf.position(font_id, px + pad_x, cy, 0)
		blf.color(font_id, 0.6, 0.8, 1.0, 1.0)
		blf.draw(font_id, key)
		blf.position(font_id, px + pad_x + key_col_w, cy, 0)
		blf.color(font_id, 0.78, 0.78, 0.78, 1.0)
		blf.draw(font_id, desc)
		cy -= line_h

	gpu.state.blend_set('NONE')


###############

class VIEW3D_OT_map_start(Operator):

	bl_idname = "view3d.map_start"
	bl_description = 'Toggle 2d map navigation'
	bl_label = "Basemap"
	bl_options = {'REGISTER'}

	#special function to auto redraw an operator popup called through invoke_props_dialog
	def check(self, context):
		return True

	def listSources(self, context):
		srcItems = []
		for srckey, src in SOURCES.items():
			#put each item in a tuple (key, label, tooltip)
			srcItems.append( (srckey, src['name'], src['description']) )
		return srcItems

	def listGrids(self, context):
		grdItems = []
		src = SOURCES[self.src]
		for gridkey, grd in GRIDS.items():
			#put each item in a tuple (key, label, tooltip)
			if gridkey == src['grid']:
				#insert at first position
				grdItems.insert(0, (gridkey, grd['name']+' (source)', grd['description']) )
			else:
				grdItems.append( (gridkey, grd['name'], grd['description']) )
		return grdItems

	def listLayers(self, context):
		layItems = []
		src = SOURCES[self.src]
		for laykey, lay in src['layers'].items():
			#put each item in a tuple (key, label, tooltip)
			layItems.append( (laykey, lay['name'], lay['description']) )
		return layItems


	src: EnumProperty(
				name = "Map",
				description = "Choose map service source",
				items = listSources
				)

	grd: EnumProperty(
				name = "Grid",
				description = "Choose cache tiles matrix",
				items = listGrids
				)

	lay: EnumProperty(
				name = "Layer",
				description = "Choose layer",
				items = listLayers
				)


	dialog: StringProperty(default='MAP') # 'MAP', 'SEARCH', 'OPTIONS'

	query: StringProperty(name="Go to")

	zoom: IntProperty(name='Zoom level', min=0, max=25)

	recenter: BoolProperty(name='Center to existing objects')

	def draw(self, context):
		addonPrefs = context.preferences.addons[PKG].preferences
		scn = context.scene
		layout = self.layout

		if self.dialog == 'SEARCH':
				layout.prop(self, 'query')

		elif self.dialog == 'OPTIONS':
			#viewPrefs = context.preferences.view
			#layout.prop(viewPrefs, "use_zoom_to_mouse")
			layout.prop(addonPrefs, "zoomToMouse")
			layout.prop(addonPrefs, "lockObj")
			layout.prop(addonPrefs, "lockOrigin")
			layout.prop(addonPrefs, "synchOrj")

		elif self.dialog == 'MAP':
			layout.prop(self, 'src', text='Source')
			layout.prop(self, 'lay', text='Layer')
			col = layout.column()
			if not HAS_GDAL:
				col.enabled = False
				col.label(text='(No raster reprojection support)')
			col.prop(self, 'grd', text='Tile matrix set')

			#srcCRS = GRIDS[SOURCES[self.src]['grid']]['CRS']
			grdCRS = GRIDS[self.grd]['CRS']
			row = layout.row()
			#row.alignment = 'RIGHT'
			desc = PredefCRS.getName(grdCRS)
			if desc is not None:
				row.label(text='CRS: ' + desc)
			else:
				row.label(text='CRS: ' + grdCRS)

			row = layout.row()
			row.prop(self, 'recenter')

			geoscn = GeoScene(scn)
			if geoscn.isPartiallyGeoref:
				#layout.separator()
				georefManagerLayout(self, context)

			#row = layout.row()
			#row.label(text='Map scale:')
			#row.prop(scn, '["'+SK.SCALE+'"]', text='')


	def invoke(self, context, event):

		if not HAS_PIL and not HAS_GDAL and not HAS_IMGIO:
			self.report({'ERROR'}, "No imaging library available. ImageIO module was not correctly installed.")
			return {'CANCELLED'}

		if not context.area.type == 'VIEW_3D':
			self.report({'WARNING'}, "View3D not found, cannot run operator")
			return {'CANCELLED'}

		#Update zoom
		geoscn = GeoScene(context.scene)
		if geoscn.hasZoom:
			self.zoom = geoscn.zoom

		#Display dialog
		return context.window_manager.invoke_props_dialog(self)

	def cancel(self, context):
		#If invoked from map viewer (G, O, SPACE), restart it on cancel
		if self.dialog in ('SEARCH', 'OPTIONS'):
			bpy.ops.view3d.map_viewer('INVOKE_DEFAULT',
				srckey=self.src, laykey=self.lay, grdkey=self.grd, recenter=False)

	def execute(self, context):
		scn = context.scene
		geoscn = GeoScene(scn)
		prefs = context.preferences.addons[PKG].preferences

		#check cache folder
		folder = prefs.cacheFolder
		if folder == "" or not os.path.exists(folder):
			self.report({'ERROR'}, "Please define a valid cache folder path in addon's preferences")
			return {'CANCELLED'}
		if not os.access(folder, os.X_OK | os.W_OK):
			self.report({'ERROR'}, "The selected cache folder has no write access")
			return {'CANCELLED'}

		if self.dialog == 'MAP':
			grdCRS = GRIDS[self.grd]['CRS']
			if geoscn.isBroken:
				self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
				return {'CANCELLED'}
			#set scene crs as grid crs
			#if not geoscn.hasCRS:
				#geoscn.crs = grdCRS
			#Check if raster reproj is needed
			if geoscn.hasCRS and geoscn.crs != grdCRS and not HAS_GDAL:
				self.report({'ERROR'}, "Please install gdal to enable raster reprojection support")
				return {'CANCELLED'}

		#Move scene origin to the researched place
		if self.dialog == 'SEARCH':
			try:
				global _nominatim_results
				_nominatim_results = nominatimQuery(self.query, referer='bgis', user_agent=USER_AGENT)
			except Exception as e:
				log.error('Failed Nominatim query', exc_info=True)
				_nominatim_results = []

			if not _nominatim_results:
				self.report({'INFO'}, "No location found")
				#Fall through to restart map viewer
			else:
				#Show results picker (it will start map viewer after selection)
				self.dialog = 'MAP'
				bpy.ops.view3d.map_search_results('INVOKE_DEFAULT',
					srckey=self.src, laykey=self.lay, grdkey=self.grd)
				return {'FINISHED'}

		#Start map viewer operator
		self.dialog = 'MAP' #reinit dialog type
		bpy.ops.view3d.map_viewer('INVOKE_DEFAULT', srckey=self.src, laykey=self.lay, grdkey=self.grd, recenter=self.recenter)

		return {'FINISHED'}





###############


class VIEW3D_OT_map_viewer(Operator):

	bl_idname = "view3d.map_viewer"
	bl_description = 'Toggle 2d map navigation'
	bl_label = "Map viewer"
	bl_options = {'INTERNAL'}

	srckey: StringProperty()

	grdkey: StringProperty()

	laykey: StringProperty()

	recenter: BoolProperty()

	@classmethod
	def poll(cls, context):
		return context.area.type == 'VIEW_3D'


	def __del__(self):
		if getattr(self, 'restart', False):
			bpy.ops.view3d.map_start('INVOKE_DEFAULT', src=self.srckey, lay=self.laykey, grd=self.grdkey, dialog=self.dialog)


	def invoke(self, context, event):

		self.restart = False
		self.dialog = 'MAP' # dialog name for MAP_START >> string in  ['MAP', 'SEARCH', 'OPTIONS']

		self.moveFactor = 0.1

		self.prefs = context.preferences.addons[PKG].preferences
		#Option to adjust or not objects location when panning
		self.updObjLoc = self.prefs.lockObj #if georef is locked then we need to adjust object location after each pan

		#Help overlay — activate persistent draw handler
		global _map_viewer_active
		_map_viewer_active = True

		#Add draw callback to view space
		args = (self, context)
		self._drawTextHandler = bpy.types.SpaceView3D.draw_handler_add(drawInfosText, args, 'WINDOW', 'POST_PIXEL')
		self._drawZoomBoxHandler = bpy.types.SpaceView3D.draw_handler_add(drawZoomBox, args, 'WINDOW', 'POST_PIXEL')

		#Add modal handler and init a timer
		context.window_manager.modal_handler_add(self)
		self.timer = context.window_manager.event_timer_add(0.04, window=context.window)

		#Switch to top view ortho (center to origin)
		view3d = context.area.spaces.active
		bpy.ops.view3d.view_axis(type='TOP')
		view3d.region_3d.view_perspective = 'ORTHO'
		context.scene.cursor.location = (0, 0, 0)
		if not self.prefs.lockOrigin:
			#bpy.ops.view3d.view_center_cursor()
			view3d.region_3d.view_location = (0, 0, 0)

		#Init some properties
		# tag if map is currently drag
		self.inMove = False
		self.x1, self.y1 = 0, 0
		# mouse crs coordinates reported in draw callback
		self.posx, self.posy = 0, 0
		# thread progress infos reported in draw callback
		self.progress = ''
		# Zoom box
		self.zoomBoxMode = False
		self.zoomBoxDrag = False
		self.zb_xmin, self.zb_xmax = 0, 0
		self.zb_ymin, self.zb_ymax = 0, 0

		#Get map
		self.map = BaseMap(context, self.srckey, self.laykey, self.grdkey)

		if self.recenter and len(context.scene.objects) > 0:
			scnBbox = getBBOX.fromScn(context.scene).to2D()
			w, h = scnBbox.dimensions
			px_diag = math.sqrt(context.area.width**2 + context.area.height**2)
			dst_diag = math.sqrt( w**2 + h**2 )
			targetRes = dst_diag / px_diag
			z = self.map.tm.getNearestZoom(targetRes, rule='lower')
			resFactor = self.map.tm.getFromToResFac(self.map.zoom, z)
			context.region_data.view_distance *= resFactor
			x, y = scnBbox.center
			if self.prefs.lockOrigin:
				context.region_data.view_location = (x, y, 0)
			else:
				self.map.moveOrigin(x, y)
			self.map.zoom = z

		self.map.get()

		return {'RUNNING_MODAL'}


	def modal(self, context, event):
		global _help_visible, _help_mouse_x, _help_mouse_y, _map_viewer_active

		context.area.tag_redraw()
		scn = bpy.context.scene

		if event.type == 'TIMER':
			#report thread progression
			self.progress = self.map.srv.report
			return {'PASS_THROUGH'}

		#When help panel is open, consume all input except:
		# - mouse move (for hover), timer, help button click (to close)
		#Update help overlay mouse position
		if event.type == 'MOUSEMOVE':
			_help_mouse_x = event.mouse_region_x
			_help_mouse_y = event.mouse_region_y


		if event.type in ['WHEELUPMOUSE', 'NUMPAD_PLUS']:

			if event.value == 'PRESS':

				if event.alt:
					# map scale up
					self.map.scale *= 10
					self.map.place()
					#Scale existing objects
					for obj in scn.objects:
						obj.location /= 10
						obj.scale /= 10

				elif event.ctrl:
					# view3d zoom up
					dst = context.region_data.view_distance
					context.region_data.view_distance -= dst * self.moveFactor
					if self.prefs.zoomToMouse:
						mouseLoc = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
						viewLoc = context.region_data.view_location
						deltaVect = (mouseLoc - viewLoc) * self.moveFactor
						viewLoc += deltaVect
				else:
					# map zoom up
					if self.map.zoom < self.map.layer.zmax and self.map.zoom < self.map.tm.nbLevels-1:
						self.map.zoom += 1
						if self.map.lockedZoom is None:
							resFactor = self.map.tm.getNextResFac(self.map.zoom)
							if not self.prefs.zoomToMouse:
								context.region_data.view_distance *= resFactor
							else:
								#Progressibly zoom to cursor
								dst = context.region_data.view_distance
								dst2 = dst * resFactor
								context.region_data.view_distance = dst2
								mouseLoc = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
								viewLoc = context.region_data.view_location
								moveFactor = (dst - dst2) / dst
								deltaVect = (mouseLoc - viewLoc) * moveFactor
								if self.prefs.lockOrigin:
									viewLoc += deltaVect
								else:
									dx, dy, dz = deltaVect
									if not self.prefs.lockObj and self.map.bkg is not None:
										self.map.bkg.location  -= deltaVect
									self.map.moveOrigin(dx, dy, updObjLoc=self.updObjLoc)
						self.map.get()


		if event.type in ['WHEELDOWNMOUSE', 'NUMPAD_MINUS']:

			if event.value == 'PRESS':

				if event.alt:
					#map scale down
					s = self.map.scale / 10
					if s < 1: s = 1
					self.map.scale = s
					self.map.place()
					#Scale existing objects
					for obj in scn.objects:
						obj.location *= 10
						obj.scale *= 10

				elif event.ctrl:
					#view3d zoom down
					dst = context.region_data.view_distance
					context.region_data.view_distance += dst * self.moveFactor
					if self.prefs.zoomToMouse:
						mouseLoc = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
						viewLoc = context.region_data.view_location
						deltaVect = (mouseLoc - viewLoc) * self.moveFactor
						viewLoc -= deltaVect
				else:
					#map zoom down
					if self.map.zoom > self.map.layer.zmin and self.map.zoom > 0:
						self.map.zoom -= 1
						if self.map.lockedZoom is None:
							resFactor = self.map.tm.getPrevResFac(self.map.zoom)
							if not self.prefs.zoomToMouse:
								context.region_data.view_distance *= resFactor
							else:
								#Progressibly zoom to cursor
								dst = context.region_data.view_distance
								dst2 = dst * resFactor
								context.region_data.view_distance = dst2
								mouseLoc = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
								viewLoc = context.region_data.view_location
								moveFactor = (dst - dst2) / dst
								deltaVect = (mouseLoc - viewLoc) * moveFactor
								if self.prefs.lockOrigin:
									viewLoc += deltaVect
								else:
									dx, dy, dz = deltaVect
									if not self.prefs.lockObj and self.map.bkg is not None:
										self.map.bkg.location  -= deltaVect
									self.map.moveOrigin(dx, dy, updObjLoc=self.updObjLoc)
						self.map.get()



		if event.type == 'MOUSEMOVE':

			#Report mouse location coords in projeted crs
			loc = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
			self.posx, self.posy = self.map.view3dToProj(loc.x, loc.y)

			if self.zoomBoxMode:
				self.zb_xmax, self.zb_ymax = event.mouse_region_x, event.mouse_region_y

			#Drag background image (edit its offset values)
			if self.inMove:
				loc1 = mouseTo3d(context, self.x1, self.y1)
				loc2 = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
				dlt = loc1 - loc2
				if event.ctrl or self.prefs.lockOrigin:
					context.region_data.view_location = self.viewLoc1 + dlt
				else:
					#Move background image
					if self.map.bkg is not None:
						self.map.bkg.location[0] = self.offx1 - dlt.x
						self.map.bkg.location[1] = self.offy1 - dlt.y
					#Move existing objects (only top level parent)
					if self.updObjLoc:
						topParents = [obj for obj in scn.objects if not obj.parent]
						for i, obj in enumerate(topParents):
							if obj == self.map.bkg: #the background empty used as basemap
								continue
							loc1 = self.objsLoc1[i]
							obj.location.x = loc1.x - dlt.x
							obj.location.y = loc1.y - dlt.y


		#Track mouse for help button hover
		if event.type == 'MOUSEMOVE':
			self._help_mouse_x = event.mouse_region_x
			self._help_mouse_y = event.mouse_region_y

		#HELP BUTTON CLICK
		if event.type == 'LEFTMOUSE' and event.value == 'PRESS' and not self.zoomBoxMode:
			bx, by, bw, bh = getHelpBtnRect(context)
			if isInsideRect(event.mouse_region_x, event.mouse_region_y, bx, by, bw, bh):
				_help_visible = not _help_visible
				return {'RUNNING_MODAL'}

		if event.type in {'LEFTMOUSE', 'MIDDLEMOUSE'}:

			if event.value == 'PRESS' and not self.zoomBoxMode:
				#Get click mouse position and background image offset (if exist)
				self.x1, self.y1 = event.mouse_region_x, event.mouse_region_y
				self.viewLoc1 = context.region_data.view_location.copy()
				if not event.ctrl:
					#Stop thread now, because we don't know when the mouse click will be released
					self.map.stop()
					if not self.prefs.lockOrigin:
						if self.map.bkg is not None:
							self.offx1 = self.map.bkg.location[0]
							self.offy1 = self.map.bkg.location[1]
						#Store current location of each objects (only top level parent)
						self.objsLoc1 = [obj.location.copy() for obj in scn.objects if not obj.parent]
				#Tag that map is currently draging
				self.inMove = True

			if event.value == 'RELEASE' and not self.zoomBoxMode:
				wasMoving = self.inMove
				self.inMove = False
				if wasMoving and not event.ctrl:
					if not self.prefs.lockOrigin:
						#Compute final shift
						loc1 = mouseTo3d(context, self.x1, self.y1)
						loc2 = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
						dlt = loc1 - loc2
						#Update map (do not update objects location because it was updated while mouse move)
						self.map.moveOrigin(dlt.x, dlt.y, updObjLoc=False)
					self.map.get()


			if event.value == 'PRESS' and self.zoomBoxMode:
				self.zoomBoxDrag = True
				self.zb_xmin, self.zb_ymin = event.mouse_region_x, event.mouse_region_y

			if event.value == 'RELEASE' and self.zoomBoxMode:
				#Get final zoom box
				xmax = max(event.mouse_region_x, self.zb_xmin)
				ymax = max(event.mouse_region_y, self.zb_ymin)
				xmin = min(event.mouse_region_x, self.zb_xmin)
				ymin = min(event.mouse_region_y, self.zb_ymin)
				#Exit zoom box mode
				self.zoomBoxDrag = False
				self.zoomBoxMode = False
				context.window.cursor_set('DEFAULT')
				#Compute the move to box origin
				w = xmax - xmin
				h = ymax - ymin
				cx = xmin + w/2
				cy = ymin + h/2
				loc = mouseTo3d(context, cx, cy)
				#Compute target resolution
				px_diag = math.sqrt(context.area.width**2 + context.area.height**2)
				mapRes = self.map.tm.getRes(self.map.zoom)
				dst_diag = math.sqrt( (w*mapRes)**2 + (h*mapRes)**2)
				targetRes = dst_diag / px_diag
				z = self.map.tm.getNearestZoom(targetRes, rule='lower')
				resFactor = self.map.tm.getFromToResFac(self.map.zoom, z)
				#Preview
				context.region_data.view_distance *= resFactor
				if self.prefs.lockOrigin:
					context.region_data.view_location = loc
				else:
					self.map.moveOrigin(loc.x, loc.y, updObjLoc=self.updObjLoc)
				self.map.zoom = z
				self.map.get()


		if event.type in ['LEFT_CTRL', 'RIGHT_CTRL']:

			if event.value == 'PRESS':
				self._viewDstZ = context.region_data.view_distance
				self._viewLoc = context.region_data.view_location.copy()

			if event.value == 'RELEASE':
				#restore view 3d distance and location
				context.region_data.view_distance = self._viewDstZ
				context.region_data.view_location = self._viewLoc


		#NUMPAD MOVES (3D VIEW or MAP)
		if event.value == 'PRESS' and event.type in ['NUMPAD_2', 'NUMPAD_4', 'NUMPAD_6', 'NUMPAD_8']:
			delta = self.map.bkg.scale.x * self.moveFactor
			if event.type == 'NUMPAD_4':
				if event.ctrl or self.prefs.lockOrigin:
					context.region_data.view_location += Vector( (-delta, 0, 0) )
				else:
					self.map.moveOrigin(-delta, 0, updObjLoc=self.updObjLoc)
			if event.type == 'NUMPAD_6':
				if event.ctrl or self.prefs.lockOrigin:
					context.region_data.view_location += Vector( (delta, 0, 0) )
				else:
					self.map.moveOrigin(delta, 0, updObjLoc=self.updObjLoc)
			if event.type == 'NUMPAD_2':
				if event.ctrl or self.prefs.lockOrigin:
					context.region_data.view_location += Vector( (0, -delta, 0) )
				else:
					self.map.moveOrigin(0, -delta, updObjLoc=self.updObjLoc)
			if event.type == 'NUMPAD_8':
				if event.ctrl or self.prefs.lockOrigin:
					context.region_data.view_location += Vector( (0, delta, 0) )
				else:
					self.map.moveOrigin(0, delta, updObjLoc=self.updObjLoc)
			if not event.ctrl:
				self.map.get()

		#SWITCH LAYER
		if event.type == 'SPACE':
			self.map.stop()
			bpy.types.SpaceView3D.draw_handler_remove(self._drawTextHandler, 'WINDOW')
			bpy.types.SpaceView3D.draw_handler_remove(self._drawZoomBoxHandler, 'WINDOW')

			context.area.header_text_set(None)
			self.restart = True
			return {'FINISHED'}

		#GO TO
		if event.type == 'G':
			self.map.stop()
			bpy.types.SpaceView3D.draw_handler_remove(self._drawTextHandler, 'WINDOW')
			bpy.types.SpaceView3D.draw_handler_remove(self._drawZoomBoxHandler, 'WINDOW')

			context.area.header_text_set(None)
			self.restart = True
			self.dialog = 'SEARCH'
			return {'FINISHED'}

		#OPTIONS
		if event.type == 'O':
			self.map.stop()
			bpy.types.SpaceView3D.draw_handler_remove(self._drawTextHandler, 'WINDOW')
			bpy.types.SpaceView3D.draw_handler_remove(self._drawZoomBoxHandler, 'WINDOW')

			context.area.header_text_set(None)
			self.restart = True
			self.dialog = 'OPTIONS'
			return {'FINISHED'}

		#Lock/unlock 3d view zoom distance
		if event.type == 'L' and event.value == 'PRESS':
			if self.map.lockedZoom is None:
				self.map.lockedZoom = self.map.zoom
			else:
				self.map.lockedZoom = None
				self.map.get()


		#ZOOM BOX
		if event.type == 'B' and event.value == 'PRESS':
			self.map.stop()
			self.zoomBoxMode = True
			self.zb_xmax, self.zb_ymax = event.mouse_region_x, event.mouse_region_y
			context.window.cursor_set('CROSSHAIR')

		#EXPORT
		if event.type == 'E' and event.value == 'PRESS':
			#
			if not self.map.srv.running and self.map.mosaic is not None:
				_map_viewer_active = False
				_help_visible = False
				self.map.stop()
				self.map.bkg.hide_viewport = True

				bpy.types.SpaceView3D.draw_handler_remove(self._drawTextHandler, 'WINDOW')
				bpy.types.SpaceView3D.draw_handler_remove(self._drawZoomBoxHandler, 'WINDOW')
				context.area.header_text_set(None)

				#Copy image to new datablock
				bpyImg = bpy.data.images.load(self.map.imgPath) #(self.map.img.filepath)
				name = 'EXPORT_' + self.map.srckey + '_' + self.map.laykey + '_' + self.map.grdkey
				bpyImg.name = name
				bpyImg.pack()

				#Add new attribute to GeoRaster (used by geoRastUVmap function)
				rast = self.map.mosaic
				setattr(rast, 'bpyImg', bpyImg)

				#Create Mesh
				dx, dy = self.map.getOriginPrj()
				mesh = rasterExtentToMesh(name, rast, dx, dy, pxLoc='CORNER')

				#Create object
				obj = placeObj(mesh, name)

				#UV mapping
				uvTxtLayer = mesh.uv_layers.new(name='rastUVmap')# Add UV map texture layer
				geoRastUVmap(obj, uvTxtLayer, rast, dx, dy)

				#Create material
				mat = bpy.data.materials.new('rastMat')
				obj.data.materials.append(mat)
				addTexture(mat, bpyImg, uvTxtLayer)

				#Adjust 3d view and display textures
				if self.prefs.adjust3Dview:
					adjust3Dview(context, getBBOX.fromObj(obj))
				if self.prefs.forceTexturedSolid:
					showTextures(context)

				return {'FINISHED'}

		#EXIT
		if event.type == 'ESC' and event.value == 'PRESS':
			if self.zoomBoxMode:
				self.zoomBoxDrag = False
				self.zoomBoxMode = False
				context.window.cursor_set('DEFAULT')
			else:
				_map_viewer_active = False
				_help_visible = False
				self.map.stop()
				bpy.types.SpaceView3D.draw_handler_remove(self._drawTextHandler, 'WINDOW')
				bpy.types.SpaceView3D.draw_handler_remove(self._drawZoomBoxHandler, 'WINDOW')
				context.area.header_text_set(None)
				return {'CANCELLED'}



		return {'RUNNING_MODAL'}



####################################

class VIEW3D_OT_map_search(bpy.types.Operator):

	bl_idname = "view3d.map_search"
	bl_description = 'Search for a place and move scene origin to it'
	bl_label = "Map search"
	bl_options = {'INTERNAL'}

	query: StringProperty(name="Go to")

	def invoke(self, context, event):
		geoscn = GeoScene(context.scene)
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken")
			return {'CANCELLED'}
		return context.window_manager.invoke_props_dialog(self)

	def execute(self, context):
		geoscn = GeoScene(context.scene)
		prefs = context.preferences.addons[PKG].preferences
		try:
			results = nominatimQuery(self.query, referer='bgis', user_agent=USER_AGENT)
		except Exception as e:
			log.error('Failed Nominatim query', exc_info=True)
			return {'CANCELLED'}
		if len(results) == 0:
			return {'CANCELLED'}
		else:
			log.debug('Nominatim search results : {}'.format([r['display_name'] for r in results]))
			result = results[0]
			lat, lon = float(result['lat']), float(result['lon'])
			if geoscn.isGeoref:
				geoscn.updOriginGeo(lon, lat, updObjLoc=prefs.lockObj)
			else:
				geoscn.setOriginGeo(lon, lat)
			#Auto-zoom based on Nominatim bounding box
			if 'boundingbox' in result:
				bbox = result['boundingbox'] #[south_lat, north_lat, west_lon, east_lon]
				lat_extent = abs(float(bbox[1]) - float(bbox[0]))
				lon_extent = abs(float(bbox[3]) - float(bbox[2]))
				max_extent = max(lat_extent, lon_extent)
				if max_extent > 0:
					zoom = int(math.log2(360 / max_extent))
					zoom = max(2, min(zoom, 16))
					geoscn.zoom = zoom
		return {'FINISHED'}


class VIEW3D_OT_map_search_results(bpy.types.Operator):
	"""Pick a location from search results"""

	bl_idname = "view3d.map_search_results"
	bl_label = "Search Results"
	bl_options = {'INTERNAL'}

	def listResults(self, context):
		global _search_result_items
		_search_result_items = []
		for i, r in enumerate(_nominatim_results):
			name = r.get('display_name', 'Unknown')
			if len(name) > 90:
				name = name[:87] + '...'
			_search_result_items.append((str(i), name, ''))
		return _search_result_items

	search_result: EnumProperty(
		name="Location",
		items=listResults
	)

	srckey: StringProperty()
	laykey: StringProperty()
	grdkey: StringProperty()

	def check(self, context):
		return True

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self, width=500)

	def draw(self, context):
		layout = self.layout
		layout.label(text="{} results:".format(len(_nominatim_results)))
		layout.prop(self, 'search_result', text='')

	def execute(self, context):
		geoscn = GeoScene(context.scene)
		prefs = context.preferences.addons[PKG].preferences

		idx = int(self.search_result)
		if idx < 0 or idx >= len(_nominatim_results):
			return {'CANCELLED'}

		result = _nominatim_results[idx]
		lat, lon = float(result['lat']), float(result['lon'])

		if geoscn.isGeoref:
			geoscn.updOriginGeo(lon, lat, updObjLoc=prefs.lockObj)
		else:
			geoscn.setOriginGeo(lon, lat)

		#Auto-zoom based on bounding box
		if 'boundingbox' in result:
			bbox = result['boundingbox']
			lat_extent = abs(float(bbox[1]) - float(bbox[0]))
			lon_extent = abs(float(bbox[3]) - float(bbox[2]))
			max_extent = max(lat_extent, lon_extent)
			if max_extent > 0:
				zoom = int(math.log2(360 / max_extent))
				zoom = max(2, min(zoom, 16))
				geoscn.zoom = zoom

		#Start map viewer
		self._start_viewer(context)

		return {'FINISHED'}

	def cancel(self, context):
		#User cancelled results dialog, restart map viewer
		self._start_viewer(context)

	def _start_viewer(self, context):
		bpy.ops.view3d.map_viewer('INVOKE_DEFAULT',
			srckey=self.srckey, laykey=self.laykey, grdkey=self.grdkey,
			recenter=False)


classes = [
	VIEW3D_OT_map_start,
	VIEW3D_OT_map_viewer,
	VIEW3D_OT_map_search,
	VIEW3D_OT_map_search_results
]

def register():
	global _help_draw_handler
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError as e:
			log.warning('{} is already registered, now unregister and retry... '.format(cls))
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)
	# Register persistent help overlay draw handler
	if _help_draw_handler is None:
		_help_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
			_drawHelpPersistent, (), 'WINDOW', 'POST_PIXEL')

def unregister():
	global _help_draw_handler, _map_viewer_active, _help_visible
	_map_viewer_active = False
	_help_visible = False
	if _help_draw_handler is not None:
		bpy.types.SpaceView3D.draw_handler_remove(_help_draw_handler, 'WINDOW')
		_help_draw_handler = None
	for cls in classes:
		bpy.utils.unregister_class(cls)
