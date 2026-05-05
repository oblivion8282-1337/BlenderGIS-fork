import json
import logging
log = logging.getLogger(__name__)
import sys, os

import bpy
from bpy.props import StringProperty, IntProperty, FloatProperty, BoolProperty, EnumProperty, FloatVectorProperty
from bpy.types import Operator, Panel, AddonPreferences
import addon_utils

from . import bl_info
from .core.proj.reproj import MapTilerCoordinates
from .core.proj.srs import SRS
from .core.checkdeps import HAS_GDAL, HAS_PYPROJ, HAS_PIL, HAS_IMGIO
from .core import settings

PKG = __package__

def getAppData():
	home = os.path.expanduser('~')
	loc = os.path.join(home, '.bgis')
	if not os.path.exists(loc):
		os.mkdir(loc, mode=0o700)
	else:
		try:
			os.chmod(loc, 0o700)
		except OSError:
			pass
	return loc

APP_DATA = getAppData()

# ---------------------------------------------------------------------------
# Persistent credentials storage (survives addon reload / Blender restart)
# ---------------------------------------------------------------------------
CREDENTIALS_FILE = os.path.join(APP_DATA, 'credentials.json')

# Keys in credentials.json that map to addon preference properties
_CREDENTIAL_KEYS = [
	'opentopography_api_key',
	'maptiler_api_key',
	'stadia_api_key',
	'mapbox_token',
	'thunderforest_api_key',
	'cdse_client_id',
	'cdse_client_secret',
]

def _load_credentials():
	"""Load credentials from ~/.bgis/credentials.json, return dict."""
	if not os.path.isfile(CREDENTIALS_FILE):
		return {}
	try:
		with open(CREDENTIALS_FILE, 'r', encoding='utf-8') as f:
			return json.load(f)
	except Exception:
		log.warning('Failed to load credentials file', exc_info=True)
		return {}

def _save_credentials(data):
	"""Save credentials dict to ~/.bgis/credentials.json with owner-only permissions."""
	try:
		flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
		fd = os.open(CREDENTIALS_FILE, flags, 0o600)
		with os.fdopen(fd, 'w', encoding='utf-8') as f:
			json.dump(data, f, indent=2)
		try:
			os.chmod(CREDENTIALS_FILE, 0o600)
		except OSError:
			pass
	except Exception:
		log.error('Failed to save credentials file', exc_info=True)

def _sync_credential(prop_name, value):
	"""Update a single key in the credentials file."""
	data = _load_credentials()
	if data.get(prop_name) != value:
		data[prop_name] = value
		_save_credentials(data)

def restore_credentials(prefs):
	"""Restore credentials from file into addon preferences (called on register)."""
	data = _load_credentials()
	# Migrate the legacy two-MapTiler-key split: maptiler_tile_key (used only
	# for tile auth) was redundant because the same MapTiler API key works for
	# both Maps and Coordinates. Fold any leftover value into maptiler_api_key.
	legacy_tile_key = data.get('maptiler_tile_key')
	if legacy_tile_key and not data.get('maptiler_api_key'):
		data['maptiler_api_key'] = legacy_tile_key
		log.info('Migrated legacy maptiler_tile_key -> maptiler_api_key')
	if 'maptiler_tile_key' in data:
		data.pop('maptiler_tile_key', None)
		_save_credentials(data)
	for key in _CREDENTIAL_KEYS:
		val = data.get(key, '')
		if val and not getattr(prefs, key, ''):
			setattr(prefs, key, val)
			log.info('Restored credential: %s', key)

'''
Default Enum properties contents (list of tuple (value, label, tootip))
Theses properties are automatically filled from a serialized json string stored in a StringProperty
This is workaround to have an editable EnumProperty (ie the user can add, remove or edit an entry)
because the Blender Python API does not provides built in functions for these tasks.
To edit the content of these enum, we just need to write new operators which will simply update the json string
As the json backend is stored in addon preferences, the property will be saved and restored for the next blender session
'''


DEFAULT_CRS = [
	# -- Global --
	('EPSG:4326', 'WGS 84 (GPS, global)', 'Longitude and latitude in degrees, DO NOT USE AS SCENE CRS (defined only for reprojection tasks)'),
	('EPSG:3857', 'Web Mercator (Google Maps, OSM)', 'Worldwide projection used by web maps, high distortions at poles, not suitable for precision modelling'),
	# -- Europe --
	('EPSG:25832', 'ETRS89 / UTM zone 32N', 'Germany, Austria, Switzerland, central Europe'),
	('EPSG:25833', 'ETRS89 / UTM zone 33N', 'Eastern Germany, Poland, Czech Republic'),
	('EPSG:25831', 'ETRS89 / UTM zone 31N', 'France, Benelux, western Europe'),
	('EPSG:32632', 'WGS 84 / UTM zone 32N', 'Central Europe (WGS 84 based)'),
	('EPSG:32633', 'WGS 84 / UTM zone 33N', 'Eastern Europe (WGS 84 based)'),
	('EPSG:2154', 'RGF93 / Lambert-93', 'France official projection'),
	('EPSG:27700', 'OSGB 1936 / British National Grid', 'United Kingdom'),
	('EPSG:31256', 'MGI / Austria GK East', 'Austria East (Gauss-Krueger)'),
	('EPSG:31257', 'MGI / Austria GK Central', 'Austria Central (Gauss-Krueger)'),
	('EPSG:31258', 'MGI / Austria GK West', 'Austria West (Gauss-Krueger)'),
	('EPSG:21781', 'CH1903 / LV03', 'Switzerland (old system)'),
	('EPSG:2056', 'CH1903+ / LV95', 'Switzerland (new official system)'),
	('EPSG:28992', 'Amersfoort / RD New', 'Netherlands official projection'),
	# -- North America --
	('EPSG:26917', 'NAD83 / UTM zone 17N', 'US East Coast'),
	('EPSG:26918', 'NAD83 / UTM zone 18N', 'US Northeast'),
	('EPSG:32610', 'WGS 84 / UTM zone 10N', 'US West Coast'),
	('EPSG:32611', 'WGS 84 / UTM zone 11N', 'US Mountain region'),
	('EPSG:2263', 'NAD83 / New York Long Island', 'New York City / Long Island (feet)'),
	# -- Other --
	('EPSG:32648', 'WGS 84 / UTM zone 48N', 'Southeast Asia'),
	('EPSG:32756', 'WGS 84 / UTM zone 56S', 'Australia East Coast'),
]


DEFAULT_DEM_SERVER = [
	("https://portal.opentopography.org/API/globaldem?demtype=SRTMGL1&west={W}&east={E}&south={S}&north={N}&outputFormat=GTiff&API_Key={API_KEY}", 'OpenTopography SRTM 30m', 'OpenTopography.org web service for SRTM 30m global DEM'),
	("https://portal.opentopography.org/API/globaldem?demtype=SRTMGL3&west={W}&east={E}&south={S}&north={N}&outputFormat=GTiff&API_Key={API_KEY}", 'OpenTopography SRTM 90m', 'OpenTopography.org web service for SRTM 90m global DEM'),
	("http://www.gmrt.org/services/GridServer?west={W}&east={E}&south={S}&north={N}&layer=topo&format=geotiff&resolution=high", 'Marine-geo.org GMRT', 'Marine-geo.org web service for GMRT global DEM (terrestrial (ASTER) and bathymetry)')
]

DEFAULT_OVERPASS_SERVER =  [
	("https://lz4.overpass-api.de/api/interpreter", 'overpass-api.de', 'Main Overpass API instance'),
	("http://overpass.openstreetmap.fr/api/interpreter", 'overpass.openstreetmap.fr', 'French Overpass API instance'),
	("https://overpass.kumi.systems/api/interpreter", 'overpass.kumi.systems', 'Kumi Systems Overpass Instance')
]

#default filter tags for OSM import
DEFAULT_OSM_TAGS = [
	'building',
	'highway',
	'landuse',
	'leisure',
	'natural',
	'railway',
	'waterway'
]



class BGIS_OT_pref_show(Operator):

	bl_idname = "bgis.pref_show"
	bl_description = 'Display CartoBlend preferences'
	bl_label = "Preferences"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		addon_utils.modules_refresh()
		context.preferences.active_section = 'ADDONS'
		bpy.context.window_manager.addon_search = bl_info['name']
		try:
			mod = addon_utils.addons_fake_modules.get(PKG)
			if mod:
				mod.bl_info['show_expanded'] = True
		except AttributeError:
			pass
		bpy.ops.screen.userpref_show('INVOKE_DEFAULT')
		return {'FINISHED'}



class BGIS_PREFS(AddonPreferences):

	bl_idname = PKG

	################
	#Predefined Spatial Ref. Systems

	def listPredefCRS(self, context):
		try:
			return [tuple(elem) for elem in json.loads(self.predefCrsJson)]
		except (json.JSONDecodeError, TypeError):
			return [('NONE', 'Error loading data', '')]

	#store crs preset as json string into addon preferences
	predefCrsJson: StringProperty(default=json.dumps(DEFAULT_CRS))

	predefCrs: EnumProperty(
		name = "Predefinate CRS",
		description = "Choose predefined Coordinate Reference System",
		#default = 1, #possible only since Blender 2.90
		items = listPredefCRS
		)

	################
	#proj engine

	def getProjEngineItems(self, context):
		items = [ ('AUTO', 'Auto detect', 'Auto select the best library for reprojection tasks') ]
		if HAS_GDAL:
			items.append( ('GDAL', 'GDAL', 'Force GDAL as reprojection engine') )
		if HAS_PYPROJ:
			items.append( ('PYPROJ', 'pyProj', 'Force pyProj as reprojection engine') )
		#if EPSGIO.ping(): #too slow
		#	items.append( ('EPSGIO', 'epsg.io', '') )
		items.append( ('EPSGIO', 'epsg.io / MapTilerCoords', 'Force epsg.io as reprojection engine') )
		items.append( ('BUILTIN', 'Built in', 'Force reprojection through built in Python functions') )
		return items

	def updateProjEngine(self, context):
		settings.proj_engine = self.projEngine

	projEngine: EnumProperty(
		name = "Projection engine",
		description = "Select projection engine",
		items = getProjEngineItems,
		update = updateProjEngine
		)

	################
	#img engine

	def getImgEngineItems(self, context):
		items = [ ('AUTO', 'Auto detect', 'Auto select the best imaging library') ]
		if HAS_GDAL:
			items.append( ('GDAL', 'GDAL', 'Force GDAL as image processing engine') )
		if HAS_IMGIO:
			items.append( ('IMGIO', 'ImageIO', 'Force ImageIO as image processing  engine') )
		if HAS_PIL:
			items.append( ('PIL', 'PIL', 'Force PIL as image processing  engine') )
		return items

	def updateImgEngine(self, context):
		settings.img_engine = self.imgEngine

	imgEngine: EnumProperty(
		name = "Image processing engine",
		description = "Select image processing engine",
		items = getImgEngineItems,
		update = updateImgEngine
		)

	################
	#OSM

	osmTagsJson: StringProperty(default=json.dumps(DEFAULT_OSM_TAGS)) #just a serialized list of tags

	def listOsmTags(self, context):
		try:
			prefs = context.preferences.addons[PKG].preferences
			tags = json.loads(prefs.osmTagsJson)
			#put each item in a tuple (key, label, tooltip)
			return [ (tag, tag, tag) for tag in tags]
		except (json.JSONDecodeError, TypeError):
			return [('NONE', 'Error loading data', '')]

	osmTags: EnumProperty(
		name = "OSM tags",
		description = "List of registered OSM tags",
		items = listOsmTags
		)

	################
	#Basemaps

	cacheFolder: StringProperty(
		name = "Cache folder",
		default = APP_DATA,
		description = "Define a folder where to store Geopackage SQlite db",
		subtype = 'DIR_PATH'
		)

	cacheExpiry: IntProperty(
		name = "Cache expiry (days)",
		default = 90,
		min = 1,
		max = 365,
		description = "Tiles older than this are re-downloaded on next access"
		)

	synchOrj: BoolProperty(
		name="Synch. lat/long",
		description='Keep geo origin synchronized with crs origin. Can be slow with remote reprojection services',
		default=True)

	zoomToMouse: BoolProperty(name="Zoom to mouse", description='Zoom towards the mouse pointer position', default=True)

	lockOrigin: BoolProperty(name="Lock origin", description='Do not move scene origin when panning map', default=False)
	lockObj: BoolProperty(name="Lock objects", description='Retain objects geolocation when moving map origin', default=True)

	resamplAlg: EnumProperty(
		name = "Resampling method",
		description = "Choose GDAL's resampling method used for reprojection",
		items = [ ('NN', 'Nearest Neighbor', ''), ('BL', 'Bilinear', ''), ('CB', 'Cubic', ''), ('CBS', 'Cubic Spline', ''), ('LCZ', 'Lanczos', '') ]
		)

	################
	#Network

	def listOverpassServer(self, context):
		try:
			return [tuple(entry) for entry in json.loads(self.overpassServerJson)]
		except (json.JSONDecodeError, TypeError):
			return [('NONE', 'Error loading data', '')]

	#store crs preset as json string into addon preferences
	overpassServerJson: StringProperty(default=json.dumps(DEFAULT_OVERPASS_SERVER))

	overpassServer: EnumProperty(
		name = "Overpass server",
		description = "Select an overpass server",
		#default = 0,
		items = listOverpassServer
		)

	def listDemServer(self, context):
		try:
			return [tuple(entry) for entry in json.loads(self.demServerJson)]
		except (json.JSONDecodeError, TypeError):
			return [('NONE', 'Error loading data', '')]

	#store crs preset as json string into addon preferences
	demServerJson: StringProperty(default=json.dumps(DEFAULT_DEM_SERVER))

	demServer: EnumProperty(
		name = "Elevation server",
		description = "Select a server that provides Digital Elevation Model datasource",
		#default = 0,
		items = listDemServer
		)

	def updateOpentopoKey(self, context):
		_sync_credential('opentopography_api_key', self.opentopography_api_key)

	opentopography_api_key: StringProperty(
		name = "",
		description="you need to register and request a key from opentopography website",
		subtype = 'PASSWORD',
		update = updateOpentopoKey
	)

	def updateMapTilerApiKey(self, context):
		settings.maptiler_api_key = self.maptiler_api_key
		_sync_credential('maptiler_api_key', self.maptiler_api_key)

	maptiler_api_key: StringProperty(
		name = "",
		description = "MapTiler API key — used for both Coordinates (CRS search) and tiles. Free tier 100k loads/month at maptiler.com.",
		subtype = 'PASSWORD',
		update = updateMapTilerApiKey
	)

	def updateMapboxToken(self, context):
		settings.mapbox_token = self.mapbox_token
		_sync_credential('mapbox_token', self.mapbox_token)

	mapbox_token: StringProperty(
		name = "",
		description = "Access token for Mapbox (register free at mapbox.com)",
		subtype = 'PASSWORD',
		update = updateMapboxToken
	)

	def updateThunderforestApiKey(self, context):
		settings.thunderforest_api_key = self.thunderforest_api_key
		_sync_credential('thunderforest_api_key', self.thunderforest_api_key)

	thunderforest_api_key: StringProperty(
		name = "",
		description = "API key for Thunderforest (register free at thunderforest.com)",
		subtype = 'PASSWORD',
		update = updateThunderforestApiKey
	)

	def updateStadiaApiKey(self, context):
		settings.stadia_api_key = self.stadia_api_key
		_sync_credential('stadia_api_key', self.stadia_api_key)

	stadia_api_key: StringProperty(
		name = "",
		description = "API key for Stadia Maps (register free at stadiamaps.com)",
		subtype = 'PASSWORD',
		update = updateStadiaApiKey
	)

	def updateCdseClientId(self, context):
		_sync_credential('cdse_client_id', self.cdse_client_id)

	cdse_client_id: StringProperty(
		name = "",
		description = "Copernicus Data Space OAuth2 Client ID (register free at dataspace.copernicus.eu)",
		subtype = 'PASSWORD',
		update = updateCdseClientId
	)

	def updateCdseClientSecret(self, context):
		_sync_credential('cdse_client_secret', self.cdse_client_secret)

	cdse_client_secret: StringProperty(
		name = "",
		description = "Copernicus Data Space OAuth2 Client Secret",
		subtype = 'PASSWORD',
		update = updateCdseClientSecret
	)

	################
	#IO options
	mergeDoubles: BoolProperty(
		name = "Merge duplicate vertices",
		description = 'Merge shared vertices between features when importing vector data',
		default = False)
	adjust3Dview: BoolProperty(
		name = "Adjust 3D view",
		description = "Update 3d view grid size and clip distances according to the new imported object's size",
		default = True)
	forceTexturedSolid: BoolProperty(
		name = "Force textured solid shading",
		description = "Update shading mode to display raster's texture",
		default = True)

	################
	#System
	def updateLogLevel(self, context):
		logger = logging.getLogger(PKG)
		logger.setLevel(logging.getLevelName(self.logLevel))

	logLevel: EnumProperty(
		name = "Logging level",
		description = "Select the logging level",
		items = [('DEBUG', 'Debug', ''), ('INFO', 'Info', ''), ('WARNING', 'Warning', ''), ('ERROR', 'Error', ''), ('CRITICAL', 'Critical', '')],
		update = updateLogLevel,
		default = 'INFO'
		)

	show_advanced: BoolProperty(
		name = "Show advanced settings",
		description = "Reveal cache behavior, custom CRS list, custom servers, engine selection, OSM tags and log level",
		default = False
		)

	################
	# Provider registration URLs (used by the "Get free key" buttons)
	_REGISTER_URLS = {
		'mapbox_token': 'https://account.mapbox.com/auth/signup/',
		'maptiler_api_key': 'https://www.maptiler.com/cloud/account/keys/',
		'thunderforest_api_key': 'https://www.thunderforest.com/pricing/',
		'stadia_api_key': 'https://client.stadiamaps.com/signup/',
		'cdse_client_id': 'https://dataspace.copernicus.eu/',
		'opentopography_api_key': 'https://portal.opentopography.org/myopentopo',
	}

	def _draw_provider_row(self, layout, label, attr, register_url=None, secondary_attr=None):
		"""One key row with status icon, label, password field, and a register link."""
		row = layout.row(align=True)
		configured = bool(getattr(self, attr, ''))
		if secondary_attr is not None:
			configured = configured and bool(getattr(self, secondary_attr, ''))
		row.label(text='', icon='CHECKMARK' if configured else 'X')
		row.label(text=label)
		if secondary_attr is None:
			row.prop(self, attr, text='')
		else:
			sub = row.row(align=True)
			sub.prop(self, attr, text='')
			sub.prop(self, secondary_attr, text='')
		if register_url:
			op = row.operator("wm.url_open", icon='URL', text='')
			op.url = register_url

	def draw(self, context):
		layout = self.layout

		# ── Map Tile Providers ────────────────────────────────────────────────
		# This is the #1 reason users open the prefs panel: enter an API key to
		# unlock more basemaps. Keep it compact, status-driven, with a one-click
		# register button that opens the provider's signup page.
		box = layout.box()
		header = box.row()
		header.label(text='Map Tile Providers', icon='WORLD_DATA')
		header.label(text='18 providers free without a key — add a key to unlock more')
		self._draw_provider_row(box, 'Mapbox', 'mapbox_token',
			register_url=self._REGISTER_URLS['mapbox_token'])
		self._draw_provider_row(box, 'MapTiler', 'maptiler_api_key',
			register_url=self._REGISTER_URLS['maptiler_api_key'])
		self._draw_provider_row(box, 'Thunderforest', 'thunderforest_api_key',
			register_url=self._REGISTER_URLS['thunderforest_api_key'])
		self._draw_provider_row(box, 'Stadia Maps', 'stadia_api_key',
			register_url=self._REGISTER_URLS['stadia_api_key'])
		self._draw_provider_row(box, 'Copernicus CDSE', 'cdse_client_id',
			register_url=self._REGISTER_URLS['cdse_client_id'],
			secondary_attr='cdse_client_secret')
		self._draw_provider_row(box, 'OpenTopography (DEM)', 'opentopography_api_key',
			register_url=self._REGISTER_URLS['opentopography_api_key'])

		# ── Tile Cache ────────────────────────────────────────────────────────
		box = layout.box()
		box.label(text='Tile Cache', icon='FILE_CACHE')
		row = box.row()
		row.prop(self, "cacheFolder", text='')
		cache_dir = self.cacheFolder
		if cache_dir and os.path.isdir(cache_dir):
			gpkg_files = [f for f in os.listdir(cache_dir) if f.endswith('.gpkg')]
			total_bytes = sum(os.path.getsize(os.path.join(cache_dir, f)) for f in gpkg_files)
			if total_bytes > 1024 ** 3:
				size_str = "{:.1f} GB".format(total_bytes / (1024 ** 3))
			elif total_bytes > 1024 ** 2:
				size_str = "{:.0f} MB".format(total_bytes / (1024 ** 2))
			else:
				size_str = "{:.0f} KB".format(total_bytes / 1024)
			box.label(text="{} across {} cached source{}".format(
				size_str, len(gpkg_files), '' if len(gpkg_files) == 1 else 's'))
		row = box.row(align=True)
		row.operator("bgis.cache_clear_expired", icon='TRASH', text="Clear Expired")
		row.operator("bgis.cache_clear_all", icon='CANCEL', text="Clear All")
		row.prop(self, "cacheExpiry", text="Expire after (days)")

		# ── Advanced (collapsed by default) ───────────────────────────────────
		box = layout.box()
		row = box.row()
		row.prop(self, "show_advanced",
			icon='TRIA_DOWN' if self.show_advanced else 'TRIA_RIGHT',
			emboss=False, text='Advanced')
		if not self.show_advanced:
			return

		# Spatial Reference Systems
		sub = box.box()
		sub.label(text='Spatial Reference Systems')
		row = sub.row().split(factor=0.5)
		row.prop(self, "predefCrs", text='')
		row.operator("bgis.add_predef_crs", icon='ADD')
		row.operator("bgis.edit_predef_crs", icon='PREFERENCES')
		row.operator("bgis.rmv_predef_crs", icon='REMOVE')
		row.operator("bgis.reset_predef_crs", icon='PLAY_REVERSE')

		# Basemap behaviour
		sub = box.box()
		sub.label(text='Basemap behaviour')
		row = sub.row()
		row.prop(self, "zoomToMouse")
		row.prop(self, "lockObj")
		row.prop(self, "lockOrigin")
		row.prop(self, "synchOrj")
		sub.prop(self, "resamplAlg")

		# Custom remote datasources
		sub = box.box()
		sub.label(text='Custom remote datasources')
		row = sub.row().split(factor=0.5)
		row.prop(self, "overpassServer")
		row.operator("bgis.add_overpass_server", icon='ADD')
		row.operator("bgis.edit_overpass_server", icon='PREFERENCES')
		row.operator("bgis.rmv_overpass_server", icon='REMOVE')
		row.operator("bgis.reset_overpass_server", icon='PLAY_REVERSE')
		row = sub.row().split(factor=0.5)
		row.prop(self, "demServer")
		row.operator("bgis.add_dem_server", icon='ADD')
		row.operator("bgis.edit_dem_server", icon='PREFERENCES')
		row.operator("bgis.rmv_dem_server", icon='REMOVE')
		row.operator("bgis.reset_dem_server", icon='PLAY_REVERSE')

		# OSM tag list + Import/Export options
		sub = box.box()
		sub.label(text='Import / Export')
		row = sub.row().split(factor=0.5)
		split = row.split(factor=0.9, align=True)
		split.prop(self, "osmTags")
		split.operator("wm.url_open", icon='INFO').url = "http://wiki.openstreetmap.org/wiki/Map_Features"
		row.operator("bgis.add_osm_tag", icon='ADD')
		row.operator("bgis.edit_osm_tag", icon='PREFERENCES')
		row.operator("bgis.rmv_osm_tag", icon='REMOVE')
		row.operator("bgis.reset_osm_tags", icon='PLAY_REVERSE')
		row = sub.row()
		row.prop(self, "mergeDoubles")
		row.prop(self, "adjust3Dview")
		row.prop(self, "forceTexturedSolid")

		# Engines and log level
		sub = box.box()
		sub.label(text='System')
		sub.prop(self, "projEngine")
		sub.prop(self, "imgEngine")
		sub.prop(self, "logLevel")

#######################

class PredefCRS():

	'''
	Collection of utility methods (callable at class level) to deal with predefined CRS dictionary
	Can be used by others operators that need to fill their own crs enum
	'''

	@staticmethod
	def getData():
		'''Load the json string'''
		prefs = bpy.context.preferences.addons[PKG].preferences
		return json.loads(prefs.predefCrsJson)

	@classmethod
	def getName(cls, key):
		'''Return the convenient name of a given srid or None if this crs does not exist in the list'''
		data = cls.getData()
		try:
			return [entry[1] for entry in data if entry[0] == key][0]
		except IndexError:
			return None

	@classmethod
	def getEnumItems(cls):
		'''Return a list of predefined crs usable to fill a bpy EnumProperty'''
		return [tuple(entry) for entry in cls.getData()]


#################
# Collection of operators to manage predefined CRS

class BGIS_OT_add_predef_crs(Operator):
	bl_idname = "bgis.add_predef_crs"
	bl_description = 'Add predefined CRS'
	bl_label = "Add"
	bl_options = {'INTERNAL'}

	crs: StringProperty(name = "Definition",  description = "Specify EPSG code or Proj4 string definition for this CRS")
	name: StringProperty(name = "Description", description = "Choose a convenient name for this CRS")
	desc: StringProperty(name = "Description", description = "Add a description or comment about this CRS")

	def check(self, context):
		return True

	def _do_search(self, context):

		apiKey = settings.maptiler_api_key

		if not apiKey:
			#self.report({'ERROR'}, "MapTiler API key is required. Please set it in the preferences.") #report is not available outside of the execute function
			log.error("No Maptiler API key")
			return

		mtc = MapTilerCoordinates(apiKey=apiKey)
		results = mtc.search(self.query)
		self.results = json.dumps(results)
		if results:
			self.crs = 'EPSG:' + str(results[0]['id']['code'])
			self.name = results[0]['name']

	def updEnum(self, context):
		crsItems = []
		if self.results != '':
			for result in json.loads(self.results):
				srid = 'EPSG:' + str(result['id']['code'])
				crsItems.append( (str(result['id']['code']), result['name'], srid) )
		return crsItems

	def fill(self, context):
		if self.results != '':
			crs = [crs for crs in json.loads(self.results) if str(crs['id']['code']) == self.crsEnum][0]
			self.crs = 'EPSG:' + str(crs['id']['code'])
			self.desc = crs['name']

	query: StringProperty(name='Query', description='Hit enter to process the search', update=_do_search)

	results: StringProperty()

	crsEnum: EnumProperty(name='Results', description='Select the desired CRS', items=updEnum, update=fill)

	search: BoolProperty(name='Search', description='Search for coordinate system into EPSG database', default=False)

	save: BoolProperty(name='Save to addon preferences',  description='Save Blender user settings after the addition', default=False)

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)#, width=300)

	def draw(self, context):
		layout = self.layout
		layout.prop(self, 'search')
		if self.search:
			prefs = context.preferences.addons[PKG].preferences
			if not prefs.maptiler_api_key:
				layout.label(text="Searching require a MapTiler API key", icon_value=3)
				layout.prop(prefs, "maptiler_api_key", text='API Key')
			else:
				layout.prop(self, 'query')
				layout.prop(self, 'crsEnum')
			layout.separator()
		layout.prop(self, 'crs')
		layout.prop(self, 'name')
		layout.prop(self, 'desc')
		#layout.prop(self, 'save') #sincce Blender2.8 prefs are autosaved

	def execute(self, context):
		if self.crs.isdigit():
			self.crs = 'EPSG:' + self.crs
		if not SRS.validate(self.crs):
			self.report({'ERROR'}, 'Invalid CRS')
			return {'CANCELLED'}
		#append the new crs def to json string
		prefs = context.preferences.addons[PKG].preferences
		data = json.loads(prefs.predefCrsJson)
		data.append((self.crs, self.name, self.desc))
		prefs.predefCrsJson = json.dumps(data)
		#change enum index to new added crs and redraw
		#prefs.predefCrs = self.crs
		if context.area:
			context.area.tag_redraw()
		#end
		if self.save:
			bpy.ops.wm.save_userpref()
		return {'FINISHED'}

class BGIS_OT_rmv_predef_crs(Operator):

	bl_idname = "bgis.rmv_predef_crs"
	bl_description = 'Remove predefined CRS'
	bl_label = "Remove"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.predefCrs
		if key != '':
			data = json.loads(prefs.predefCrsJson)
			data = [e for e in data if e[0] != key]
			prefs.predefCrsJson = json.dumps(data)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_reset_predef_crs(Operator):

	bl_idname = "bgis.reset_predef_crs"
	bl_description = 'Reset predefined CRS'
	bl_label = "Reset"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		prefs.predefCrsJson = json.dumps(DEFAULT_CRS)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_edit_predef_crs(Operator):

	bl_idname = "bgis.edit_predef_crs"
	bl_description = 'Edit predefined CRS'
	bl_label = "Edit"
	bl_options = {'INTERNAL'}

	crs: StringProperty(name = "EPSG code or Proj4 string",  description = "Specify EPSG code or Proj4 string definition for this CRS")
	name: StringProperty(name = "Description", description = "Choose a convenient name for this CRS")
	desc: StringProperty(name = "Name", description = "Add a description or comment about this CRS")

	def invoke(self, context, event):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.predefCrs
		if key == '':
			return {'CANCELLED'}
		data = json.loads(prefs.predefCrsJson)
		matches = [entry for entry in data if entry[0] == key]
		if not matches:
			self.report({'ERROR'}, 'Entry not found')
			return {'CANCELLED'}
		entry = matches[0]
		self.crs, self.name, self.desc = entry
		return context.window_manager.invoke_props_dialog(self)

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.predefCrs
		data = json.loads(prefs.predefCrsJson)

		if SRS.validate(self.crs):
			data = [entry for entry in data if entry[0] != key] #deleting
			data.append((self.crs, self.name, self.desc))
			prefs.predefCrsJson = json.dumps(data)
			if context.area:
				context.area.tag_redraw()
		else:
			self.report({'ERROR'}, 'Invalid CRS')

		return {'FINISHED'}


#################
# Collection of operators to manage predefineds OSM Tags

class BGIS_OT_add_osm_tag(Operator):
	bl_idname = "bgis.add_osm_tag"
	bl_description = 'Add new predefined OSM filter tag'
	bl_label = "Add"
	bl_options = {'INTERNAL'}

	tag: StringProperty(name = "Tag",  description = "Specify the tag (examples : 'building', 'landuse=forest' ...)")

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)#, width=300)

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		tags = json.loads(prefs.osmTagsJson)
		tags.append(self.tag)
		prefs.osmTagsJson = json.dumps(tags)
		prefs.osmTags = self.tag #update current idx
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_rmv_osm_tag(Operator):

	bl_idname = "bgis.rmv_osm_tag"
	bl_description = 'Remove predefined OSM filter tag'
	bl_label = "Remove"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		tag = prefs.osmTags
		if tag != '':
			tags = json.loads(prefs.osmTagsJson)
			del tags[tags.index(tag)]
			prefs.osmTagsJson = json.dumps(tags)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_reset_osm_tags(Operator):

	bl_idname = "bgis.reset_osm_tags"
	bl_description = 'Reset predefined OSM filter tag'
	bl_label = "Reset"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		prefs.osmTagsJson = json.dumps(DEFAULT_OSM_TAGS)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_edit_osm_tag(Operator):

	bl_idname = "bgis.edit_osm_tag"
	bl_description = 'Edit predefined OSM filter tag'
	bl_label = "Edit"
	bl_options = {'INTERNAL'}

	tag: StringProperty(name = "Tag",  description = "Specify the tag (examples : 'building', 'landuse=forest' ...)")

	def invoke(self, context, event):
		prefs = context.preferences.addons[PKG].preferences
		self.tag = prefs.osmTags
		if self.tag == '':
			return {'CANCELLED'}
		return context.window_manager.invoke_props_dialog(self)

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		tag = prefs.osmTags
		tags = json.loads(prefs.osmTagsJson)
		del tags[tags.index(tag)]
		tags.append(self.tag)
		prefs.osmTagsJson = json.dumps(tags)
		prefs.osmTags = self.tag #update current idx
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

#################
# Collection of operators to manage DEM server urls

class BGIS_OT_add_dem_server(Operator):
	bl_idname = "bgis.add_dem_server"
	bl_description = 'Add new topography web service'
	bl_label = "Add"
	bl_options = {'INTERNAL'}

	url: StringProperty(name = "Url template",  description = "Define url template string. Bounding box variables are {W}, {E}, {S} and {N}")
	name: StringProperty(name = "Description", description = "Choose a convenient name for this server")
	desc: StringProperty(name = "Description", description = "Add a description or comment about this remote datasource")

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)#, width=300)

	def execute(self, context):
		templates = ['{W}', '{E}', '{S}', '{N}']
		if all([t in self.url for t in templates]):
			prefs = context.preferences.addons[PKG].preferences
			data = json.loads(prefs.demServerJson)
			data.append( (self.url, self.name, self.desc) )
			prefs.demServerJson = json.dumps(data)
			if context.area:
				context.area.tag_redraw()
		else:
			self.report({'ERROR'}, 'Invalid URL')
		return {'FINISHED'}

class BGIS_OT_rmv_dem_server(Operator):

	bl_idname = "bgis.rmv_dem_server"
	bl_description = 'Remove a given topography web service'
	bl_label = "Remove"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.demServer
		if key != '':
			data = json.loads(prefs.demServerJson)
			data = [e for e in data if e[0] != key]
			prefs.demServerJson = json.dumps(data)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_reset_dem_server(Operator):

	bl_idname = "bgis.reset_dem_server"
	bl_description = 'Reset default topographic web server'
	bl_label = "Reset"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		prefs.demServerJson = json.dumps(DEFAULT_DEM_SERVER)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_edit_dem_server(Operator):

	bl_idname = "bgis.edit_dem_server"
	bl_description = 'Edit a topographic web server'
	bl_label = "Edit"
	bl_options = {'INTERNAL'}

	url: StringProperty(name = "Url template",  description = "Define url template string. Bounding box variables are {W}, {E}, {S} and {N}")
	name: StringProperty(name = "Description", description = "Choose a convenient name for this server")
	desc: StringProperty(name = "Description", description = "Add a description or comment about this remote datasource")

	def invoke(self, context, event):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.demServer
		if key == '':
			return {'CANCELLED'}
		data = json.loads(prefs.demServerJson)
		matches = [entry for entry in data if entry[0] == key]
		if not matches:
			self.report({'ERROR'}, 'Entry not found')
			return {'CANCELLED'}
		entry = matches[0]
		self.url, self.name, self.desc = entry
		return context.window_manager.invoke_props_dialog(self)

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.demServer
		data = json.loads(prefs.demServerJson)
		templates = ['{W}', '{E}', '{S}', '{N}']
		if all([t in self.url for t in templates]):
			data = [entry for entry in data if entry[0] != key] #deleting
			data.append((self.url, self.name, self.desc))
			prefs.demServerJson = json.dumps(data)
			if context.area:
				context.area.tag_redraw()
		else:
			self.report({'ERROR'}, 'Invalid URL')
		return {'FINISHED'}

#################

class EditEnum():
	'''
	Helper to deal with an enum property that use a serialized json backend
	Can be used by others operators to edit and EnumProperty
	WORK IN PROGRESS
	'''

	def __init__(self, enumName):
		self.prefs = bpy.context.preferences.addons[PKG].preferences
		self.enumName = enumName
		self.jsonName = enumName + 'Json'

	def getData(self):
		'''Load the json string'''
		data = json.loads(getattr(self.prefs, self.jsonName))
		return [tuple(entry) for entry in data]

	def append(self, value, label, tooltip, check=lambda x: True):
		if not check(value):
			return
		data = self.getData()
		data.append((value, label, tooltip))
		setattr(self.prefs, self.jsonName, json.dumps(data))

	def remove(self, key):
		if key != '':
			data = self.getData()
			data = [e for e in data if e[0] != key]
			setattr(self.prefs, self.jsonName, json.dumps(data))

	def edit(self, key, value, label, tooltip):
		self.remove(key)
		self.append(value, label, tooltip)

	def reset(self):
		setattr(self.prefs, self.jsonName, json.dumps(DEFAULT_OVERPASS_SERVER))

#################
# Collection of operators to manage Overpass server urls

class BGIS_OT_add_overpass_server(Operator):
	bl_idname = "bgis.add_overpass_server"
	bl_description = 'Add new OSM overpass server url'
	bl_label = "Add"
	bl_options = {'INTERNAL'}

	url: StringProperty(name = "Url template",  description = "Define the url end point of the overpass server")
	name: StringProperty(name = "Description", description = "Choose a convenient name for this server")
	desc: StringProperty(name = "Description", description = "Add a description or comment about this remote server")

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)#, width=300)

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		data = json.loads(prefs.overpassServerJson)
		data.append( (self.url, self.name, self.desc) )
		prefs.overpassServerJson = json.dumps(data)
		#EditEnum('overpassServer').append(self.url, self.name, self.desc, check=lambda url: url.startswith('http'))
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_rmv_overpass_server(Operator):

	bl_idname = "bgis.rmv_overpass_server"
	bl_description = 'Remove a given overpass server'
	bl_label = "Remove"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.overpassServer
		if key != '':
			data = json.loads(prefs.overpassServerJson)
			data = [e for e in data if e[0] != key]
			prefs.overpassServerJson = json.dumps(data)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_reset_overpass_server(Operator):

	bl_idname = "bgis.reset_overpass_server"
	bl_description = 'Reset default overpass server'
	bl_label = "Reset"
	bl_options = {'INTERNAL'}

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		prefs.overpassServerJson = json.dumps(DEFAULT_OVERPASS_SERVER)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}

class BGIS_OT_edit_overpass_server(Operator):

	bl_idname = "bgis.edit_overpass_server"
	bl_description = 'Edit an overpass server url'
	bl_label = "Edit"
	bl_options = {'INTERNAL'}

	url: StringProperty(name = "Url template",  description = "Define the url end point of the overpass server")
	name: StringProperty(name = "Description", description = "Choose a convenient name for this server")
	desc: StringProperty(name = "Description", description = "Add a description or comment about this remote server")

	def invoke(self, context, event):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.overpassServer
		if key == '':
			return {'CANCELLED'}
		data = json.loads(prefs.overpassServerJson)
		matches = [entry for entry in data if entry[0] == key]
		if not matches:
			self.report({'ERROR'}, 'Entry not found')
			return {'CANCELLED'}
		entry = matches[0]
		self.url, self.name, self.desc = entry
		return context.window_manager.invoke_props_dialog(self)

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		key = prefs.overpassServer
		data = json.loads(prefs.overpassServerJson)
		data = [entry for entry in data if entry[0] != key] #deleting
		data.append((self.url, self.name, self.desc))
		prefs.overpassServerJson = json.dumps(data)
		if context.area:
			context.area.tag_redraw()
		return {'FINISHED'}


class BGIS_OT_cache_clear_all(Operator):
	bl_idname = "bgis.cache_clear_all"
	bl_description = 'Delete all cached tiles'
	bl_label = "Clear All Cache"

	def execute(self, context):
		prefs = context.preferences.addons[PKG].preferences
		cache_dir = prefs.cacheFolder
		if not cache_dir or not os.path.isdir(cache_dir):
			self.report({'WARNING'}, "Cache folder not found")
			return {'CANCELLED'}
		count = 0
		for f in os.listdir(cache_dir):
			if f.endswith('.gpkg'):
				try:
					os.remove(os.path.join(cache_dir, f))
					count += 1
				except OSError as e:
					log.warning("Cannot remove %s: %s", f, e)
		self.report({'INFO'}, "Removed {} cache files".format(count))
		return {'FINISHED'}

	def invoke(self, context, event):
		return context.window_manager.invoke_confirm(self, event)


class BGIS_OT_cache_clear_expired(Operator):
	bl_idname = "bgis.cache_clear_expired"
	bl_description = 'Remove expired tiles from cache and reclaim disk space'
	bl_label = "Clear Expired Tiles"

	def execute(self, context):
		import sqlite3
		prefs = context.preferences.addons[PKG].preferences
		cache_dir = prefs.cacheFolder
		expiry = prefs.cacheExpiry
		if not cache_dir or not os.path.isdir(cache_dir):
			self.report({'WARNING'}, "Cache folder not found")
			return {'CANCELLED'}
		total_removed = 0
		for f in os.listdir(cache_dir):
			if not f.endswith('.gpkg'):
				continue
			db_path = os.path.join(cache_dir, f)
			try:
				db = sqlite3.connect(db_path)
				cursor = db.execute(
					"DELETE FROM gpkg_tiles WHERE julianday('now','localtime') - julianday(last_modified) > ?",
					(expiry,))
				removed = cursor.rowcount
				if removed > 0:
					db.commit()
					db.execute("VACUUM")
					total_removed += removed
				db.close()
			except Exception as e:
				log.warning("Cannot clean %s: %s", f, e)
		self.report({'INFO'}, "Removed {} expired tiles".format(total_removed))
		return {'FINISHED'}


classes = [
BGIS_OT_pref_show,
BGIS_PREFS,
BGIS_OT_add_predef_crs,
BGIS_OT_rmv_predef_crs,
BGIS_OT_reset_predef_crs,
BGIS_OT_edit_predef_crs,
BGIS_OT_add_osm_tag,
BGIS_OT_rmv_osm_tag,
BGIS_OT_reset_osm_tags,
BGIS_OT_edit_osm_tag,
BGIS_OT_add_dem_server,
BGIS_OT_rmv_dem_server,
BGIS_OT_reset_dem_server,
BGIS_OT_edit_dem_server,
BGIS_OT_add_overpass_server,
BGIS_OT_rmv_overpass_server,
BGIS_OT_reset_overpass_server,
BGIS_OT_edit_overpass_server,
BGIS_OT_cache_clear_all,
BGIS_OT_cache_clear_expired,
]

def register():
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError as e:
			#log.error('Cannot register {}'.format(cls), exc_info=True)
			log.warning('{} is already registered, now unregister and retry... '.format(cls))
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)

	# Set default cache folder and restore persisted credentials.
	# The addon entry may not yet exist in preferences.addons when register() is
	# invoked via low-level paths (e.g. addon_utils.enable() right after a
	# read_factory_settings, or during certain reload sequences). Skip silently
	# in that case — the UI will populate the entry on first access.
	try:
		prefs = bpy.context.preferences.addons[PKG].preferences
	except KeyError:
		log.debug('Addon entry %s not yet in preferences.addons; deferring prefs init', PKG)
		return
	if prefs.cacheFolder == '':
		prefs.cacheFolder = APP_DATA
	restore_credentials(prefs)


def unregister():
	for cls in classes:
		bpy.utils.unregister_class(cls)
