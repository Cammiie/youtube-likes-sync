from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import imageio_ffmpeg
from mutagen.flac import FLAC, Picture
import requests

from .common import SyncError
from .provider import response, small_bytes


def safe_name(value, fallback="Unknown", limit=65):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip().rstrip(". ")
    value = value[:limit].rstrip(". ") or fallback
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
        value = "_" + value
    return value


def destination(output: Path, track):
    artist = safe_name(next(iter(track.get("artists", [])), "Unknown Artist"), "Unknown Artist", 48)
    album = safe_name(track.get("album"), "Unknown Album", 48)
    title = track["title"] + (" (" + track["version"] + ")" if track.get("version") else "")
    number = str(track.get("track_number") or 0).zfill(2)
    name = f"{number} - {safe_name(title, limit=65)} [{track['id']}].flac"
    path = output / artist / album / name
    if len(str(path.resolve())) >= 245:
        name = f"{number} - {safe_name(title, limit=25)} [{track['id']}].flac"
        path = output / artist[:24] / album[:24] / name
    return path


def run_ffmpeg(args, timeout=600):
    try:
        process = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return process
    except (subprocess.SubprocessError, OSError):
        raise SyncError("audio_validation_failed") from None


def validate(path: Path, expected_duration=0):
    try:
        audio = FLAC(path)
        duration = audio.info.length
        if duration <= 0:
            raise ValueError()
        if expected_duration and abs(duration - float(expected_duration)) > max(3, float(expected_duration) * 0.02):
            raise SyncError("audio_duration_mismatch", 86400)
        checked = run_ffmpeg(["-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path),
                              "-map", "0:a:0", "-f", "null", "-"])
        if checked.returncode:
            raise ValueError()
        return audio
    except SyncError:
        raise
    except Exception:
        raise SyncError("invalid_flac_audio") from None


class Downloader:
    def __init__(self, provider, settings):
        self.provider, self.settings = provider, settings
        # Deliberately separate from the authenticated YouTube session.
        self.session = requests.Session()

    def transfer(self, plan, target, deadline):
        total = 0
        try:
            with target.open("wb") as output:
                for resource in plan.resources:
                    # A range resource is fetched independently; credentials are never sent.
                    headers = {}
                    if resource.byte_range:
                        if not re.fullmatch(r"\d+-\d+", resource.byte_range):
                            raise SyncError("invalid_segment_range", 86400)
                        headers["Range"] = "bytes=" + resource.byte_range
                    # Range is scoped to this transfer and cleared even after failure.
                    self.session.headers.update(headers)
                    try:
                        with response(self.session, resource.url) as r:
                            if resource.byte_range and r.status_code != 206:
                                raise SyncError("segment_range_ignored")
                            expected = int(r.headers.get("Content-Length", 0))
                            received = 0
                            for chunk in r.iter_content(256 * 1024):
                                received += len(chunk)
                                total += len(chunk)
                                if total > self.settings["max_download_bytes"]:
                                    raise SyncError("download_size_limit", 86400)
                                if time.monotonic() > deadline:
                                    raise SyncError("download_time_limit")
                                output.write(chunk)
                            if expected and received != expected:
                                raise SyncError("incomplete_download")
                    finally:
                        self.session.headers.pop("Range", None)
                output.flush()
                os.fsync(output.fileno())
            if not total:
                raise SyncError("empty_download")
        except requests.RequestException:
            raise SyncError("download_network_error") from None
        except OSError:
            raise SyncError("download_disk_error") from None

    def tag(self, path, track, video_id):
        audio = FLAC(path)
        audio["title"] = track["title"] + (" (" + track["version"] + ")" if track.get("version") else "")
        audio["artist"] = track.get("artists") or ["Unknown Artist"]
        audio["album"] = track.get("album") or "Unknown Album"
        audio["tracknumber"] = str(track.get("track_number") or 0)
        audio["discnumber"] = str(track.get("disc_number") or 1)
        if track.get("date"):
            audio["date"] = track["date"]
        if track.get("isrc"):
            audio["isrc"] = track["isrc"]
        audio["monochrome_track_id"] = str(track["id"])
        audio["youtube_video_id"] = video_id
        cover = track.get("cover")
        if cover and re.fullmatch(r"[a-fA-F0-9-]{32,40}", cover):
            try:
                image, _ = small_bytes(self.session, "https://resources.tidal.com/images/" + cover.replace("-", "/") + "/640x640.jpg",
                                       limit=8 * 1024**2)
                if image.startswith(b"\xff\xd8\xff"):
                    picture = Picture()
                    picture.type, picture.mime, picture.data = 3, "image/jpeg", image
                    audio.clear_pictures()
                    audio.add_picture(picture)
            except SyncError:
                # Missing optional artwork never forces a duplicate audio download.
                pass
        audio.save()

    def existing(self, path, track):
        if not path.exists():
            return False
        try:
            audio = validate(path, track.get("duration"))
            if audio.get("monochrome_track_id") == [str(track["id"])]:
                return True
        except SyncError:
            pass
        raise SyncError("existing_file_conflict", 86400)

    def resolve(self, track, quality):
        return self.provider.resolve(track['id'], quality)

    def input_options(self, plan):
        return []

    def download(self, track, video_id, target):
        if self.existing(target, track):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        # This directory belongs only to this catalog id; the worker lock prevents overlap.
        staging = target.parent / (".ytlikes-" + str(track["id"]))
        staging.mkdir(exist_ok=True)
        source, final = staging / "audio.part", staging / "tagged.flac"
        deadline = time.monotonic() + self.settings["max_job_seconds"]
        last = SyncError("lossless_unavailable", 86400)
        try:
            for quality in ("HI_RES_LOSSLESS", "LOSSLESS"):
                source.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
                try:
                    plan = self.resolve(track, quality)
                    self.transfer(plan, source, deadline)
                    input_options = self.input_options(plan)
                    probe = run_ffmpeg([*input_options, "-i", str(source)], timeout=60)
                    info = probe.stderr.decode("utf-8", errors="replace")
                    # Require actual decoded stream identification, never catalog claims.
                    if not re.search(r"Stream #\d+:\d+[^\r\n]*: Audio: flac(?:[ ,(]|$)", info):
                        raise SyncError("lossy_audio_rejected", 86400)
                    remux = run_ffmpeg(["-y", "-v", "error", "-xerror", *input_options, "-i", str(source), "-map", "0:a:0",
                                        "-c:a", "copy", "-f", "flac", str(final)])
                    if remux.returncode:
                        raise SyncError("flac_remux_failed")
                    validate(final, track.get("duration"))
                    self.tag(final, track, video_id)
                    validate(final, track.get("duration"))
                    with final.open("r+b") as f:
                        os.fsync(f.fileno())
                    # On Windows rename refuses to overwrite an existing destination.
                    if target.exists():
                        raise SyncError("existing_file_conflict", 86400)
                    os.rename(final, target)
                    return target
                except SyncError as error:
                    last = error
                    if error.code in ("existing_file_conflict", "download_disk_error", "download_size_limit", "download_time_limit", "api_access_required", "provider_rate_limited"):
                        raise
            raise last
        except OSError:
            raise SyncError("download_disk_error") from None
        finally:
            source.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            try:
                staging.rmdir()
            except OSError:
                pass
