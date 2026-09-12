"""MIT implementation of Antra's published Tidal mirror wire protocol.

Reference revision: 79d3513745a44e999fbf0176fe5d9a21e4b500c4.
No Antra implementation, user account token, or browser session is included.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .common import SyncError, atomic_write, dpapi
from .media import Downloader, validate
from .provider import retry_after

ENGINE = 'antra_tidal'
MANIFEST = 'https://gist.githubusercontent.com/anandprtp/fdc2c16b7bfdc2d337fbc86161b79371/raw'


def catalog_id(value):
    value = str(value)
    if not re.fullmatch(r'[0-9]{1,20}', value):
        raise SyncError('invalid_catalog_id')
    return value


def checked_access(value):
    if not isinstance(value, dict):
        raise SyncError('api_access_required', 1800)
    endpoint, key = value.get('endpoint'), value.get('key')
    try:
        url = urlsplit(endpoint if isinstance(endpoint, str) else '')
        valid = (url.scheme == 'https' and url.hostname and not url.username and not url.password
                 and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid or not isinstance(key, str) or not 1 <= len(key) <= 4096 or '\r' in key or '\n' in key:
        raise SyncError('api_access_required', 1800)
    return {'endpoint': endpoint.rstrip('/'), 'key': key}


def save_access(root, value):
    atomic_write(Path(root)/'mirror-access.dpapi', dpapi(json.dumps(checked_access(value)).encode()))


def access_status(root):
    try:
        retry = json.loads((Path(root)/'mirror-health.json').read_text()).get('retry_at', 0)
        retry = float(retry)
    except (OSError, ValueError, TypeError):
        retry = 0
    return {'provider': ENGINE, 'browserless': True,
            'configured': (Path(root)/'mirror-access.dpapi').is_file(), 'retry_at': retry}


class AntraCatalog:
    def __init__(self, root, settings=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.trust_env = False
        self._access = None

    def close(self):
        self.session.close()

    def access(self):
        if self._access is None:
            file = self.root/'mirror-access.dpapi'
            if file.exists():
                try:
                    self._access = checked_access(json.loads(dpapi(file.read_bytes(), decrypt=True)))
                except (ValueError, OSError, SyncError):
                    raise SyncError('api_access_required', 1800) from None
            else:
                try:
                    # Only the public bootstrap uses redirects. It receives no credentials.
                    with self.session.get(MANIFEST, timeout=(4, 8), stream=True) as response:
                        if response.status_code != 200:
                            raise SyncError('api_access_required', 1800)
                        manifest = self.read_json(response, 65536)
                    self._access = checked_access({'endpoint': manifest['mirrors']['tidal'], 'key': manifest['api_key']})
                    save_access(self.root, self._access)
                except (KeyError, TypeError, ValueError, requests.RequestException):
                    raise SyncError('api_access_required', 1800) from None
        return self._access

    @staticmethod
    def read_json(response, limit=4*1024**2):
        body = bytearray()
        deadline = time.monotonic() + 12
        try:
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > limit or time.monotonic() > deadline:
                    raise SyncError('provider_response_invalid')
            return json.loads(body)
        except (ValueError, requests.RequestException):
            raise SyncError('provider_response_invalid') from None

    def request(self, route, params=None, *, audio=False):
        retry = access_status(self.root)['retry_at']
        if time.time() < retry:
            raise SyncError('provider_rate_limited', retry-time.time())
        access = self.access()
        try:
            response = self.session.get(access['endpoint']+route, params=params,
                headers={'X-API-Key': access['key']}, timeout=(4, 20 if audio else 8),
                stream=True, allow_redirects=False)
        except requests.RequestException:
            raise SyncError('provider_unavailable') from None
        if response.status_code != 200:
            status = response.status_code
            delay = retry_after(response.headers.get('Retry-After')) if status == 429 else 300
            response.close()
            if status == 429:
                atomic_write(self.root/'mirror-health.json', json.dumps({'retry_at': time.time()+delay}).encode())
            code = ('api_access_required' if status in (401, 403) else 'provider_rate_limited' if status == 429
                    else 'lossless_unavailable' if audio and status in (404, 409, 422)
                    else 'catalog_track_unavailable' if status == 404 else 'provider_unavailable')
            raise SyncError(code, max(delay, 1800) if code == 'api_access_required' else delay)
        return response

    def data(self, route, params=None):
        with self.request(route, params) as response:
            return self.read_json(response)

    @staticmethod
    def track(row):
        try:
            album = row.get('album') or {}
            artists = row.get('artists') or [row.get('artist') or {}]
            artists = [a.get('name', '') if isinstance(a, dict) else str(a) for a in artists]
            duration = float(row.get('duration') or float(row.get('duration_ms') or 0)/1000)
            if not row.get('title') or not any(artists) or not math.isfinite(duration) or not 0 <= duration <= 86400:
                raise ValueError()
            cover = album.get('cover') or ''
            if not cover:
                match = re.fullmatch(r'https://resources\.tidal\.com/images/([a-fA-F0-9/]+)/\d+x\d+\.jpg',
                                     str(row.get('artwork_url') or album.get('artwork_url') or ''))
                cover = match[1].replace('/', '-') if match else ''
            return {'id': catalog_id(row.get('track_id', row.get('id'))), 'transport': ENGINE,
                    'title': str(row['title'])[:500], 'artists': artists, 'version': str(row.get('version') or ''),
                    'album': str(album.get('title') or ''), 'cover': cover, 'duration': duration,
                    'isrc': str(row.get('isrc') or ''),
                    'track_number': int(row.get('trackNumber', row.get('track_number')) or 0),
                    'disc_number': int(row.get('volumeNumber', row.get('disc_number')) or 1),
                    'date': str(row.get('releaseDate') or album.get('release_date') or '')[:10]}
        except (AttributeError, TypeError, ValueError, OverflowError):
            raise SyncError('invalid_catalog_metadata') from None

    def search(self, source):
        title = source.get('title', '').strip()
        if not title:
            return []
        query = ' '.join([title, *source.get('artists', [])])[:300]
        data = self.data('/search/', {'s': query, 'offset': 0, 'limit': 50})
        if isinstance(data, dict):
            data = data.get('data', data)
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise SyncError('provider_search_shape_changed')
        tracks = [self.track(row) for row in data['items'][:50]]
        # The service has no documented dedicated ISRC search. Enrich the bounded
        # returned set when ISRC is available so existing ranking can prefer it.
        if source.get('isrc'):
            tracks = [self.metadata(track) for track in tracks[:8]]
        return tracks

    def metadata(self, candidate):
        selected = catalog_id(candidate['id'])
        result = self.track(self.data('/api/meta/track/'+selected))
        if result['id'] != selected:
            raise SyncError('provider_recording_mismatch', 86400)
        return result


class AntraDownloader(Downloader):
    def download(self, track, video_id, target):
        target = Path(target)
        identifier = catalog_id(track['id'])
        if self.existing(target, track):
            return target
        temporary = target.with_suffix('.part')
        deadline = time.monotonic() + self.settings['max_job_seconds']
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for params in ({'strict_24': '1'}, {'prefer_16': '1'}):
                try:
                    with self.provider.request('/api/stream/'+identifier, params, audio=True) as response:
                        mime = response.headers.get('Content-Type', '').lower()
                        if any(value in mime for value in ('json', 'html', 'mpeg', 'mp4')):
                            raise SyncError('unsupported_audio_response', 86400)
                        received = 0
                        with temporary.open('wb') as output:
                            for chunk in response.iter_content(262144):
                                received += len(chunk)
                                if received > self.settings['max_download_bytes']:
                                    raise SyncError('download_size_limit', 86400)
                                if time.monotonic() > deadline:
                                    raise SyncError('download_time_limit')
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        length = response.headers.get('Content-Length')
                        if not received or (length and (not length.isdigit() or received != int(length))):
                            raise SyncError('incomplete_download')
                    validate(temporary, track.get('duration'))
                    self.tag(temporary, track, video_id)
                    tagged = validate(temporary, track.get('duration'))
                    tagged['download_transport'] = ENGINE
                    tagged.save()
                    with temporary.open('r+b') as output:
                        os.fsync(output.fileno())
                    # On Windows this refuses to overwrite an existing destination.
                    os.rename(temporary, target)
                    return target
                except SyncError as error:
                    if params == {'strict_24': '1'} and error.code == 'lossless_unavailable':
                        continue
                    raise
        except requests.RequestException:
            raise SyncError('download_interrupted') from None
        except OSError:
            raise SyncError('download_disk_error') from None
        finally:
            temporary.unlink(missing_ok=True)
