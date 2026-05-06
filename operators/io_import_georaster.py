# -*- coding:utf-8 -*-

# This file is part of CartoBlend

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
import bmesh
import os
import math
import threading
import numpy as np  # Ship with Blender since 2.70

import logging
log = logging.getLogger(__name__)

from mathutils import Vector

from ..geoscene import GeoScene, georefManagerLayout
from ..prefs import PredefCRS

from ..core.georaster import GeoRaster
from .utils import placeObj, adjust3Dview, showTextures, addTexture, getBBOX
from .utils import rasterExtentToMesh, geoRastUVmap, setDisplacer

from ..core import HAS_GDAL
if HAS_GDAL:
	from osgeo import gdal

from ..core import XY as xy
from ..core.errors import OverlapError
from ..core.proj import Reproj

from bpy_extras.io_utils import ImportHelper  # helper class defines filename and invoke() function which calls the file selector
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator

PKG = __package__.rsplit('.', maxsplit=1)[0]  # bl_ext.user_default.cartoblend

# ---------------------------------------------------------------------------
# Module-level state for background georaster import
# ---------------------------------------------------------------------------
_georaster_state_lock = threading.Lock()
_georaster_thread = None
_georaster_result = None   # dict: 'ok', 'error', 'mode', 'data' (mode-specific payload)
_georaster_ctx = None      # dict: operator args captured before thread starts

# ---------------------------------------------------------------------------
# Worker helpers  (NO bpy.data.* / bpy.context.* / bmesh allowed here)
# ---------------------------------------------------------------------------

class _RasterData:
	"""Plain-Python container that carries the pure-data result from Phase A/B.
	All fields are primitive types or numpy arrays — no Blender objects."""
	__slots__ = (
		# shared
		'path', 'name', 'importMode',
		'geoSize_x', 'geoSize_y',
		'center_x', 'center_y',
		'size_x', 'size_y',
		'corners', 'cornersCenter',
		'pxSize_x', 'pxSize_y',
		'rotation_xy',
		'bbox',
		'depth', 'ddtype', 'format', 'noData',
		'is_float', 'raw',
		# processed-file path (may differ from original when clip/fillNodata caused re-write)
		'load_path',
		# DEM / DEM_RAW only
		'verts', 'faces',
		# geometry (for new-plane modes)
		'origin_updated', 'dx', 'dy',
		# raw GeoRaster handle (used by Phase C to load image into bpy)
		'_rast',
	)

	def __init__(self):
		for s in self.__slots__:
			setattr(self, s, None)


def _worker_phase_ab(ctx, result_holder):
	"""Phase A (parse) + Phase B (transform/resample) — runs in background thread.
	Populates result_holder with either {'ok': True, 'data': _RasterData}
	or {'ok': False, 'error': str}.
	"""
	try:
		_do_phase_ab(ctx, result_holder)
	except Exception as exc:
		log.error("Georaster background worker failed", exc_info=True)
		with _georaster_state_lock:
			result_holder['ok'] = False
			result_holder['error'] = str(exc)


def _do_phase_ab(ctx, result_holder):
	"""Actual work — separated so exceptions are cleanly caught."""
	importMode = ctx['importMode']
	filePath   = ctx['filePath']
	subBox     = ctx['subBox']
	clip       = ctx['clip']
	fillNodata = ctx['fillNodata']
	step       = ctx['step']
	buildFaces = ctx['buildFaces']
	subdivision= ctx['subdivision']
	rprjToRaster = ctx.get('rprjToRaster')
	rprjToScene  = ctx.get('rprjToScene')
	dx         = ctx['dx']
	dy         = ctx['dy']
	geoscn_isGeoref = ctx['geoscn_isGeoref']

	rd = _RasterData()
	rd.importMode = importMode
	rd.name = os.path.splitext(os.path.basename(filePath))[0]

	# -----------------------------------------------------------------------
	# Helper: open a GeoRaster without bpy (pure file/numpy I/O)
	# -----------------------------------------------------------------------
	def _open_raw(path, subBoxGeo=None):
		"""Open GeoRaster using only file I/O (no bpy calls)."""
		return GeoRaster(path, subBoxGeo=subBoxGeo, useGDAL=HAS_GDAL)

	def _preprocess_raster(path, subBoxGeo=None, clip_sub=False, fillNodata_=False):
		"""Replicate bpyGeoRaster preprocessing without calling bpy.data.images.load.
		Returns (load_path, georaster_obj).
		"""
		rast = GeoRaster(path, subBoxGeo=subBoxGeo, useGDAL=HAS_GDAL)
		needs_convert = (
			rast.format not in ['GTiff', 'TIFF', 'BMP', 'PNG', 'JPEG', 'JPEG2000']
			or (clip_sub and rast.subBoxGeo is not None)
			or fillNodata_
			or rast.ddtype == 'int16'
		)
		if needs_convert:
			if clip_sub:
				img = rast.readAsNpArray(subset=True)
			else:
				img = rast.readAsNpArray()
			img.cast2float()
			if fillNodata_:
				img.fillNodata()
			out_path = os.path.splitext(rast.path)[0] + '_bgis.tif'
			img.save(out_path)
			rast = GeoRaster(out_path, useGDAL=HAS_GDAL)
			return out_path, rast
		return path, rast

	# -----------------------------------------------------------------------
	if importMode == 'PLANE':
		load_path, rast = _preprocess_raster(filePath)
		_fill_raster_meta(rd, rast, load_path, raw=False)
		if not geoscn_isGeoref:
			new_dx, new_dy = rast.center.x, rast.center.y
			if rprjToScene is not None:
				new_dx, new_dy = rprjToScene.pt(new_dx, new_dy)
			rd.origin_updated = True
			rd.dx = new_dx
			rd.dy = new_dy
		else:
			rd.origin_updated = False
			rd.dx = dx
			rd.dy = dy

	# -----------------------------------------------------------------------
	elif importMode == 'BKG':
		load_path, rast = _preprocess_raster(filePath)
		_fill_raster_meta(rd, rast, load_path, raw=False)
		if not geoscn_isGeoref:
			rd.origin_updated = True
			rd.dx = rast.center.x
			rd.dy = rast.center.y
		else:
			rd.origin_updated = False
			rd.dx = dx
			rd.dy = dy

	# -----------------------------------------------------------------------
	elif importMode == 'MESH':
		load_path, rast = _preprocess_raster(filePath, subBoxGeo=subBox)
		_fill_raster_meta(rd, rast, load_path, raw=False)
		rd.origin_updated = False
		rd.dx = dx
		rd.dy = dy

	# -----------------------------------------------------------------------
	elif importMode == 'DEM':
		load_path, rast = _preprocess_raster(
			filePath, subBoxGeo=subBox,
			clip_sub=clip, fillNodata_=fillNodata
		)
		_fill_raster_meta(rd, rast, load_path, raw=True)
		# Geometry for new plane (if not demOnMesh)
		if not ctx['demOnMesh']:
			if not geoscn_isGeoref:
				new_dx, new_dy = rast.center.x, rast.center.y
				if rprjToScene is not None:
					new_dx, new_dy = rprjToScene.pt(new_dx, new_dy)
				rd.origin_updated = True
				rd.dx = new_dx
				rd.dy = new_dy
			else:
				rd.origin_updated = False
				rd.dx = dx
				rd.dy = dy
			# For subdivision='mesh' pre-compute vertex/face data (pure numpy, no bpy)
			if subdivision == 'mesh':
				verts, faces = _compute_flat_mesh_data(rast, rd.dx, rd.dy, step, rprjToScene)
				rd.verts = verts
				rd.faces = faces
		else:
			rd.origin_updated = False
			rd.dx = dx
			rd.dy = dy

	# -----------------------------------------------------------------------
	elif importMode == 'DEM_RAW':
		try:
			rast = _open_raw(filePath, subBoxGeo=subBox)
		except (OSError, OverlapError):
			raise
		if not geoscn_isGeoref:
			new_dx, new_dy = rast.center.x, rast.center.y
			if rprjToScene is not None:
				new_dx, new_dy = rprjToScene.pt(new_dx, new_dy)
			rd.origin_updated = True
			rd.dx = new_dx
			rd.dy = new_dy
		else:
			rd.origin_updated = False
			rd.dx = dx
			rd.dy = dy
		# Pre-compute ALL vertex/face data as plain Python lists (no bpy)
		verts, faces = _compute_dem_raw_mesh_data(
			rast, rd.dx, rd.dy, step, buildFaces,
			clip, subBox, rprjToScene
		)
		rd.verts = verts
		rd.faces = faces
		_fill_raster_meta(rd, rast, filePath, raw=False)

	else:
		raise ValueError(f"Unknown importMode: {importMode}")

	with _georaster_state_lock:
		result_holder['ok'] = True
		result_holder['data'] = rd


# ---------------------------------------------------------------------------
# Pure-numpy helpers (no bpy) for mesh pre-computation
# ---------------------------------------------------------------------------

def _fill_raster_meta(rd, rast, load_path, raw=False):
	"""Copy GeoRaster metadata into _RasterData (no bpy)."""
	rd.path      = rast.path
	rd.load_path = load_path
	rd.raw       = raw
	rd.depth     = rast.depth
	rd.ddtype    = rast.ddtype
	rd.format    = rast.format
	rd.noData    = rast.noData
	rd.is_float  = rast.isFloat if hasattr(rast, 'isFloat') else False
	rd.size_x    = rast.size.x
	rd.size_y    = rast.size.y
	rd.center_x  = rast.center.x
	rd.center_y  = rast.center.y
	rd.pxSize_x  = rast.pxSize.x
	rd.pxSize_y  = rast.pxSize.y
	rd.geoSize_x = rast.geoSize.x
	rd.geoSize_y = rast.geoSize.y
	rd.corners       = [(pt[0], pt[1]) for pt in rast.corners]
	rd.cornersCenter = [(pt[0], pt[1]) for pt in rast.cornersCenter]
	rd.rotation_xy   = list(rast.rotation.xy) if hasattr(rast, 'rotation') else [0, 0]
	rd.bbox          = rast.bbox
	# Expose the full raster object on rd for Phase C helpers that still need it
	# (georef, subBoxPx, etc.).  We keep the attribute name private.
	rd._rast = rast


def _compute_flat_mesh_data(rast, dx, dy, step, reproj):
	"""Flat (z=0) mesh as plain Python lists — used for DEM subdivision='mesh'."""
	georef = rast.georef
	x0, y0 = georef.origin
	pxSizeX, pxSizeY = georef.pxSize.x, georef.pxSize.y
	w, h = georef.rSize.x, georef.rSize.y
	w_s = math.ceil(w / step)
	h_s = math.ceil(h / step)
	pxSizeX_s = pxSizeX * step
	pxSizeY_s = pxSizeY * step

	x = np.array([(x0 + pxSizeX_s * i) - dx for i in range(w_s)], dtype=np.float64)
	y = np.array([(y0 + pxSizeY_s * i) - dy for i in range(h_s)], dtype=np.float64)
	xx, yy = np.meshgrid(x, y)
	zz = np.zeros((h_s, w_s), dtype=np.float32)

	if reproj is not None:
		pts_raw = list(zip(xx.ravel().tolist(), yy.ravel().tolist()))
		reproj_pts = reproj.pts(pts_raw)
		ra = np.array(reproj_pts, dtype=np.float64)
		xx_flat = ra[:, 0]
		yy_flat = ra[:, 1]
	else:
		xx_flat = xx.ravel()
		yy_flat = yy.ravel()

	zz_flat = zz.ravel()
	verts = list(zip(xx_flat.tolist(), yy_flat.tolist(), zz_flat.tolist()))
	faces = [
		(x_ + y_ * w_s, x_ + y_ * w_s + 1, x_ + y_ * w_s + 1 + w_s, x_ + y_ * w_s + w_s)
		for y_ in range(h_s - 1) for x_ in range(w_s - 1)
	]
	return verts, faces


def _compute_dem_raw_mesh_data(rast, dx, dy, step, buildFaces, clip, subBox, reproj):
	"""Compute DEM_RAW vertex/face lists purely in numpy (no bpy)."""
	subset = clip and subBox is not None

	if not subset:
		georef = rast.georef
	else:
		georef = rast.getSubBoxGeoRef()

	x0, y0 = georef.origin
	pxSizeX, pxSizeY = georef.pxSize.x, georef.pxSize.y
	w, h = georef.rSize.x, georef.rSize.y
	w_s = math.ceil(w / step)
	h_s = math.ceil(h / step)
	pxSizeX_s = pxSizeX * step
	pxSizeY_s = pxSizeY * step

	x = np.array([(x0 + pxSizeX_s * i) - dx for i in range(w_s)], dtype=np.float64)
	y = np.array([(y0 + pxSizeY_s * i) - dy for i in range(h_s)], dtype=np.float64)
	xx, yy = np.meshgrid(x, y)

	zz = rast.readAsNpArray(subset=subset).data[::step, ::step]
	nodata_val = rast.noData
	nodata_mask = (zz == nodata_val) if nodata_val is not None else np.zeros(zz.shape, dtype=bool)

	if reproj is not None:
		pts_raw = list(zip(xx.ravel().tolist(), yy.ravel().tolist()))
		reproj_pts = reproj.pts(pts_raw)
		ra = np.array(reproj_pts, dtype=np.float64)
		xx_flat = ra[:, 0]
		yy_flat = ra[:, 1]
	else:
		xx_flat = xx.ravel()
		yy_flat = yy.ravel()

	zz_flat = zz.ravel()
	nodata_flat = nodata_mask.ravel()

	if not buildFaces or nodata_flat.any():
		verts = []
		faces = []
		nodata_set = set()
		idxMap = {}
		for lin_idx in range(h_s * w_s):
			py = lin_idx // w_s
			px = lin_idx % w_s
			if nodata_flat[lin_idx]:
				nodata_set.add(lin_idx)
				continue
			verts.append((float(xx_flat[lin_idx]), float(yy_flat[lin_idx]), float(zz_flat[lin_idx])))
			idxMap[lin_idx] = len(verts) - 1
			if buildFaces and px > 0 and py > 0:
				v1 = lin_idx
				v2 = v1 - 1
				v3 = v2 - w_s
				v4 = v1 - w_s
				f = [v4, v3, v2, v1]
				if not any(v in nodata_set for v in f):
					faces.append([idxMap[v] for v in f])
	else:
		verts = list(zip(xx_flat.tolist(), yy_flat.tolist(), zz_flat.tolist()))
		faces = [
			(x_ + y_ * w_s, x_ + y_ * w_s + 1, x_ + y_ * w_s + 1 + w_s, x_ + y_ * w_s + w_s)
			for y_ in range(h_s - 1) for x_ in range(w_s - 1)
		]
	return verts, faces


# ---------------------------------------------------------------------------
# Phase C helpers — MAIN THREAD ONLY (bpy.data.* allowed)
# ---------------------------------------------------------------------------

def _phase_c_build(rd, ctx):
	"""Build all Blender objects from the pre-computed _RasterData.
	Called from the timer callback — guaranteed to run in the main thread.
	Returns (obj_or_None, newObjCreated: bool, error_msg_or_None).
	"""
	importMode = rd.importMode
	prefs = bpy.context.preferences.addons[PKG].preferences
	scn = bpy.context.scene
	geoscn = GeoScene(scn)

	# Unpack reprojectors from ctx (they are lightweight Python objects)
	rprjToRaster = ctx.get('rprjToRaster')
	rprjToScene  = ctx.get('rprjToScene')

	dx = rd.dx
	dy = rd.dy
	name = rd.name

	obj = None
	newObjCreated = False

	try:
		# Update scene origin if Phase A/B determined a new one
		if rd.origin_updated:
			geoscn.setOriginPrj(dx, dy)

		# ----------------------------------------------------------------
		if importMode == 'PLANE':
			rast = _load_bpy_image(rd)
			mesh = rasterExtentToMesh(name, rd._rast, dx, dy, reproj=rprjToScene)
			obj = placeObj(mesh, name)
			uvTxtLayer = mesh.uv_layers.new(name='rastUVmap')
			geoRastUVmap(obj, uvTxtLayer, rd._rast, dx, dy, reproj=rprjToRaster)
			mat = bpy.data.materials.new('rastMat')
			obj.data.materials.append(mat)
			addTexture(mat, rast, uvTxtLayer, name='rastText')
			newObjCreated = True

		# ----------------------------------------------------------------
		elif importMode == 'BKG':
			rast_img = _load_bpy_image(rd)
			trueSizeX = rd.geoSize_x
			trueSizeY = rd.geoSize_y
			ratio = rd.size_x / rd.size_y
			if rd.origin_updated:
				offx, offy = 0, 0
			else:
				offx = rd.center_x - dx
				offy = rd.center_y - dy
			bkg = bpy.data.objects.new(name, None)
			bkg.empty_display_type = 'IMAGE'
			bkg.empty_image_depth = 'BACK'
			bkg.data = rast_img
			scn.collection.objects.link(bkg)
			bkg.empty_display_size = 1
			bkg.scale = (trueSizeX, trueSizeY * ratio, 1)
			bkg.location = (offx, offy, 0)
			bpy.context.view_layer.objects.active = bkg
			bkg.select_set(True)
			obj = bkg
			if prefs.adjust3Dview:
				adjust3Dview(bpy.context, rd._rast.bbox)
			newObjCreated = False  # background image is not a mesh obj

		# ----------------------------------------------------------------
		elif importMode == 'MESH':
			rast_img = _load_bpy_image(rd)
			obj = scn.objects[ctx['objectsLst_idx']]
			obj.select_set(True)
			bpy.context.view_layer.objects.active = obj
			mesh = obj.data
			uvTxtLayer = mesh.uv_layers.new(name='rastUVmap')
			uvTxtLayer.active = True
			geoRastUVmap(obj, uvTxtLayer, rd._rast, dx, dy, reproj=rprjToRaster)
			mat = bpy.data.materials.new('rastMat')
			obj.data.materials.append(mat)
			addTexture(mat, rast_img, uvTxtLayer, name='rastText')
			newObjCreated = False

		# ----------------------------------------------------------------
		elif importMode == 'DEM':
			rast_img = _load_bpy_image(rd)
			demOnMesh = ctx['demOnMesh']
			subdivision = ctx['subdivision']

			if demOnMesh:
				obj = scn.objects[ctx['objectsLst_idx']]
				mesh = obj.data
				obj.select_set(True)
				bpy.context.view_layer.objects.active = obj
				newObjCreated = False
			else:
				if subdivision == 'mesh':
					# verts/faces pre-computed in thread
					mesh = bpy.data.meshes.new(name)
					mesh.from_pydata(rd.verts, [], rd.faces)
					mesh.update()
				else:
					mesh = rasterExtentToMesh(
						name, rd._rast, dx, dy,
						pxLoc='CENTER', reproj=rprjToScene
					)
				obj = placeObj(mesh, name)
				newObjCreated = True

			previousUVmapIdx = mesh.uv_layers.active_index
			uvTxtLayer = mesh.uv_layers.new(name='demUVmap')
			geoRastUVmap(obj, uvTxtLayer, rd._rast, dx, dy, reproj=rprjToRaster)
			if previousUVmapIdx != -1:
				mesh.uv_layers.active_index = previousUVmapIdx
			if subdivision == 'subsurf':
				if 'SUBSURF' not in [mod.type for mod in obj.modifiers]:
					subsurf = obj.modifiers.new('DEM', type='SUBSURF')
					subsurf.subdivision_type = 'SIMPLE'
					subsurf.levels = 6
					subsurf.render_levels = 6
			dsp = setDisplacer(obj, rd._rast, uvTxtLayer, interpolation=ctx['demInterpolation'])
			if not demOnMesh:
				subBox = getBBOX.fromObj(obj).toGeo(geoscn)

		# ----------------------------------------------------------------
		elif importMode == 'DEM_RAW':
			mesh = bpy.data.meshes.new("DEM")
			mesh.from_pydata(rd.verts, [], rd.faces)
			mesh.update()
			obj = placeObj(mesh, name)
			newObjCreated = True

	except Exception as exc:
		log.error("Phase C (bpy build) failed", exc_info=True)
		return None, False, str(exc)

	# Final view adjustments
	if newObjCreated and prefs.adjust3Dview and obj is not None:
		bb = getBBOX.fromObj(obj)
		adjust3Dview(bpy.context, bb)
	if prefs.forceTexturedSolid:
		showTextures(bpy.context)

	return obj, newObjCreated, None


def _load_bpy_image(rd):
	"""Load image into Blender data — main-thread only."""
	try:
		img = bpy.data.images.load(rd.load_path)
	except Exception as exc:
		raise OSError(f"Unable to load raster into Blender: {exc}") from exc
	if rd.raw:
		img.colorspace_settings.is_data = True
	# Attach so Phase C helpers (setDisplacer, addTexture) can reach it via rd._rast.bpyImg
	rd._rast.bpyImg = img
	return img


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_georaster(Operator, ImportHelper):
	"""Import georeferenced raster (need world file)"""
	bl_idname = "importgis.georaster"
	bl_description = 'Import raster georeferenced with world file'
	bl_label = "Import georaster"
	bl_options = {"UNDO"}

	def listObjects(self, context):
		objs = []
		for index, object in enumerate(bpy.context.scene.objects):
			if object.type == 'MESH':
				objs.append((str(index), object.name, "Object named " + object.name))
		return objs

	# ImportHelper class properties
	filter_glob: StringProperty(
		default="*.tif;*.jpg;*.jpeg;*.png;*.bmp",
		options={'HIDDEN'},
	)

	# Raster CRS definition
	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	rastCRS: EnumProperty(
		name="Raster CRS",
		description="Choose a Coordinate Reference System",
		items=listPredefCRS,
	)
	reprojection: BoolProperty(
		name="Specify raster CRS",
		description="Specify raster CRS if it's different from scene CRS",
		default=False,
	)

	importMode: EnumProperty(
		name="Mode",
		description="Select import mode",
		items=[
			('PLANE',   'Basemap on new plane',          "Place raster texture on new plane mesh"),
			('BKG',     'Basemap as background',         "Place raster as background image"),
			('MESH',    'Basemap on mesh',               "UV map raster on an existing mesh"),
			('DEM',     'DEM as displacement texture',   "Use DEM raster as height texture to wrap a base mesh"),
			('DEM_RAW', 'DEM raw data build [slow]',     "Import a DEM as pixels points cloud with building faces. Do not use with huge dataset."),
		],
	)

	objectsLst: EnumProperty(name="Objects", description="Choose object to edit", items=listObjects)

	def listSubdivisionModes(self, context):
		items = [('subsurf', 'Subsurf', "Add a subsurf modifier"), ('none', 'None', "No subdivision")]
		if not self.demOnMesh:
			items.append(('mesh', 'Mesh', "Create vertices at each pixels"))
		return items

	subdivision: EnumProperty(
		name="Subdivision",
		description="How to subdivise the plane (dispacer needs vertex to work with)",
		items=listSubdivisionModes,
	)

	demOnMesh: BoolProperty(
		name="Apply on existing mesh",
		description="Use DEM as displacer for an existing mesh",
		default=False,
	)
	clip: BoolProperty(
		name="Clip to working extent",
		description="Use the reference bounding box to clip the DEM",
		default=False,
	)
	demInterpolation: BoolProperty(
		name="Smooth relief",
		description="Use texture interpolation to smooth the resulting terrain",
		default=True,
	)
	fillNodata: BoolProperty(
		name="Fill nodata values",
		description="Interpolate existing nodata values to get an usable displacement texture",
		default=False,
	)
	step: IntProperty(name="Step", default=1, description="Pixel step", min=1)
	buildFaces: BoolProperty(name="Build faces", default=True, description='Build quad faces connecting pixel point cloud')

	def draw(self, context):
		layout = self.layout
		layout.prop(self, 'importMode')
		scn = bpy.context.scene
		geoscn = GeoScene(scn)
		if self.importMode == 'PLANE':
			pass
		if self.importMode == 'BKG':
			pass
		if self.importMode == 'MESH':
			if geoscn.isGeoref and len(self.objectsLst) > 0:
				layout.prop(self, 'objectsLst')
			else:
				layout.label(text="There isn't georef mesh to UVmap on")
		if self.importMode == 'DEM':
			layout.prop(self, 'demOnMesh')
			if self.demOnMesh:
				if geoscn.isGeoref and len(self.objectsLst) > 0:
					layout.prop(self, 'objectsLst')
					layout.prop(self, 'clip')
				else:
					layout.label(text="There isn't georef mesh to apply on")
			layout.prop(self, 'subdivision')
			layout.prop(self, 'demInterpolation')
			if self.subdivision == 'mesh':
				layout.prop(self, 'step')
			layout.prop(self, 'fillNodata')
		if self.importMode == 'DEM_RAW':
			layout.prop(self, 'buildFaces')
			layout.prop(self, 'step')
			layout.prop(self, 'clip')
			if self.clip:
				if geoscn.isGeoref and len(self.objectsLst) > 0:
					layout.prop(self, 'objectsLst')
				else:
					layout.label(text="There isn't georef mesh to refer")
		if geoscn.isPartiallyGeoref:
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self.crsInputLayout(context)
			georefManagerLayout(self, context)
		else:
			self.crsInputLayout(context)

	def crsInputLayout(self, context):
		layout = self.layout
		row = layout.row(align=True)
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, "rastCRS", text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	# ------------------------------------------------------------------
	def execute(self, context):
		global _georaster_thread, _georaster_result, _georaster_ctx

		# Doppelklick-Guard
		with _georaster_state_lock:
			if _georaster_thread is not None and _georaster_thread.is_alive():
				self.report({'INFO'}, "Import already running, please wait...")
				return {'CANCELLED'}

		prefs = context.preferences.addons[PKG].preferences
		scn = context.scene
		geoscn = GeoScene(scn)

		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		scale = geoscn.scale  # TODO

		dx, dy = 0, 0
		if geoscn.isGeoref:
			dx, dy = geoscn.getOriginPrj()
			rastCRS = self.rastCRS if self.reprojection else geoscn.crs
		else:
			rastCRS = self.rastCRS
			try:
				geoscn.crs = rastCRS
			except Exception:
				log.error("Cannot set scene crs", exc_info=True)
				self.report({'ERROR'}, "Cannot set scene crs, check logs for more infos")
				return {'CANCELLED'}

		# Build reprojectors (lightweight Python — safe to create here)
		if geoscn.crs != rastCRS:
			rprj = True
			rprjToRaster = Reproj(geoscn.crs, rastCRS)
			rprjToScene  = Reproj(rastCRS, geoscn.crs)
		else:
			rprj = False
			rprjToRaster = None
			rprjToScene  = None

		filePath = self.filepath
		importMode = self.importMode

		# Validate mode-specific prerequisites & resolve subBox before thread
		subBox = None
		objectsLst_idx = None  # resolved integer index for MESH / DEM on existing mesh

		bpy.ops.object.select_all(action='DESELECT')

		if importMode == 'BKG':
			if rprj:
				self.report({'ERROR'}, "Raster reprojection is not possible in background mode")
				return {'CANCELLED'}

		if importMode == 'MESH':
			if not geoscn.isGeoref or len(self.objectsLst) == 0:
				self.report({'ERROR'}, "There isn't georef mesh to apply on")
				return {'CANCELLED'}
			objectsLst_idx = int(self.objectsLst)
			obj = scn.objects[objectsLst_idx]
			obj.select_set(True)
			context.view_layer.objects.active = obj
			subBox = getBBOX.fromObj(obj).toGeo(geoscn)
			if rprj:
				subBox = rprjToRaster.bbox(subBox)

		if importMode == 'DEM':
			if self.demOnMesh:
				if not geoscn.isGeoref or len(self.objectsLst) == 0:
					self.report({'ERROR'}, "There isn't georef mesh to apply on")
					return {'CANCELLED'}
				objectsLst_idx = int(self.objectsLst)
				obj = scn.objects[objectsLst_idx]
				mesh = obj.data
				obj.select_set(True)
				context.view_layer.objects.active = obj
				subBox = getBBOX.fromObj(obj).toGeo(geoscn)
				if rprj:
					subBox = rprjToRaster.bbox(subBox)

		if importMode == 'DEM_RAW':
			if self.clip:
				if not geoscn.isGeoref or len(self.objectsLst) == 0:
					self.report({'ERROR'}, "No working extent")
					return {'CANCELLED'}
				objectsLst_idx = int(self.objectsLst)
				obj = scn.objects[objectsLst_idx]
				subBox = getBBOX.fromObj(obj).toGeo(geoscn)
				if rprj:
					subBox = rprjToRaster.bbox(subBox)

		# Package everything the worker needs (no bpy refs — only primitives)
		ctx = {
			'importMode':      importMode,
			'filePath':        filePath,
			'subBox':          subBox,
			'clip':            self.clip,
			'fillNodata':      self.fillNodata,
			'step':            self.step,
			'buildFaces':      self.buildFaces,
			'subdivision':     self.subdivision,
			'demOnMesh':       self.demOnMesh,
			'demInterpolation': self.demInterpolation,
			'objectsLst_idx':  objectsLst_idx,
			'rprjToRaster':    rprjToRaster,
			'rprjToScene':     rprjToScene,
			'dx':              dx,
			'dy':              dy,
			'geoscn_isGeoref': geoscn.isGeoref,
		}

		# Set wait cursor
		context.window.cursor_set('WAIT')

		with _georaster_state_lock:
			_georaster_result = {'ok': None, 'error': None, 'data': None}
			_georaster_ctx = ctx
			_georaster_thread = threading.Thread(
				target=_worker_phase_ab,
				args=(ctx, _georaster_result),
				daemon=True,
			)
			_georaster_thread.start()

		self.report({'INFO'}, "Importing georaster in background, please wait...")

		# Register polling timer
		def _poll_georaster():
			global _georaster_thread, _georaster_result, _georaster_ctx
			with _georaster_state_lock:
				if _georaster_thread is None or _georaster_thread.is_alive():
					return 0.5  # still running — poll again
				# Thread finished — consume state atomically
				_georaster_thread = None
				result = _georaster_result
				ctx_local = _georaster_ctx
				_georaster_result = None
				_georaster_ctx = None

			# Reset cursor regardless of outcome
			try:
				bpy.context.window.cursor_set('DEFAULT')
			except Exception:
				pass

			if not result or not result.get('ok'):
				err = result.get('error', 'Unknown error') if result else 'No result'
				log.error("Georaster background phase failed: %s", err)
				return None  # stop timer

			# Phase C — main-thread build
			rd = result['data']
			obj, newObjCreated, err_msg = _phase_c_build(rd, ctx_local)
			if err_msg:
				log.error("Georaster phase C failed: %s", err_msg)

			return None  # stop timer

		bpy.app.timers.register(_poll_georaster, first_interval=0.5)

		return {'FINISHED'}


def register():
	try:
		bpy.utils.register_class(IMPORTGIS_OT_georaster)
	except ValueError:
		log.warning('%s is already registered, now unregister and retry... ', IMPORTGIS_OT_georaster)
		unregister()
		bpy.utils.register_class(IMPORTGIS_OT_georaster)


def unregister():
	bpy.utils.unregister_class(IMPORTGIS_OT_georaster)
