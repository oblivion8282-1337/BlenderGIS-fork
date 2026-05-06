# Derived from https://github.com/hrbaer/Blender-ASCII-Grid-Import

import re
import os
import string
import threading
import bpy
import math
import numpy as np

import logging
log = logging.getLogger(__name__)

from bpy_extras.io_utils import ImportHelper #helper class defines filename and invoke() function which calls the file selector
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator

from ..core.proj import Reproj
from ..core.utils import XY
from ..geoscene import GeoScene, georefManagerLayout
from ..prefs import PredefCRS

from .utils import bpyGeoRaster as GeoRaster
from .utils import placeObj, adjust3Dview, showTextures, addTexture, getBBOX
from .utils import rasterExtentToMesh, geoRastUVmap, setDisplacer

PKG = __package__.rsplit('.', maxsplit=1)[0]  # bl_ext.user_default.cartoblend


# ---------------------------------------------------------------------------
# Module-level state for background ASC parse/reproject thread
# ---------------------------------------------------------------------------
_asc_state_lock = threading.Lock()
_asc_thread = None
_asc_result = None   # dict with keys: 'vertices', 'faces', 'reprojection_to', 'name', 'error'
_asc_args = None     # tuple of context-derived values needed in the poll callback


def _asc_worker(filename, nrows, ncols, cellsize, nodata, step, offset,
                reprojection_from, rprj, rprjToScene, import_mode, result):
    """
    Background thread: Phase A (np.loadtxt) + Phase B (reproject).
    Writes vertices/faces into *result*. No bpy calls allowed here.
    """
    try:
        # --- Phase A: parse data matrix ---
        with open(filename, 'r', encoding='utf-8') as f:
            # Skip the 6-line header that was already parsed in execute()
            for _ in range(6):
                f.readline()
            arr = np.loadtxt(f, dtype=np.float32)

        if arr.shape != (nrows, ncols):
            raise ValueError(
                'Data shape {} does not match header ({}, {})'.format(arr.shape, nrows, ncols))

        # Flip so row 0 = south; then decimate
        arr = arr[::-1, :]
        arr = arr[::step, ::step]

        sub_nrows, sub_ncols = arr.shape

        # Build coordinate grids in source CRS
        col_idx = np.arange(sub_ncols, dtype=np.float32) * (cellsize * step) + offset.x
        row_idx = np.arange(sub_nrows, dtype=np.float32) * (cellsize * step) + offset.y
        xs, ys = np.meshgrid(col_idx, row_idx)  # (sub_nrows, sub_ncols)

        # --- Phase B: reproject ---
        if rprj:
            src_xs = xs.ravel() + reprojection_from.x
            src_ys = ys.ravel() + reprojection_from.y
            reproj_pts = rprjToScene.pts(list(zip(src_xs.tolist(), src_ys.tolist())))
            reproj_arr = np.array(reproj_pts, dtype=np.float32)
            xs_out = reproj_arr[:, 0] - result['reprojection_to_x']
            ys_out = reproj_arr[:, 1] - result['reprojection_to_y']
        else:
            xs_out = xs.ravel()
            ys_out = ys.ravel()

        zs = arr.ravel()

        if import_mode == 'CLOUD':
            mask = zs != nodata
            xs_out = xs_out[mask]
            ys_out = ys_out[mask]
            zs = zs[mask]
        else:
            zs = np.where(zs == nodata, np.float32(0.0), zs)

        vertices = list(zip(xs_out.tolist(), ys_out.tolist(), zs.tolist()))

        faces = []
        if import_mode == 'MESH':
            index = 0
            for r in range(sub_nrows - 1):
                for c in range(sub_ncols - 1):
                    v1 = index
                    v2 = v1 + sub_ncols
                    v3 = v2 + 1
                    v4 = v1 + 1
                    faces.append((v1, v2, v3, v4))
                    index += 1
                index += 1

        with _asc_state_lock:
            result['vertices'] = vertices
            result['faces'] = faces
            result['ok'] = True

    except Exception as exc:
        log.error('ASC background worker failed', exc_info=True)
        with _asc_state_lock:
            result['ok'] = False
            result['error'] = str(exc)


class IMPORTGIS_OT_ascii_grid(Operator, ImportHelper):
    """Import ESRI ASCII grid file"""
    bl_idname = "importgis.asc_file"  # important since its how bpy.ops.importgis.asc is constructed (allows calling operator from python console or another script)
    #bl_idname rules: must contain one '.' (dot) charactere, no capital letters, no reserved words (like 'import')
    bl_description = 'Import ESRI ASCII grid with world file'
    bl_label = "Import ASCII Grid"
    bl_options = {"UNDO"}

    # ImportHelper class properties
    filter_glob: StringProperty(
        default="*.asc;*.grd",
        options={'HIDDEN'},
    )

    # Raster CRS definition
    def listPredefCRS(self, context):
        return PredefCRS.getEnumItems()
    fileCRS: EnumProperty(
        name = "CRS",
        description = "Choose a Coordinate Reference System",
        items = listPredefCRS,
    )

    # List of operator properties, the attributes will be assigned
    # to the class instance from the operator settings before calling.
    importMode: EnumProperty(
        name = "Mode",
        description = "Select import mode",
        items = [
            ('MESH', 'Mesh', "Create triangulated regular network mesh"),
            ('CLOUD', 'Point cloud', "Create vertex point cloud"),
        ],
    )

    # Step makes point clouds with billions of points possible to read on consumer hardware
    step: IntProperty(
        name = "Step",
        description = "Only read every Nth point for massive point clouds",
        default = 1,
        min = 1
    )

    # Let the user decide whether to use the faster newline method
    # Alternatively, use self.total_newlines(filename) to see whether total >= nrows and automatically decide (at the cost of time spent counting lines)
    newlines: BoolProperty(
        name = "Newline-delimited rows",
        description = "Use this method if the file contains newline separated rows for faster import",
        default = True,
    )

    def draw(self, context):
        #Function used by blender to draw the panel.
        layout = self.layout
        layout.prop(self, 'importMode')
        layout.prop(self, 'step')
        layout.prop(self, 'newlines')

        row = layout.row(align=True)
        split = row.split(factor=0.35, align=True)
        split.label(text='CRS:')
        split.prop(self, "fileCRS", text='')
        row.operator("bgis.add_predef_crs", text='', icon='ADD')
        scn = bpy.context.scene
        geoscn = GeoScene(scn)
        if geoscn.isPartiallyGeoref:
            georefManagerLayout(self, context)


    def total_lines(self, filename):
        """
        Count newlines in file.
        512MB file ~3 seconds.
        """
        with open(filename, encoding='utf-8') as f:
            lines = 0
            for _ in f:
                lines += 1
            return lines

    def read_row_newlines(self, f, ncols):
        """
        Read a row by columns separated by newline.
        """
        return f.readline().split()

    def read_row_whitespace(self, f, ncols):
        """
        Read a row by columns separated by whitespace (including newlines).
        6x slower than readlines() method but faster than any other method I can come up with. See commit 4d337c4 for alternatives.
        """
        # choose a buffer that requires the least reads, but not too much memory (32MB max)
        # cols * 6 allows us 5 chars plus space, approximating values such as '12345', '-1234', '12.34', '-12.3'
        buf_size = min(1024 * 32, ncols * 6)
        row = []
        read_f = f.read
        while True:
            chunk = read_f(buf_size)

            # assuming we read a complete chunk, remove end of string up to last whitespace to avoid partial values
            # if the chunk is smaller than our buffer size, then we've read to the end of file and
            #   can skip truncating the chunk since we know the last value will be complete
            if len(chunk) == buf_size:
                for i in range(len(chunk) - 1, -1, -1):
                    if chunk[i].isspace():
                        f.seek(f.tell() - (len(chunk) - i))
                        chunk = chunk[:i]
                        break

            # either read was EOF or chunk was all whitespace
            if not chunk:
                return row  # eof without reaching ncols?

            # find each value separated by any whitespace char
            for m in re.finditer(r'([^\s]+)', chunk):
                row.append(m.group(0))
                if len(row) == ncols:
                    # completed a row within this chunk, rewind the position to start at the beginning of the next row
                    f.seek(f.tell() - (len(chunk) - m.end()))
                    return row

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        global _asc_thread, _asc_result, _asc_args

        prefs = context.preferences.addons[PKG].preferences
        bpy.ops.object.select_all(action='DESELECT')
        #Get scene and some georef data
        scn = bpy.context.scene
        geoscn = GeoScene(scn)
        if geoscn.isBroken:
            self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
            return {'CANCELLED'}
        dx, dy = 0, 0
        if geoscn.isGeoref:
            dx, dy = geoscn.getOriginPrj()
        scale = geoscn.scale #TODO
        if not geoscn.hasCRS:
            try:
                geoscn.crs = self.fileCRS
            except Exception as e:
                log.error("Cannot set scene crs", exc_info=True)
                self.report({'ERROR'}, "Cannot set scene crs, check logs for more infos")
                return {'CANCELLED'}

        #build reprojector objects
        if geoscn.crs != self.fileCRS:
            rprj = True
            rprjToRaster = Reproj(geoscn.crs, self.fileCRS)
            rprjToScene = Reproj(self.fileCRS, geoscn.crs)
        else:
            rprj = False
            rprjToRaster = None
            rprjToScene = None

        #Path
        filename = self.filepath
        name = os.path.splitext(os.path.basename(filename))[0]
        log.info('Importing {}...'.format(filename))

        # --- Parse 6-line header (fast, stays in main thread) ---
        with open(filename, 'r', encoding='utf-8') as f:
            meta_re = re.compile(r'^([^\s]+)\s+([^\s]+)$')  # 'abc  123'
            meta = {}
            for i in range(6):
                line = f.readline()
                m = meta_re.match(line)
                if m:
                    meta[m.group(1).lower()] = m.group(2)
        log.debug(meta)

        step = self.step
        try:
            nrows = int(meta['nrows'])
            ncols = int(meta['ncols'])
            cellsize = float(meta['cellsize'])
        except KeyError as e:
            log.error("Missing required header key: %s", e)
            self.report({'ERROR'}, "Missing required ASC header key: {}".format(e))
            return {'CANCELLED'}
        try:
            nodata = float(meta.get('nodata_value', -9999))
        except (TypeError, ValueError):
            nodata = -9999.0

        reprojection = {}
        offset = XY(0, 0)
        if 'xllcorner' in meta:
            llcorner = XY(float(meta['xllcorner']), float(meta['yllcorner']))
            reprojection['from'] = llcorner
        elif 'xllcenter' in meta:
            centre = XY(float(meta['xllcenter']), float(meta['yllcenter']))
            offset = XY(-cellsize / 2, -cellsize / 2)
            reprojection['from'] = centre
        else:
            log.error("ASC file missing xllcorner/xllcenter header")
            self.report({'ERROR'}, "ASC file is missing xllcorner or xllcenter header")
            return {'CANCELLED'}

        if rprj:
            reprojection['to'] = XY(*rprjToScene.pt(*reprojection['from']))
            log.debug('{name} reprojected from {from} to {to}'.format(**reprojection, name=name))
        else:
            reprojection['to'] = reprojection['from']

        if not geoscn.isGeoref:
            centre = (reprojection['from'].x + offset.x + ((ncols / 2) * cellsize),
                      reprojection['from'].y + offset.y + ((nrows / 2) * cellsize))
            if rprj:
                centre = rprjToScene.pt(*centre)
            geoscn.setOriginPrj(*centre)
            dx, dy = geoscn.getOriginPrj()

        # Doppelklick-Guard: don't start a second thread while one is running
        with _asc_state_lock:
            if _asc_thread is not None and _asc_thread.is_alive():
                self.report({'INFO'}, "Import already running, please wait...")
                return {'CANCELLED'}

            # Initialise shared result dict; reprojection_to_x/y are needed inside worker
            _asc_result = {
                'ok': None,
                'error': None,
                'vertices': None,
                'faces': None,
                'reprojection_to_x': reprojection['to'].x,
                'reprojection_to_y': reprojection['to'].y,
            }
            _asc_args = {
                'name': name,
                'dx': dx,
                'dy': dy,
                'reprojection_to': reprojection['to'],
                'adjust3Dview': prefs.adjust3Dview,
            }
            _asc_thread = threading.Thread(
                target=_asc_worker,
                args=(
                    filename, nrows, ncols, cellsize, nodata, step, offset,
                    reprojection['from'], rprj, rprjToScene,
                    self.importMode, _asc_result,
                ),
                daemon=True,
            )
            _asc_thread.start()

        self.report({'INFO'}, "Importing ASC grid in background, please wait...")

        # --- Timer: Phase C runs in main thread once worker is done ---
        def _poll_asc_thread():
            global _asc_thread, _asc_result, _asc_args

            with _asc_state_lock:
                if _asc_thread is None or _asc_thread.is_alive():
                    return 0.5  # poll again
                # Thread finished — consume state under lock
                _asc_thread = None
                result = _asc_result
                args = _asc_args
                _asc_result = None
                _asc_args = None

            if not result or not result.get('ok'):
                err = result.get('error', 'Unknown error') if result else 'No result'
                log.error('ASC import failed: %s', err)
                try:
                    bpy.context.window.cursor_set('DEFAULT')
                except Exception:
                    pass
                return None  # stop timer

            # --- Phase C: build mesh in main thread ---
            try:
                name = args['name']
                dx = args['dx']
                dy = args['dy']
                reprojection_to = args['reprojection_to']

                me = bpy.data.meshes.new(name)
                ob = bpy.data.objects.new(name, me)
                ob.location = (reprojection_to.x - dx, reprojection_to.y - dy, 0)

                scn = bpy.context.scene
                scn.collection.objects.link(ob)
                bpy.context.view_layer.objects.active = ob
                ob.select_set(True)

                me.from_pydata(result['vertices'], [], result['faces'])
                me.update()

                if args['adjust3Dview']:
                    bb = getBBOX.fromObj(ob)
                    adjust3Dview(bpy.context, bb)

                log.info('ASC import finished: %s', name)
            except Exception:
                log.error('ASC mesh build failed', exc_info=True)
            finally:
                try:
                    bpy.context.window.cursor_set('DEFAULT')
                except Exception:
                    pass

            return None  # stop timer

        bpy.app.timers.register(_poll_asc_thread, first_interval=0.5)

        return {'FINISHED'}

def register():
	try:
		bpy.utils.register_class(IMPORTGIS_OT_ascii_grid)
	except ValueError as e:
		log.warning('{} is already registered, now unregister and retry... '.format(IMPORTGIS_OT_ascii_grid))
		unregister()
		bpy.utils.register_class(IMPORTGIS_OT_ascii_grid)

def unregister():
	bpy.utils.unregister_class(IMPORTGIS_OT_ascii_grid)
