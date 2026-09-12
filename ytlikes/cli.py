from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

from .common import SyncError, RunLock, atomic_write, config, data_dir, load_auth, save_auth
from .antra import AntraCatalog, AntraDownloader, access_status, save_access
from .migration import migrate
from .media import Downloader
from .browser_download import BrowserDownloader, ensure_host, BASE, reset_browser_retry
from .browser_host import browser_status
from .provider import Monochrome, get_json, search_items
from .service import sync
from .state import State
from .youtube import YouTube, parse_headers
from .unified import UnifiedDownloader, save_session, session_status
from .renewal import renewal_status


def emit(value, quiet=False):
    if not quiet and sys.stdout is not None:
        print(json.dumps(value, indent=2, ensure_ascii=True))


def auth_dialog():
    import tkinter as tk
    from tkinter import messagebox
    import webbrowser
    window = tk.Tk()
    window.title("Connect YouTube Music — YouTube Likes Sync")
    window.geometry("790x570")
    window.minsize(700, 500)
    instructions = (
        "Connect your account locally. Your Google password is never needed.\n\n"
        "1. Open YouTube Music below and sign in to the account you want to sync.\n"
        "2. Press F12, open Network, then click Library or Liked Music.\n"
        "3. Filter requests by 'browse' and select a POST request to music.youtube.com.\n"
        "4. Right-click that request → Copy → Copy as fetch (Node.js).\n"
        "5. Paste below and click Connect. Do not paste it into chat.\n\n"
        "Headers stay on this PC, encrypted for your Windows user. The first successful\n"
        "connection records existing likes without downloading them. Reconnecting preserves\n"
        "your original baseline. The background task checks for new likes every five minutes."
    )
    tk.Label(window, text=instructions, justify="left", anchor="w", padx=16, pady=12,
             font=("Segoe UI", 10)).pack(fill="x")
    if config(data_dir()).get('browser_launch_allowed', False):
        tk.Button(window, text="Open YouTube Music", command=lambda: webbrowser.open("https://music.youtube.com")).pack(anchor="w", padx=16)
    box = tk.Text(window, height=10, wrap="word", font=("Consolas", 10))
    box.pack(fill="both", expand=True, padx=16, pady=10)
    result = []

    def connect():
        try:
            result.append(parse_headers(box.get("1.0", "end").strip()))
            box.delete("1.0", "end")
            window.destroy()
        except SyncError:
            messagebox.showerror("Headers not recognized", "Use Copy as fetch (Node.js), or copy Request Headers containing Cookie, from a signed-in YouTube Music browse request.")

    tk.Button(window, text="Connect", command=connect, width=20).pack(pady=(0, 12))
    window.mainloop()
    if not result:
        raise SyncError("setup_cancelled")
    return result[0]


def connect_account(state, root, headers, *, allow_account_change=False):
    songs, account = YouTube(headers).fetch()
    if state.get("baseline_at") is not None and state.get("account") != account and not allow_account_change:
        raise SyncError("different_youtube_account")
    # Saving is atomic and encrypted; failed fetches do not replace working credentials.
    save_auth(root, headers)
    created = state.baseline(songs, account,allow_account_change=allow_account_change)
    state.set("auth_required", False)
    state.set("poll_retry_at", 0)
    state.set("poll_failures", 0)
    state.set("last_error", None)
    return {"status": "connected", "baseline_created": created, "current_likes": len(songs),
            "downloads_started": 0, "note": "Existing baseline preserved." if not created else "Existing likes recorded; only future likes will download."}


def doctor(provider):
    bases = provider.load_instances()["api"]

    def probe(base):
        import requests
        try:
            data = get_json(requests.Session(), base + "/search/", {"s": "Rick Astley Never Gonna Give You Up"})
            tracks = search_items(data)
            return {"server": base, "status": "ok", "matches": len(tracks)}
        except SyncError as e:
            return {"server": base, "status": e.code}
        except Exception:
            return {"server": base, "status": "provider_response_changed"}

    return list(ThreadPoolExecutor(max_workers=4).map(probe, bases))


def scheduler(action):
    from .runtime import assets_dir, app_dir, bundled
    script = assets_dir() / "schedule.ps1"
    extra = ['-Executable', str(app_dir()/'YouTubeLikesSync.exe'), '-WorkingDirectory', str(app_dir())] if bundled() else []
    process = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", action, *extra],
                             capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if process.returncode:
        raise SyncError("scheduler_operation_failed")
    return {"scheduler": action}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Automatically save future YouTube Music likes as validated FLAC.")
    parser.add_argument("command", choices=["setup", "setup-google", "setup-headers", "setup-clipboard", "sync", "dry-run", "status", "retry", "pause", "resume", "doctor",
                                            "install-scheduler", "remove-scheduler", "open-monochrome", "monochrome-status", "configure-api", "api-status", "migrate"])
    parser.add_argument("--data-dir", type=Path, default=data_dir())
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--switch-account", action="store_true",help="Explicitly connect another account while preserving downloaded files and prior baselines.")
    args = parser.parse_args(argv)
    root = args.data_dir
    state = None
    provider = downloader = None
    try:
        if args.command == 'setup':
            from .extension_setup import run_setup
            emit(run_setup(root,allow_account_change=args.switch_account),args.quiet)
            return 0
        if args.command == 'setup-google':
            from .onboarding import run_setup
            emit(run_setup(root,allow_account_change=args.switch_account),args.quiet)
            return 0
        if args.command in ("install-scheduler", "remove-scheduler"):
            if root != data_dir():
                raise SyncError("scheduler_requires_default_data_directory")
            emit(scheduler("Install" if args.command == "install-scheduler" else "Remove"), args.quiet)
            return 0
        state = State(root)
        settings = config(root)
        if args.command == 'api-status':
            emit(access_status(root) if settings['download_engine'] == 'antra_tidal' else session_status(root), args.quiet)
            return 0
        if args.command in ('open-monochrome', 'monochrome-status'):
            if not settings.get('browser_launch_allowed', False):
                if args.command == 'open-monochrome':
                    raise SyncError('browser_disabled', 86400)
                emit({'browser':'disabled', 'api':session_status(root)}, args.quiet)
                return 0
            if root != data_dir():
                raise SyncError('monochrome_requires_default_data_directory')
            session = ensure_host(root)
            if args.command == 'open-monochrome':
                response = session.post(BASE+'/open', json={}, timeout=30)
                if not response.ok:
                    raise SyncError('monochrome_helper_unavailable')
            emit(session.get(BASE+'/health', timeout=5).json(), args.quiet)
            return 0
        if args.command == "status":
            result = state.status()
            result.update({"output": settings["output"], "data_dir": str(root),
                           "connected": (root / "auth.dpapi").exists(), "setup_required": state.get("baseline_at") is None,
                           "download_engine": settings.get('download_engine', 'native'),
                           "browser_launch_allowed": settings.get('browser_launch_allowed') is True,
                           "browser_window_mode":settings.get('browser_window_mode','minimized'),
                           "browser_downloads_ready":settings.get('browser_downloads_ready',True),
                           "browser_status":{'browser':'disabled'} if settings['download_engine'] == 'antra_tidal' else browser_status(root),
                           "api":access_status(root) if settings["download_engine"] == "antra_tidal" else session_status(root),
                           "auto_api_renewal":settings.get('auto_api_renewal') is True,
                           "api_renewal":{'status':'disabled'} if settings['download_engine'] == 'antra_tidal' else renewal_status(root),
                           "allow_encrypted_lossless": settings.get('allow_encrypted_lossless') is True})
            emit(result, args.quiet)
            return 0
        if args.command in ("pause", "resume"):
            state.set("paused", args.command == "pause")
            emit({"status": args.command}, args.quiet)
            return 0
        with RunLock(root):
            if args.command != "dry-run":
                settings = migrate(state, root, settings)
            if args.command == 'configure-api':
                import getpass
                if not sys.stdin.isatty():
                    raise SyncError('api_configuration_requires_local_terminal')
                base = input('Compatible Tidal mirror HTTPS endpoint: ').strip()
                token = getpass.getpass('API credential (hidden; stored with Windows DPAPI): ')
                save_access(root, {'endpoint':base, 'key':token})
                settings.update(download_engine='antra_tidal', browser_launch_allowed=False, api_migration_pending=False)
                atomic_write(root/'config.json', json.dumps(settings, indent=2).encode())
                state.retry()
                result = {'status':'api_configured', 'note':'Access will be checked on the next download; no browser will open.'}
            elif args.command in ("setup-headers", "setup-clipboard"):
                if args.command == 'setup-clipboard':
                    copied = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                        '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw'],
                        capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    headers = parse_headers(copied.stdout.decode('utf-8-sig').strip())
                else:
                    emit({"status": "waiting_for_local_authentication"}, args.quiet)
                    headers = auth_dialog()
                emit({"status": "checking_account_and_complete_likes_playlist"}, args.quiet)
                result = connect_account(state, root, headers)
                if not (root / "config.json").exists():
                    atomic_write(root / "config.json", json.dumps(settings, indent=2).encode())
            elif args.command == "retry":
                state.retry()
                if settings.get('download_engine') == 'monochrome':
                    reset_browser_retry(root)
                atomic_write(root/'api-renewal.json', b'{"status":"retry_requested","next_try":0}')
                state.set("poll_retry_at", 0)
                state.set("poll_failures", 0)
                result = {"status": "pending_tracks_will_retry_on_next_check"}
            elif args.command == "migrate":
                result = {"status": "browserless_ready", "download_engine": settings['download_engine']}
            elif args.command == "doctor":
                provider = AntraCatalog(root, settings)
                result = {"provider": "antra_tidal", "browserless": True,
                          "matches": len(provider.search({"title": "Sextape", "artists": ["Deftones"]}))}
            else:
                if state.get("paused", False):
                    result = {"status": "paused"}
                elif state.get("baseline_at") is None:
                    result = {"status": "setup_required", "downloads_started": 0}
                else:
                    engine = settings.get('download_engine', 'antra_tidal')
                    provider = AntraCatalog(root, settings) if engine == 'antra_tidal' else Monochrome(root, settings)
                    if engine == 'antra_tidal':
                        downloader = AntraDownloader(provider, settings)
                    elif engine == 'unified_native':
                        downloader = UnifiedDownloader(provider, settings, root)
                    elif engine == 'monochrome':
                        downloader = BrowserDownloader(provider, settings)
                    elif engine == 'native':
                        downloader = Downloader(provider, settings)
                    else:
                        raise SyncError('download_engine_invalid')
                    result = sync(state, YouTube(load_auth(root),root=root), provider, downloader, settings,
                                  dry_run=args.command == "dry-run")
        emit(result, args.quiet)
        return 0
    except SyncError as error:
        if state and args.command not in ("dry-run", "status") and error.code != "already_running":
            state.set("last_error", error.code)
        emit({"error": error.code, "help": "Run Connect YouTube Music.cmd locally to connect or reconnect your account."}
             if error.code in ("setup_required", "youtube_auth_required", "credential_decryption_failed") else {"error": error.code}, args.quiet)
        return 0 if error.code in ("already_running", "setup_cancelled", "youtube_auth_required") else 2
    except Exception:
        # Never print third-party tracebacks: their URLs/headers may contain credentials.
        if state and args.command != "dry-run":
            state.set("last_error", "unexpected_error")
        emit({"error": "unexpected_error"}, args.quiet)
        return 2
    finally:
        if downloader:
            downloader.session.close()
        if provider and hasattr(provider, "close"):
            provider.close()
        if state:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
