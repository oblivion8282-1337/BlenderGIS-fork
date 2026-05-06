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
import logging
log = logging.getLogger(__name__)

import os
import io
import math
import datetime
import sqlite3
import threading


#http://www.geopackage.org/spec/#tiles
#https://github.com/GitHubRGI/geopackage-python/blob/master/Packaging/tiles2gpkg_parallel.py
#https://github.com/Esri/raster2gpkg/blob/master/raster2gpkg.py


#table_name refer to the name of the table witch contains tiles data
#here for simplification, table_name will always be named "gpkg_tiles"

class GeoPackage():

	MAX_DAYS = 90  # default, overridden by addon preferences if available

	@staticmethod
	def _read_max_days_from_prefs():
		"""Read cache expiry from addon prefs. MUST be called from the main thread."""
		try:
			import bpy
			pkg = __package__.split('.')[0]
			prefs = bpy.context.preferences.addons[pkg].preferences
			return prefs.cacheExpiry
		except Exception:
			log.debug('cacheExpiry pref unavailable, using default', exc_info=True)
			return GeoPackage.MAX_DAYS

	def _get_max_days(self):
		# Cached snapshot taken at __init__ time (main thread). Worker threads
		# must never touch bpy.context, so we read this attribute instead.
		return self._max_days

	def __init__(self, path, tm, max_days=None):
		self.dbPath = path
		self.name = os.path.splitext(os.path.basename(path))[0]
		# Snapshot the expiry policy now while we are still on the main thread.
		# Workers later call self._get_max_days() against this cached value.
		self._max_days = max_days if max_days is not None else GeoPackage._read_max_days_from_prefs()

		# Thread-local storage for per-thread SQLite connections
		self._local = threading.local()
		self._all_connections = []
		self._conn_lock = threading.Lock()

		#Get props from TileMatrix object
		self.auth, self.code = tm.CRS.split(':')
		self.code = int(self.code)
		self.tileSize = tm.tileSize
		self.xmin, self.ymin, self.xmax, self.ymax = tm.globalbbox
		self.resolutions = tm.getResList()

		if not self.isGPKG():
			self.create()
			self.insertMetadata()

			self.insertCRS(self.code, str(self.code), self.auth)
			#self.insertCRS(3857, "Web Mercator")
			#self.insertCRS(4326, "WGS84")

			self.insertTileMatrixSet()
		else:
			# Migration: pre-existing caches lack the (zoom_level, last_modified)
			# index used by listExistingTiles. Add it lazily.
			try:
				db = self._get_connection()
				db.execute("""
					CREATE INDEX IF NOT EXISTS idx_tiles_z_modified
					ON gpkg_tiles (zoom_level, last_modified);
				""")
				db.commit()
			except Exception:
				log.debug('Could not ensure last_modified index on existing GPKG', exc_info=True)


	def _get_connection(self, detect_types=0):
		"""Return a cached per-thread SQLite connection, reconnecting if closed."""
		attr = '_conn_dt' if detect_types else '_conn'
		conn = getattr(self._local, attr, None)
		if conn is not None:
			try:
				conn.execute("SELECT 1")
			except Exception:
				log.debug('Stale GPKG connection, reconnecting', exc_info=True)
				conn = None
				setattr(self._local, attr, None)
		if conn is None:
			conn = sqlite3.connect(self.dbPath, detect_types=detect_types)
			setattr(self._local, attr, conn)
			with self._conn_lock:
				self._all_connections.append(conn)
		return conn

	def close(self):
		"""Close all cached connections on the current thread."""
		for attr in ('_conn', '_conn_dt'):
			conn = getattr(self._local, attr, None)
			if conn is not None:
				try:
					conn.close()
				except Exception:
					log.debug('Error closing GPKG connection', exc_info=True)
				setattr(self._local, attr, None)

	def close_all(self):
		"""Close all tracked connections across all threads."""
		with self._conn_lock:
			for conn in self._all_connections:
				try:
					conn.close()
				except Exception:
					log.debug('Error closing GPKG connection', exc_info=True)
			self._all_connections.clear()


	def isGPKG(self):
		if not os.path.exists(self.dbPath):
			return False
		db = self._get_connection()

		#check application id
		app_id = db.execute("PRAGMA application_id").fetchone()
		if not app_id[0] == 1196437808:
			return False
		#quick check of table schema
		try:
			db.execute('SELECT table_name FROM gpkg_contents LIMIT 1')
			db.execute('SELECT srs_name FROM gpkg_spatial_ref_sys LIMIT 1')
			db.execute('SELECT table_name FROM gpkg_tile_matrix_set LIMIT 1')
			db.execute('SELECT table_name FROM gpkg_tile_matrix LIMIT 1')
			db.execute('SELECT zoom_level, tile_column, tile_row, tile_data FROM gpkg_tiles LIMIT 1')
		except Exception as e:
			log.error('Incorrect GPKG schema', exc_info=True)
			return False
		else:
			return True


	def create(self):
		"""Create default geopackage schema on the database."""
		db = self._get_connection()
		cursor = db.cursor()

		# Add GeoPackage version 1.0 ("GP10" in ASCII) to the Sqlite header
		cursor.execute("PRAGMA application_id = 1196437808;")

		cursor.execute("""
			CREATE TABLE gpkg_contents (
				table_name TEXT NOT NULL PRIMARY KEY,
				data_type TEXT NOT NULL,
				identifier TEXT UNIQUE,
				description TEXT DEFAULT '',
				last_change DATETIME NOT NULL DEFAULT
				(strftime('%Y-%m-%dT%H:%M:%fZ','now')),
				min_x DOUBLE,
				min_y DOUBLE,
				max_x DOUBLE,
				max_y DOUBLE,
				srs_id INTEGER,
				CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id)
					REFERENCES gpkg_spatial_ref_sys(srs_id));
		""")

		cursor.execute("""
			CREATE TABLE gpkg_spatial_ref_sys (
				srs_name TEXT NOT NULL,
				srs_id INTEGER NOT NULL PRIMARY KEY,
				organization TEXT NOT NULL,
				organization_coordsys_id INTEGER NOT NULL,
				definition TEXT NOT NULL,
				description TEXT);
		""")

		cursor.execute("""
			CREATE TABLE gpkg_tile_matrix_set (
				table_name TEXT NOT NULL PRIMARY KEY,
				srs_id INTEGER NOT NULL,
				min_x DOUBLE NOT NULL,
				min_y DOUBLE NOT NULL,
				max_x DOUBLE NOT NULL,
				max_y DOUBLE NOT NULL,
				CONSTRAINT fk_gtms_table_name FOREIGN KEY (table_name)
					REFERENCES gpkg_contents(table_name),
				CONSTRAINT fk_gtms_srs FOREIGN KEY (srs_id)
					REFERENCES gpkg_spatial_ref_sys(srs_id));
		""")

		cursor.execute("""
			CREATE TABLE gpkg_tile_matrix (
				table_name TEXT NOT NULL,
				zoom_level INTEGER NOT NULL,
				matrix_width INTEGER NOT NULL,
				matrix_height INTEGER NOT NULL,
				tile_width INTEGER NOT NULL,
				tile_height INTEGER NOT NULL,
				pixel_x_size DOUBLE NOT NULL,
				pixel_y_size DOUBLE NOT NULL,
				CONSTRAINT pk_ttm PRIMARY KEY (table_name, zoom_level),
				CONSTRAINT fk_ttm_table_name FOREIGN KEY (table_name)
					REFERENCES gpkg_contents(table_name));
		""")

		cursor.execute("""
			CREATE TABLE gpkg_tiles (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				zoom_level INTEGER NOT NULL,
				tile_column INTEGER NOT NULL,
				tile_row INTEGER NOT NULL,
				tile_data BLOB NOT NULL,
				last_modified TIMESTAMP DEFAULT (datetime('now')),
				UNIQUE (zoom_level, tile_column, tile_row));
		""")

		cursor.execute("""
			CREATE INDEX IF NOT EXISTS idx_tiles_zxy
			ON gpkg_tiles (zoom_level, tile_column, tile_row);
		""")

		# Speeds up cache-expiry scans (julianday(last_modified) filter in
		# listExistingTiles / getTiles) on large caches.
		cursor.execute("""
			CREATE INDEX IF NOT EXISTS idx_tiles_z_modified
			ON gpkg_tiles (zoom_level, last_modified);
		""")

		db.commit()


	def insertMetadata(self):
		db = self._get_connection()
		query = """INSERT INTO gpkg_contents (
					table_name, data_type,
					identifier, description,
					min_x, min_y, max_x, max_y,
					srs_id)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);"""
		db.execute(query, ("gpkg_tiles", "tiles", self.name, "Created with CartoBlend", self.xmin, self.ymin, self.xmax, self.ymax, self.code))
		db.commit()


	def insertCRS(self, code, name, auth='EPSG', wkt=''):
		db = self._get_connection()
		db.execute(""" INSERT INTO gpkg_spatial_ref_sys (
					srs_id,
					organization,
					organization_coordsys_id,
					srs_name,
					definition)
				VALUES (?, ?, ?, ?, ?)
			""", (code, auth, code, name, wkt))
		db.commit()


	def insertTileMatrixSet(self):
		db = self._get_connection()

		#Tile matrix set
		query = """INSERT OR REPLACE INTO gpkg_tile_matrix_set (
					table_name, srs_id,
					min_x, min_y, max_x, max_y)
				VALUES (?, ?, ?, ?, ?, ?);"""
		db.execute(query, ('gpkg_tiles', self.code, self.xmin, self.ymin, self.xmax, self.ymax))


		#Tile matrix of each levels
		for level, res in enumerate(self.resolutions):

			w = math.ceil( (self.xmax - self.xmin) / (self.tileSize * res) )
			h = math.ceil( (self.ymax - self.ymin) / (self.tileSize * res) )

			query = """INSERT OR REPLACE INTO gpkg_tile_matrix (
						table_name, zoom_level,
						matrix_width, matrix_height,
						tile_width, tile_height,
						pixel_x_size, pixel_y_size)
					VALUES (?, ?, ?, ?, ?, ?, ?, ?);"""
			db.execute(query, ('gpkg_tiles', level, w, h, self.tileSize, self.tileSize, res, res))


		db.commit()


	def hasTile(self, x, y, z):
		if self.getTile(x ,y, z) is not None:
			return True
		else:
			return False

	def getTile(self, x, y, z):
		'''return tilde_data if tile exists otherwie return None'''
		db = self._get_connection(detect_types=sqlite3.PARSE_DECLTYPES)
		query = 'SELECT tile_data, last_modified FROM gpkg_tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?'
		result = db.execute(query, (z, x, y)).fetchone()
		if result is None:
			return None
		try:
			# DB stores UTC ('datetime("now")'), so compare against utcnow() to
			# avoid timezone drift in the cache-expiry calculation.
			timeDelta = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - result[1]
			if timeDelta.days > self._get_max_days():
				return None
		except (TypeError, AttributeError):
			pass
		return result[0]

	def putTile(self, x, y, z, data):
		db = self._get_connection()
		query = """INSERT OR REPLACE INTO gpkg_tiles
		(tile_column, tile_row, zoom_level, tile_data) VALUES (?,?,?,?)"""
		db.execute(query, (x, y, z, data))
		db.commit()


	def listExistingTiles(self, tiles):
		"""
		input : tiles list [(x,y,z)]
		output : tiles list set [(x,y,z)] of existing records in cache db"""
		if not tiles:
			return set()

		db = self._get_connection(detect_types=sqlite3.PARSE_DECLTYPES)

		# split out the axises
		x, y, z = zip(*tiles)

		query = "SELECT tile_column, tile_row, zoom_level FROM gpkg_tiles " \
				"WHERE julianday() - julianday(last_modified) < ?" \
				"AND zoom_level BETWEEN ? AND ? AND tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?"

		result = db.execute(
			query,
			(
				self._get_max_days(),
				min(z), max(z),
				min(x), max(x),
				min(y), max(y)
			)
		).fetchall()

		return set(result)

	def listMissingTiles(self, tiles):
		existing = self.listExistingTiles(tiles)
		return set(tiles) - existing # difference


	def getTiles(self, tiles):
		"""tiles = list of (x,y,z) tuple
		return list of (x,y,z,data) tuple"""
		if not tiles:
			return []

		db = self._get_connection(detect_types=sqlite3.PARSE_DECLTYPES)

		# split out the axises
		x, y, z = zip(*tiles)

		query = "SELECT tile_column, tile_row, zoom_level, tile_data FROM gpkg_tiles " \
				"WHERE julianday() - julianday(last_modified) < ?" \
				"AND zoom_level BETWEEN ? AND ? AND tile_column BETWEEN ? AND ? AND tile_row BETWEEN ? AND ?"

		rows = db.execute(
			query,
			(
				self._get_max_days(),
				min(z), max(z),
				min(x), max(x),
				min(y), max(y)
			)
		).fetchall()

		found = {(r[0], r[1], r[2]): r[3] for r in rows}
		return [(x, y, z, found.get((x, y, z))) for x, y, z in tiles]


	def putTiles(self, tiles):
		"""tiles = list of (x,y,z,data) tuple"""
		db = self._get_connection()
		query = """INSERT OR REPLACE INTO gpkg_tiles
		(tile_column, tile_row, zoom_level, tile_data) VALUES (?,?,?,?)"""
		db.executemany(query, tiles)
		db.commit()

	def deleteTiles(self, tiles):
		"""Delete specific tiles from cache. tiles = list of (col, row, zoom)"""
		db = self._get_connection()
		query = "DELETE FROM gpkg_tiles WHERE tile_column=? AND tile_row=? AND zoom_level=?"
		db.executemany(query, tiles)
		db.commit()
