"""Bounded normal-browser verification for explicitly enabled native renewal."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time

from .browser_host import BASE, PORT, Handler, Host, ThreadingHTTPServer
from .common import SyncError, atomic_write, config, dpapi
from .runtime import engine_dir


def renewal_status(root):
    try:
        return json.loads((root/'api-renewal.json').read_text())
    except (OSError, ValueError):
        return {'status':'not_attempted','next_try':0}


def validate_access(root, settings, *, api=None):
    from .state import State
    from .unified import UnifiedAPI
    state = State(root)
    try:
        row = state.db.execute('SELECT candidate,source FROM jobs ORDER BY updated DESC LIMIT 1').fetchone()
    finally:
        state.close()
    if row:
        track = json.loads(row['candidate'] or row['source'])
        try:
            (api or UnifiedAPI(root,settings)).resolve(track,'HI_RES_LOSSLESS')
        except SyncError as error:
            # These responses concern a recording or transport after access was
            # accepted. They do not mean the newly issued credential is invalid.
            if error.code not in ('catalog_audio_unavailable','unified_source_unsupported',
                                  'encrypted_audio_unsupported','encrypted_segments_unsupported',
                                  'hls_audio_unsupported','lossy_audio_rejected'):
                raise


def run_verification(root, *, api=None):
    """Called under the main worker lock. Own and close just this window/server."""
    if not (engine_dir(root)/'verify-api.html').is_file():
        raise SyncError('api_verification_assets_missing')
    binary = Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
    if not binary.is_file():
        raise SyncError('monochrome_browser_missing')
    # Do not attach a new app tab to a manually opened profile that we cannot own.
    env = dict(os.environ, YTLIKES_VERIFY_PROFILE=str(root/'monochrome-browser'))
    probe = subprocess.run(['powershell.exe','-NoProfile','-Command',
        "$p=$env:YTLIKES_VERIFY_PROFILE.Replace('/','\\'); "
        "$found=Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object {"
        "$_.CommandLine -and $_.CommandLine.Replace('/','\\').Contains($p)}; "
        "if ($found) {exit 10}"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
    if probe.returncode:
        raise SyncError('api_verification_window_already_open' if probe.returncode==10 else 'api_verification_launch_failed')
    host = Host(root, launch_browser=False)
    host.native_api = api
    try:
        server = ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    except OSError:
        raise SyncError('api_verification_host_busy') from None
    server.host = host
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    browser = None
    started = False
    try:
        settings = config(root)
        settings['api_migration_pending'] = True
        atomic_write(root/'config.json',json.dumps(settings,indent=2).encode())
        atomic_write(root/'unified-verification-attempt.json',json.dumps({'status':'pending','at':time.time()}).encode())
        thread.start()
        started = True
        browser = subprocess.Popen([str(binary),f'--user-data-dir={root / "monochrome-browser"}',
            '--no-first-run','--no-default-browser-check','--disable-background-timer-throttling',
            '--disable-renderer-backgrounding',f'--app={BASE}/verify-api.html'],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic()+90
        while time.monotonic()<deadline:
            result = json.loads((root/'unified-verification-attempt.json').read_text())
            if result['status'] == 'ok':
                return
            if result['status'] != 'pending':
                raise SyncError(result['status'])
            if browser.poll() is not None:
                raise SyncError('api_verification_window_closed')
            time.sleep(.5)
        raise SyncError('api_verification_timeout')
    except OSError:
        raise SyncError('api_verification_launch_failed') from None
    finally:
        try:
            if browser and browser.poll() is None:
                subprocess.run(['taskkill.exe','/PID',str(browser.pid),'/T','/F'],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                browser.wait(timeout=10)
        finally:
            if started:
                server.shutdown()
            server.server_close()
            settings = config(root)
            settings['api_migration_pending'] = False
            atomic_write(root/'config.json',json.dumps(settings,indent=2).encode())


def renew_if_due(root, settings, *, now=None, force=False, api=None):
    """Check on normal sync ticks; renew ten minutes before expiry by default."""
    if not settings.get('auto_api_renewal'):
        return {'status':'disabled'}
    now = time.time() if now is None else now
    try:
        protected = (root/'unified-api.dpapi').read_bytes()
        auth = json.loads(dpapi(protected,decrypt=True))
    except (OSError, ValueError, SyncError):
        return {'status':'api_configuration_required'}
    if not auth.get('verification_required'):
        return {'status':'verification_not_required'}
    previous = renewal_status(root)
    lead = max(300,min(1800,int(settings.get('api_renewal_lead_seconds',600))))
    try:
        access = json.loads((root/'unified-status.json').read_text()).get('status')
    except (OSError, ValueError):
        access = None
    if not force and auth.get('expires',0) > now+lead and access != 'api_access_required':
        return {'status':'token_valid','expires':auth['expires']}
    if not force and now < previous.get('next_try',0):
        return {'status':'waiting_to_renew','next_try':previous['next_try'],'last_result':previous['status']}
    retry = max(1800,int(settings.get('api_renewal_retry_seconds',1800)))
    # Persist the lease first: a terminated scheduled worker must not reopen a
    # window every five minutes. Its kernel lock protects concurrent attempts.
    result = {'status':'renewing','last_attempt':now,'next_try':now+retry}
    status_path = root/'unified-status.json'
    old_status = status_path.read_bytes() if status_path.exists() else None
    atomic_write(root/'api-renewal.json',json.dumps(result).encode())
    try:
        if api is None:
            run_verification(root)
        else:
            run_verification(root,api=api)
        refreshed = json.loads(dpapi((root/'unified-api.dpapi').read_bytes(),decrypt=True))
        if refreshed.get('expires',0) <= time.time()+60:
            raise SyncError('api_verification_session_invalid')
        if api is None:
            validate_access(root,settings)
        else:
            validate_access(root,settings,api=api)
        result.update(status='renewed',next_try=now+retry,expires=refreshed['expires'])
    except Exception as error:
        # Fixed SyncError codes only; arbitrary browser/network exceptions may
        # contain transport data. Keep the last credential on unsuccessful runs.
        atomic_write(root/'unified-api.dpapi',protected)
        if old_status is not None:
            atomic_write(status_path,old_status)
        else:
            status_path.unlink(missing_ok=True)
        result['status'] = error.code if isinstance(error,SyncError) else 'api_verification_failed'
    atomic_write(root/'api-renewal.json',json.dumps(result).encode())
    return result
