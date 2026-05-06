import logging
log = logging.getLogger(__name__)

#GDAL
try:
	from osgeo import gdal
except ImportError:
	HAS_GDAL = False
	log.debug('GDAL Python binding unavailable')
else:
	HAS_GDAL = True
	log.debug('GDAL Python binding available')


#PyProj
try:
	import pyproj
except ImportError:
	HAS_PYPROJ = False
	log.debug('PyProj unavailable')
else:
	HAS_PYPROJ = True
	log.debug('PyProj available')


#PIL/Pillow
try:
	from PIL import Image
except ImportError:
	HAS_PIL = False
	log.debug('Pillow unavailable')
else:
	HAS_PIL = True
	log.debug('Pillow available')


#Imageio freeimage plugin
try:
	from .lib import imageio
except Exception as e:
	log.error("Cannot import ImageIO", exc_info=True)
	HAS_IMGIO = False
else:
	HAS_IMGIO = True
	log.debug('ImageIO available (FreeImage lib will be fetched on demand)')


_freeimage_ready = False

def ensure_freeimage():
	"""Lazily download/initialise the FreeImage shared library on first use.

	Called by code paths that actually need ImageIO's FreeImage plugin so that
	a synchronous network download never blocks the Blender UI thread at
	addon import time.
	"""
	global _freeimage_ready
	if not HAS_IMGIO or _freeimage_ready:
		return _freeimage_ready
	try:
		from .lib import imageio
		imageio.plugins._freeimage.get_freeimage_lib()
		_freeimage_ready = True
		log.debug('ImageIO Freeimage plugin available')
	except Exception:
		log.error("Cannot install ImageIO's Freeimage plugin", exc_info=True)
		_freeimage_ready = False
	return _freeimage_ready
