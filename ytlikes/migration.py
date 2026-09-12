"""Idempotent browserless migration. Caller must hold the runtime RunLock."""
import json
import time

import requests

from .common import SyncError, atomic_write


def stop_legacy_host(root):
    # Never call ensure_host: migration must not start a browser or host.
    if not (root/'bridge-key.dpapi').exists():
        return
    from .browser_download import bridge_session, BASE
    with bridge_session(root) as session:
        try:
            health = session.get(BASE+'/health', timeout=2)
        except requests.ConnectionError:
            return
        except requests.RequestException:
            raise SyncError('legacy_helper_quit_required') from None
        if health.status_code == 403:
            return  # Another runtime's bridge; it is not ours to stop.
        try:
            if health.status_code != 200 or health.json().get('service') != 'ytlikes-monochrome':
                raise SyncError('legacy_helper_quit_required')
            stopped = session.post(BASE+'/shutdown', json={}, timeout=3)
            if stopped.status_code != 200:
                raise SyncError('legacy_helper_quit_required')
            for _ in range(20):
                try:
                    session.get(BASE+'/health', timeout=.5)
                except requests.ConnectionError:
                    return
                time.sleep(.1)
            raise SyncError('legacy_helper_quit_required')
        except (ValueError, requests.RequestException):
            raise SyncError('legacy_helper_quit_required') from None


def migrate(state, root, settings):
    if state.get('browserless_migration') == 1:
        return settings
    stop_legacy_host(root)
    updated = dict(settings, download_engine='antra_tidal', browser_launch_allowed=False,
                   auto_api_renewal=False, api_migration_pending=False)
    # A crash after the config write is safe: the idempotent DB phase runs again.
    atomic_write(root/'config.json', json.dumps(updated, indent=2).encode())
    with state.db:
        state.db.execute("""UPDATE jobs SET next_try=0, error=NULL
            WHERE state='pending' AND (error LIKE 'monochrome_verification%'
            OR error IN ('browser_mode_validation_required','browser_disabled','api_access_required'))""")
        state.db.execute("INSERT OR REPLACE INTO meta VALUES ('browserless_migration','1')")
    return updated
