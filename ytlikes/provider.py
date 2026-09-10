"""Independent client for the HTTP interfaces used by Monochrome (Apache-2.0).

Reference: monochrome-music/monochrome at 60b76d0d9258b9aabc6a9ee83cb8a54bd76ce90b.
No browser application code, accounts, decryption, or shared credentials are bundled.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse

from defusedxml import ElementTree as ET
import requests

from .common import SyncError, atomic_write

UPSTREAM = "https://raw.githubusercontent.com/monochrome-music/monochrome/main/public/instances.json"
FALLBACK = {
    "api": ["https://eu-central.monochrome.tf", "https://us-west.monochrome.tf", "https://arran.monochrome.tf",
            "https://api.monochrome.tf", "https://monochrome-api.samidy.com", "https://triton.squid.wtf",
            "https://wolf.qqdl.site", "https://maus.qqdl.site", "https://vogel.qqdl.site",
            "https://hund.qqdl.site", "https://tidal.kinoplus.online"],
    "streaming": ["https://arran.monochrome.tf", "https://triton.squid.wtf", "https://wolf.qqdl.site",
                  "https://maus.qqdl.site", "https://vogel.qqdl.site", "https://katze.qqdl.site",
                  "https://hund.qqdl.site", "https://hifi.p1nkhamster.xyz"],
}


def https(url):
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise SyncError("unsupported_resource_url", 86400)
    return url


def retry_after(value):
    try:
        return max(300, int(value))
    except (ValueError, TypeError):
        try:
            return max(300, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except Exception:
            return 300


def response(session, url, *, params=None):
    """All network exceptions are replaced before entering persistent status/logs."""
    try:
        for _ in range(5):
            r = session.get(https(url), params=params, timeout=(6, 15), stream=True, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                url, params = urljoin(r.url, r.headers.get("Location", "")), None
                r.close()
                continue
            if r.status_code == 429:
                delay = retry_after(r.headers.get("Retry-After"))
                r.close()
                raise SyncError("provider_rate_limited", delay)
            if not 200 <= r.status_code < 300:
                status = r.status_code
                r.close()
                raise SyncError("provider_http_" + str(status))
            return r
        raise SyncError("too_many_redirects")
    except requests.RequestException:
        raise SyncError("provider_network_error") from None


def small_bytes(session, url, *, params=None, limit=4 * 1024**2):
    try:
        with response(session, url, params=params) as r:
            output = bytearray()
            for chunk in r.iter_content(64 * 1024):
                output.extend(chunk)
                if len(output) > limit:
                    raise SyncError("response_too_large")
            return bytes(output), r.url
    except requests.RequestException:
        raise SyncError("provider_network_error") from None


def get_json(session, url, params=None):
    content, _ = small_bytes(session, url, params=params)
    try:
        return json.loads(content)
    except (ValueError, UnicodeError):
        raise SyncError("provider_invalid_json") from None


def normalize_track(t):
    if isinstance(t, dict) and isinstance(t.get("item"), dict):
        t = t["item"]
    if not isinstance(t, dict) or not str(t.get("id", "")).isdigit() or not t.get("title"):
        return None
    if "video" in str(t.get("type", "")).lower():
        return None
    artists = t.get("artists") or [t.get("artist") or {}]
    album = t.get("album") or {}
    return {"id": str(t["id"]), "title": str(t["title"]), "version": str(t.get("version") or ""),
            "artists": [str(a.get("name") or "") for a in artists if isinstance(a, dict)],
            "album": str(album.get("title") or ""), "cover": str(album.get("cover") or ""),
            "duration": t.get("duration") or 0, "isrc": str(t.get("isrc") or ""),
            "track_number": t.get("trackNumber") or 0, "disc_number": t.get("volumeNumber") or 1,
            "date": str(t.get("streamStartDate") or album.get("releaseDate") or "")[:10]}


def search_items(data):
    """Accept the versioned envelopes and bare track-search lists used by HiFi."""
    if isinstance(data, list):
        return [t for x in data if (t := normalize_track(x))]
    if isinstance(data, dict):
        for key in ("tracks", "items", "data"):
            if key in data:
                return search_items(data[key])
    raise SyncError("provider_search_shape_changed")


@dataclass
class Resource:
    url: str
    byte_range: str | None = None


@dataclass
class AudioPlan:
    resources: list[Resource]
    quality: str


def seconds(s):
    m = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", s or "")
    if not m:
        return 0
    h, m, sec = (float(v or 0) for v in m.groups())
    return h * 3600 + m * 60 + sec


def parse_mpd(text, base_url, quality):
    try:
        root = ET.fromstring(text)
        # Strip namespaces only after parsing with external entities disabled.
        for node in root.iter():
            node.tag = node.tag.split("}")[-1]
        if root.tag != "MPD" or root.get("type", "static") != "static":
            raise SyncError("unsupported_dynamic_manifest", 86400)
        periods = root.findall("Period")
        if len(periods) != 1:
            raise SyncError("unsupported_multi_period_manifest", 86400)
        if root.find(".//ContentProtection") is not None:
            raise SyncError("encrypted_audio_unsupported", 86400)
        period = periods[0]
        candidates = []
        for adaptation in period.findall("AdaptationSet"):
            for rep in adaptation.findall("Representation"):
                codec = (rep.get("codecs") or adaptation.get("codecs") or "").lower()
                if "flac" in codec:
                    candidates.append((int(rep.get("audioSamplingRate") or adaptation.get("audioSamplingRate") or 0),
                                       int(rep.get("bandwidth") or 0), adaptation, rep))
        if not candidates:
            raise SyncError("lossless_representation_unavailable", 86400)
        _, _, adaptation, rep = max(candidates, key=lambda x: x[:2])
        chain = [root, period, adaptation, rep]
        resolved = base_url
        for n in chain:
            b = n.find("BaseURL")
            if b is not None and b.text:
                resolved = urljoin(resolved, b.text.strip())
        resources = []
        lists = [n.find("SegmentList") for n in chain if n.find("SegmentList") is not None]
        templates = [n.find("SegmentTemplate") for n in chain if n.find("SegmentTemplate") is not None]
        if lists:
            segment_list = lists[-1]
            init = segment_list.find("Initialization")
            if init is not None:
                resources.append(Resource(https(urljoin(resolved, init.get("sourceURL", ""))), init.get("range")))
            for seg in segment_list.findall("SegmentURL"):
                resources.append(Resource(https(urljoin(resolved, seg.get("media", ""))), seg.get("mediaRange")))
        elif templates:
            attrs = {}
            timeline = None
            for t in templates:
                attrs.update(t.attrib)
                if t.find("SegmentTimeline") is not None:
                    timeline = t.find("SegmentTimeline")
            timescale = int(attrs.get("timescale", 1))
            total_duration = seconds(period.get("duration")) or seconds(root.get("mediaPresentationDuration"))
            total_units = math.ceil(total_duration * timescale)

            def render(pattern, number=0, timestamp=0):
                pattern = pattern.replace("$$", "\0")
                values = {"RepresentationID": rep.get("id", ""), "Bandwidth": rep.get("bandwidth", "0"),
                          "Number": number, "Time": timestamp}
                def substitute(m):
                    value = values[m[1]]
                    return str(value).zfill(int(m[2] or 0))
                pattern = re.sub(r"\$(RepresentationID|Bandwidth|Number|Time)(?:%0(\d+)d)?\$", substitute, pattern)
                if "$" in pattern:
                    raise SyncError("unsupported_manifest_template", 86400)
                return https(urljoin(resolved, pattern.replace("\0", "$")))

            if attrs.get("initialization"):
                resources.append(Resource(render(attrs["initialization"])))
            number, timestamp = int(attrs.get("startNumber", 1)), 0
            entries = list(timeline) if timeline is not None else []
            if not entries:
                duration = int(attrs.get("duration", 0))
                if not duration or not total_units:
                    raise SyncError("unsupported_manifest_timing", 86400)
                entries = [ET.fromstring(f'<S d="{duration}" r="{math.ceil(total_units / duration) - 1}"/>')]
            for i, entry in enumerate(entries):
                timestamp = int(entry.get("t", timestamp))
                duration = int(entry.get("d", 0))
                repeats = int(entry.get("r", 0))
                if duration <= 0 or repeats < -1:
                    raise SyncError("invalid_manifest_timing", 86400)
                if repeats == -1:
                    boundary = int(entries[i + 1].get("t", 0)) if i + 1 < len(entries) else total_units
                    if boundary <= timestamp:
                        raise SyncError("unsupported_manifest_timing", 86400)
                    repeats = math.ceil((boundary - timestamp) / duration) - 1
                if repeats > 20000 or len(resources) + repeats + 1 > 20000:
                    raise SyncError("manifest_segment_limit", 86400)
                for _ in range(repeats + 1):
                    resources.append(Resource(render(attrs["media"], number, timestamp)))
                    number, timestamp = number + 1, timestamp + duration
        elif resolved != base_url:
            resources = [Resource(https(resolved))]
        else:
            raise SyncError("unsupported_manifest_layout", 86400)
        if not resources or len(resources) > 20000:
            raise SyncError("invalid_manifest_segments", 86400)
        return AudioPlan(resources, quality)
    except SyncError:
        raise
    except Exception:
        raise SyncError("invalid_dash_manifest", 86400) from None


def decode_manifest(manifest, base_url, quality):
    if isinstance(manifest, str):
        text = manifest.strip()
        if not text.startswith(("<", "{")):
            try:
                text = base64.b64decode(text, validate=True).decode()
            except Exception:
                raise SyncError("unsupported_manifest_encoding", 86400) from None
        if text.startswith("<"):
            return parse_mpd(text, base_url, quality)
        try:
            manifest = json.loads(text)
        except ValueError:
            raise SyncError("unsupported_manifest_format", 86400) from None
    if not isinstance(manifest, dict):
        raise SyncError("unsupported_manifest_format", 86400)
    if str(manifest.get("encryptionType", "NONE")).upper() not in ("NONE", "", "NULL") or manifest.get("keyId"):
        raise SyncError("encrypted_audio_unsupported", 86400)
    codec = str(manifest.get("codecs") or manifest.get("codec") or "").lower()
    if codec and "flac" not in codec:
        raise SyncError("lossless_representation_unavailable", 86400)
    urls = manifest.get("urls") or []
    if not urls:
        raise SyncError("unsupported_manifest_format", 86400)
    # BTS URLs are mirrors for a single file, not concatenated segments.
    return AudioPlan([Resource(https(urls[0]))], quality)


class Monochrome:
    def __init__(self, root: Path, settings=None, session=None):
        self.root, self.settings = root, settings or {}
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "YouTubeLikesSync/0.1"})
        self.instances = None
        self.cooldowns = {}

    def load_instances(self):
        if self.instances is not None:
            return self.instances
        cache = self.root / "instances.json"
        payload = dict(FALLBACK)
        if cache.exists():
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        if not cache.exists() or time.time() - cache.stat().st_mtime > 86400:
            try:
                fresh = get_json(self.session, UPSTREAM)
                if all(isinstance(fresh.get(k), list) and fresh[k] for k in ("api", "streaming")):
                    payload = fresh
                    atomic_write(cache, json.dumps(payload).encode())
            except SyncError:
                pass
        self.instances = {k: [https(u).rstrip("/") for u in self.settings.get(k + "_instances", payload[k])][:12]
                          for k in ("api", "streaming")}
        return self.instances

    def call(self, route, params, kind="api", validator=None):
        last = SyncError("provider_unavailable")
        bases = self.load_instances()[kind]
        for base in list(bases):
            if self.cooldowns.get(base, 0) > time.time():
                last = SyncError("provider_waiting_to_retry", max(300, math.ceil(self.cooldowns[base] - time.time())))
                continue
            try:
                result = get_json(self.session, base + route, params)
                if validator is not None:
                    result = validator(result, base + route)
                # Promote a working instance for subsequent calls in this process.
                bases.remove(base)
                bases.insert(0, base)
                return result, base + route
            except SyncError as error:
                last = error
                if error.code == "provider_rate_limited":
                    self.cooldowns[base] = time.time() + error.delay
                elif error.code in ("provider_network_error", "provider_http_500", "provider_http_502", "provider_http_503", "provider_http_530"):
                    self.cooldowns[base] = time.time() + 300
        raise last

    def search(self, source):
        if source.get("isrc"):
            try:
                exact, _ = self.call("/search/", {"i": source["isrc"]}, validator=lambda d, _: search_items(d))
                exact = [t for t in exact if t.get("isrc", "").upper() == source["isrc"].upper()]
                if exact:
                    return exact
            except SyncError as e:
                if e.code not in ("provider_http_400", "provider_http_404", "provider_search_shape_changed"):
                    raise
        query = " ".join([source.get("title", ""), *source.get("artists", [])]).strip()
        if not source.get("title"):
            return []
        data, _ = self.call("/search/", {"s": query}, validator=lambda d, _: search_items(d))
        return data

    def metadata(self, candidate):
        def parse(data, _):
            raw = data.get("data", data) if isinstance(data, dict) else data
            for entry in raw if isinstance(raw, list) else [raw]:
                track = normalize_track(entry)
                if track and track["id"] == candidate["id"]:
                    return track
            raise SyncError("provider_metadata_shape_changed")
        data, _ = self.call("/info/", {"id": candidate["id"]}, validator=parse)
        return data

    def resolve(self, track_id, quality):
        errors = []
        fmt = "FLAC_HIRES" if quality == "HI_RES_LOSSLESS" else "FLAC"
        # Current trackManifests, then legacy HiFi track endpoint still used by published instances.
        for route, params in (("/trackManifests/", {"id": track_id, "quality": quality, "adaptive": "false", "formats": fmt}),
                              ("/track/", {"id": track_id, "quality": quality})):
            try:
                plan, _ = self.call(route, params, "streaming", validator=lambda d, b: self.parse_response(d, b, quality))
                return plan
            except SyncError as e:
                errors.append(e)
        meaningful = [e for e in errors if not e.code.startswith("provider_http_")]
        raise (meaningful or errors)[-1]

    def parse_response(self, payload, base, quality):
        if isinstance(payload, dict) and "data" in payload:
            return self.parse_response(payload["data"], base, quality)
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and ("manifest" in item or "attributes" in item):
                    return self.parse_response(item, base, quality)
            raise SyncError("provider_manifest_shape_changed")
        if not isinstance(payload, dict):
            raise SyncError("provider_manifest_shape_changed")
        attrs = payload.get("attributes") or payload
        if attrs.get("drmData") or attrs.get("licenseUrl"):
            raise SyncError("encrypted_audio_unsupported", 86400)
        if str(attrs.get("assetPresentation") or attrs.get("trackPresentation") or "FULL").upper() != "FULL":
            raise SyncError("preview_audio_rejected", 86400)
        actual_quality = attrs.get("audioQuality", quality)
        if actual_quality not in ("LOSSLESS", "HI_RES_LOSSLESS", "HIRES_LOSSLESS"):
            raise SyncError("lossless_representation_unavailable", 86400)
        if attrs.get("uri"):
            content, final_url = small_bytes(self.session, attrs["uri"])
            return decode_manifest(content.decode("utf-8-sig"), final_url, actual_quality)
        if attrs.get("manifest"):
            return decode_manifest(attrs["manifest"], base, actual_quality)
        if attrs.get("urls"):
            return decode_manifest(attrs, base, actual_quality)
        raise SyncError("provider_manifest_shape_changed")
