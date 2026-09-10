# YouTube Likes Sync

Save future YouTube Music likes as matching FLAC tracks on Windows. Likes made on your phone are picked up when this PC is awake and you are signed in.

Existing likes become a baseline when you first connect. They are not downloaded. Unliking a song never deletes a file, and liking it again does not download it twice.

## Install on Windows

Download **YouTubeLikesSync-Setup-0.3.0-windows-x64.exe** from [GitHub Releases](https://github.com/Cammiie/youtube-likes-sync/releases/latest) and run it. The installer includes Python, the built Monochrome engine, and FFmpeg. It registers the local connector and sets up automatic checks. Windows 10/11 x64 and Microsoft Edge are required. No separate Python, Node.js, or Google Cloud setup is needed. The installer is currently unsigned, so Windows may show an unknown-publisher warning.

## Connect with the browser extension

1. Open **Connect YouTube Music** from the installer or Start menu.
2. Choose Brave, Edge, or Chrome in that window and click **Open Extensions**. Turn on **Developer mode**, choose **Load unpacked**, and select the app's `extension` folder. The window can copy the folder path for you.
3. Open YouTube Music in that browser and sign in normally. Select the account you want to sync and open its Library.
4. Click the **YouTube Likes Sync** extension, check the download folder, and click **Connect YouTube Music**. It briefly reloads the selected Music tab, checks every accessible page of likes, then reports **Connected**.

For a different account, explicitly check **Use this as a different sync account** before connecting. Its current likes become a new baseline; existing files, mappings, prior baselines, and the queue remain intact. Reconnecting the same account preserves its baseline.

**No Google Cloud project, OAuth client file, or copied DevTools headers are needed.** The complete connection flow has been tested in Brave, including full baseline retrieval and a repeat check without duplicates. Only future likes are queued. Likes made on a phone work when they belong to that same account.

The extension is included with the Windows app and currently loaded locally, not published in a browser store. Keep the installed app in place so the browser can find the extension. Browser-store publication is a separate step. Advanced users building from source can download the repository ZIP and run Install.cmd; that source-only route requires Python 3.11+ with Tkinter and Node/npm.

## Connection privacy and recovery

The extension has access only to `music.youtube.com`. After you click Connect, it observes one signed-in library request from the selected tab and sends only the required session headers directly to the local app using native messaging. It does not use an HTTP bridge, upload credentials, modify requests, or store cookies in extension storage. The native host accepts only this extension's fixed ID and protects accepted credentials with current-user Windows DPAPI. A failed or incomplete library check preserves the active connection and baseline.

YouTube can expire or rotate browser sessions. If polling needs reconnection, open your signed-in Music Library and click Connect again. The extension is not an automatic credential monitor. Removing it does not erase the local app's saved connection. To disconnect fully, pause/remove scheduling and remove local `auth.dpapi`; keep the database if you want to retain deduplication history. Signing out of the browser may also invalidate the saved session.

The **Connect YouTube Music** Start-menu entry repairs native-host registration and opens installation guidance. Source installs use **Connect YouTube Music.cmd**. If the extension says the app is missing, repair the connector and reopen the extension. The connector is registered for the current Windows user in Brave, Edge, and Chrome. Live end-to-end acceptance is confirmed on Brave; Edge and Chrome use the same implemented protocol but are not independently verified.

Google desktop OAuth remains available experimentally as `python -m ytlikes.cli setup-google`. Music rejected tested OAuth tokens with HTTP 400 despite successful Google login and official YouTube Data API access. It is not used by the extension. Legacy `setup-headers` and `setup-clipboard` remain available for troubleshooting.

[Privacy and local data](PRIVACY.md).

## Quiet downloads

Audio downloads use the pinned upstream Monochrome JavaScript engine in one normal, dedicated Edge session. The window stays hidden behind a tray icon. It starts when a download needs it and is reused afterward; no headless browser or automatic native API token exchange is used.

Tray actions: **Open downloader**, **Hide/minimize**, **Retry pending**, **Pause**, **Resume**, and **Quit and pause**. If ordinary browser verification needs attention, the app sends one notification and keeps the track pending. Clicking the notification reveals the downloader. Failed verification waits at least 30 minutes before another automatic attempt; likes can still enter the queue.

Only genuine FLAC is saved. The downloader prefers high-resolution lossless, then standard lossless, identifies the codec, fully decodes the file for validation, checks duration, adds metadata/artwork, and finalizes atomically. It never converts lossy audio into FLAC. Supported encrypted source audio is stream-copied through upstream FFmpeg and still passes the same final FLAC checks.

Files use `Artist\Album\Track - Title [catalog ID].flac`. Catalog matching is automatic; different recordings or releases can be chosen. Provider availability and verification can change, so uninterrupted downloading is not guaranteed.

## Controls and recovery

- **Connect YouTube Music.cmd** opens extension setup and repairs the local connector. Connect/reconnect from the extension popup; use its explicit account-switch checkbox when changing accounts.
- **Sync Status.cmd** shows the destination, queue, selected matches, failures, and browser attention state.
- **Pause Sync.cmd** and **Resume Sync.cmd** control background polling.
- **Open Monochrome Helper.cmd** explicitly opens the dedicated downloader window.
- **Check Downloader.cmd** checks catalog-server reachability.
- `python -m ytlikes.cli retry` requests another attempt for pending tracks; use the installed virtual environment.
- `python -m ytlikes.cli remove-scheduler` removes automatic checks without deleting music or connection data. Use Quit and pause to close an already-running tray helper.

The old DevTools connection flow remains available as `python -m ytlikes.cli setup-headers` for troubleshooting; the extension is the normal connection method. Native API configuration commands are retained for existing installations, but they are not required by the browser downloader.

Runtime data lives in the current user's LocalAppData. An ignored `runtime-path.json` may pin the physical location for packaged Windows environments. Do not copy that file, runtime databases, `.dpapi` files, browser profiles, or `.venv` to another PC.

## Uninstall

Use Windows Installed apps to uninstall YouTube Likes Sync. The uninstaller removes integration owned by that installation. Downloaded music and local credentials/history are retained; remove the runtime data separately if you also want to erase your connection and history. Removing the app does not automatically remove the unpacked browser extension.

## Development

Run `install.ps1` to install pinned dependencies. Browser source is pinned by `monochrome-source.json` and its dependency lock; `tools/build_monochrome.py` builds the local worker.

Bundled builds use PyInstaller 6.22.0 and Inno Setup 6.7.3. See packaging/build.ps1 and packaging/app.spec. Build from a clean checkout, supply a built ytlikes-dist directory, and inspect dependency notices and privacy audit results before publishing.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/test_browser_diagnostics.mjs tests/test_extension.mjs
```

Monochrome source/license notices and narrow raw-audio/stream-copy changes are described in `THIRD_PARTY_NOTICES.txt`. The upstream Apache-2.0 license is included. Dependencies retain their own licenses, including pystray's LGPL notices. This project is not affiliated with Google, YouTube, Monochrome, or the audio providers.
