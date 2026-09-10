# YouTube Likes Sync

Automatically save new YouTube Music likes as FLAC on Windows—including likes from your phone.

**[Download the Windows installer](https://github.com/Cammiie/youtube-likes-sync/releases/latest)**

Requires Windows 10/11 x64 and Microsoft Edge. Everything else is bundled. The installer is unsigned, so Windows may show an unknown-publisher warning.

## Set up

1. Install the app and open **Connect YouTube Music** from the Start menu.
2. Choose Brave, Edge, or Chrome and click **Open Extensions**. Enable **Developer mode**, click **Load unpacked**, and select the extension folder shown by the app.
3. Sign in to [YouTube Music](https://music.youtube.com/) in that browser and open your Library.
4. Open the **YouTube Likes Sync** extension, choose your download folder, and click **Connect YouTube Music**.

Once it says **Connected**, you're ready. Existing likes are skipped; only new likes download. No Google Cloud setup or copied browser headers needed.

## How it works

- Checks every five minutes while your PC is awake and you're signed in.
- Saves validated FLAC with metadata and artwork, organized by artist and album.
- Runs the Monochrome downloader in a dedicated Edge session hidden behind a tray icon. Verification may occasionally need your click.
- Offers **Pause**, **Resume**, **Retry pending**, and **Open downloader** from the tray.
- Keeps downloaded files when you unlike a song and avoids downloading it again when re-liked.

Catalog matching is automatic, so a different recording may be selected. Some tracks may be unavailable.

## Reconnect or remove

If your session expires, open your signed-in Music Library and reconnect through the extension. When switching accounts, check **Use this as a different sync account**; existing files and history stay intact.

Uninstall through Windows Installed apps. Music and local connection/history data are retained; remove the browser extension separately.

Credentials are encrypted locally for your Windows user. See [Privacy and local data](PRIVACY.md).

---

For source setup, run `Install.cmd` with Python 3.11+ (including Tkinter) and Node/npm installed. Packaging scripts are in [`packaging/`](../packaging/).

Uses [Monochrome](https://github.com/monochrome-music/monochrome). See [third-party notices](../THIRD_PARTY_NOTICES.txt). Not affiliated with Google, YouTube, Monochrome, or audio providers.
