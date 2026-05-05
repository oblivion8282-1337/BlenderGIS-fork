# -*- coding:utf-8 -*-
"""Flat provider catalog management.

Each entry is a "provider" keyed by a dotted compound_key like
'OpenStreetMap.Mapnik' that combines a source family with a specific style
or layer. The catalog merges:

1. **Built-ins** — derived from servicesDefs.SOURCES at module load time.
   These map back to (srckey, laykey) so MapService keeps working unchanged.
2. **User overrides** — JSON list in addon prefs that hides a built-in,
   tweaks fields, or defines a fully-new custom TMS entry.

For pure-custom (user-defined) entries the helper inject_custom_into_sources()
synthesises a SOURCES entry on the fly so MapService can dispatch to them
without any further changes downstream.
"""

import json
import logging

from .servicesDefs import SOURCES, GRIDS

log = logging.getLogger(__name__)


# Map from source key to the addon-prefs attribute that holds its API key.
# A provider whose source appears here is considered "needs key" — the
# N-Panel filter and the prefs status icon use this.
KEYED_SOURCES = {
	'MAPBOX': ('mapbox_token',),
	'MAPTILER': ('maptiler_api_key',),
	'THUNDERFOREST': ('thunderforest_api_key',),
	'STADIA': ('stadia_api_key',),
	'CDSE_S2': ('cdse_client_id', 'cdse_client_secret'),
}


def _flatten_builtins():
	"""Convert servicesDefs.SOURCES into a flat dict {compound_key: entry}."""
	catalog = {}
	for srckey, src in SOURCES.items():
		for laykey, lay in src.get('layers', {}).items():
			compound = '{}.{}'.format(srckey, laykey)
			catalog[compound] = {
				'key': compound,
				'name': '{} — {}'.format(src.get('name', srckey), lay.get('name', laykey)),
				'srckey': srckey,
				'laykey': laykey,
				'description': lay.get('description') or src.get('description', ''),
				'format': lay.get('format', 'png'),
				'zmin': lay.get('zmin', 0),
				'zmax': lay.get('zmax', 22),
				'service': src.get('service', 'TMS'),
				'grid': src.get('grid', 'WM'),
				'needs_key_attrs': KEYED_SOURCES.get(srckey, ()),
				'is_custom': False,
				'is_builtin': True,
			}
	return catalog


# Curated default visibility — only show ~15 popular built-ins after fresh
# install. Power users can flip the rest on with one click in the prefs UI.
DEFAULT_VISIBLE = {
	'GOOGLE.SAT', 'GOOGLE.MAP',
	'OSM.MAPNIK',
	'BING.SAT', 'BING.MAP',
	'ESRI.AERIAL', 'ESRI.STREET', 'ESRI.TOPO',
	'CARTO_LIGHT.LABELS', 'CARTO_DARK.LABELS', 'CARTO_VOYAGER.LABELS',
	'OPENTOPOMAP.TOPO',
	'NASA_GIBS.MODIS_TERRA',
	'EOX_S2.S2_2024',
	'WIKIMEDIA.MAP',
}


def _parse_json(s, default):
	try:
		return json.loads(s) if s else default
	except (json.JSONDecodeError, TypeError):
		return default


def get_user_overrides(prefs):
	"""Return user-customised overrides as a dict {compound_key: override_dict}."""
	return _parse_json(getattr(prefs, 'customProvidersJson', ''), {})


def set_user_overrides(prefs, data):
	prefs.customProvidersJson = json.dumps(data)


def get_catalog(prefs):
	"""Return ordered list of provider entries, with user overrides applied.

	Each entry has the schema described at the top of this module plus a
	'visible' bool reflecting the user's pick (or DEFAULT_VISIBLE for fresh
	installs).
	"""
	overrides = get_user_overrides(prefs)
	catalog = []
	seen = set()

	# Built-ins first, in insertion order, with overrides folded on top.
	for key, entry in _flatten_builtins().items():
		merged = dict(entry)
		ov = overrides.get(key, {})
		merged['visible'] = ov.get('visible', key in DEFAULT_VISIBLE)
		for f in ('name', 'url', 'format', 'zmin', 'zmax', 'description'):
			if f in ov:
				merged[f] = ov[f]
		catalog.append(merged)
		seen.add(key)

	# Pure-custom entries (user-defined, no built-in twin).
	for key, ov in overrides.items():
		if key in seen:
			continue
		if not ov.get('is_custom'):
			continue
		catalog.append({
			'key': key,
			'name': ov.get('name', key),
			'srckey': key,
			'laykey': 'CUSTOM',
			'description': ov.get('description', ''),
			'format': ov.get('format', 'png'),
			'zmin': ov.get('zmin', 0),
			'zmax': ov.get('zmax', 22),
			'service': 'TMS',
			'grid': ov.get('grid', 'WM'),
			'needs_key_attrs': (),
			'is_custom': True,
			'is_builtin': False,
			'url': ov.get('url', ''),
			'visible': ov.get('visible', True),
		})

	return catalog


def get_visible_entries(prefs):
	return [e for e in get_catalog(prefs) if e.get('visible', True)]


def inject_custom_into_sources(prefs):
	"""Synthesise a SOURCES entry for each user-defined custom TMS provider so
	MapService can resolve them without a special code path.

	Idempotent — replaces previous synthetic entries on each call so editing
	a custom URL is reflected immediately.
	"""
	# Drop previously injected entries first (anything not in the original
	# servicesDefs is treated as user-injected).
	overrides = get_user_overrides(prefs)
	for key, ov in list(overrides.items()):
		if not ov.get('is_custom'):
			continue
		# Normalise xyzservices-style lowercase placeholders to our uppercase ones.
		url = ov.get('url', '')
		url = url.replace('{x}', '{X}').replace('{y}', '{Y}').replace('{z}', '{Z}')
		# Strip retina/ext placeholders that we don't support.
		url = url.replace('{r}', '').replace('{ext}', ov.get('format', 'png'))
		SOURCES[key] = {
			'name': ov.get('name', key),
			'description': ov.get('description', ''),
			'service': 'TMS',
			'grid': ov.get('grid', 'WM'),
			'quadTree': False,
			'layers': {
				'CUSTOM': {
					'urlKey': '',
					'name': ov.get('name', key),
					'description': ov.get('description', ''),
					'format': ov.get('format', 'png'),
					'zmin': ov.get('zmin', 0),
					'zmax': ov.get('zmax', 22),
				}
			},
			'urlTemplate': url,
			'referer': ov.get('referer', ''),
		}


def get_compound_routing(compound_key):
	"""Map a compound_key back to (srckey, laykey) for MapService dispatch.

	Built-ins have explicit srckey/laykey; custom entries are injected as
	srckey == compound_key with synthetic laykey 'CUSTOM'.
	"""
	flat = _flatten_builtins()
	if compound_key in flat:
		e = flat[compound_key]
		return e['srckey'], e['laykey']
	return compound_key, 'CUSTOM'


# ---------------------------------------------------------------------------
# xyzservices catalog import
# ---------------------------------------------------------------------------

XYZSERVICES_URL = (
	'https://raw.githubusercontent.com/geopandas/xyzservices/main/'
	'provider_sources/leaflet-providers-parsed.json'
)


def _walk_xyzservices(node, path=()):
	"""Yield (compound_key, leaf_dict) tuples from an xyzservices catalog
	(dict-of-dicts with leaf entries identified by a 'url' field)."""
	if isinstance(node, dict):
		if 'url' in node:
			key = node.get('name') or '.'.join(path)
			yield key, node
			return
		for k, v in node.items():
			yield from _walk_xyzservices(v, path + (k,))


def _adapt_xyz_entry(entry):
	"""Convert one xyzservices leaf into our provider-override schema, or
	return None if the entry can't be represented as a plain TMS URL."""
	import re
	url = entry.get('url', '')
	if not url:
		return None
	apikey_field = entry.get('apikey')
	# xyzservices marks unfilled keys with a literal placeholder string. We
	# can't satisfy those at import time so skip them.
	if apikey_field == '<insert your api key here>' or apikey_field == '':
		return None
	# Adapt placeholders to our format.
	mapped = (url
		.replace('{z}', '{Z}')
		.replace('{x}', '{X}')
		.replace('{y}', '{Y}'))
	# Retina markers we don't support → drop.
	mapped = mapped.replace('{r}', '')
	# File extension: substitute literal value
	if '{ext}' in mapped:
		ext = entry.get('ext') or 'png'
		mapped = mapped.replace('{ext}', ext)
	# Subdomain: take first from list, fall back to 'a'.
	if '{s}' in mapped:
		subs = entry.get('subdomains') or 'abc'
		first = subs[0] if subs else 'a'
		mapped = mapped.replace('{s}', first)
	# Real apikey value baked in (some entries inline a free token, e.g.
	# OpenSnowMap doesn't but Thunderforest gives an empty 'apikey' key —
	# we already skipped those).
	if apikey_field and isinstance(apikey_field, str):
		mapped = mapped.replace('{apikey}', apikey_field)
	# Any remaining unresolved braces means we'd produce a broken URL → skip.
	leftovers = re.findall(r'\{([^}]+)\}', mapped)
	leftovers = [m for m in leftovers if m not in ('Z', 'X', 'Y')]
	if leftovers:
		return None
	# Heuristic format: extension in URL wins, else infer from ext field.
	ext_lower = (entry.get('ext') or '').lower()
	if mapped.lower().endswith(('.jpg', '.jpeg')) or ext_lower in ('jpg', 'jpeg'):
		fmt = 'jpg'
	else:
		fmt = 'png'
	return {
		'url': mapped,
		'format': fmt,
		'zmin': int(entry.get('min_zoom', 0)),
		'zmax': int(entry.get('max_zoom', 19)),
		'description': entry.get('attribution', '') or '',
	}


def import_xyz_catalog(prefs, fetch_fn=None):
	"""Fetch the xyzservices catalog and merge it into the user overrides as
	is_imported entries. Re-importing replaces previous imports while keeping
	visibility flags so the user's curation survives a refresh.

	Returns (added, skipped, refreshed).
	"""
	import urllib.request, json
	if fetch_fn is None:
		def fetch_fn(url):
			req = urllib.request.Request(url, headers={'User-Agent': 'CartoBlend xyz-import'})
			with urllib.request.urlopen(req, timeout=20) as resp:
				return resp.read().decode('utf-8')
	body = fetch_fn(XYZSERVICES_URL)
	xyz = json.loads(body)

	overrides = get_user_overrides(prefs)
	# Capture visibility of previously imported entries so the user's hide/show
	# choices don't get clobbered on refresh.
	prev_visible = {k: v.get('visible', False)
		for k, v in overrides.items() if v.get('is_imported')}
	# Drop previous imports.
	for k in list(overrides.keys()):
		if overrides[k].get('is_imported'):
			del overrides[k]

	builtin_keys = set(_flatten_builtins().keys())
	added = 0
	skipped = 0
	for compound, leaf in _walk_xyzservices(xyz):
		# Imports never override a built-in — user can't edit those via import.
		if compound in builtin_keys:
			continue
		adapted = _adapt_xyz_entry(leaf)
		if adapted is None:
			skipped += 1
			continue
		entry = {
			'is_custom': True,
			'is_imported': True,
			'visible': prev_visible.get(compound, False),  # default hidden
			'name': compound,
			'grid': 'WM',
		}
		entry.update(adapted)
		overrides[compound] = entry
		added += 1

	set_user_overrides(prefs, overrides)
	return (added, skipped, len(prev_visible))
