from __future__ import annotations

import json
from pathlib import Path
import time

from .common import SyncError
from .matching import choose, score
from .media import destination


def sync(state, youtube, provider, downloader, settings, *, dry_run=False):
    if state.get("baseline_at") is None:
        raise SyncError("setup_required")
    if state.get("paused", False):
        return {"status": "paused"}
    if state.get("auth_required", False):
        raise SyncError("youtube_auth_required")
    retry_at = state.get("poll_retry_at", 0)
    if not dry_run and time.time() < retry_at:
        return {"status": "waiting_to_retry"}
    try:
        songs, account = youtube.fetch()
        if account != state.get("account"):
            raise SyncError("different_youtube_account")
    except SyncError as e:
        if not dry_run:
            if e.code in ("youtube_auth_required", "different_youtube_account"):
                state.set("auth_required", True)
            failures = state.get("poll_failures", 0) + 1
            state.set("poll_failures", failures)
            state.set("poll_retry_at", time.time() + max(e.delay, min(86400, 300 * 2 ** min(failures - 1, 9))))
            state.set("last_error", e.code)
        raise
    if dry_run:
        # No matching calls, queue writes, files, or baseline changes during preview.
        return {"status": "dry_run", "new_likes": state.unseen(songs), "downloads_started": 0}
    state.recover()
    fresh = state.ingest(songs)
    state.set("last_check", time.time())
    state.set("poll_failures", 0)
    state.set("poll_retry_at", 0)
    state.set("last_error", None)
    prepare = getattr(downloader, 'prepare_session', None)
    if prepare:
        prepare(state)
    completed, failed = 0, 0
    deadline = time.monotonic() + 1200
    for job in state.due(settings["max_jobs_per_run"]):
        if time.monotonic() > deadline or state.get("paused", False):
            break
        try:
            source = json.loads(job["source"])
            candidate = json.loads(job["candidate"]) if job["candidate"] else choose(source, provider.search(source))
            if not candidate:
                raise SyncError("catalog_match_not_found", 86400)
            if not job["candidate"]:
                candidate = provider.metadata(candidate)
                candidate["match_score"] = round(score(source, candidate), 3)
            if settings.get('download_engine') == 'antra_tidal':
                candidate = dict(candidate, transport='antra_tidal')
            target = (state.downloaded(candidate["id"]) or (Path(job["path"]) if job["path"] else None)
                      or destination(Path(settings["output"]), candidate))
            state.selected(job["video_id"], candidate, target)
            path = downloader.download(candidate, job["video_id"], target)
            state.completed(job["video_id"], candidate["id"], path)
            completed += 1
        except SyncError as error:
            state.fail(job["video_id"], error)
            failed += 1
            if error.code in ('api_access_required', 'provider_rate_limited', 'provider_unavailable', 'browser_mode_validation_required') or error.code.startswith('monochrome_verification'):
                break  # Shared API access/rate failures apply to the rest of this batch.
        except Exception:
            # Third-party exception text may include transport secrets; persist only a fixed code.
            state.fail(job["video_id"], SyncError("unexpected_job_error"))
            failed += 1
    return {"status": "ok", "new_likes": fresh, "completed": completed, "pending_failures": failed}
