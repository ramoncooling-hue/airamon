"""Model catalog and task routes share the legacy settings file and transaction."""
import ipaddress
import json
import re
from urllib.parse import urlsplit

from . import store

MAX_CONFIG = 65536
_FIELDS = {'id', 'name', 'provider', 'base_url', 'model', 'api_key'}
_REQUIRED = _FIELDS - {'api_key'}
_PUBLIC_FIELDS = {'api_key_set', 'clear_api_key'}
_TIERS = {'fast', 'reasoning'}
RevisionConflict = store.RevisionConflict


def _valid_url(value):
    if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise ValueError('Invalid model URL')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or '?' in value or '#' in value or parsed.netloc.endswith(':')
            or re.search(r'%(?![0-9A-Fa-f]{2})', parsed.path)):
        raise ValueError('Invalid model URL')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('Invalid model port')
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        hostname = host.encode('idna').decode('ascii').rstrip('.')
        if (len(hostname) > 253 or not hostname
                or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                       for label in hostname.split('.'))):
            raise ValueError('Invalid model hostname')


def validate(profiles, routes, *, allow_public=False):
    if not isinstance(profiles, list) or len(profiles) > 100:
        raise ValueError('Invalid model profiles')
    ids = set()
    for profile in profiles:
        allowed = _FIELDS | (_PUBLIC_FIELDS if allow_public else set())
        if (not isinstance(profile, dict) or not _REQUIRED <= profile.keys()
                or profile.keys() - allowed):
            raise ValueError('Invalid model profile')
        for key in _FIELDS & profile.keys():
            value = profile[key]
            if (not isinstance(value, str) or len(value) > (8192 if key == 'api_key' else 2048)
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError('Invalid model field')
        id = profile['id']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', id) or id in ids:
            raise ValueError('Invalid or duplicate model id')
        ids.add(id)
        if not profile['name'].strip() or not profile['model'].strip():
            raise ValueError('Model name and identifier are required')
        _valid_url(profile['base_url'])
        for key in _PUBLIC_FIELDS & profile.keys():
            if not isinstance(profile[key], bool):
                raise ValueError('Invalid model flag')
    if not isinstance(routes, dict) or routes.keys() != _TIERS:
        raise ValueError('Invalid model routes')
    if any(not isinstance(id, str) or (id and id not in ids) for id in routes.values()):
        raise ValueError('Unknown model route')
    raw = json.dumps({'profiles': profiles, 'routes': routes}, ensure_ascii=False, allow_nan=False)
    if len(raw.encode('utf-8')) > MAX_CONFIG:
        raise ValueError('Model configuration exceeds size limit')


def public_profiles(profiles):
    return [{**{key: p[key] for key in _FIELDS - {'api_key'}},
             'api_key': store._mask(p.get('api_key', '')),
             'api_key_set': bool(p.get('api_key'))} for p in profiles]


def _public(cfg, revision):
    return {'profiles': public_profiles(cfg['model_profiles']),
            'routes': dict(cfg['model_routes']), 'revision': revision}


def read():
    with store._LOCK:
        cfg, revision = store._load()
        return _public(cfg, revision)


def save(profiles, routes, revision):
    validate(profiles, routes, allow_public=True)
    with store._transaction():
        cfg, current = store._load()
        if not isinstance(revision, str) or revision != current:
            raise RevisionConflict('Settings changed; reload before saving')
        previous = {p['id']: p.get('api_key', '') for p in cfg['model_profiles']}
        merged = []
        for profile in profiles:
            p = {key: profile[key] for key in _REQUIRED}
            key = profile.get('api_key', '')
            if profile.get('clear_api_key') is True:
                key = ''
            elif not key or key.startswith(('••••', '****')):
                key = previous.get(p['id'], '')
            p['api_key'] = key
            merged.append(p)
        cfg['model_profiles'] = merged
        cfg['model_routes'] = dict(routes)
        revision = store._save(cfg, current)
        return _public(cfg, revision)


def resolve(cfg, tier):
    """An empty route retains the legacy tier; an invalid route never falls back."""
    if tier not in _TIERS or not isinstance(cfg, dict):
        raise ValueError('Invalid model tier')
    profiles = cfg.get('model_profiles', [])
    routes = cfg.get('model_routes', {'fast': '', 'reasoning': ''})
    validate(profiles, routes)
    selected = routes[tier]
    if selected:
        return dict(next(p for p in profiles if p['id'] == selected))
    legacy = cfg.get(tier, {})
    if not isinstance(legacy, dict):
        raise ValueError('Invalid legacy model tier')
    return dict(legacy)
