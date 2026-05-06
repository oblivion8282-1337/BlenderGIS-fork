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

import numpy as np

#bpy imports
import bpy
from mathutils import Vector
from bpy.types import Operator, Panel, AddonPreferences, PropertyGroup
from bpy.props import StringProperty, IntProperty, FloatProperty, BoolProperty, EnumProperty, FloatVectorProperty, PointerProperty
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

PKG = __package__.rsplit('.', maxsplit=1)[0]  # bl_ext.user_default.cartoblend

#Nominatim search results cache
_nominatim_results = []
_search_result_items = [] #enum items cache (prevent garbage collection)

#Search history (most recent first, max 10)
_search_history = []
_search_history_items = [] #enum items cache (prevent garbage collection)

#Last map session config (for resume)
_last_map_src = None
_last_map_lay = None
_last_map_grd = None

#Flag: N-Panel "Go" button was clicked while map viewer is running
_goto_pending = False
_goto_prev_zoom = None  # zoom level BEFORE map_goto overwrites geoscn.zoom

#Flag: N-Panel "Export" button was clicked while map viewer is running
_export_pending = False

#Flag: N-Panel "Exit" button was clicked while map viewer is running
_exit_pending = False

#Name of last exported mesh object (hidden on resume)
_last_export_obj_name = None

#Flag: N-Panel source/layer was changed while map viewer is running
_source_change_pending = False

#Info overlay data — updated by operator, read by persistent draw handler
_overlay_zoom = 0
_overlay_lat = 0.0
_overlay_lon = 0.0
_overlay_scale = 1
_overlay_detail_offset = 0
_overlay_export_tiles = 0

#Flag: N-Panel detail offset was changed while map viewer is running
_detail_changed_pending = False

#Flag: N-Panel zoom field was changed by the user
_zoom_jump_pending = False

#Guard: suppress zoom callback while modal syncs the property
_zoom_syncing = False

#GPU shader cache — created once, reused every frame
_cached_uniform_shader = None

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

		#Init MapService class
		self.srv = MapService(srckey, cacheFolder)
		# Apply prefs to this instance only (not the class) so multiple
		# concurrent MapServices don't clobber each other.
		alg = prefs.resamplAlg
		if alg in MapService._RESAMP_ALG_VALID:
			self.srv.resampAlg = alg
		else:
			log.warning('Unknown resampling algo %r in prefs, using default %r', alg, self.srv.resampAlg)
		#Set destination tile matrix
		if grdkey is None:
			grdkey = self.srv.srcGridKey
		self.name = srckey + '_' + laykey + '_' + grdkey
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
		self.mosaic = None #tiled mosaic raster (set after first successful run)
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
		# crsx/crsy/scale/zoom live on the Scene as custom props; reading them
		# from the worker thread would touch bpy data off the main thread.
		# Snapshot them here so request() can use the cached values safely.
		self._req_crsx = self.crsx
		self._req_crsy = self.crsy
		self._req_scale = self.scale
		self._req_zoom = self.zoom
		#Cache effective detail offset (scene property, not thread-safe)
		try:
			self._detail_offset = self.scn.gis_basemap.detail_offset
		except Exception:
			self._detail_offset = 0
		self.srv.start()
		self.thread = threading.Thread(target=self.run)
		self.thread.start()

	def stop(self):
		'''Stop actual thread'''
		if self.srv.running:
			# Signal first; wait for the worker; only then close HTTP connections.
			self.srv.stop()
			if self.thread is not None:
				self.thread.join()
			self.srv.cleanup()

	def run(self):
		"""thread method"""
		self.mosaic = self.request()
		needsPlace = self.srv.running and self.mosaic is not None
		# Only signal-stop here; the pool is closed in BaseMap.stop() after join.
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

		# Use the snapshot taken in get() so we never touch bpy.* off-thread.
		zoom = self._req_zoom
		scale = self._req_scale
		crsx = self._req_crsx
		crsy = self._req_crsy

		#Compute effective tile zoom (navigation zoom + detail offset)
		detail = getattr(self, '_detail_offset', 0)
		tile_zoom = zoom + detail
		tile_zoom = max(0, min(tile_zoom, self.tm.nbLevels - 1))
		tile_zoom = max(self.layer.zmin, min(tile_zoom, self.layer.zmax))

		#Get area bbox coords in destination tile matrix crs (map origin is bottom left)
		#BBox is computed from navigation zoom (what the user sees)
		z = zoom
		res = self.tm.getRes(z)
		if self.crs == 'EPSG:4326':
			res = meters2dd(res)
		dx, dy, dz = self._req_view_location
		ox = crsx + (dx * scale)
		oy = crsy + (dy * scale)
		xmin = ox - w/2 * res * scale
		ymax = oy + h/2 * res * scale
		xmax = ox + w/2 * res * scale
		ymin = oy - h/2 * res * scale
		bbox = (xmin, ymin, xmax, ymax)
		#reproj bbox to destination grid crs if scene crs is different
		if self.crs != self.tm.CRS:
			bbox = reprojBbox(self.crs, self.tm.CRS, bbox)

		log.debug('Bounding box request : {} (tile zoom: {})'.format(bbox, tile_zoom))

		if self.srv.srcGridKey == self.grdkey:
			toDstGrid = False
		else:
			toDstGrid = True

		#Fetch tiles at effective zoom (may differ from navigation zoom)
		mosaic = self.srv.getImage(self.laykey, bbox, tile_zoom, toDstGrid=toDstGrid, outCRS=self.crs)

		return mosaic


	def place(self):
		'''Set map as background image'''
		if self.mosaic is None:
			return

		img_w, img_h = self.mosaic.size

		#Get or create bpy image and upload pixels directly from numpy array
		if self.img is not None and self.img.name in bpy.data.images:
			#Reuse existing image, resize if needed
			if self.img.size[0] != img_w or self.img.size[1] != img_h:
				self.img.scale(img_w, img_h)
		else:
			self.img = bpy.data.images.new(self.name, img_w, img_h, alpha=True)
			self.img.colorspace_settings.name = 'sRGB'

		#Convert numpy array (uint8 RGBA, top-to-bottom) to Blender pixels (float32 RGBA, bottom-to-top)
		px_data = self.mosaic.data
		if px_data.shape[2] == 3:
			#Add alpha channel if missing
			alpha = np.full((img_h, img_w, 1), 255, dtype=np.uint8)
			px_data = np.concatenate((px_data, alpha), axis=2)
		#Flip vertically (Blender images are bottom-to-top)
		px_data = px_data[::-1, :, :]
		#Convert to float32 [0.0, 1.0] and flatten
		flat = np.ascontiguousarray(px_data, dtype=np.float32).ravel()
		flat *= (1.0 / 255.0)
		self.img.pixels.foreach_set(flat)
		self.img.update()

		#Get or reuse background image empty
		empties = [obj for obj in self.scn.objects if obj.type == 'EMPTY']
		bkgs = [obj for obj in empties if obj.empty_display_type == 'IMAGE' and obj.get("_cartoblend_basemap")]
		if bkgs:
			self.bkg = bkgs[0]
			self.bkg.name = self.name
			self.bkg.data = self.img
			self.bkg.hide_set(False)
			self.bkg.hide_render = False
			# Remove stale leftover background empties
			for bkg in bkgs[1:]:
				bpy.data.objects.remove(bkg, do_unlink=True)
		else:
			self.bkg = bpy.data.objects.new(self.name, None)
			self.bkg.empty_display_type = 'IMAGE'
			self.bkg["_cartoblend_basemap"] = True
			self.bkg.empty_image_depth = 'BACK'
			self.bkg.data = self.img
			self.scn.collection.objects.link(self.bkg)

		#Get some image props
		img_ox, img_oy = self.mosaic.center
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
		z = self.zoom
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

	def _place_on_main_thread(self):
		'''Timer callback to execute place() safely on the main thread.

		The bpy.app timer can fire after the modal operator has been torn
		down (addon disabled, scene reset, Blender quit). In that case
		Blender PyObjects we hold are dangling and any access raises
		ReferenceError. Drop the callback silently in that case.'''
		try:
			self.place()
		except ReferenceError:
			log.debug('place() callback dropped: operator torn down', exc_info=True)
		return None #don't repeat




####################################
def drawInfosText(self, context):
	"""Update header bar with essential status and push overlay data to module-level vars."""
	global _overlay_zoom, _overlay_lat, _overlay_lon, _overlay_scale, _overlay_detail_offset, _overlay_export_tiles

	try:
		_ = self.map
	except ReferenceError:
		return  # operator was removed (addon reload)

	#Get map props stored in scene
	geoscn = GeoScene(context.scene)
	zoom = geoscn.zoom
	scale = geoscn.scale

	# --- Update module-level overlay data for the persistent draw handler ---
	_overlay_zoom = zoom
	_overlay_scale = int(scale)
	settings = context.scene.gis_basemap
	_overlay_detail_offset = settings.detail_offset
	if settings.detail_offset != 0:
		export_z = _clamp_export_zoom(self.map, settings.detail_offset)
		_overlay_export_tiles = _estimate_export_tiles(self.map, export_z)
	else:
		_overlay_export_tiles = 0

	# Convert projected cursor coords to geographic lat/lon
	try:
		if self.posx != 0 or self.posy != 0:
			lon, lat = self.map.tm.projToGeo(self.posx, self.posy)
			_overlay_lat = lat
			_overlay_lon = lon
	except Exception:
		pass  # keep previous values on error

	# --- Simplified header: only progress and detail offset status ---
	txt = "Map view"
	if settings.detail_offset != 0:
		export_z = _clamp_export_zoom(self.map, settings.detail_offset)
		txt += "  [Export: z{} ({:+d})]".format(export_z, settings.detail_offset)
	if self.progress:
		txt += "  " + self.progress
	if context.area:
		context.area.header_text_set(txt)


def drawZoomBox(self, context):
	try:
		_ = self.zoomBoxMode
	except ReferenceError:
		return  # operator was removed (addon reload)

	if not context.area:
		return

	# NOTE: Batches are intentionally NOT cached here. The crosshair and zoom-box
	# rectangle coordinates change every MOUSEMOVE event, so a cache key would
	# change just as often and provide no benefit. Only 1–2 tiny batches are
	# created per draw call, making the overhead negligible.

	if self.zoomBoxMode and not self.zoomBoxDrag:
		# before selection starts draw infinite cross
		px, py = self.zb_xmax, self.zb_ymax
		p1 = (0, py, 0)
		p2 = (context.area.width, py, 0)
		p3 = (px, 0, 0)
		p4 = (px, context.area.height, 0)
		coords = [p1, p2, p3, p4]
		shader = _get_uniform_shader()
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
		shader = _get_uniform_shader()
		batch = batch_for_shader(shader, 'LINES', {"pos": coords})
		shader.bind()
		shader.uniform_float("color", (0, 0, 0, 1))
		batch.draw(shader)


# Overlay persistent state — lives at module level, independent of operator lifecycle
_map_viewer_active = False
_overlay_draw_handler = None


def _get_uniform_shader():
	"""Return the cached UNIFORM_COLOR shader, creating it if necessary.
	Re-creates on GPU context loss (caught via exception on invalid shader)."""
	global _cached_uniform_shader, _rect_batch_cache_key, _rect_batch_cache_batches
	if _cached_uniform_shader is not None:
		try:
			# Quick validity check: bind will raise if the GPU context was lost
			_cached_uniform_shader.bind()
			return _cached_uniform_shader
		except Exception:
			_cached_uniform_shader = None
	_cached_uniform_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
	# Invalidate batch caches that were built with the old shader
	_rect_batch_cache_key = None
	_rect_batch_cache_batches = None
	return _cached_uniform_shader


_rect_batch_cache_key = None
_rect_batch_cache_batches = None

def drawRoundedRect(x, y, w, h, color, radius=6):
	"""Draw a rectangle with rounded corners. Batches cached for last geometry."""
	global _rect_batch_cache_key, _rect_batch_cache_batches
	key = (x, y, w, h, radius)
	shader = _get_uniform_shader()
	shader.bind()
	shader.uniform_float("color", color)
	if key != _rect_batch_cache_key:
		batches = []
		# Center rect
		batches.append(batch_for_shader(shader, 'TRI_STRIP', {"pos": [
			(x + radius, y, 0), (x + w - radius, y, 0),
			(x + radius, y + h, 0), (x + w - radius, y + h, 0),
		]}))
		# Left edge
		batches.append(batch_for_shader(shader, 'TRI_STRIP', {"pos": [
			(x, y + radius, 0), (x + radius, y + radius, 0),
			(x, y + h - radius, 0), (x + radius, y + h - radius, 0),
		]}))
		# Right edge
		batches.append(batch_for_shader(shader, 'TRI_STRIP', {"pos": [
			(x + w - radius, y + radius, 0), (x + w, y + radius, 0),
			(x + w - radius, y + h - radius, 0), (x + w, y + h - radius, 0),
		]}))
		# Corners
		for cx, cy in [(x, y), (x + w - radius, y), (x, y + h - radius), (x + w - radius, y + h - radius)]:
			batches.append(batch_for_shader(shader, 'TRI_STRIP', {"pos": [
				(cx + 1, cy, 0), (cx + radius, cy, 0),
				(cx, cy + 1, 0), (cx + radius, cy + radius, 0),
			]}))
		_rect_batch_cache_key = key
		_rect_batch_cache_batches = batches
	for batch in _rect_batch_cache_batches:
		batch.draw(shader)

def _drawInfoOverlay(context):
	"""Draw zoom level, coordinates and scale overlay in the bottom-left corner."""
	global _overlay_zoom, _overlay_lat, _overlay_lon, _overlay_scale, _overlay_detail_offset, _overlay_export_tiles

	font_id = 0
	pad_x, pad_y = 14, 10
	line_h = 22
	margin = 16

	# Build text lines (bottom-up order: first item drawn at bottom)
	zoom_txt = "Z: {}".format(_overlay_zoom)
	if _overlay_detail_offset != 0:
		zoom_txt += "  (Export: {:+d})".format(_overlay_detail_offset)

	# Format lat/lon
	lat_dir = "N" if _overlay_lat >= 0 else "S"
	lon_dir = "E" if _overlay_lon >= 0 else "W"
	coord_txt = "{:.4f}\u00b0 {}, {:.4f}\u00b0 {}".format(
		abs(_overlay_lat), lat_dir, abs(_overlay_lon), lon_dir)

	scale_txt = "Scale 1:{}".format(_overlay_scale)

	lines = [zoom_txt, coord_txt, scale_txt]

	# Measure panel size
	blf.size(font_id, 14)
	max_w = 0
	for line in lines:
		w = blf.dimensions(font_id, line)[0]
		if w > max_w:
			max_w = w

	panel_w = int(max_w + pad_x * 2)
	panel_h = int(len(lines) * line_h + pad_y * 2)

	px = margin
	py = margin

	# Panel background
	drawRoundedRect(px, py, panel_w, panel_h, (0.10, 0.10, 0.10, 0.75))

	# Panel border
	shader = _get_uniform_shader()
	border = [
		(px, py, 0), (px + panel_w, py, 0),
		(px + panel_w, py, 0), (px + panel_w, py + panel_h, 0),
		(px + panel_w, py + panel_h, 0), (px, py + panel_h, 0),
		(px, py + panel_h, 0), (px, py, 0)
	]
	batch = batch_for_shader(shader, 'LINES', {"pos": border})
	shader.bind()
	shader.uniform_float("color", (0.35, 0.35, 0.35, 0.40))
	batch.draw(shader)

	# Draw text lines top-to-bottom
	blf.size(font_id, 14)
	cy = py + panel_h - pad_y - 14
	for i, line in enumerate(lines):
		blf.position(font_id, px + pad_x, cy, 0)
		if i == 0:
			# Zoom level in accent color
			blf.color(font_id, 0.6, 0.85, 1.0, 1.0)
		else:
			blf.color(font_id, 0.85, 0.85, 0.85, 0.95)
		blf.draw(font_id, line)
		cy -= line_h


def _drawOverlayPersistent():
	"""Persistent draw callback — shows info overlay when map viewer is active"""
	if not _map_viewer_active:
		return

	context = bpy.context
	if context.area is None or context.area.type != 'VIEW_3D':
		return

	gpu.state.blend_set('ALPHA')
	_drawInfoOverlay(context)
	gpu.state.blend_set('NONE')


def _zoom_from_nominatim(result):
	"""Derive an appropriate zoom level from a Nominatim result dict."""
	if 'boundingbox' in result:
		bbox = result['boundingbox']
		lat_extent = abs(float(bbox[1]) - float(bbox[0]))
		lon_extent = abs(float(bbox[3]) - float(bbox[2]))
		max_extent = max(lat_extent, lon_extent)
		if max_extent > 0:
			zoom = int(math.log2(360 / max_extent))
			return max(2, min(zoom, 16))
	# No bounding box — point feature, use type-based fallback
	result_class = result.get('class', '')
	if result_class in ('highway', 'amenity', 'shop', 'tourism', 'building', 'place'):
		return 16
	return 14


def _clamp_export_zoom(basemap, detail_offset):
	"""Compute and clamp export zoom from current zoom + offset."""
	z = basemap.zoom + detail_offset
	z = max(0, min(z, basemap.tm.nbLevels - 1))
	z = max(basemap.layer.zmin, min(z, basemap.layer.zmax))
	return z


_estimate_cache = {'key': None, 'value': 0}

def _estimate_export_tiles(basemap, export_zoom):
	"""Estimate how many tiles would be needed for export at the given zoom.

	Cached on (zoom, view_location, area_size, scale, export_zoom, crs, layer)
	so the per-frame draw handler doesn't recompute when nothing changed.
	"""
	try:
		w, h = basemap.area.width, basemap.area.height
		loc = basemap.reg3d.view_location
		key = (basemap.zoom, tuple(loc), w, h, basemap.scale, export_zoom,
			basemap.crs, basemap.tm.CRS)
		if _estimate_cache['key'] == key:
			return _estimate_cache['value']
		z = basemap.zoom
		res = basemap.tm.getRes(z)
		if basemap.crs == 'EPSG:4326':
			res = meters2dd(res)
		dx, dy, dz = loc
		ox = basemap.crsx + (dx * basemap.scale)
		oy = basemap.crsy + (dy * basemap.scale)
		xmin = ox - w/2 * res * basemap.scale
		ymax = oy + h/2 * res * basemap.scale
		xmax = ox + w/2 * res * basemap.scale
		ymin = oy - h/2 * res * basemap.scale
		bbox = (xmin, ymin, xmax, ymax)
		if basemap.crs != basemap.tm.CRS:
			bbox = reprojBbox(basemap.crs, basemap.tm.CRS, bbox)
		export_res = basemap.tm.getRes(export_zoom)
		tile_size = basemap.tm.tileSize
		bxmin, bymin, bxmax, bymax = bbox
		cols = math.ceil((bxmax - bxmin) / (tile_size * export_res))
		rows = math.ceil((bymax - bymin) / (tile_size * export_res))
		value = max(0, cols) * max(0, rows)
		_estimate_cache['key'] = key
		_estimate_cache['value'] = value
		return value
	except Exception:
		return 0


###############

from ..core.basemaps import providers as providers_mod

# Sources whose tile URLs require an addon-prefs API key. We hide them from the
# N-Panel picker until the matching key field is populated, so users don't pick
# a provider only to see empty/error tiles.
_KEYED_SOURCES = providers_mod.KEYED_SOURCES


# Module-level item caches keep the tuples (and their inner strings) alive
# across EnumProperty reads. Blender's C side stores raw pointers into the
# returned strings without taking a reference, so simply overwriting the
# cache on the next call leaves stale pointers pointing at freed memory and
# the dropdown shows garbage characters. See https://developer.blender.org/T48873
#
# The robust workaround used by the Blender community is to keep *every*
# previously-returned items list alive until the addon unloads. We dedupe by
# the items tuple so a stable selection doesn't grow the cache on every poll.
_PROVIDER_ITEMS_CACHE = {}
_SOURCE_ITEMS_CACHE = {}
_LAYER_ITEMS_CACHE = {}
_OPSTART_SRC_CACHE = {}
_OPSTART_GRID_CACHE = {}
_OPSTART_LAY_CACHE = {}


def _hold(cache, items):
	"""Return a stable list reference for the given items, retaining it in
	the module-level cache so Blender's pointer into the strings stays valid."""
	key = tuple(items)
	cached = cache.get(key)
	if cached is None:
		cache[key] = items
		return items
	return cached


def _list_providers(self, context):
	"""Flat compound-key list (Source.Layer) filtered by:
	 * provider visibility toggle in the catalog
	 * keyed providers without configured API keys."""
	prefs = None
	try:
		prefs = context.preferences.addons[PKG].preferences
	except (KeyError, AttributeError):
		pass
	if prefs is None:
		# Fallback to plain SOURCES expansion if prefs aren't ready yet
		items = []
		for srckey, src in SOURCES.items():
			for laykey, lay in src.get('layers', {}).items():
				items.append((
					'{}.{}'.format(srckey, laykey),
					'{} — {}'.format(src['name'], lay['name']),
					lay.get('description') or src.get('description', ''),
				))
		return _hold(_PROVIDER_ITEMS_CACHE, items)
	items = []
	for entry in providers_mod.get_visible_entries(prefs):
		needs = entry.get('needs_key_attrs', ())
		if needs and not all(getattr(prefs, a, '') for a in needs):
			continue
		items.append((entry['key'], entry['name'], entry.get('description', '')))
	if not items:
		# Nothing visible — surface a placeholder so the dropdown isn't blank
		items.append(('__none__', '(no providers visible — see addon prefs)', ''))
	return _hold(_PROVIDER_ITEMS_CACHE, items)


def _on_provider_changed(self, context):
	"""When the flat picker changes, split compound key back into srckey/laykey
	so the rest of the basemap pipeline keeps working unchanged."""
	if not self.provider or self.provider == '__none__':
		_on_source_layer_changed(self, context)
		return
	try:
		srckey, laykey = providers_mod.get_compound_routing(self.provider)
	except Exception:
		log.debug('Provider routing failed for %r', self.provider, exc_info=True)
		_on_source_layer_changed(self, context)
		return
	# Use attribute assignment so the EnumProperty validates and rebuilds its
	# index. The src setter recomputes the lay items list, so we must set src
	# first, then lay.
	if srckey != self.src:
		try:
			self.src = srckey
		except TypeError:
			# Item not in current src enum (filtered out); fallback to subscript
			# which bypasses the items check.
			self['src'] = srckey
	if laykey != self.lay:
		try:
			self.lay = laykey
		except TypeError:
			self['lay'] = laykey
	_on_source_layer_changed(self, context)


# Legacy items callbacks kept for back-compat with the start operator dialog.
def _list_sources(self, context):
	prefs = None
	try:
		prefs = context.preferences.addons[PKG].preferences
	except (KeyError, AttributeError):
		pass
	items = []
	for srckey, src in SOURCES.items():
		required = _KEYED_SOURCES.get(srckey)
		if required and prefs is not None:
			if not all(getattr(prefs, attr, '') for attr in required):
				continue
		items.append((srckey, src['name'], src['description']))
	return _hold(_SOURCE_ITEMS_CACHE, items)


def _list_layers(self, context):
	items = []
	srckey = self.src
	if srckey in SOURCES:
		for laykey, lay in SOURCES[srckey]['layers'].items():
			items.append((laykey, lay['name'], lay['description']))
	return _hold(_LAYER_ITEMS_CACHE, items)

def _on_source_layer_changed(self, context):
	global _source_change_pending, _zoom_syncing
	if _map_viewer_active:
		_source_change_pending = True
	# Clamp zoom to new source/layer's valid range
	srckey = self.src
	if srckey in SOURCES:
		layers = SOURCES[srckey]['layers']
		lay = self.lay
		if lay not in layers:
			lay = next(iter(layers))
		zmax = layers[lay]['zmax']
		zmin = layers[lay]['zmin']
		zoom = context.scene.get(SK.ZOOM)
		if zoom is not None:
			clamped = max(zmin, min(zoom, zmax))
			if clamped != zoom:
				context.scene['zoom'] = clamped
				_zoom_syncing = True
				self.map_zoom = clamped
				_zoom_syncing = False

def _on_detail_offset_changed(self, context):
	global _detail_changed_pending
	if _map_viewer_active:
		_detail_changed_pending = True

def _on_zoom_input_changed(self, context):
	global _zoom_jump_pending, _zoom_syncing
	if _map_viewer_active and not _zoom_syncing:
		_zoom_jump_pending = True

class GIS_PG_basemap_settings(PropertyGroup):
	# Flat picker — one compound key like 'OSM.MAPNIK' or a custom provider name.
	provider: EnumProperty(
		name="Basemap",
		description="Choose a map tile provider",
		items=_list_providers,
		update=_on_provider_changed,
	)
	# Internal source / layer split kept for backward compatibility with the
	# rest of the pipeline (MapService dispatch, modal start args). Hidden in
	# the N-Panel — the flat 'provider' picker keeps these in sync.
	src: EnumProperty(
		name="Source",
		description="Internal: source key derived from provider",
		items=_list_sources,
		update=_on_source_layer_changed
	)
	lay: EnumProperty(
		name="Layer",
		description="Internal: layer key derived from provider",
		items=_list_layers,
		update=_on_source_layer_changed
	)
	detail_offset: IntProperty(
		name="Detail Offset",
		description="Adjust tile detail level. Positive = more detail, negative = less detail",
		default=0,
		min=-5,
		max=8,
		update=_on_detail_offset_changed,
	)
	map_zoom: IntProperty(
		name="Zoom",
		description="Current map zoom level. Type a value to jump directly to that zoom",
		default=0,
		min=0,
		max=25,
		update=_on_zoom_input_changed,
	)


class VIEW3D_OT_map_start(Operator):

	bl_idname = "view3d.map_start"
	bl_description = 'Toggle 2d map navigation'
	bl_label = "Basemap"
	bl_options = {'REGISTER'}

	def listSources(self, context):
		# Same key-aware filter as the N-Panel dropdown: hide providers whose
		# API key isn't configured.
		prefs = None
		try:
			prefs = context.preferences.addons[PKG].preferences
		except (KeyError, AttributeError):
			pass
		srcItems = []
		for srckey, src in SOURCES.items():
			required = _KEYED_SOURCES.get(srckey)
			if required and prefs is not None:
				if not all(getattr(prefs, attr, '') for attr in required):
					continue
			srcItems.append( (srckey, src['name'], src['description']) )
		return _hold(_OPSTART_SRC_CACHE, srcItems)

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
		return _hold(_OPSTART_GRID_CACHE, grdItems)

	def listLayers(self, context):
		layItems = []
		src = SOURCES[self.src]
		for laykey, lay in src['layers'].items():
			#put each item in a tuple (key, label, tooltip)
			layItems.append( (laykey, lay['name'], lay['description']) )
		return _hold(_OPSTART_LAY_CACHE, layItems)


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

	def listHistory(self, context):
		global _search_history_items
		_search_history_items = [('NONE', '-- Recent searches --', '')]
		for i, q in enumerate(_search_history):
			_search_history_items.append((str(i), q, ''))
		return _search_history_items

	history: EnumProperty(
		name="History",
		description="Recent searches",
		items=listHistory
	)

	zoom: IntProperty(name='Zoom level', min=0, max=25)

	recenter: BoolProperty(name='Center to existing objects', default=True)

	#special function to auto redraw an operator popup called through invoke_props_dialog
	def check(self, context):
		# If user picks a history entry, populate the query field
		if self.history != 'NONE':
			idx = int(self.history)
			if 0 <= idx < len(_search_history):
				self.query = _search_history[idx]
			self.history = 'NONE'
		return True

	def draw(self, context):
		addonPrefs = context.preferences.addons[PKG].preferences
		layout = self.layout

		if self.dialog == 'SEARCH':
				layout.prop(self, 'query')
				if _search_history:
					layout.separator()
					layout.prop(self, 'history', text="Recent")

		elif self.dialog == 'OPTIONS':
			layout.prop(addonPrefs, "zoomToMouse")
			layout.prop(addonPrefs, "lockObj")
			layout.prop(addonPrefs, "lockOrigin")
			layout.prop(addonPrefs, "synchOrj")


	def invoke(self, context, event):

		if not HAS_PIL and not HAS_GDAL and not HAS_IMGIO:
			self.report({'ERROR'}, "No imaging library available. ImageIO module was not correctly installed.")
			return {'CANCELLED'}

		if not context.area or not context.area.type == 'VIEW_3D':
			self.report({'WARNING'}, "View3D not found, cannot run operator")
			return {'CANCELLED'}

		if self.dialog == 'MAP':
			# Read source/layer from scene properties (set in N-panel)
			settings = context.scene.gis_basemap
			self.src = settings.src
			# Fallback to first layer if stored value is empty/invalid
			lay = settings.lay
			if not lay or lay not in SOURCES[self.src]['layers']:
				lay = next(iter(SOURCES[self.src]['layers']))
			self.lay = lay
			# Use source's native grid
			self.grd = SOURCES[self.src]['grid']
			#Update zoom
			geoscn = GeoScene(context.scene)
			if geoscn.hasZoom:
				self.zoom = geoscn.zoom
			# Start directly, no popup
			return self.execute(context)

		# SEARCH and OPTIONS dialogs still use popup
		#Pre-fill with last used source/layer/grid if available
		if _last_map_src is not None:
			self.src = _last_map_src
			self.lay = _last_map_lay
			self.grd = _last_map_grd

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
				# Auto-fix: set CRS to grid CRS if missing
				if not geoscn.hasCRS:
					geoscn.crs = grdCRS
					log.info(f"Auto-set CRS to {grdCRS}")
			if not geoscn.hasCRS:
				geoscn.crs = grdCRS
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
				# Add to search history (most recent first, no duplicates, max 10)
				global _search_history
				q = self.query.strip()
				if q:
					if q in _search_history:
						_search_history.remove(q)
					_search_history.insert(0, q)
					_search_history = _search_history[:10]
				#Show results picker (it will start map viewer after selection)
				self.dialog = 'MAP'
				bpy.ops.view3d.map_search_results('INVOKE_DEFAULT',
					srckey=self.src, laykey=self.lay, grdkey=self.grd)
				return {'FINISHED'}

		#Start map viewer operator
		self.dialog = 'MAP' #reinit dialog type
		#Save last config for resume
		global _last_map_src, _last_map_lay, _last_map_grd
		_last_map_src = self.src
		_last_map_lay = self.lay
		_last_map_grd = self.grd
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
		return context.area is not None and context.area.type == 'VIEW_3D'


	def __del__(self):
		# Intentionally a no-op. Restarting the modal operator from __del__
		# is dangerous because Python's GC can run during Blender shutdown or
		# addon reload, when bpy.ops or our own attributes may already be gone.
		# A pending restart is scheduled via _schedule_restart() instead.
		pass


	def _schedule_restart(self):
		"""Re-launch the modal viewer on the next idle tick. Safe to call
		from inside modal/exit paths because it doesn't run bpy.ops directly."""
		src, lay, grd, dialog = self.srckey, self.laykey, self.grdkey, self.dialog
		def _fire():
			try:
				bpy.ops.view3d.map_start('INVOKE_DEFAULT', src=src, lay=lay, grd=grd, dialog=dialog, recenter=False)
			except Exception:
				log.debug('Deferred map_start failed', exc_info=True)
			return None  # one-shot
		bpy.app.timers.register(_fire, first_interval=0.0)


	def invoke(self, context, event):

		if not context.area:
			return {'CANCELLED'}

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
		# 0.1s = 10 Hz — fast enough for progress text and N-Panel button responsiveness,
		# 2.5× cheaper than the previous 0.04s (25 Hz) idle load.
		self.timer = context.window_manager.event_timer_add(0.1, window=context.window)

		#Switch to top view ortho (center to origin)
		view3d = context.area.spaces.active
		bpy.ops.view3d.view_axis(type='TOP')
		view3d.region_3d.view_perspective = 'ORTHO'
		context.scene.cursor.location = (0, 0, 0)
		if not self.prefs.lockOrigin:
			#bpy.ops.view3d.view_center_cursor()
			view3d.region_3d.view_location = (0, 0, 0)

		#Hide last exported mesh when resuming
		if _last_export_obj_name and _last_export_obj_name in context.scene.objects:
			exp_obj = context.scene.objects[_last_export_obj_name]
			exp_obj.hide_set(True)
			exp_obj.hide_render = True

		#Init some properties
		# tag if map is currently drag
		self.inMove = False
		self.x1, self.y1 = 0, 0
		# cached parent objects for drag (built on PRESS, reused on MOUSEMOVE)
		self._topParents = []
		# mouse crs coordinates reported in draw callback
		self.posx, self.posy = 0, 0
		# thread progress infos reported in draw callback
		self.progress = ''
		# Zoom box
		self.zoomBoxMode = False
		self.zoomBoxDrag = False
		self.zb_xmin, self.zb_xmax = 0, 0
		self.zb_ymin, self.zb_ymax = 0, 0
		self._viewDstZ = None
		self._viewLoc = None

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


	def _cleanup_modal(self, context):
		"""Remove draw handlers, timer, header text and deactivate map viewer."""
		global _map_viewer_active
		_map_viewer_active = False
		self.map.stop()
		try:
			bpy.types.SpaceView3D.draw_handler_remove(self._drawTextHandler, 'WINDOW')
		except Exception:
			pass
		try:
			bpy.types.SpaceView3D.draw_handler_remove(self._drawZoomBoxHandler, 'WINDOW')
		except Exception:
			pass
		if context.area:
			context.area.header_text_set(None)
		try:
			context.window_manager.event_timer_remove(self.timer)
		except Exception:
			pass

	def _do_export(self, context):
		"""Export current basemap tiles as textured mesh."""
		if self.map.bkg is None:
			return
		self._cleanup_modal(context)
		self.map.bkg.hide_set(True)
		self.map.bkg.hide_render = True

		#Save mosaic to disk for export (live viewer skips disk I/O)
		self.map.mosaic.save(self.map.imgPath)

		#Copy image to new datablock
		bpyImg = bpy.data.images.load(self.map.imgPath)
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
		global _last_export_obj_name
		_last_export_obj_name = obj.name

		#UV mapping
		uvTxtLayer = mesh.uv_layers.new(name='rastUVmap')
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

		#Auto-snap GPX tracks to the freshly exported terrain mesh
		self._snap_tracks_to_terrain(context, obj)

		#Restore 3D perspective view
		if context.area:
			context.area.spaces.active.region_3d.view_perspective = 'PERSP'

		return {'FINISHED'}

	@staticmethod
	def _snap_tracks_to_terrain(context, terrain_obj):
		"""Apply 'Snap to Terrain' GN modifier to any mesh objects that lack one."""
		from .io_import_gpx import _get_or_create_gpx_snap_geonodes
		snap_ng = _get_or_create_gpx_snap_geonodes()
		for obj in context.scene.objects:
			if obj.type not in ('MESH', 'CURVE') or obj == terrain_obj:
				continue
			# Skip objects that already have a snap modifier
			if any(m.type == 'NODES' and m.node_group == snap_ng for m in obj.modifiers):
				continue
			# Skip EXPORT_ meshes (other basemap exports)
			if obj.name.startswith('EXPORT_'):
				continue
			snap_mod = obj.modifiers.new('Snap to Terrain', 'NODES')
			snap_mod.node_group = snap_ng
			for item in snap_ng.interface.items_tree:
				if item.name == 'Terrain' and hasattr(item, 'identifier'):
					snap_mod[item.identifier] = terrain_obj
					break
			# Move snap modifier to top so it runs before other modifiers
			idx = list(obj.modifiers).index(snap_mod)
			if idx > 0:
				obj.modifiers.move(idx, 0)

	def modal(self, context, event):
		global _map_viewer_active, _goto_pending, _goto_prev_zoom, _export_pending, _exit_pending, _source_change_pending, _detail_changed_pending, _zoom_jump_pending, _zoom_syncing

		if not context.area:
			return {'CANCELLED'}

		scn = bpy.context.scene

		if event.type == 'TIMER':
			#report thread progression
			self.progress = self.map.srv.report
			#Check if user typed a zoom value in the N-Panel field (BEFORE sync!)
			if _zoom_jump_pending:
				_zoom_jump_pending = False
				new_zoom = context.scene.gis_basemap.map_zoom
				new_zoom = max(self.map.layer.zmin, min(new_zoom, self.map.layer.zmax))
				new_zoom = max(0, min(new_zoom, self.map.tm.nbLevels - 1))
				if new_zoom != self.map.zoom:
					resFactor = self.map.tm.getFromToResFac(self.map.zoom, new_zoom)
					context.region_data.view_distance *= resFactor
					self.map.zoom = new_zoom
				self.map.get()
			#Sync map_zoom property so the N-Panel field shows current zoom (only when value differs)
			try:
				if context.scene.gis_basemap.map_zoom != self.map.zoom:
					_zoom_syncing = True
					context.scene.gis_basemap.map_zoom = self.map.zoom
					_zoom_syncing = False
			except (AttributeError, KeyError, ReferenceError):
				log.debug('Zoom sync skipped (gis_basemap unavailable)', exc_info=True)
				_zoom_syncing = False
			#Check if N-Panel "Go" button requested a location change
			if _goto_pending:
				_goto_pending = False
				# self.map.zoom is already the NEW zoom (geoscn.zoom was set by map_goto)
				# Use _goto_prev_zoom to compute the view_distance ratio
				if _goto_prev_zoom is not None and _goto_prev_zoom != self.map.zoom:
					resFactor = self.map.tm.getFromToResFac(_goto_prev_zoom, self.map.zoom)
					context.region_data.view_distance *= resFactor
				_goto_prev_zoom = None
				self.map.get()
			#Check if N-Panel "Exit" was clicked
			if _exit_pending:
				_exit_pending = False
				self._cleanup_modal(context)
				return {'CANCELLED'}
			#Check if N-Panel source/layer was changed
			elif _source_change_pending:
				_source_change_pending = False
				self._cleanup_modal(context)
				self._schedule_restart()
				return {'FINISHED'}
			#Check if N-Panel "Export" button was clicked
			elif _export_pending:
				if not self.map.srv.running and self.map.mosaic is not None:
					_export_pending = False
					return self._do_export(context)
				else:
					# Keep _export_pending set so the export fires automatically
					# as soon as the tiles finish loading; the user shouldn't
					# have to click Export again.
					self.progress = 'Tiles still loading, exporting when ready…'
			#Check if N-Panel detail offset was changed
			elif _detail_changed_pending:
				_detail_changed_pending = False
				self.map.get()
			# Timer: always redraw to update progress text and overlay data
			context.area.tag_redraw()
			return {'PASS_THROUGH'}

		#Pass through events when mouse is over sidebar or toolbar
		#(allows N-Panel interaction while map viewer is running)
		if event.type not in {'TIMER', 'INBETWEEN_MOUSEMOVE'}:
			mx, my = event.mouse_x, event.mouse_y
			for region in context.area.regions:
				if region.type in {'UI', 'TOOLS', 'HEADER', 'TOOL_HEADER'}:
					if (region.x <= mx <= region.x + region.width and
						region.y <= my <= region.y + region.height):
						return {'PASS_THROUGH'}

		# Track whether this event requires a redraw
		needs_redraw = False

		if event.type in ['WHEELUPMOUSE', 'NUMPAD_PLUS']:
			if context.region_data is None:
				return {'PASS_THROUGH'}

			if event.value == 'PRESS':
				needs_redraw = True

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
						viewLoc = context.region_data.view_location.copy()
						deltaVect = (mouseLoc - viewLoc) * self.moveFactor
						context.region_data.view_location = viewLoc + deltaVect
				else:
					# map zoom up
					if self.map.zoom < self.map.layer.zmax and self.map.zoom < self.map.tm.nbLevels-1:
						self.map.zoom += 1
						resFactor = self.map.tm.getNextResFac(self.map.zoom)
						if not self.prefs.zoomToMouse:
							context.region_data.view_distance *= resFactor
						else:
							#Progressibly zoom to cursor
							dst = context.region_data.view_distance
							if dst == 0:
								return {'PASS_THROUGH'}
							dst2 = dst * resFactor
							context.region_data.view_distance = dst2
							mouseLoc = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
							viewLoc = context.region_data.view_location.copy()
							moveFactor = (dst - dst2) / dst
							deltaVect = (mouseLoc - viewLoc) * moveFactor
							if self.prefs.lockOrigin:
								context.region_data.view_location = viewLoc + deltaVect
							else:
								dx, dy, dz = deltaVect
								if not self.prefs.lockObj and self.map.bkg is not None:
									self.map.bkg.location  -= deltaVect
								self.map.moveOrigin(dx, dy, updObjLoc=self.updObjLoc)
						self.map.get()


		if event.type in ['WHEELDOWNMOUSE', 'NUMPAD_MINUS']:
			if context.region_data is None:
				return {'PASS_THROUGH'}

			if event.value == 'PRESS':
				needs_redraw = True

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
						viewLoc = context.region_data.view_location.copy()
						deltaVect = (mouseLoc - viewLoc) * self.moveFactor
						context.region_data.view_location = viewLoc - deltaVect
				else:
					#map zoom down
					if self.map.zoom > self.map.layer.zmin and self.map.zoom > 0:
						self.map.zoom -= 1
						resFactor = self.map.tm.getPrevResFac(self.map.zoom)
						if not self.prefs.zoomToMouse:
							context.region_data.view_distance *= resFactor
						else:
							#Progressibly zoom to cursor
							dst = context.region_data.view_distance
							if dst == 0:
								return {'PASS_THROUGH'}
							dst2 = dst * resFactor
							context.region_data.view_distance = dst2
							mouseLoc = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
							viewLoc = context.region_data.view_location.copy()
							moveFactor = (dst - dst2) / dst
							deltaVect = (mouseLoc - viewLoc) * moveFactor
							if self.prefs.lockOrigin:
								context.region_data.view_location = viewLoc + deltaVect
							else:
								dx, dy, dz = deltaVect
								if not self.prefs.lockObj and self.map.bkg is not None:
									self.map.bkg.location  -= deltaVect
								self.map.moveOrigin(dx, dy, updObjLoc=self.updObjLoc)
						self.map.get()



		if event.type == 'MOUSEMOVE':

			#Report mouse location coords in projeted crs
			mouseLoc = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
			if mouseLoc is not None:
				self.posx, self.posy = self.map.view3dToProj(mouseLoc.x, mouseLoc.y)

			if self.zoomBoxMode:
				self.zb_xmax, self.zb_ymax = event.mouse_region_x, event.mouse_region_y
				needs_redraw = True  # crosshair cursor needs update

			#Drag background image (edit its offset values)
			if self.inMove:
				needs_redraw = True  # map is being dragged
				loc1 = mouseTo3d(context, self.x1, self.y1)
				if mouseLoc is None or loc1 is None:
					return {'PASS_THROUGH'}
				dlt = loc1 - mouseLoc
				if event.ctrl or self.prefs.lockOrigin:
					context.region_data.view_location = self.viewLoc1 + dlt
				else:
					#Move background image
					if self.map.bkg is not None:
						self.map.bkg.location[0] = self.offx1 - dlt.x
						self.map.bkg.location[1] = self.offy1 - dlt.y
					#Move existing objects (use cached parent list from PRESS)
					if self.updObjLoc:
						for obj, orig_loc in zip(self._topParents, self.objsLoc1):
							obj.location.x = orig_loc.x - dlt.x
							obj.location.y = orig_loc.y - dlt.y


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
						#Cache top-level parent objects and their locations (reused during drag)
						self._topParents = [obj for obj in scn.objects if not obj.parent and obj != self.map.bkg]
						self.objsLoc1 = [obj.location.copy() for obj in self._topParents]
				#Tag that map is currently draging
				self.inMove = True
				needs_redraw = True

			if event.value == 'RELEASE' and not self.zoomBoxMode:
				wasMoving = self.inMove
				self.inMove = False
				if wasMoving and not event.ctrl:
					needs_redraw = True
					if not self.prefs.lockOrigin:
						#Compute final shift
						loc1 = mouseTo3d(context, self.x1, self.y1)
						loc2 = mouseTo3d(context, event.mouse_region_x, event.mouse_region_y)
						if loc1 is None or loc2 is None:
							self.map.get()
							return {'RUNNING_MODAL'}
						dlt = loc1 - loc2
						#Update map (do not update objects location because it was updated while mouse move)
						self.map.moveOrigin(dlt.x, dlt.y, updObjLoc=False)
					self.map.get()


			if event.value == 'PRESS' and self.zoomBoxMode:
				self.zoomBoxDrag = True
				self.zb_xmin, self.zb_ymin = event.mouse_region_x, event.mouse_region_y
				needs_redraw = True

			if event.value == 'RELEASE' and self.zoomBoxMode:
				needs_redraw = True
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
				if loc is None:
					return {'PASS_THROUGH'}
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
			needs_redraw = True

			if event.value == 'PRESS':
				self._viewDstZ = context.region_data.view_distance
				self._viewLoc = context.region_data.view_location.copy()

			if event.value == 'RELEASE':
				if self._viewDstZ is None:
					return {'PASS_THROUGH'}
				#restore view 3d distance and location
				context.region_data.view_distance = self._viewDstZ
				context.region_data.view_location = self._viewLoc


		#NUMPAD MOVES (3D VIEW or MAP)
		if event.value == 'PRESS' and event.type in ['NUMPAD_2', 'NUMPAD_4', 'NUMPAD_6', 'NUMPAD_8']:
			if self.map.bkg is None:
				return {'RUNNING_MODAL'}
			needs_redraw = True
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
		if event.type == 'SPACE' and event.value == 'PRESS':
			self._cleanup_modal(context)
			self._schedule_restart()
			return {'FINISHED'}

		#GO TO
		if event.type == 'G' and event.value == 'PRESS':
			self._cleanup_modal(context)
			self.dialog = 'SEARCH'
			self._schedule_restart()
			return {'FINISHED'}

		#OPTIONS
		if event.type == 'O' and event.value == 'PRESS':
			self._cleanup_modal(context)
			self.dialog = 'OPTIONS'
			self._schedule_restart()
			return {'FINISHED'}

		#ZOOM BOX
		if event.type == 'B' and event.value == 'PRESS':
			self.map.stop()
			self.zoomBoxMode = True
			self.zb_xmax, self.zb_ymax = event.mouse_region_x, event.mouse_region_y
			context.window.cursor_set('CROSSHAIR')
			needs_redraw = True

		#EXPORT
		if event.type == 'E' and event.value == 'PRESS':
			if self.map.srv.running or self.map.mosaic is None:
				self.progress = 'Tiles still loading, please wait…'
				return {'RUNNING_MODAL'}
			else:
				return self._do_export(context)

		#EXIT
		if event.type == 'ESC' and event.value == 'PRESS':
			if self.zoomBoxMode:
				self.zoomBoxDrag = False
				self.zoomBoxMode = False
				context.window.cursor_set('DEFAULT')
				needs_redraw = True
			else:
				self._cleanup_modal(context)
				return {'CANCELLED'}

		if needs_redraw:
			context.area.tag_redraw()

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
			geoscn.zoom = _zoom_from_nominatim(result)
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
			# Append Nominatim result type (e.g. "city", "residential")
			rtype = r.get('type', '')
			if rtype:
				rtype = rtype.replace('_', ' ').capitalize()
				suffix = ' ({})'.format(rtype)
			else:
				suffix = ''
			# Truncate keeping room for the type suffix
			max_name = 90 - len(suffix)
			if len(name) > max_name:
				name = name[:max_name - 3] + '...'
			name += suffix
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
		geoscn.zoom = _zoom_from_nominatim(result)

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


class VIEW3D_OT_map_goto(bpy.types.Operator):
	"""Search for a location using the query from the N-Panel input field"""

	bl_idname = "view3d.map_goto"
	bl_label = "Go"
	bl_description = 'Search for the location entered above'
	bl_options = {'INTERNAL'}

	def execute(self, context):
		query = context.scene.gis_goto_query.strip()
		if not query:
			self.report({'INFO'}, "Please enter a location")
			return {'CANCELLED'}

		geoscn = GeoScene(context.scene)
		prefs = context.preferences.addons[PKG].preferences

		#Query Nominatim
		try:
			global _nominatim_results
			_nominatim_results = nominatimQuery(query, referer='bgis', user_agent=USER_AGENT)
		except Exception as e:
			log.error('Failed Nominatim query', exc_info=True)
			_nominatim_results = []

		if not _nominatim_results:
			self.report({'INFO'}, "No location found")
			return {'CANCELLED'}

		#Save to search history
		global _search_history
		if query in _search_history:
			_search_history.remove(query)
		_search_history.insert(0, query)
		_search_history = _search_history[:10]

		#Apply first result and store resolved name
		result = _nominatim_results[0]
		context.scene.gis_goto_result = result.get('display_name', query)
		lat, lon = float(result['lat']), float(result['lon'])
		# On a fresh scene geoscn.crs is unset; setOriginGeo would then silently
		# swallow the reproject failure and leave origin unset, so the viewer
		# would land on (0,0) instead of the searched place. Mirror the auto-set
		# done in map_start.execute so the very first goto resolves correctly.
		if not geoscn.hasCRS:
			src_key = context.scene.gis_basemap.src
			geoscn.crs = GRIDS[SOURCES[src_key]['grid']]['CRS']
		if geoscn.isGeoref:
			geoscn.updOriginGeo(lon, lat, updObjLoc=prefs.lockObj)
		else:
			geoscn.setOriginGeo(lon, lat)
		#If map viewer is running, save the OLD zoom before overwriting
		global _goto_pending, _goto_prev_zoom
		if _map_viewer_active:
			_goto_prev_zoom = geoscn.zoom  # Save current zoom before map_goto overwrites it
		geoscn.zoom = _zoom_from_nominatim(result)

		#If map viewer is running, signal it to refresh at new location
		if _map_viewer_active:
			_goto_pending = True
			name = result.get('display_name', query)
			if len(name) > 60:
				name = name[:57] + '...'
			self.report({'INFO'}, name)
		else:
			#Map viewer is not running. Route through map_start so that all
			#init work happens (CRS check, _last_map_src tracking, view_distance
			#derived from zoom). map_start.invoke with dialog='MAP' reads
			#source/layer from the N-panel and goes straight to execute().
			# recenter=False: we just set the origin/zoom for the searched
			# location and don't want map_viewer to override them with the
			# scene-bbox center (e.g. the default cube at the world origin).
			bpy.ops.view3d.map_start('INVOKE_DEFAULT', dialog='MAP', recenter=False)

		return {'FINISHED'}


class VIEW3D_OT_map_goto_history(bpy.types.Operator):
	"""Pick a location from search history"""

	bl_idname = "view3d.map_goto_history"
	bl_label = "Search History"
	bl_options = {'INTERNAL'}

	index: IntProperty()

	def execute(self, context):
		if 0 <= self.index < len(_search_history):
			context.scene.gis_goto_query = _search_history[self.index]
		return {'FINISHED'}


class VIEW3D_OT_map_resume(bpy.types.Operator):
	"""Resume the map viewer with the last used settings"""

	bl_idname = "view3d.map_resume"
	bl_label = "Resume Map"
	bl_description = 'Resume map viewer with last settings (no dialog)'
	bl_options = {'INTERNAL'}

	@classmethod
	def poll(cls, context):
		return (context.area is not None and context.area.type == 'VIEW_3D'
			and _last_map_src is not None
			and _last_map_lay is not None
			and _last_map_grd is not None)

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		#check cache folder
		folder = prefs.cacheFolder
		if folder == "" or not os.path.exists(folder):
			self.report({'ERROR'}, "Please define a valid cache folder path in addon's preferences")
			return {'CANCELLED'}
		bpy.ops.view3d.map_viewer('INVOKE_DEFAULT',
			srckey=_last_map_src, laykey=_last_map_lay, grdkey=_last_map_grd,
			recenter=False)
		return {'FINISHED'}


class VIEW3D_OT_map_export(bpy.types.Operator):
	"""Export current basemap view as textured mesh"""

	bl_idname = "view3d.map_export"
	bl_label = "Export as Mesh"
	bl_description = 'Export current basemap tiles as a textured mesh'
	bl_options = {'INTERNAL'}

	@classmethod
	def poll(cls, context):
		return _map_viewer_active

	def execute(self, context):
		global _export_pending
		_export_pending = True
		return {'FINISHED'}


class VIEW3D_OT_map_exit(bpy.types.Operator):
	"""Exit the map viewer"""

	bl_idname = "view3d.map_exit"
	bl_label = "Exit"
	bl_description = 'Exit the map viewer'
	bl_options = {'INTERNAL'}

	@classmethod
	def poll(cls, context):
		return _map_viewer_active

	def execute(self, context):
		global _exit_pending
		_exit_pending = True
		return {'FINISHED'}


MARKER_COLLECTION_NAME = 'Markers'

def _get_or_create_marker_collection(scene):
	"""Get or create the Markers collection in the scene."""
	for col in scene.collection.children:
		if col.name == MARKER_COLLECTION_NAME:
			return col
	col = bpy.data.collections.new(MARKER_COLLECTION_NAME)
	scene.collection.children.link(col)
	return col

def _get_marker_objects(scene):
	"""Return all marker empties in the Markers collection."""
	for col in scene.collection.children:
		if col.name == MARKER_COLLECTION_NAME:
			return [obj for obj in col.objects if obj.type == 'EMPTY' and obj.get('_cartoblend_marker')]
	return []


class VIEW3D_OT_marker_add(bpy.types.Operator):
	"""Add a location marker by searching for a place name"""

	bl_idname = "view3d.marker_add"
	bl_label = "Add Marker"
	bl_description = 'Search for a location and place a marker empty'
	bl_options = {'REGISTER', 'UNDO'}

	def execute(self, context):
		query = context.scene.gis_marker_query.strip()
		if not query:
			self.report({'INFO'}, "Please enter a location")
			return {'CANCELLED'}

		geoscn = GeoScene(context.scene)
		if not geoscn.isGeoref:
			self.report({'ERROR'}, "Scene must be georeferenced first (use Map Viewer)")
			return {'CANCELLED'}

		# Query Nominatim
		try:
			results = nominatimQuery(query, referer='bgis', user_agent=USER_AGENT)
		except Exception as e:
			log.error('Marker: Nominatim query failed', exc_info=True)
			self.report({'ERROR'}, "Search failed: {}".format(e))
			return {'CANCELLED'}

		if not results:
			self.report({'INFO'}, "No location found")
			return {'CANCELLED'}

		result = results[0]
		lat = float(result['lat'])
		lon = float(result['lon'])
		display_name = result.get('display_name', query)
		# Short name: first part before the first comma
		short_name = display_name.split(',')[0].strip()

		# Convert lat/lon to scene coordinates
		from ..core.proj.reproj import reprojPt
		x, y = reprojPt(4326, geoscn.crs, lon, lat)
		vx, vy = geoscn.projToView3d(x, y)

		# Create empty at location
		marker_col = _get_or_create_marker_collection(context.scene)
		empty = bpy.data.objects.new(short_name, None)
		empty.empty_display_type = 'PLAIN_AXES'
		empty.empty_display_size = 0.5
		empty.location = (vx, vy, 0)
		empty.show_name = True
		empty['_cartoblend_marker'] = True
		empty['marker_lat'] = lat
		empty['marker_lon'] = lon
		empty['marker_display_name'] = display_name
		marker_col.objects.link(empty)

		# Clear search field
		context.scene.gis_marker_query = ""

		self.report({'INFO'}, "Marker: {}".format(short_name))
		return {'FINISHED'}


class VIEW3D_OT_marker_remove(bpy.types.Operator):
	"""Remove a location marker"""

	bl_idname = "view3d.marker_remove"
	bl_label = "Remove Marker"
	bl_description = 'Remove this marker from the scene'
	bl_options = {'REGISTER', 'UNDO'}

	name: StringProperty()

	def execute(self, context):
		for col in context.scene.collection.children:
			if col.name == MARKER_COLLECTION_NAME:
				for obj in col.objects:
					if obj.name == self.name and obj.get('_cartoblend_marker'):
						bpy.data.objects.remove(obj, do_unlink=True)
						return {'FINISHED'}
		self.report({'WARNING'}, "Marker not found")
		return {'CANCELLED'}


class VIEW3D_OT_marker_select(bpy.types.Operator):
	"""Select and zoom to a marker"""

	bl_idname = "view3d.marker_select"
	bl_label = "Select Marker"
	bl_description = 'Select this marker and center the view on it'
	bl_options = {'INTERNAL'}

	name: StringProperty()

	def execute(self, context):
		for col in context.scene.collection.children:
			if col.name == MARKER_COLLECTION_NAME:
				for obj in col.objects:
					if obj.name == self.name and obj.get('_cartoblend_marker'):
						# Deselect all, select marker
						bpy.ops.object.select_all(action='DESELECT')
						obj.select_set(True)
						context.view_layer.objects.active = obj
						# Center view on marker
						if context.region_data:
							context.region_data.view_location = obj.location.copy()
						return {'FINISHED'}
		return {'CANCELLED'}


classes = [
	VIEW3D_OT_map_start,
	VIEW3D_OT_map_viewer,
	VIEW3D_OT_map_search,
	VIEW3D_OT_map_search_results,
	VIEW3D_OT_map_goto,
	VIEW3D_OT_map_goto_history,
	VIEW3D_OT_map_resume,
	VIEW3D_OT_map_export,
	VIEW3D_OT_map_exit,
	VIEW3D_OT_marker_add,
	VIEW3D_OT_marker_remove,
	VIEW3D_OT_marker_select,
]

def register():
	global _overlay_draw_handler
	bpy.utils.register_class(GIS_PG_basemap_settings)
	bpy.types.Scene.gis_basemap = PointerProperty(type=GIS_PG_basemap_settings)
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError as e:
			log.warning('{} is already registered, now unregister and retry... '.format(cls))
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)
	# Scene properties for inline "Go to Location" input in N-Panel
	def _on_goto_query_confirm(self, context):
		if not self.gis_goto_query.strip():
			return
		#Property update callbacks have no VIEW_3D area in context, so map_start.invoke
		#would bail with "View3D not found". Override with the first VIEW_3D area found.
		for win in bpy.context.window_manager.windows:
			for area in win.screen.areas:
				if area.type == 'VIEW_3D':
					region = next((r for r in area.regions if r.type == 'WINDOW'), None)
					if region is not None:
						with bpy.context.temp_override(window=win, area=area, region=region):
							bpy.ops.view3d.map_goto('EXEC_DEFAULT')
						return
	bpy.types.Scene.gis_goto_query = StringProperty(
		name="Location",
		description="Search for a place name or address",
		default="",
		update=_on_goto_query_confirm
	)
	bpy.types.Scene.gis_goto_result = StringProperty(
		name="Result",
		description="Resolved location from last search",
		default=""
	)
	bpy.types.Scene.gis_marker_query = StringProperty(
		name="Marker Search",
		description="Search for a place to add a marker",
		default=""
	)
	# Register persistent info overlay draw handler
	global _overlay_draw_handler
	if _overlay_draw_handler is None:
		_overlay_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
			_drawOverlayPersistent, (), 'WINDOW', 'POST_PIXEL')

def unregister():
	global _overlay_draw_handler, _map_viewer_active
	_map_viewer_active = False
	if _overlay_draw_handler is not None:
		bpy.types.SpaceView3D.draw_handler_remove(_overlay_draw_handler, 'WINDOW')
		_overlay_draw_handler = None
	if hasattr(bpy.types.Scene, 'gis_marker_query'):
		del bpy.types.Scene.gis_marker_query
	if hasattr(bpy.types.Scene, 'gis_goto_result'):
		del bpy.types.Scene.gis_goto_result
	if hasattr(bpy.types.Scene, 'gis_goto_query'):
		del bpy.types.Scene.gis_goto_query
	for cls in classes:
		bpy.utils.unregister_class(cls)
	if hasattr(bpy.types.Scene, 'gis_basemap'):
		del bpy.types.Scene.gis_basemap
	bpy.utils.unregister_class(GIS_PG_basemap_settings)
