from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import time

import requests

from .browser_host import BASE, browser_status
from .common import SyncError, data_dir, dpapi, config, atomic_write
import json
from .media import Downloader, run_ffmpeg, validate


def bridge_session(root):
    s = requests.Session()
    s.trust_env = False
    try:
        s.headers['X-Bridge-Key'] = dpapi((root / 'bridge-key.dpapi').read_bytes(), decrypt=True).decode()
    except FileNotFoundError:
        pass
    return s


def reset_browser_retry(root):
    policy = browser_status(root)
    policy['next_try'] = 0
    atomic_write(root/'browser-status.json',json.dumps(policy).encode())
    # Notify an existing host; a CLI retry must never open a new browser itself.
    try:
        bridge_session(root).post(BASE+'/worker/retry',json={},timeout=2)
    except requests.RequestException:
        pass


def ensure_host(root=None):
    root = root or data_dir()
    if not config(root).get('browser_launch_allowed', False):
        raise SyncError('browser_disabled', 86400)
    if not (root / 'monochrome/ytlikes-dist/worker.html').is_file():
        raise SyncError('monochrome_install_required', 86400)
    s = bridge_session(root)
    try:
        if s.get(BASE + '/health', timeout=2).json().get('service') == 'ytlikes-monochrome':
            return s
    except (requests.RequestException, ValueError):
        pass
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    subprocess.Popen([str(pythonw), '-m', 'ytlikes.browser_host'], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.monotonic()+20
    while time.monotonic()<deadline:
        time.sleep(.3)
        s = bridge_session(root)
        try:
            r=s.get(BASE+'/health',timeout=2)
            if r.ok and r.json().get('service')=='ytlikes-monochrome':
                return s
        except (requests.RequestException, ValueError):
            continue
    raise SyncError('monochrome_helper_unavailable')


class BrowserDownloader(Downloader):
    """Upstream supplies original bytes; native validation/tagging owns final files."""
    def download(self, track, video_id, target):
        if self.existing(target, track):
            return target
        if self.settings.get('browser_downloads_ready') is False:
            raise SyncError('browser_mode_validation_required',1800)
        policy = browser_status(data_dir())
        if time.time() < policy.get('next_try',0):
            raise SyncError(policy.get('attention') or 'monochrome_verification_required',
                            max(1800,policy['next_try']-time.time()))
        s = ensure_host()
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / ('.ytlikes-' + str(track['id']))
        staging.mkdir(exist_ok=True)
        source, final = staging / 'audio.part', staging / 'tagged.flac'
        id = None
        try:
            r = s.post(BASE+'/jobs', json={'track':track, 'max_bytes':self.settings['max_download_bytes'],
                        'timeout_ms':self.settings['max_job_seconds']*1000}, timeout=30)
            if not r.ok:
                from .browser_host import ERRORS
                code = r.json().get('error')
                raise SyncError(code if code in ERRORS else 'monochrome_helper_busy',1800)
            id = r.json()['id']
            deadline = time.monotonic()+self.settings['max_job_seconds']+30
            while time.monotonic()<deadline:
                response = s.get(BASE+'/jobs/'+id, timeout=15)
                if response.status_code == 404:
                    raise SyncError('monochrome_helper_restarted')
                response.raise_for_status()
                status = response.json()
                if status.get('state') == 'failed':
                    code = status.get('code') or 'monochrome_download_failed'
                    policy = browser_status(data_dir())
                    delay = max(1800,policy.get('next_try',0)-time.time()) if code.startswith('monochrome_verification') or code == 'provider_rate_limited' else 300
                    raise SyncError(code,delay)
                if status.get('state') == 'done':
                    break
                health = s.get(BASE+'/health',timeout=5).json()
                if health.get('attention') in ('monochrome_engine_load_failed','monochrome_browser_missing'):
                    raise SyncError(health['attention'])
                time.sleep(1)
            else:
                raise SyncError('monochrome_download_timeout')
            with s.get(BASE+'/result/'+id, timeout=(5,30), stream=True) as r:
                r.raise_for_status()
                received=0
                with source.open('wb') as f:
                    for chunk in r.iter_content(256*1024):
                        received+=len(chunk)
                        if received>self.settings['max_download_bytes']:
                            raise SyncError('download_size_limit',86400)
                        f.write(chunk)
            probe=run_ffmpeg(['-i',str(source)],timeout=60)
            if not re.search(r'Stream #\d+:\d+[^\r\n]*: Audio: flac(?:[ ,(]|$)',probe.stderr.decode('utf-8',errors='replace')):
                raise SyncError('lossy_audio_rejected',86400)
            remux=run_ffmpeg(['-y','-v','error','-xerror','-i',str(source),'-map','0:a:0','-c:a','copy','-f','flac',str(final)])
            if remux.returncode:
                raise SyncError('flac_remux_failed')
            validate(final,track.get('duration'))
            self.tag(final,track,video_id)
            validate(final,track.get('duration'))
            with final.open('r+b') as f:
                os.fsync(f.fileno())
            if target.exists():
                raise SyncError('existing_file_conflict',86400)
            os.rename(final,target)
            return target
        except requests.RequestException:
            raise SyncError('monochrome_helper_connection_failed') from None
        except OSError:
            raise SyncError('download_disk_error') from None
        finally:
            if id:
                try:
                    s.delete(BASE+'/jobs/'+id,timeout=5)
                except requests.RequestException:
                    pass
            source.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            try:
                staging.rmdir()
            except OSError:
                pass
