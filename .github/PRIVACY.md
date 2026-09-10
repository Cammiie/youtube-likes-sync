# Privacy and local data

YouTube Likes Sync runs on your Windows PC. It has no account-data collection server or analytics.

When you click Connect, the browser extension observes one signed-in library request from the selected YouTube Music tab. It sends the necessary session headers directly to the local Windows app through native messaging. It does not monitor other websites, modify requests, or store cookies in extension storage. Temporary extension storage contains connection status and the chosen output path.

The app uses the session only for YouTube account and liked-song requests. Accepted credentials are encrypted with Windows DPAPI for the current Windows user. The app does not collect your Google password. Google OAuth is retained as an optional experimental connection method; its credentials are likewise stored locally and encrypted.

Account identity, baseline records, track metadata, matches, queue state, retries, and completed paths are kept in a local SQLite database. Track names, artists, albums, duration, or recording identifiers may be sent to Monochrome catalog services to find a matching recording. Google/YouTube credentials are never sent to Monochrome. The dedicated audio browser stores provider verification data locally. Audio and artwork requests reach their hosting providers.

Credentials and signed audio URLs are excluded from normal application logs. Windows account security remains important because encryption is tied to that account. Do not share runtime folders, .dpapi files, browser profiles, databases, or authentication headers.

Pause stops polling. Unliking music does not delete files. Removing the extension alone does not remove the saved Windows connection. To disconnect, pause sync and remove the local auth.dpapi file. To erase all history, remove the runtime folder after removing scheduling and quitting the downloader; this affects future deduplication. The chosen music folder is separate. The uninstaller retains music, connection data, and history so reinstalls can recover them.

Default runtime location: `%LOCALAPPDATA%\YouTubeLikesSync`. An installation-specific runtime-path.json can override it. If you used experimental Google OAuth, revoke it separately through your Google Account connections.

For support, use the project's GitHub issues. Never attach passwords, session headers, tokens, profiles, databases, or raw credential-bearing logs. Browser sessions can expire; reconnect from your signed-in Music tab when needed.
