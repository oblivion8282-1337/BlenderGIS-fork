# -*- coding:utf-8 -*-
import os
import json
import logging

from .checkdeps import HAS_GDAL, HAS_PYPROJ, HAS_IMGIO, HAS_PIL

log = logging.getLogger(__name__)

def getAvailableProjEngines():
	engines = ['AUTO', 'BUILTIN']
	#if EPSGIO.ping():
	engines.append('EPSGIO')
	if HAS_GDAL:
		engines.append('GDAL')
	if HAS_PYPROJ:
		engines.append('PYPROJ')
	return engines

def getAvailableImgEngines():
	engines = ['AUTO']
	if HAS_GDAL:
		engines.append('GDAL')
	if HAS_IMGIO:
		engines.append('IMGIO')
	if HAS_PIL:
		engines.append('PIL')
	return engines


class Settings():

	def __init__(self, **kwargs):
		self._proj_engine = kwargs['proj_engine']
		self._img_engine = kwargs['img_engine']
		self.user_agent = kwargs['user_agent']
		if 'maptiler_api_key' in kwargs:
			self.maptiler_api_key = kwargs['maptiler_api_key']
		else:
			self.maptiler_api_key = None
		self.stadia_api_key = kwargs.get('stadia_api_key')
		self.mapbox_token = kwargs.get('mapbox_token')
		self.thunderforest_api_key = kwargs.get('thunderforest_api_key')

	@property
	def proj_engine(self):
		return self._proj_engine

	@proj_engine.setter
	def proj_engine(self, engine):
		if engine not in getAvailableProjEngines():
			raise ValueError('Unknown proj_engine: {!r}'.format(engine))
		else:
			self._proj_engine = engine

	@property
	def img_engine(self):
		return self._img_engine

	@img_engine.setter
	def img_engine(self, engine):
		if engine not in getAvailableImgEngines():
			raise ValueError('Unknown img_engine: {!r}'.format(engine))
		else:
			self._img_engine = engine


cfgFile = os.path.join(os.path.dirname(__file__), "settings.json")

try:
	with open(cfgFile, 'r', encoding='utf-8') as cfg:
		prefs = json.load(cfg)
	settings = Settings(**prefs)
except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
	log.warning('Cannot load settings: %s', e)
	settings = Settings(
		proj_engine='AUTO',
		img_engine='AUTO',
		user_agent='CartoBlend'
	)
