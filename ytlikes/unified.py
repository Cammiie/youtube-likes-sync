"""Native transport for the pinned Monochrome unified playback API.

Requests/resource selection follow upstream api.js. No challenge execution or
browser fallback. Credentials and ephemeral playback data never enter SQLite.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import requests

from .common import SyncError, atomic_write, dpapi
from .media import Downloader
from .provider import AudioPlan, Resource, https, parse_mpd, retry_after, small_bytes


def api_base(value):
    if not isinstance(value, str):
        raise SyncError('api_configuration_invalid')
    parsed = urlsplit(https(value))
    if parsed.query or parsed.fragment:
        raise SyncError('api_configuration_invalid')
    return value.rstrip('/')


def jwt_expiry(token):
    try:
        payload = token.split('.')[1]
        value = float(json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['exp'])
        return value if math.isfinite(value) else 0
    except (ValueError, KeyError, IndexError, TypeError):
        return 0


def save_session(root, value, *, require_live=False):
    base = api_base(value.get('base'))
    token = value.get('token', '')
    jwt = value.get('jwt', '')
    if not isinstance(token, str) or not token.strip() or len(token) > 8192:
        raise SyncError('api_configuration_invalid')
    if not isinstance(jwt, str) or len(jwt) > 16384 or any(c in token+jwt for c in '\r\n'):
        raise SyncError('api_configuration_invalid')
    required = value.get('verification_required') is True
    expires = jwt_expiry(jwt) if jwt else 0
    if require_live and required and expires <= time.time()+30:
        raise SyncError('api_access_required', 86400)
    # Endpoint and token are one protected record; endpoint changes cannot leak
    # credentials from an older configuration to a different host.
    record = dict(base=base, token=token.strip(), jwt=jwt,
                  verification_required=required, expires=expires)
    atomic_write(root/'unified-api.dpapi', dpapi(json.dumps(record).encode()))
    atomic_write(root/'unified-status.json', b'{"status":"configured"}')


def session_status(root):
    try:
        value = json.loads(dpapi((root/'unified-api.dpapi').read_bytes(), decrypt=True))
    except (FileNotFoundError, ValueError, SyncError):
        return {'status':'api_access_required', 'configured':False}
    expired = value.get('verification_required') and value.get('expires', 0) <= time.time()+30
    try:
        last = json.loads((root/'unified-status.json').read_text())['status']
    except (OSError, ValueError, KeyError):
        last = 'configured'
    return {'status':'api_access_required' if expired else last, 'configured':True,
            'verification_expires':value.get('expires') or None}


@dataclass
class UnifiedPlan(AudioPlan):
    decryption_key: str | None = field(default=None, repr=False)


def lookup_params(track, quality):
    # Match getAmazonTrackTitle/Artist/Album/Duration and intent=download.
    title = str(track.get('title') or '').strip()
    if not title:
        raise SyncError('api_track_invalid', 86400)
    if track.get('version'):
        title += ' (' + str(track['version']).strip() + ')'
    params = {'track':title, 'intent':'download', 'quality':quality}
    artists = track.get('artists') or []
    if artists:
        params['artist'] = ', '.join(str(a).strip() for a in artists if str(a).strip())
    if track.get('album'):
        params['album'] = track['album'].strip()
    if track.get('isrc'):
        params['isrc'] = track['isrc'].strip().upper()
    duration = float(track.get('duration') or 0)
    if math.isfinite(duration) and duration > 0:
        params['duration'] = str(math.floor((duration/1000 if duration > 10000 else duration)+.5))
    return params


def decryption_key(resource):
    candidates = [resource.get('decryption_key'), resource.get('decryptionKey')]
    for section in ('encryption','decryption','drm'):
        obj = resource.get(section)
        if isinstance(obj, dict):
            key = obj.get('key')
            candidates.extend([key.get('value') if isinstance(key, dict) else key,
                               obj.get('decryption_key'), obj.get('decryptionKey')])
    key = next((x for x in candidates if x), None)
    if key is not None and (not isinstance(key, str) or not re.fullmatch('[a-fA-F0-9]{32}', key)):
        raise SyncError('encrypted_audio_unsupported', 86400)
    return key


class UnifiedAPI:
    def __init__(self, root, settings):
        self.root, self.settings = root, settings
        self.session = requests.Session()
        self.session.trust_env = False
        self.audio_session = requests.Session()
        self.audio_session.trust_env = False

    def exchange_verification(self, response_token):
        """Exchange an approved browser proof using the native client's identity."""
        if not isinstance(response_token, str) or not 1 <= len(response_token) <= 16384:
            raise SyncError('api_verification_exchange_failed')
        try:
            auth = json.loads(dpapi((self.root/'unified-api.dpapi').read_bytes(), decrypt=True))
            with self.session.post(api_base(auth['base'])+'/api/auth/turnstile',
                    headers={'Authorization':'Bearer '+auth['token'],'Content-Type':'application/json'},
                    json={'turnstile_token':response_token}, timeout=(6,15), stream=True,
                    allow_redirects=False) as response:
                if response.status_code != 200:
                    raise SyncError('api_verification_exchange_failed')
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data)>65536:
                        raise SyncError('api_verification_exchange_failed')
                payload = json.loads(data)
            token = payload.get('access_token') or payload.get('jwt') or payload.get('token')
            auth.update(jwt=token, verification_required=True)
            save_session(self.root, auth, require_live=True)
        except (requests.RequestException, OSError, ValueError, TypeError, AttributeError):
            raise SyncError('api_verification_exchange_failed') from None

    def resolve(self, track, quality):
        try:
            plan = self._resolve(track, quality)
        except SyncError as error:
            atomic_write(self.root/'unified-status.json', json.dumps({'status':error.code,
                         'retry_at':time.time()+error.delay}).encode())
            raise
        atomic_write(self.root/'unified-status.json', b'{"status":"ok"}')
        return plan

    def _resolve(self, track, quality):
        try:
            status = json.loads((self.root/'unified-status.json').read_text())
        except (OSError, ValueError):
            status = {}
        if status.get('status') == 'api_access_required':
            raise SyncError('api_access_required', 86400)
        if status.get('status') == 'provider_rate_limited' and status.get('retry_at', 0) > time.time():
            raise SyncError('provider_rate_limited', math.ceil(status['retry_at']-time.time()))
        try:
            auth = json.loads(dpapi((self.root/'unified-api.dpapi').read_bytes(), decrypt=True))
        except (OSError, ValueError, SyncError):
            raise SyncError('api_access_required', 86400) from None
        if auth.get('verification_required') and auth.get('expires', 0) <= time.time()+30:
            raise SyncError('api_access_required', 86400)
        # Keep this native client's headers consistent for issuance and lookup.
        headers = {'Authorization':'Bearer '+auth['token']}
        if auth.get('jwt') and auth.get('expires', 0) > time.time()+30:
            headers['X-Turnstile-JWT'] = auth['jwt']
        try:
            # Never forward authorization across redirects, to audio, or to YouTube.
            with self.session.get(api_base(auth['base'])+'/api/v2/track/',
                                  params=lookup_params(track, quality), headers=headers,
                                  timeout=(6,20), stream=True, allow_redirects=False) as response:
                if response.status_code in (401,403,428):
                    raise SyncError('api_access_required', 86400)
                if response.status_code == 429:
                    raise SyncError('provider_rate_limited', retry_after(response.headers.get('Retry-After')))
                if response.status_code in (404,502):
                    raise SyncError('catalog_audio_unavailable', 86400)
                if response.status_code != 200:
                    raise SyncError('unified_api_request_failed')
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > 4*1024**2:
                        raise SyncError('response_too_large')
                envelope = json.loads(data)
        except requests.RequestException:
            raise SyncError('provider_network_error') from None
        except (ValueError, TypeError):
            raise SyncError('provider_invalid_json') from None
        if not isinstance(envelope, dict) or str(envelope.get('schema_version','')).split('.')[0] not in ('1','2'):
            raise SyncError('unified_schema_unsupported', 86400)
        playback = envelope.get('playback')
        if not isinstance(playback, list):
            raise SyncError('unified_schema_unsupported', 86400)
        resource = next((r for r in playback if isinstance(r, dict) and isinstance(r.get('url'),str)
                         and r['url'] and r.get('kind') in ('audio','manifest')
                         and r.get('delivery') in ('direct','dash','hls')), None)
        if not resource:
            raise SyncError('catalog_audio_unavailable', 86400)
        provider = str(resource.get('source') or envelope.get('selected_source') or '').lower()
        if provider not in ('amazon','tidal','mono','monochrome'):
            raise SyncError('unified_source_unsupported', 86400)
        key = decryption_key(resource)
        encrypted = (key or resource.get('encrypted') or resource.get('drm') or resource.get('encryption')
                     or resource.get('decryption') or 'cenc' in str(resource.get('stream_type', '')).lower())
        if encrypted and not (key and provider == 'amazon' and self.settings.get('allow_encrypted_lossless') is True):
            raise SyncError('encrypted_audio_unsupported', 86400)
        if resource.get('lossless') is False:
            raise SyncError('lossy_audio_rejected', 86400)
        url = resource['url']
        mime = str(resource.get('mime_type') or '')
        if resource.get('delivery') == 'hls' or 'mpegurl' in mime or '.m3u8' in url:
            raise SyncError('hls_audio_unsupported', 86400)
        manifest = resource.get('kind') == 'manifest' or resource.get('delivery') == 'dash' or 'dash' in mime or '.mpd' in url
        if manifest:
            if key:
                raise SyncError('encrypted_segments_unsupported', 86400)
            xml, base = small_bytes(self.audio_session, https(url))
            return parse_mpd(xml, base, quality)
        return UnifiedPlan([Resource(https(url))], quality, key)


class UnifiedDownloader(Downloader):
    def __init__(self, provider, settings, root):
        super().__init__(provider, settings)
        self.session.trust_env = False
        self.api = UnifiedAPI(root, settings)

    def prepare_session(self, state):
        from .renewal import renew_if_due
        result = renew_if_due(self.api.root, self.settings, api=self.api)
        if result['status'] == 'renewed':
            with state.db:
                state.db.execute("UPDATE jobs SET next_try=0,attempts=0 WHERE state='pending' AND error='api_access_required'")

    def resolve(self, track, quality):
        return self.api.resolve(track, quality)

    def input_options(self, plan):
        key = getattr(plan, 'decryption_key', None)
        return ['-decryption_key', key] if key else []


def migration_page(root):
    """One-time export by an already-open helper; never runs verification."""
    source = (root/'monochrome/js/storage.js').read_text(encoding='utf-8')
    block = source.split('export const unifiedPlaybackSettings = {',1)[1].split('export const deezerFallbackSettings',1)[0]
    match = re.search(r"DEFAULT_API_TOKEN:\s*(['\"])(.*?)\1", block)
    default_token = match.group(2) if match else ''
    defaults = json.dumps({'base':'https://music-api.geeked.wtf','token':default_token}).replace('<','\\u003c')
    return ('''<!doctype html><meta charset="utf-8"><title>Monochrome API migration</title>
<p id="status">Moving the existing API session into protected local storage…</p>
<script>
(async()=>{try{
const defaults='''+defaults+''';
const token=localStorage.getItem('unified-playback-api-token') || localStorage.getItem('amazon-music-turnstile-bypass-token') || defaults.token;
let base=localStorage.getItem('unified-playback-api-base-url') || localStorage.getItem('amazon-music-api-base-url') || defaults.base;
if(['https://amz.geeked.wtf','https://track-api.monochrome.tf','https://mono.geeked.wtf'].includes(base.replace(/\\/+$/,''))) base=defaults.base;
const result=await fetch('/migration/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({base,token,jwt:localStorage.getItem('unified-playback-turnstile-jwt')||'',verification_required:token===defaults.token})});
document.getElementById('status').textContent=result.ok?'Session transferred. This helper can now close.':'The existing session could not be transferred. No browser verification was requested.';
}catch{document.getElementById('status').textContent='Session transfer unavailable. No browser verification was requested.'}})();
</script>''').encode()
