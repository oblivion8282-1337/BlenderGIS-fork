# -*- coding:utf-8 -*-
import os
import threading
import bpy
import bmesh
import mathutils

import logging
log = logging.getLogger(__name__)

from ..core.lib.shapefile import Writer as shpWriter
from ..core.lib.shapefile import POINTZ, POLYLINEZ, POLYGONZ, MULTIPOINTZ

from bpy_extras.io_utils import ExportHelper #helper class defines filename and invoke() function which calls the file selector
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
from bpy.types import Operator

from ..geoscene import GeoScene

from ..core.proj import SRS

# ---------------------------------------------------------------------------
# Module-level state for background SHP export
# ---------------------------------------------------------------------------
_shp_state_lock = threading.Lock()
_shp_thread = None
_shp_result = None   # dict: {'ok': bool, 'error': str, 'filePath': str}


def _shp_write_thread(filePath, shapeType, fieldDefs, records, wkt, result_holder):
    """Phase B: run in a background thread.

    All inputs are plain Python objects (no bpy). Writes the SHP/DBF files
    and optionally a .prj sidecar.

    Parameters
    ----------
    filePath  : str  – destination .shp path
    shapeType : int  – shapefile shape-type constant (POINTZ / POLYLINEZ / …)
    fieldDefs : list – list of (name, type, length, decimal) tuples that
                       describe every field in the schema
    records   : list – list of dicts, each with keys:
                       'geom_type'  : one of 'pointz', 'multipointz',
                                      'linez', 'polyz'
                       'geom'       : the geometry argument for the writer call
                       'nFeat'      : int, how many records to emit for this obj
                       'attributes' : dict of field-name → value
    wkt       : str or None  – CRS WKT string for .prj sidecar
    result_holder : dict     – shared result dict (written under no lock here;
                               caller reads it only after thread joins)
    """
    try:
        outShp = shpWriter(filePath)
        outShp.shapeType = shapeType

        # Recreate field schema
        for fdef in fieldDefs:
            name, ftype, length, decimal = fdef
            if decimal:
                outShp.field(name, ftype, length, decimal)
            else:
                outShp.field(name, ftype, length)

        # Write geometries + records
        for rec in records:
            gtype = rec['geom_type']
            geom  = rec['geom']
            attrs = rec['attributes']

            if gtype == 'pointz':
                outShp.pointz(*geom)
            elif gtype == 'multipointz':
                outShp.multipointz(geom)
            elif gtype == 'linez':
                outShp.linez(geom)
            elif gtype == 'polyz':
                outShp.polyz(geom)

            outShp.record(**attrs)

        outShp.close()

        # Write .prj sidecar
        if wkt is not None:
            prjPath = os.path.splitext(filePath)[0] + '.prj'
            with open(prjPath, 'w') as prj:
                prj.write(wkt)

        with _shp_state_lock:
            result_holder['ok'] = True

    except Exception as e:
        log.error('SHP export thread failed', exc_info=True)
        with _shp_state_lock:
            result_holder['ok'] = False
            result_holder['error'] = str(e)


class EXPORTGIS_OT_shapefile(Operator, ExportHelper):
    """Export from ESRI shapefile file format (.shp)"""
    bl_idname = "exportgis.shapefile" # important since its how bpy.ops.import.shapefile is constructed (allows calling operator from python console or another script)
    #bl_idname rules: must contain one '.' (dot) charactere, no capital letters, no reserved words (like 'import')
    bl_description = 'export to ESRI shapefile file format (.shp)'
    bl_label = "Export SHP"
    bl_options = {"UNDO"}


    # ExportHelper class properties
    filename_ext = ".shp"
    filter_glob: StringProperty(
            default = "*.shp",
            options = {'HIDDEN'},
            )

    exportType: EnumProperty(
            name = "Feature type",
            description = "Select feature type",
            items = [
                ('POINTZ', 'Point', ""),
                ('POLYLINEZ', 'Line', ""),
                ('POLYGONZ', 'Polygon', "")
            ])

    objectsSource: EnumProperty(
            name = "Objects",
            description = "Objects to export",
            items = [
                ('COLLEC', 'Collection', "Export a collection of objects"),
                ('SELECTED', 'Selected objects', "Export the current selection")
            ],
            default = 'SELECTED'
            )

    def listCollections(self, context):
        return [(c.name, c.name, "Collection") for c in bpy.data.collections]

    selectedColl: EnumProperty(
        name = "Collection",
        description = "Select the collection to export",
        items = listCollections)

    mode: EnumProperty(
            name = "Mode",
            description = "Select the export strategy",
            items = [
                ('OBJ2FEAT', 'Objects to features', "Create one multipart feature per object"),
                ('MESH2FEAT', 'Mesh to features', "Decompose mesh primitives to separate features")
            ],
            default = 'OBJ2FEAT'
            )


    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def draw(self, context):
        #Function used by blender to draw the panel.
        layout = self.layout
        layout.prop(self, 'objectsSource')
        if self.objectsSource == 'COLLEC':
            layout.prop(self, 'selectedColl')
        layout.prop(self, 'mode')
        layout.prop(self, 'exportType')

    def execute(self, context):
        # ------------------------------------------------------------------
        # Phase A – Main-Thread: bpy/bmesh data extraction
        # Everything here touches bpy; must run in the main thread.
        # ------------------------------------------------------------------
        filePath = self.filepath
        scn = context.scene
        geoscn = GeoScene(scn)

        if geoscn.isGeoref:
            dx, dy = geoscn.getOriginPrj()
            crs = SRS(geoscn.crs)
            try:
                wkt = crs.getWKT()
            except Exception as e:
                log.warning('Cannot convert crs to wkt', exc_info=True)
                wkt = None
        elif geoscn.isBroken:
            self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
            return {'CANCELLED'}
        else:
            dx, dy = (0, 0)
            wkt = None

        if self.objectsSource == 'SELECTED':
            objects = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
        elif self.objectsSource == 'COLLEC':
            try:
                coll = bpy.data.collections[self.selectedColl]
            except KeyError:
                self.report({'ERROR'}, "Collection '{}' not found".format(self.selectedColl))
                return {'CANCELLED'}
            objects = [obj for obj in coll.all_objects if obj.type == 'MESH']

        if not objects:
            self.report({'ERROR'}, "Selection is empty or does not contain any mesh")
            return {'CANCELLED'}

        # --- Determine shape type ---
        if self.exportType == 'POLYGONZ':
            shapeType = POLYGONZ
        elif self.exportType == 'POLYLINEZ':
            shapeType = POLYLINEZ
        elif self.exportType == 'POINTZ' and self.mode == 'MESH2FEAT':
            shapeType = POINTZ
        else:  # POINTZ + OBJ2FEAT
            shapeType = MULTIPOINTZ

        # --- Build field schema (collision detection preserved from original) ---
        cLen = 255  # string fields default length
        nLen = 20   # numeric fields default length
        dLen = 5    # numeric fields default decimal precision
        maxFieldNameLen = 8  # shp capabilities limit field name length to 8 characters

        # fieldDefs: ordered list of (name, type, length, decimal) for Phase B
        fieldDefs = [('objId', 'N', nLen, 0)]
        knownFields = {'objId'}

        # fieldNameMap: origKey → resolved shp field name
        # truncToOrig:  truncated-8-char name → the first origKey that claimed it
        fieldNameMap = {}
        truncToOrig = {}

        for obj in objects:
            for k, v in obj.items():
                origKey = k
                if origKey in fieldNameMap:
                    k = fieldNameMap[origKey]
                else:
                    k = origKey[0:maxFieldNameLen]
                    if k in truncToOrig and truncToOrig[k] != origKey:
                        suffix_n = 1
                        while True:
                            suffix = '_' + str(suffix_n)
                            candidate = k[0:maxFieldNameLen - len(suffix)] + suffix
                            if candidate not in knownFields:
                                log.warning(
                                    "Field name collision: '{}' truncated to '{}' conflicts "
                                    "with an existing field; renamed to '{}'.".format(
                                        origKey, k, candidate))
                                k = candidate
                                break
                            suffix_n += 1
                    else:
                        truncToOrig[k] = origKey
                    fieldNameMap[origKey] = k

                if k not in knownFields:
                    if isinstance(v, float) or isinstance(v, int):
                        fieldType = 'N'
                    elif isinstance(v, str):
                        if v.lstrip("-+").isdigit():
                            v = int(v)
                            fieldType = 'N'
                        else:
                            try:
                                v = float(v)
                            except ValueError:
                                fieldType = 'C'
                            else:
                                fieldType = 'N'
                    else:
                        continue

                    if fieldType == 'C':
                        fieldDefs.append((k, fieldType, cLen, 0))
                    elif fieldType == 'N':
                        if isinstance(v, int):
                            fieldDefs.append((k, fieldType, nLen, 0))
                        else:
                            fieldDefs.append((k, fieldType, nLen, dLen))
                    knownFields.add(k)

        # fieldTypes snapshot for attribute casting (same logic as original)
        fieldTypes = {fd[0]: fd[1] for fd in fieldDefs}
        fieldNames = list(fieldTypes.keys())

        # --- Extract geometry + attributes from bmesh (still Main-Thread) ---
        depsgraph = context.evaluated_depsgraph_get()
        records = []  # list of dicts for Phase B

        for i, obj in enumerate(objects):
            bm = bmesh.new()
            bm.from_object(obj, depsgraph)
            bm.transform(obj.matrix_world)

            if self.exportType == 'POINTZ':
                if len(bm.verts) == 0:
                    bm.free()
                    continue
                pts = [[v.co.x + dx, v.co.y + dy, v.co.z] for v in bm.verts]

                attributes = _build_attributes(i, obj, fieldNameMap, fieldTypes, fieldNames)

                if self.mode == 'MESH2FEAT':
                    for pt in pts:
                        records.append({
                            'geom_type': 'pointz',
                            'geom': pt,
                            'attributes': attributes,
                        })
                elif self.mode == 'OBJ2FEAT':
                    records.append({
                        'geom_type': 'multipointz',
                        'geom': pts,
                        'attributes': attributes,
                    })

            elif self.exportType == 'POLYLINEZ':
                if len(bm.edges) == 0:
                    bm.free()
                    continue
                lines = [
                    [(vert.co.x + dx, vert.co.y + dy, vert.co.z) for vert in edge.verts]
                    for edge in bm.edges
                ]
                attributes = _build_attributes(i, obj, fieldNameMap, fieldTypes, fieldNames)

                if self.mode == 'MESH2FEAT':
                    for line in lines:
                        records.append({
                            'geom_type': 'linez',
                            'geom': [line],
                            'attributes': attributes,
                        })
                elif self.mode == 'OBJ2FEAT':
                    records.append({
                        'geom_type': 'linez',
                        'geom': lines,
                        'attributes': attributes,
                    })

            elif self.exportType == 'POLYGONZ':
                if len(bm.faces) == 0:
                    bm.free()
                    continue
                polygons = []
                for face in bm.faces:
                    poly = [(vert.co.x + dx, vert.co.y + dy, vert.co.z) for vert in face.verts]
                    poly.append(poly[0])  # close poly
                    poly.reverse()        # clockwise for shapefiles
                    polygons.append(poly)
                attributes = _build_attributes(i, obj, fieldNameMap, fieldTypes, fieldNames)

                if self.mode == 'MESH2FEAT':
                    for polygon in polygons:
                        records.append({
                            'geom_type': 'polyz',
                            'geom': [polygon],
                            'attributes': attributes,
                        })
                elif self.mode == 'OBJ2FEAT':
                    records.append({
                        'geom_type': 'polyz',
                        'geom': polygons,
                        'attributes': attributes,
                    })

            bm.free()

        if not records:
            self.report({'ERROR'}, "No geometry to export (empty meshes?)")
            return {'CANCELLED'}

        # ------------------------------------------------------------------
        # Phase B – Background Thread: file I/O (no bpy)
        # ------------------------------------------------------------------
        global _shp_thread, _shp_result

        # Doppelklick-Guard
        with _shp_state_lock:
            if _shp_thread is not None and _shp_thread.is_alive():
                self.report({'INFO'}, "Export already running, please wait...")
                return {'CANCELLED'}
            _shp_result = {'ok': None, 'error': None, 'filePath': filePath}
            _shp_thread = threading.Thread(
                target=_shp_write_thread,
                args=(filePath, shapeType, fieldDefs, records, wkt, _shp_result),
                daemon=True)
            _shp_thread.start()

        self.report({'INFO'}, "Exporting SHP in background, please wait...")

        # Register polling timer (closure captures module globals via 'global')
        def _poll_shp_thread():
            global _shp_thread, _shp_result
            with _shp_state_lock:
                if _shp_thread is None or _shp_thread.is_alive():
                    return 0.5  # poll again in 0.5 s
                # Thread finished – consume state
                _shp_thread = None
                result = _shp_result
                _shp_result = None

            if not result or not result.get('ok'):
                err = result.get('error', 'Unknown error') if result else 'No result'
                log.error('SHP export failed: %s', err)
                try:
                    bpy.context.window.cursor_set('DEFAULT')
                except Exception:
                    pass
                return None  # stop timer

            fp = result.get('filePath', '')
            log.info('SHP export complete: %s', fp)
            try:
                bpy.context.window.cursor_set('DEFAULT')
            except Exception:
                pass
            return None  # stop timer

        bpy.app.timers.register(_poll_shp_thread, first_interval=0.5)

        return {'FINISHED'}


def _build_attributes(obj_idx, obj, fieldNameMap, fieldTypes, fieldNames):
    """Build the attribute dict for one object (called in Main-Thread during Phase A).

    Returns a plain dict with field-name → value suitable for ``shpWriter.record(**attrs)``.
    """
    attributes = {'objId': obj_idx}
    for k, v in obj.items():
        k = fieldNameMap.get(k, k[0:8])
        fType = fieldTypes.get(k)
        if fType is None:
            continue
        if fType in ('N', 'F'):
            try:
                v = float(v)
            except (ValueError, TypeError):
                log.info(
                    'Cannot cast value %r to float for field %s, NULL inserted', v, k)
                v = None
        attributes[k] = v
    # Orphan fields → None
    for fn in fieldNames:
        if fn not in attributes:
            attributes[fn] = None
    return attributes


def register():
    try:
        bpy.utils.register_class(EXPORTGIS_OT_shapefile)
    except ValueError as e:
        log.warning('{} is already registered, now unregister and retry... '.format(EXPORTGIS_OT_shapefile))
        unregister()
        bpy.utils.register_class(EXPORTGIS_OT_shapefile)

def unregister():
    bpy.utils.unregister_class(EXPORTGIS_OT_shapefile)
