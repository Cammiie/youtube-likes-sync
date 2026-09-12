# YouTube Likes Sync

Automatically save new YouTube Music likes as FLAC on Windows—including likes from your phone.

**[Download the Windows installer](https://github.com/Cammiie/youtube-likes-sync/releases/latest)**

Requires Windows 10/11 x64 and Brave, Edge, or Chrome for the YouTube connection. The download runtime is bundled. The installer is unsigned, so Windows may show an unknown-publisher warning.

## Set up

1. Install the app and open **Connect YouTube Music** from the Start menu.
2. Choose Brave, Edge, or Chrome and click **Open Extensions**. Enable **Developer mode**, click **Load unpacked**, and select the extension folder shown by the app.
3. Sign in to [YouTube Music](https://music.youtube.com/) in that browser and open your Library.
4. Open the **YouTube Likes Sync** extension, choose your download folder, and click **Connect YouTube Music**.

Once it says **Connected**, you're ready. Existing likes are skipped; only new likes download. No Google Cloud setup or copied browser headers needed.

## How it works

- Checks every five minutes while your PC is awake and you're signed in.
- Saves validated FLAC with metadata and artwork, organized by artist and album.
- Downloads directly through a Tidal mirror using Antra’s request protocol. No downloader browser or verification window.
- Open **Downloader status** from the Start menu for progress, pending reasons, Pause, Resume, and Retry.
- Keeps downloaded files when you unlike a song and avoids downloading it again when re-liked.

Catalog matching is automatic, so a different recording may be selected. Some tracks may be unavailable. Provider failures remain pending; audio never falls back to a browser or lossy conversion.

## Reconnect or remove

If your session expires, open your signed-in Music Library and reconnect through the extension. When switching accounts, check **Use this as a different sync account**; existing files and history stay intact.

Uninstall through Windows Installed apps. Music and local connection/history data are retained; remove the browser extension separately.

Credentials are encrypted locally for your Windows user. See [Privacy and local data](PRIVACY.md).

---

For source setup, run `Install.cmd` with Python 3.11+ (including Tkinter) installed. Packaging scripts are in [`packaging/`](../packaging/).

Uses an original implementation of [Antra’s](https://github.com/anandprtp/Antra) published Tidal mirror protocol. See [third-party notices](../THIRD_PARTY_NOTICES.txt). Not affiliated with Google, YouTube, Antra, Tidal, or audio providers.
