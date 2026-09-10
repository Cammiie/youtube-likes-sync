"""Loopback-only job handoff to Monochrome's browser download engine."""
from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import threading
import time
import sys
from urllib.parse import urlsplit, unquote

from .common import RunLock, atomic_write, config, data_dir, dpapi
from .runtime import command, engine_dir

PORT = 18767
BASE = f"http://localhost:{PORT}"
ERRORS = {'encrypted_audio_unsupported', 'monochrome_verification_required', 'monochrome_lossless_unavailable',
          'monochrome_download_failed', 'monochrome_engine_load_failed', 'monochrome_cancelled', 'monochrome_verification_failed'}
ERRORS.update({'monochrome_verification_script_failed', 'monochrome_verification_timeout',
               'monochrome_verification_widget_failed', 'monochrome_verification_exchange_failed',
               'monochrome_verification_response_invalid'})
ERRORS.add('provider_rate_limited')
ERRORS.add('monochrome_browser_closed')


def browser_status(root):
    try:
        return json.loads((root / 'browser-status.json').read_text())
    except (OSError, ValueError):
        return {'attention':None, 'next_try':0, 'notified':False}


class ThreadingHTTPServer(_ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Browser shutdown can reset loopback connections. Never print raw
        # handler exceptions from the quiet scheduled worker.
        pass


class Host:
    def __init__(self, root, *, launch_browser=True):
        self.root = root
        self.dist = engine_dir(root)
        self.spool = root / 'bridge-downloads'
        self.spool.mkdir(parents=True, exist_ok=True)
        key_path = root / 'bridge-key.dpapi'
        if key_path.exists():
            self.key = dpapi(key_path.read_bytes(), decrypt=True).decode()
        else:
            self.key = secrets.token_urlsafe(32)
            atomic_write(key_path, dpapi(self.key.encode()))
        self.jobs = {}
        self.pending = queue.Queue()
        self.guard = threading.RLock()
        self.last_worker = 0
        self.attention = None
        self.last_diagnostic = None
        self.browser = None
        self.mode = 'stopped'
        self.launch_browser = launch_browser
        self.desktop = None
        self.tray = None
        self.stopping = threading.Event()
        self.verification_requested = False
        self.verification_force = False
        self.kick_at = 0
        self.policy = browser_status(root)
        self.attention = self.policy.get('attention')
        for path in self.spool.glob('*.audio'):
            if len(path.stem) == 32 and time.time() - path.stat().st_mtime > 86400:
                path.unlink(missing_ok=True)

    def open_browser(self, visible=False):
        if not self.launch_browser or not config(self.root).get('browser_launch_allowed', False):
            return
        with self.guard:
            from .browser_desktop import BrowserWindow
            import psutil
            if self.desktop is None:
                self.desktop = BrowserWindow(self.root / 'monochrome-browser')
            mode = 'visible' if visible else config(self.root).get('browser_window_mode', 'minimized')
            if mode not in ('tray', 'minimized', 'visible'):
                mode = 'minimized'
            if self.desktop.alive() or self.desktop.recover():
                self.desktop.apply(mode, user_requested=visible)
                self.mode = mode
                if visible and self.attention:
                    self.verification_requested = True
                return
            candidates = [Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
                          Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Microsoft/Edge/Application/msedge.exe']
            binary = next((p for p in candidates if p.exists()), None)
            if binary is None:
                self.attention = 'monochrome_browser_missing'
                return
            profile = self.root / 'monochrome-browser'
            args = [str(binary), f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check',
                    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
                    '--disable-backgrounding-occluded-windows']
            args += ['--start-minimized']
            args += [f'--app={BASE}/worker.html']
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 7  # Minimize without activating, then hide for tray mode.
            self.browser = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup)
            self.desktop.bind(psutil.Process(self.browser.pid))
            self.desktop.desired = mode
            self.mode = 'opening' if visible else mode
            if visible:
                self.verification_requested = bool(self.attention)
            # The monitor applies the desired state as soon as Edge creates its HWND.

    def save_policy(self):
        atomic_write(self.root/'browser-status.json', json.dumps(self.policy).encode())

    def require_attention(self, code, delay=1800):
        with self.guard:
            if not self.policy.get('attention'):
                self.policy['notified'] = False
            self.attention = code
            self.policy.update(attention=code, next_try=max(self.policy.get('next_try',0), time.time()+max(1800,delay)))
            self.save_policy()

    def verified(self):
        with self.guard:
            previous = self.policy.get('attention')
            self.attention = None
            self.policy.update(attention=None, next_try=0, notified=False, verified_at=time.time())
            self.save_policy()
        if previous:
            from .state import State
            state = State(self.root)
            with state.db:
                state.db.execute("UPDATE jobs SET next_try=0 WHERE state='pending' AND error LIKE 'monochrome_verification%'")
            state.close()
            self.kick_at = time.time()+5

    def action(self, name):
        from .state import State
        if name == 'open':
            self.open_browser(visible=True)
            return
        if name == 'hide':
            if self.desktop:
                self.mode = config(self.root).get('browser_window_mode','minimized')
                self.desktop.apply(self.mode)
            return
        state = State(self.root)
        try:
            if name in ('pause','resume','quit'):
                state.set('paused',name != 'resume')
            if name == 'retry':
                state.retry()
                self.policy['next_try'] = 0
                self.save_policy()
            if name in ('resume','retry'):
                self.kick_at = time.time()+1
            if name == 'quit':
                self.stopping.set()
        finally:
            state.close()

    def monitor(self, server):
        from .state import State
        state = State(self.root)
        while not self.stopping.wait(.5):
            try:
                if self.desktop and self.desktop.alive():
                    self.desktop.apply(self.desktop.desired, user_requested=False)
                    # A visible request made during startup is applied only once.
                    if self.desktop.desired == 'visible' and self.mode == 'opening':
                        if self.desktop.apply('visible', user_requested=True):
                            self.mode = 'visible'
                elif self.desktop and self.mode != 'stopped':
                    with self.guard:
                        for job in self.jobs.values():
                            if job['state'] in ('claimed','running'):
                                job.update(state='failed',code='monochrome_browser_closed')
                    self.mode = 'stopped'
                if self.tray:
                    paused = state.get('paused',False)
                    with self.guard:
                        busy = any(j['state'] in ('running','receiving') for j in self.jobs.values())
                    self.tray.update('Paused' if paused else 'Verification needed' if self.attention else 'Downloading' if busy else 'Ready')
                    if self.attention and self.attention.startswith('monochrome_verification') and not self.policy.get('notified'):
                        if self.tray.notify():
                            self.policy['notified'] = True
                            self.save_policy()
                if self.kick_at and time.time() >= self.kick_at:
                    self.kick_at = 0
                    subprocess.Popen(command('ytlikes.cli', 'sync', '--quiet'),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                self.attention = 'monochrome_window_control_failed'
        state.close()
        if self.desktop:
            self.desktop.close()
        if self.tray:
            self.tray.stop()
        server.shutdown()

    def submit(self, track, max_bytes, timeout_ms):
        if not config(self.root).get('browser_launch_allowed', False) and self.launch_browser:
            raise ValueError('browser_disabled')
        if self.launch_browser and time.time() < self.policy.get('next_try',0):
            raise ValueError(self.policy.get('attention') or 'monochrome_verification_required')
        if not isinstance(track, dict) or not str(track.get('id', '')).isdigit():
            raise ValueError('invalid_track')
        with self.guard:
            self.expire_jobs()
            if any(j['state'] in ('queued', 'claimed', 'running', 'receiving') for j in self.jobs.values()):
                raise ValueError('busy')
            id = secrets.token_hex(16)
            job = {'id':id, 'track':track, 'max_bytes':min(int(max_bytes), 2*1024**3),
                   'allow_encrypted_lossless':config(self.root).get('allow_encrypted_lossless') is True,
                   'timeout_ms':min(int(timeout_ms), 1200000), 'state':'queued', 'code':None,
                   'path':self.spool / f'{id}.audio', 'created':time.time(), 'cancelled':False}
            self.jobs[id] = job
            self.pending.put(id)
        if time.time() - self.last_worker > 20 or (self.launch_browser and (not self.desktop or not self.desktop.alive())):
            self.open_browser()
        return id

    def expire_jobs(self):
        # A killed watcher cannot DELETE its job. Reclaim its lease so the next
        # scheduled run can recover from SQLite instead of remaining busy forever.
        with self.guard:
            for id, job in list(self.jobs.items()):
                if time.time() - job['created'] <= job['timeout_ms'] / 1000 + 60:
                    if job['state'] == 'claimed' and time.time() - job['claimed_at'] > 15:
                        job.update(state='queued', delivery_id=None)
                        self.pending.put(id)
                    continue
                job['cancelled'] = True
                try:
                    job['path'].unlink(missing_ok=True)
                except PermissionError:
                    pass  # The receiving handler owns cleanup of an open file.
                del self.jobs[id]


class Handler(BaseHTTPRequestHandler):
    server_version = 'YouTubeLikesSync'

    @property
    def host(self):
        return self.server.host

    def log_message(self, *args):
        pass  # No URLs, request headers, tokens, or upstream logs.

    def valid_host(self):
        return self.headers.get('Host') in (f'localhost:{PORT}', f'127.0.0.1:{PORT}')

    def authorized(self):
        if not self.valid_host():
            return False
        origin = self.headers.get('Origin')
        if origin and origin != BASE:
            return False
        key = self.headers.get('X-Bridge-Key', '')
        if not key:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                key = cookie['ytlikes_bridge'].value if 'ytlikes_bridge' in cookie else ''
            except Exception:
                return False
        return secrets.compare_digest(key, self.host.key)

    def reply(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def small_json(self):
        size = int(self.headers.get('Content-Length', 0))
        if not 0 <= size <= 65536:
            raise ValueError('request_size')
        return json.loads(self.rfile.read(size) or b'{}')

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.valid_host():
            return self.reply({'error':'host_rejected'}, 403)
        if path.startswith(('/jobs/', '/worker/', '/result/')) or path == '/health':
            if not self.authorized():
                return self.reply({'error':'unauthorized'}, 403)
            if path == '/health':
                return self.reply({'service':'ytlikes-monochrome', 'engine':'upstream', 'browser':self.host.mode,
                                   'worker_ready':time.time()-self.host.last_worker < 30, 'attention':self.host.attention,
                                   'window':self.host.desktop.snapshot() if self.host.desktop else None,
                                   'tray_ready':bool(self.host.tray and self.host.tray.ready),
                                   'next_try':self.host.policy.get('next_try',0),
                                   'last_diagnostic':self.host.last_diagnostic})
            if path == '/worker/control':
                self.host.last_worker = time.time()
                requested = self.host.verification_requested
                force = self.host.verification_force
                self.host.verification_requested = False
                self.host.verification_force = False
                return self.reply({'verify':requested, 'force_refresh':force})
            if path == '/worker/next':
                self.host.last_worker = time.time()
                self.host.expire_jobs()
                try:
                    id = self.host.pending.get(timeout=10)
                except queue.Empty:
                    return self.reply(None)
                with self.host.guard:
                    job = self.host.jobs.get(id)
                    if not job or job['cancelled']:
                        return self.reply(None)
                    if job['state'] != 'queued':
                        return self.reply(None)
                    job.update(state='claimed', claimed_at=time.time(), delivery_id=secrets.token_hex(16))
                    return self.reply({k:job[k] for k in ('id','track','max_bytes','timeout_ms','delivery_id','allow_encrypted_lossless')})
            id = path.rsplit('/', 1)[-1]
            with self.host.guard:
                job = self.host.jobs.get(id)
                if not job:
                    return self.reply({'error':'unknown_job'}, 404)
                if path.startswith('/jobs/'):
                    return self.reply({k:job[k] for k in ('id','state','code')})
                if path.startswith('/result/') and job['state'] == 'done':
                    with job['path'].open('rb') as f:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(job['path'].stat().st_size))
                        self.end_headers()
                        while chunk := f.read(256*1024):
                            self.wfile.write(chunk)
                    return
            return self.reply({'error':'not_ready'}, 409)
        # Only the prebuilt upstream bundle is served, never source/runtime data.
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.reply({'error':'cross_site_rejected'}, 403)
        if path == '/worker.html' and config(self.host.root).get('api_migration_pending'):
            from .unified import migration_page
            body = migration_page(self.host.root)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Set-Cookie', f'ytlikes_bridge={self.host.key}; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(body)
            return
        relative = unquote(path).lstrip('/') or 'worker.html'
        target = (self.host.dist / relative).resolve()
        if not target.is_relative_to(self.host.dist.resolve()) or not target.is_file():
            return self.reply({'error':'not_found'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(target.stat().st_size))
        self.send_header('Cache-Control', 'no-store' if target.suffix == '.html' else 'public,max-age=3600')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if target.suffix == '.html':
            self.send_header('Set-Cookie', f'ytlikes_bridge={self.host.key}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        with target.open('rb') as f:
            while chunk := f.read(256*1024):
                self.wfile.write(chunk)

    def do_POST(self):
        if not self.authorized():
            return self.reply({'error':'unauthorized'}, 403)
        path = urlsplit(self.path).path
        try:
            if path.startswith('/worker/result/'):
                id = path.rsplit('/', 1)[-1]
                with self.host.guard:
                    job = self.host.jobs.get(id)
                    if not job or job['state'] != 'running' or job['cancelled']:
                        return self.reply({'error':'unknown_job'}, 404)
                    size = int(self.headers.get('Content-Length', 0))
                    if size <= 0 or size > job['max_bytes']:
                        return self.reply({'error':'size_limit'}, 413)
                    job['state'] = 'receiving'
                self.connection.settimeout(30)
                remaining = size
                try:
                    with job['path'].open('wb') as f:
                        while remaining:
                            chunk = self.rfile.read(min(256*1024, remaining))
                            if not chunk or job['cancelled']:
                                raise ValueError('incomplete_upload')
                            f.write(chunk)
                            remaining -= len(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    with self.host.guard:
                        if job['cancelled']:
                            raise ValueError('cancelled')
                        job['state'] = 'done'
                except Exception:
                    job['path'].unlink(missing_ok=True)
                    job.update(state='failed', code='monochrome_download_failed')
                    raise
                return self.reply({'ok':True})
            body = self.small_json()
            if path == '/migration/failure':
                with self.host.guard:
                    settings = config(self.host.root)
                    if not settings.get('api_migration_pending'):
                        return self.reply({'error':'migration_closed'}, 409)
                    code = body.get('code')
                    allowed = ERRORS | {'api_verification_exchange_failed'}
                    result = {'status':code if code in allowed else 'monochrome_verification_failed', 'at':time.time()}
                    if re.fullmatch(r'\d{6}', str(body.get('turnstile_code', ''))):
                        result['turnstile_code'] = body['turnstile_code']
                    atomic_write(self.host.root/'unified-verification-attempt.json', json.dumps(result).encode())
                    settings['api_migration_pending'] = False
                    atomic_write(self.host.root/'config.json', json.dumps(settings, indent=2).encode())
                return self.reply({'ok':True})
            if path in ('/migration/session', '/migration/exchange'):
                from .unified import save_session, UnifiedAPI
                from .common import SyncError
                with self.host.guard:
                    settings = config(self.host.root)
                    if not settings.get('api_migration_pending'):
                        return self.reply({'error':'migration_closed'}, 409)
                    try:
                        if path == '/migration/exchange':
                            client = getattr(self.host,'native_api',None) or UnifiedAPI(self.host.root, settings)
                            client.exchange_verification(body.get('response'))
                        else:
                            save_session(self.host.root, body, require_live=True)
                    except SyncError as error:
                        return self.reply({'error':error.code}, 400)
                    settings['api_migration_pending'] = False
                    atomic_write(self.host.root/'config.json', json.dumps(settings, indent=2).encode())
                    atomic_write(self.host.root/'unified-verification-attempt.json',
                                 json.dumps({'status':'ok','at':time.time()}).encode())
                    if isinstance(body.get('browser_api_ok'), bool):
                        atomic_write(self.host.root/'unified-verification.json',
                                     json.dumps({'browser_api_ok':body['browser_api_ok'], 'verified_at':time.time()}).encode())
                return self.reply({'ok':True})
            if path.startswith('/worker/started/'):
                id = path.rsplit('/', 1)[-1]
                with self.host.guard:
                    self.host.expire_jobs()
                    job = self.host.jobs.get(id)
                    if not job or job['state'] != 'claimed' or body.get('delivery_id') != job.get('delivery_id'):
                        return self.reply({'error':'delivery_expired'}, 409)
                    job['state'] = 'running'
                return self.reply({'ok':True})
            if path == '/jobs':
                try:
                    id = self.host.submit(body['track'], body['max_bytes'], body['timeout_ms'])
                except ValueError as error:
                    code = str(error)
                    if code not in ERRORS:
                        raise
                    return self.reply({'error':code}, 409)
                return self.reply({'id':id})
            if path == '/open':
                if not config(self.host.root).get('browser_launch_allowed', False):
                    return self.reply({'error':'browser_disabled'}, 403)
                self.host.open_browser(visible=True)
                return self.reply({'ok':True})
            if path == '/hide':
                self.host.action('hide')
                return self.reply({'ok':True})
            if path == '/verify':
                self.host.verification_requested = True
                self.host.verification_force = body.get('force_refresh') is True
                return self.reply({'ok':True})
            if path == '/worker/retry':
                self.host.action('retry')
                return self.reply({'ok':True})
            if path == '/worker/verified':
                self.host.verified()
                return self.reply({'ok':True})
            if path == '/worker/attention':
                if body.get('code') in ERRORS:
                    self.host.require_attention(body['code'])
                return self.reply({'ok':True})
            if path.startswith('/worker/error/'):
                id = path.rsplit('/', 1)[-1]
                with self.host.guard:
                    job = self.host.jobs.get(id)
                    if job and not job['cancelled']:
                        job.update(state='failed', code=body.get('code') if body.get('code') in ERRORS else 'monochrome_download_failed')
                        if job['code'].startswith('monochrome_verification') or job['code'] == 'provider_rate_limited':
                            self.host.require_attention(job['code'], min(86400,max(1800,float(body.get('retry_after',1800)))))
                        diagnostic = body.get('diagnostic')
                        if isinstance(diagnostic, dict) and isinstance(diagnostic.get('track_request_sent'), bool):
                            self.host.last_diagnostic = {'code':job['code'], 'track_request_sent':diagnostic['track_request_sent']}
                            if re.fullmatch(r'\d{6}', str(diagnostic.get('turnstile_code', ''))):
                                self.host.last_diagnostic['turnstile_code'] = diagnostic['turnstile_code']
                            status = diagnostic.get('track_http_status')
                            if isinstance(status, int) and 100 <= status <= 599:
                                self.host.last_diagnostic['track_http_status'] = status
                            if diagnostic.get('audio_source') in ('tidal','amazon','mono','monochrome','deezer','qobuz','unknown'):
                                self.host.last_diagnostic['audio_source'] = diagnostic['audio_source']
                            if diagnostic.get('stage') in ('resolving','track_lookup','resource_received','resource_missing','local_upload'):
                                self.host.last_diagnostic['stage'] = diagnostic['stage']
                return self.reply({'ok':True})
            return self.reply({'error':'not_found'}, 404)
        except (ValueError, KeyError, OSError):
            return self.reply({'error':'invalid_request'}, 400)

    def do_DELETE(self):
        if not self.authorized():
            return self.reply({'error':'unauthorized'}, 403)
        id = urlsplit(self.path).path.rsplit('/',1)[-1]
        with self.host.guard:
            job = self.host.jobs.pop(id, None)
            if job:
                job['cancelled'] = True
                try:
                    job['path'].unlink(missing_ok=True)
                except PermissionError:
                    pass  # An in-progress upload removes its own partial file.
        return self.reply({'ok':True})


def main():
    root = data_dir()
    with RunLock(root / 'bridge-host'):
        host = Host(root)
        server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
        server.daemon_threads = True
        server.host = host
        from .browser_desktop import TrayController, BrowserWindow
        host.desktop = BrowserWindow(root/'monochrome-browser')
        if host.desktop.recover():
            host.mode = config(root).get('browser_window_mode','minimized')
            host.desktop.apply(host.mode)
        host.tray = TrayController(host.action)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=host.monitor, args=(server,), daemon=True).start()
        try:
            host.tray.run()
        finally:
            host.stopping.set()
            if host.desktop:
                host.desktop.close()
            server.server_close()


if __name__ == '__main__':
    main()
