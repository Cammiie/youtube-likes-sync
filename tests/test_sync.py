import json
import time

import pytest
import requests

from ytlikes.common import SyncError, RunLock, config
from ytlikes.service import sync
from ytlikes.state import State
from ytlikes.youtube import YouTube, parse_headers
from ytlikes.matching import choose


def song(id="new", **extra):
    return dict(video_id=id, title="My Song", artists=["My Artist"], album="My Album", duration=180, isrc="", **extra)


def candidate(id="123", **extra):
    d = dict(id=id, title="My Song", artists=["My Artist"], album="My Album", duration=180, isrc="", version="", track_number=1)
    d.update(extra)
    return d


class Source:
    def __init__(self, songs, account="account"):
        self.songs, self.account = songs, account
    def fetch(self):
        if isinstance(self.songs, Exception):
            raise self.songs
        return self.songs, self.account


class Provider:
    def __init__(self, candidates=None):
        self.candidates = [candidate()] if candidates is None else candidates
    def search(self, source):
        return self.candidates
    def metadata(self, track):
        return track


class Download:
    def __init__(self):
        self.writes = 0
    def download(self, track, video_id, path):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
            self.writes += 1
        return path


@pytest.fixture
def state(tmp_path):
    db = State(tmp_path / "data")
    yield db
    db.close()


def settings(tmp_path):
    s = config(tmp_path)
    s["output"] = str(tmp_path / "music")
    return s


def test_baseline_only_then_new_like_exactly_once_after_restart(state, tmp_path):
    state.baseline([song("old")], "account")
    download = Download()
    source = Source([song("old"), song("new")])
    result = sync(state, source, Provider(), download, settings(tmp_path))
    assert result["new_likes"] == result["completed"] == download.writes == 1
    assert state.db.execute("SELECT video_id FROM jobs").fetchall()[0][0] == "new"
    state.close()
    state.db = State(tmp_path / "data").db
    assert sync(state, source, Provider(), download, settings(tmp_path))["new_likes"] == 0
    assert download.writes == 1


def test_two_video_ids_one_catalog_file(state, tmp_path):
    state.baseline([], "account")
    download = Download()
    sync(state, Source([song("a"), song("b")]), Provider(), download, settings(tmp_path))
    assert download.writes == 1
    assert state.status()["counts"] == {"completed": 2}


def test_unlike_relike_never_deletes_or_requeues(state, tmp_path):
    state.baseline([song("old")], "account")
    dl = Download()
    for items in ([song("new")], [], [song("old"), song("new")]):
        sync(state, Source(items), Provider(), dl, settings(tmp_path))
    assert dl.writes == 1
    assert state.status()["counts"] == {"completed": 1}


def test_sync_without_baseline_refuses(state, tmp_path):
    with pytest.raises(SyncError, match="setup_required"):
        sync(state, Source([song()]), Provider(), Download(), settings(tmp_path))
    assert state.status()["baseline_and_seen"] == 0


def test_failed_snapshot_does_not_ingest_anything(state, tmp_path):
    state.baseline([song("old")], "account")
    with pytest.raises(SyncError, match="incomplete_likes_snapshot"):
        sync(state, Source(SyncError("incomplete_likes_snapshot")), Provider(), Download(), settings(tmp_path))
    assert state.status()["baseline_and_seen"] == 1
    assert state.status()["counts"] == {}


def test_auth_expiry_pauses_without_reset(state, tmp_path):
    state.baseline([song("old")], "account")
    with pytest.raises(SyncError, match="youtube_auth_required"):
        sync(state, Source(SyncError("youtube_auth_required")), Provider(), Download(), settings(tmp_path))
    assert state.get("auth_required")
    assert state.status()["baseline_and_seen"] == 1


def test_different_account_cannot_enqueue(state, tmp_path):
    state.baseline([song("old")], "account")
    with pytest.raises(SyncError, match="different_youtube_account"):
        sync(state, Source([song()], "other"), Provider(), Download(), settings(tmp_path))
    assert state.get("auth_required")
    assert not state.status()["counts"]


def test_dry_run_is_read_only(state, tmp_path):
    state.baseline([], "account")
    before = state.db.total_changes
    result = sync(state, Source([song()]), Provider(), Download(), settings(tmp_path), dry_run=True)
    assert len(result["new_likes"]) == 1
    assert state.db.total_changes == before
    assert not (tmp_path / "music").exists()


def test_missing_match_retries_daily(state, tmp_path):
    state.baseline([], "account")
    sync(state, Source([song()]), Provider([]), Download(), settings(tmp_path))
    job = state.status()["jobs"][0]
    assert job["error"] == "catalog_match_not_found"
    assert job["next_try"] >= time.time() + 86390
    assert state.due() == []


def test_interrupted_job_recovered_and_mapping_preserved(state, tmp_path):
    state.baseline([], "account")
    state.ingest([song()])
    state.selected("new", candidate(), tmp_path / "some.flac")
    state.recover()
    assert state.due()[0]["state"] == "pending"
    assert state.due()[0]["catalog_id"] == "123"


def test_rate_limit_and_exponential_retry(state):
    state.baseline([], "account")
    state.ingest([song()])
    state.fail("new", SyncError("provider_rate_limited", 7200))
    job = state.status()["jobs"][0]
    assert job["next_try"] >= time.time() + 7190
    state.retry()
    assert len(state.due()) == 1


def test_pause_blocks_network(state, tmp_path):
    state.baseline([], "account")
    state.set("paused", True)
    assert sync(state, Source(SyncError("should_not_call")), Provider(), Download(), settings(tmp_path)) == {"status": "paused"}


def test_kernel_lock_prevents_overlap_and_releases(tmp_path):
    with RunLock(tmp_path):
        with pytest.raises(SyncError, match="already_running"):
            with RunLock(tmp_path):
                pass
    with RunLock(tmp_path):
        pass


class FakeClient:
    def get_account_info(self):
        return {"channelHandle": "@fixture"}
    def get_liked_songs(self, limit):
        assert limit is None
        return {"trackCount": 201, "tracks": [{"videoId": str(i), "title": "Song"} for i in range(201)]}


def test_youtube_fetch_requests_unlimited_not_default_100():
    songs, account = YouTube({}, FakeClient()).fetch()
    assert len(songs) == 201
    assert "fixture" not in account


def test_youtube_count_mismatch_rejects_partial_snapshot():
    client = FakeClient()
    client.get_liked_songs = lambda limit: {"trackCount": 201, "tracks": [{"videoId": "1"}]}
    with pytest.raises(SyncError, match="incomplete_likes_snapshot"):
        YouTube({}, client).fetch()


def test_continuation_failure_never_returns_partial_results():
    class BrokenClient(FakeClient):
        def get_liked_songs(self, limit):
            first_page = [{"videoId": "old"}]
            raise requests.ConnectionError("private URL and cookie must not escape")
    with pytest.raises(SyncError, match="^youtube_network_error$"):
        YouTube({}, BrokenClient()).fetch()


def test_header_parser_strips_unneeded_secrets():
    raw = json.dumps({"cookie": "SAPISID=fixture;", "authorization": "must-not-store", "x-goog-authuser": "0"})
    headers = parse_headers(raw)
    assert "authorization" not in headers
    assert headers["cookie"] == "SAPISID=fixture;"


def test_real_ytmusic_client_recognizes_sanitized_browser_headers():
    raw = 'cookie: SAPISID=synthetic-only; __Secure-3PAPISID=synthetic-only;\nx-goog-authuser: 0\nx-origin: https://music.youtube.com\nauthorization: discarded\n'
    headers = parse_headers(raw)
    assert "authorization" not in headers
    yt = YouTube(headers)
    assert yt.client.headers["authorization"].startswith("SAPISIDHASH ")


@pytest.mark.parametrize("variant", ["Live", "Remix", "Acoustic", "Karaoke", "Cover", "Sped Up"])
def test_original_beats_different_version(variant):
    assert choose(song(), [candidate("1", version=variant), candidate("2")])["id"] == "2"


def test_remix_source_prefers_remix():
    s = song()
    s["title"] = "My Song (Remix)"
    assert choose(s, [candidate("1"), candidate("2", version="Remix")])["id"] == "2"


def test_artist_match_beats_cover_and_duration_breaks_ties():
    assert choose(song(), [candidate("1", artists=["Cover Band"]), candidate("2")])["id"] == "2"
    assert choose(song(), [candidate("1", duration=210), candidate("2")])["id"] == "2"


def test_isrc_wins_and_closest_match_has_no_review_threshold():
    s = song()
    s["isrc"] = "ABC123"
    assert choose(s, [candidate("1"), candidate("2", isrc="ABC123", title="Alternate spelling")])["id"] == "2"
    assert choose(song(), [candidate("3", title="Different title")])["id"] == "3"
