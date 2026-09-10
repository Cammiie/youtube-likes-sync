# YouTube Likes Sync

Save future YouTube Music likes as matching FLAC tracks on Windows. Likes made on your phone are picked up when this PC is awake and you are signed in.

Existing likes become a baseline when you first connect. They are not downloaded. Unliking a song never deletes a file, and liking it again does not download it twice.

## Install

Requirements: Windows 10/11, Python 3.11 or newer with Tkinter, Node.js LTS with npm, and Microsoft Edge. This is currently a source installation, not a standalone executable.

1. Clone or download this repository into a folder you will keep.
2. Run **Install.cmd**. It installs the Python environment, builds the pinned Monochrome browser engine, and registers a task that runs every five minutes and at sign-in.
3. In **Connect YouTube Music**, choose your download folder and click **Sign in with Google**.
4. Approve read access on Google's page. The app checks your entire accessible liked-songs library and finishes automatically.
5. Like a new song to test downloading. Use **Sync Status.cmd** to see pending tracks and completed files.

Google sign-in needs an OAuth application configuration first. If the person sharing the app supplied `google-client.json`, place it next to `Install.cmd` before connecting. Otherwise use **Import app configuration** with the Desktop app client JSON downloaded from Google Cloud. Never share your Google password, browser cookies, or personal OAuth tokens.

New installations default to your Music folder. If CrammyPlayer has exactly one existing recursively watched library folder, that folder's `YouTube Likes` subfolder is suggested. Existing installations retain their configured destination and connection until a replacement connection passes validation.

## One-time Google app setup

This part is for the person configuring the shared application; friends can use the same application configuration with their own Google accounts.

1. Create or choose a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **YouTube Data API v3**.
3. Configure the Google Auth Platform branding and audience. For a small trial, keep the app in Testing and add your friends as test users.
Connection and complete-library retrieval require live validation.
5. Create an OAuth client with application type **Desktop app**. Download its JSON file.
6. Import that JSON into the connection window, or provide it locally as `google-client.json` beside the installer.

The application uses Google's desktop authorization flow with PKCE and a temporary loopback callback. It does not use a TV/device-code client, embedded Google login page, or a manual authorization-code paste. See [Google's desktop OAuth documentation](https://developers.google.com/identity/protocols/oauth2/native-app).

Testing-mode consent has Google-imposed limits and may require periodic reconnection. Broader distribution may require publishing/verifying the OAuth application. Consult [Google's OAuth production guidance](https://developers.google.com/identity/protocols/oauth2/production-readiness/policy-compliance).

The app configuration and user credentials are deliberately excluded from Git. Share the app configuration separately with intended users; each person's refresh tokens remain on their own PC under Windows current-user DPAPI. A fresh clone without app configuration cannot sign in yet.

The Google connection implementation has automated coverage for callback validation, cancellation, refresh, encrypted persistence, baseline creation, and reconnect safety. Live compatibility with your Google OAuth project must be verified before calling a build ready for friends.

Connection and complete-library retrieval require live validation.

## Quiet downloads

Audio downloads use the pinned upstream Monochrome JavaScript engine in one normal, dedicated Edge session. The window stays hidden behind a tray icon. It starts when a download needs it and is reused afterward; no headless browser or automatic native API token exchange is used.

Tray actions: **Open downloader**, **Hide/minimize**, **Retry pending**, **Pause**, **Resume**, and **Quit and pause**. If ordinary browser verification needs attention, the app sends one notification and keeps the track pending. Clicking the notification reveals the downloader. Failed verification waits at least 30 minutes before another automatic attempt; likes can still enter the queue.

Only genuine FLAC is saved. The downloader prefers high-resolution lossless, then standard lossless, identifies the codec, fully decodes the file for validation, checks duration, adds metadata/artwork, and finalizes atomically. It never converts lossy audio into FLAC. Supported encrypted source audio is stream-copied through upstream FFmpeg and still passes the same final FLAC checks.

Files use `Artist\Album\Track - Title [catalog ID].flac`. Catalog matching is automatic; different recordings or releases can be chosen. Provider availability and verification can change, so uninterrupted downloading is not guaranteed.

## Controls and recovery

- **Connect YouTube Music.cmd** connects or reconnects without resetting an existing baseline. Normal reconnection must use the same YouTube account. For an intentional account change, run `python -m ytlikes.cli setup --switch-account` using the installed virtual environment. The new account starts with its existing likes recorded, while prior baselines, seen-song history, the queue, and downloaded files are retained.
- **Sync Status.cmd** shows the destination, queue, selected matches, failures, and browser attention state.
- **Pause Sync.cmd** and **Resume Sync.cmd** control background polling.
- **Open Monochrome Helper.cmd** explicitly opens the dedicated downloader window.
- **Check Downloader.cmd** checks catalog-server reachability.
- `python -m ytlikes.cli retry` requests another attempt for pending tracks; use the installed virtual environment.
- `python -m ytlikes.cli remove-scheduler` removes automatic checks without deleting music or connection data. Use Quit and pause to close an already-running tray helper.

The old DevTools connection flow remains available as `python -m ytlikes.cli setup-headers` for troubleshooting; it is not the normal onboarding path. Native API configuration commands are retained for existing installations, but they are not required by the browser downloader.

Runtime data lives in the current user's LocalAppData. An ignored `runtime-path.json` may pin the physical location for packaged Windows environments. Do not copy that file, runtime databases, `.dpapi` files, browser profiles, or `.venv` to another PC.

## Development

Run `install.ps1` to install pinned dependencies. Browser source is pinned by `monochrome-source.json` and its dependency lock; `tools/build_monochrome.py` builds the local worker.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/test_browser_diagnostics.mjs
```

Monochrome source/license notices and narrow raw-audio/stream-copy changes are described in `THIRD_PARTY_NOTICES.txt`. The upstream Apache-2.0 license is included. Dependencies retain their own licenses, including pystray's LGPL notices. This project is not affiliated with Google, YouTube, Monochrome, or the audio providers.
