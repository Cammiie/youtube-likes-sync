from pathlib import Path
import shutil

from mutagen.flac import FLAC
import pytest
import requests

from ytlikes.common import SyncError, config, dpapi, load_auth, save_auth
from ytlikes.media import Downloader, destination, run_ffmpeg, safe_name, validate
from ytlikes.provider import AudioPlan, Resource
from ytlikes.service import sync
from ytlikes.state import State


@pytest.fixture(scope="module")
def audio_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("audio")
    for filename, codec in (("lossless.flac", "flac"), ("lossy.m4a", "aac")):
        process = run_ffmpeg(["-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "1", "-c:a", codec, str(root / filename)])
        assert process.returncode == 0, process.stderr.decode(errors="replace")
    return root


def track(id="123"):
    return {"id": id, "title": "Test Tone", "artists": ["Test Artist"], "album": "Test Album", "duration": 1,
            "version": "", "track_number": 1, "disc_number": 1, "isrc": ""}


class Provider:
    def __init__(self):
        self.qualities = []
    def resolve(self, id, quality):
        self.qualities.append(quality)
        return AudioPlan([Resource("https://media.example/test")], quality)
    def search(self, song):
        return [track()]
    def metadata(self, candidate):
        return candidate


class FixtureDownloader(Downloader):
    def __init__(self, provider, settings, source):
        super().__init__(provider, settings)
        self.source, self.transfers = source, 0
    def transfer(self, plan, target, deadline):
        self.transfers += 1
        shutil.copyfile(self.source, target)


def test_real_flac_download_tag_validate_and_duplicate_skip(tmp_path, audio_files):
    provider = Provider()
    dl = FixtureDownloader(provider, config(tmp_path), audio_files / "lossless.flac")
    path = destination(tmp_path / "music", track())
    assert dl.download(track(), "yt-id", path) == path
    audio = validate(path, 1)
    assert audio["artist"] == ["Test Artist"]
    assert audio["monochrome_track_id"] == ["123"]
    assert audio["youtube_video_id"] == ["yt-id"]
    assert audio.info.sample_rate == 44100
    before = path.read_bytes()
    assert dl.download(track(), "other-video", path) == path
    assert path.read_bytes() == before
    assert dl.transfers == 1


def test_lossy_payload_cannot_become_flac(tmp_path, audio_files):
    dl = FixtureDownloader(Provider(), config(tmp_path), audio_files / "lossy.m4a")
    path = destination(tmp_path, track())
    with pytest.raises(SyncError, match="lossy_audio_rejected"):
        dl.download(track(), "yt-id", path)
    assert not path.exists()
    assert not list(tmp_path.rglob("*.part"))
    assert dl.provider.qualities == ["HI_RES_LOSSLESS", "LOSSLESS"]


def test_high_res_unavailable_falls_back_to_flac(tmp_path, audio_files):
    class Fallback(Provider):
        def resolve(self, id, quality):
            self.qualities.append(quality)
            if quality == "HI_RES_LOSSLESS":
                raise SyncError("lossless_representation_unavailable")
            return AudioPlan([Resource("https://media.example/test")], quality)
    dl = FixtureDownloader(Fallback(), config(tmp_path), audio_files / "lossless.flac")
    dl.download(track(), "yt-id", destination(tmp_path, track()))
    assert dl.provider.qualities == ["HI_RES_LOSSLESS", "LOSSLESS"]


def test_existing_unrelated_file_never_overwritten(tmp_path, audio_files):
    path = destination(tmp_path, track())
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing user file")
    dl = FixtureDownloader(Provider(), config(tmp_path), audio_files / "lossless.flac")
    with pytest.raises(SyncError, match="existing_file_conflict"):
        dl.download(track(), "yt-id", path)
    assert path.read_bytes() == b"existing user file"
    assert dl.transfers == 0


def test_preview_duration_rejected_and_corrupt_audio_rejected(tmp_path, audio_files):
    with pytest.raises(SyncError, match="audio_duration_mismatch"):
        validate(audio_files / "lossless.flac", 180)
    corrupt = tmp_path / "bad.flac"
    corrupt.write_bytes(b"fLaC" + b"bad" * 10)
    with pytest.raises(SyncError, match="invalid_flac_audio"):
        validate(corrupt)


def test_path_collisions_are_disambiguated_by_catalog_id(tmp_path):
    assert destination(tmp_path, track("1")) != destination(tmp_path, track("2"))
    assert safe_name("CON") == "_CON"
    assert safe_name('A/B:C?') == "A_B_C_"
    assert safe_name("...") == "Unknown"


def test_end_to_end_baseline_new_like_real_flac_restart(tmp_path, audio_files):
    settings = config(tmp_path)
    settings["output"] = str(tmp_path / "music")
    provider = Provider()
    dl = FixtureDownloader(provider, settings, audio_files / "lossless.flac")
    class YouTube:
        def fetch(self):
            return [{"video_id": "old", "title": "Old"}, {"video_id": "new", "title": "Test Tone", "artists": ["Test Artist"]}], "account"
    state = State(tmp_path / "state")
    state.baseline([{"video_id": "old"}], "account")
    assert not list(tmp_path.rglob("*.flac"))
    result = sync(state, YouTube(), provider, dl, settings)
    assert result["completed"] == 1
    assert len(list(tmp_path.rglob("*.flac"))) == 1
    state.close()
    state = State(tmp_path / "state")
    assert sync(state, YouTube(), provider, dl, settings)["completed"] == 0
    assert dl.transfers == 1
    state.close()


class FakeResponse:
    status_code = 200
    url = "https://media.example/test"
    def __init__(self, data, interrupt=False):
        self.data, self.interrupt = data, interrupt
        self.headers = {"Content-Length": str(len(data))}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def close(self):
        pass
    def iter_content(self, chunk):
        yield self.data[:len(self.data)//2]
        if self.interrupt:
            raise requests.ConnectionError("secret signed url")
        yield self.data[len(self.data)//2:]


def test_interrupted_stream_cleanup_then_recovery(tmp_path, audio_files, monkeypatch):
    dl = Downloader(Provider(), config(tmp_path))
    payload = (audio_files / "lossless.flac").read_bytes()
    monkeypatch.setattr(dl.session, "get", lambda *a, **kw: FakeResponse(payload, interrupt=True))
    target = destination(tmp_path, track())
    with pytest.raises(SyncError, match="download_network_error"):
        dl.download(track(), "yt-id", target)
    assert not target.exists()
    assert not list(tmp_path.rglob("*.part"))
    monkeypatch.setattr(dl.session, "get", lambda *a, **kw: FakeResponse(payload))
    dl.download(track(), "yt-id", target)
    validate(target, 1)


def test_actual_fragmented_flac_remuxes_without_transcoding(tmp_path, audio_files, monkeypatch):
    fragment = tmp_path / "source.mp4"
    process = run_ffmpeg(["-y", "-i", str(audio_files / "lossless.flac"), "-c:a", "copy", "-strict", "-2",
                          "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "500000", str(fragment)])
    assert process.returncode == 0
    payload = fragment.read_bytes()
    cuts = [0]
    offset = 0
    while offset + 8 <= len(payload):
        size = int.from_bytes(payload[offset:offset+4], "big")
        if payload[offset+4:offset+8] == b"moof":
            cuts.append(offset)
        assert size >= 8
        offset += size
    cuts.append(len(payload))
    chunks = [payload[a:b] for a, b in zip(cuts, cuts[1:])]
    class Segments(Provider):
        def resolve(self, id, quality):
            return AudioPlan([Resource(f"https://media.example/{i}") for i in range(len(chunks))], quality)
    dl = Downloader(Segments(), config(tmp_path))
    monkeypatch.setattr(dl.session, "get", lambda url, **kw: FakeResponse(chunks[int(url.rsplit('/',1)[1])]))
    target = destination(tmp_path / "output", track())
    dl.download(track(), "yt-id", target)
    assert validate(target, 1).info.md5_signature == FLAC(audio_files / "lossless.flac").info.md5_signature


def test_dpapi_round_trip_and_no_plaintext_auth(tmp_path):
    headers = {"cookie": "SAPISID=synthetic-test-only"}
    save_auth(tmp_path, headers)
    assert b"SAPISID" not in (tmp_path / "auth.dpapi").read_bytes()
    assert load_auth(tmp_path) == headers
    ciphertext = dpapi(b"synthetic")
    damaged = bytearray(ciphertext)
    damaged[-1] ^= 1
    with pytest.raises(SyncError, match="credential_decryption_failed"):
        dpapi(bytes(damaged), decrypt=True)


def test_crash_after_atomic_rename_recovers_original_path(tmp_path, audio_files):
    settings = config(tmp_path)
    settings["output"] = str(tmp_path / "new-output")
    original_path = destination(tmp_path / "old-output", track())
    dl = FixtureDownloader(Provider(), settings, audio_files / "lossless.flac")
    state = State(tmp_path / "state")
    state.baseline([], "account")
    source = {"video_id": "new", "title": "Test Tone", "artists": ["Test Artist"]}
    state.ingest([source])
    state.selected("new", track(), original_path)
    dl.download(track(), "new", original_path)
    # Simulate death after rename but before the completion transaction.
    class YouTube:
        def fetch(self):
            return [source], "account"
    result = sync(state, YouTube(), Provider(), dl, settings)
    assert result["completed"] == 1
    assert dl.transfers == 1
    assert not (tmp_path / "new-output").exists()
    assert state.downloaded("123") == original_path
    state.close()
