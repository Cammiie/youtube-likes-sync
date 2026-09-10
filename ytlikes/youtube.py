from __future__ import annotations

import hashlib
import json
import re
import requests
from urllib.parse import urlsplit
from ytmusicapi import YTMusic, setup
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError
from ytmusicapi.continuations import get_continuation_token, CONTINUATION_ITEMS
from ytmusicapi.navigation import nav, TWO_COLUMN_RENDERER, SECTION, CONTENT

from .common import SyncError


def parse_headers(raw: str) -> dict:
    try:
        # DevTools' Node.js fetch copy includes cookies; parse it as data only.
        # Never execute JavaScript or a shell command copied from the browser.
        if match := re.match(r'\s*fetch\(\s*', raw):
            decoder = json.JSONDecoder()
            url, end = decoder.raw_decode(raw, match.end())
            target = urlsplit(url)
            if target.scheme != 'https' or target.hostname != 'music.youtube.com' or target.path != '/youtubei/v1/browse':
                raise ValueError()
            request, _ = decoder.raw_decode(raw, raw.index('{', end))
            raw = json.dumps(request['headers'])
        headers = json.loads(raw) if raw.lstrip().startswith("{") else json.loads(setup(headers_raw=raw))
        headers = {str(k).lower(): str(v) for k, v in headers.items()}
        if "cookie" not in headers or not any(x in headers["cookie"] for x in ("SAPISID=", "__Secure-3PAPISID=")):
            raise ValueError()
        # YTMusic computes authorization afresh; never persist a copied expiring authorization header.
        allowed = {"cookie", "x-goog-authuser", "x-goog-visitor-id", "x-origin", "origin", "user-agent"}
        return {k: v for k, v in headers.items() if k in allowed}
    except Exception:
        raise SyncError("invalid_youtube_request_headers") from None


def source_song(s: dict) -> dict:
    album = s.get("album") or {}
    return {"video_id": str(s["videoId"]), "title": s.get("title") or "",
            "artists": [a.get("name", "") for a in s.get("artists", [])],
            "album": album.get("name", ""), "duration": s.get("duration_seconds") or 0,
            "isrc": s.get("isrc") or "", "available": s.get("isAvailable", True)}


class TimedSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (10, 30))
        return super().request(method, url, **kwargs)


class LikesAudit:
    """Prove pagination ended, independently of a stale playlist header count."""
    def __init__(self):
        self.started = False
        self.pending = None
        self.tokens = set()
        self.rows = 0

    def observe(self, body, response):
        if body.get('browseId') == 'VLLM':
            if self.started:
                raise SyncError('incomplete_likes_snapshot')
            self.started = True
            items = nav(response, [*TWO_COLUMN_RENDERER, 'secondaryContents', *SECTION,
                                   *CONTENT, 'musicPlaylistShelfRenderer', 'contents'], True)
        elif 'continuation' in body:
            if not self.started or body['continuation'] != self.pending:
                raise SyncError('incomplete_likes_snapshot')
            items = nav(response, CONTINUATION_ITEMS, True)
            if not items:
                raise SyncError('incomplete_likes_snapshot')
        else:
            return
        if not isinstance(items, list):
            raise SyncError('incomplete_likes_snapshot')
        for item in items:
            if 'musicResponsiveListItemRenderer' in item:
                self.rows += 1
            elif 'continuationItemRenderer' not in item:
                raise SyncError('incomplete_likes_snapshot')
        self.pending = get_continuation_token(items) if items else None
        if any('continuationItemRenderer' in item for item in items) and not self.pending:
            raise SyncError('incomplete_likes_snapshot')
        if self.pending:
            if self.pending in self.tokens:
                raise SyncError('incomplete_likes_snapshot')
            self.tokens.add(self.pending)

    def validate(self, tracks):
        if not self.started or self.pending or not isinstance(tracks, list) or len(tracks) != self.rows:
            raise SyncError('incomplete_likes_snapshot')


class CompleteYTMusic(YTMusic):
    _likes_audit = None
    likes_snapshot_complete = False

    def _send_request(self, endpoint, body, additionalParams=''):
        response = super()._send_request(endpoint, body, additionalParams)
        if self._likes_audit is not None and endpoint == 'browse':
            self._likes_audit.observe(body, response)
        return response

    def get_liked_songs(self, limit=None):
        self.likes_snapshot_complete = False
        audit = self._likes_audit = LikesAudit()
        try:
            playlist = super().get_liked_songs(limit=None)
            audit.validate(playlist.get('tracks'))
            self.likes_snapshot_complete = True
            return playlist
        finally:
            self._likes_audit = None


class YouTube:
    def __init__(self, headers: dict, client=None, root=None):
        session = TimedSession()
        if headers.get('kind') == 'google_oauth':
            from .google_auth import ProtectedCredentials
            self.client = client or CompleteYTMusic(auth=headers['tokens'], requests_session=session, language='en',
                                                    oauth_credentials=ProtectedCredentials(headers,root))
            return
        # Timeout is enforced on each continuation as well as the first request.
        # ytmusicapi detects browser auth by the scheme, then generates a fresh
        # SAPISIDHASH on each request. Do not persist the copied expiring hash.
        runtime_headers = dict(headers, authorization="SAPISIDHASH")
        self.client = client or CompleteYTMusic(auth=runtime_headers, requests_session=session, language="en")

    def fetch(self) -> tuple[list[dict], str]:
        try:
            account = self.client.get_account_info()
            identity = account.get("channelHandle") or account.get("accountName")
            if not identity:
                raise SyncError("youtube_auth_required")
            # None is forwarded to get_playlist and drains every continuation.
            playlist = self.client.get_liked_songs(limit=None)
            tracks = playlist.get("tracks")
            if not isinstance(tracks, list):
                raise SyncError("incomplete_likes_snapshot")
            expected = playlist.get("trackCount")
            if expected is not None and int(expected) > len(tracks) and not getattr(self.client, 'likes_snapshot_complete', False):
                raise SyncError("incomplete_likes_snapshot")
            if any(not isinstance(t, dict) for t in tracks):
                raise SyncError("incomplete_likes_snapshot")
            songs = [source_song(s) for s in tracks if s.get("videoId")]
            songs = list({s["video_id"]: s for s in songs}.values())
            return songs, hashlib.sha256(identity.encode()).hexdigest()
        except SyncError:
            raise
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                raise SyncError("youtube_auth_required") from None
            raise SyncError("youtube_request_failed") from None
        except (YTMusicUserError, YTMusicServerError) as e:
            # Inspect in memory only; upstream error messages can contain request details.
            message = str(e).lower()
            if any(s in message for s in ("401", "403", "sign in", "login", "authentication", "not authenticated")):
                raise SyncError("youtube_auth_required") from None
            raise SyncError("youtube_response_changed") from None
        except requests.RequestException:
            raise SyncError("youtube_network_error") from None
        except Exception:
            raise SyncError("youtube_response_changed") from None
