import base64
import json

import pytest

from ytlikes.common import SyncError
from ytlikes.provider import Monochrome, parse_mpd, decode_manifest, search_items, retry_after


def mpd(body, duration="PT6S"):
    return f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="{duration}"><Period><AdaptationSet mimeType="audio/mp4" codecs="fLaC">{body}</AdaptationSet></Period></MPD>'


def test_segment_template_timeline_and_relative_urls():
    text = mpd('<SegmentTemplate timescale="1" initialization="init.mp4" media="part-$Number%03d$-$Time$.m4s"><SegmentTimeline><S t="0" d="2" r="2"/></SegmentTimeline></SegmentTemplate><Representation id="1" bandwidth="500000"/>')
    plan = parse_mpd(text, "https://media.example/audio/manifest.mpd", "LOSSLESS")
    assert [r.url for r in plan.resources] == ["https://media.example/audio/init.mp4", *[f"https://media.example/audio/part-{i+1:03d}-{i*2}.m4s" for i in range(3)]]


def test_negative_repeat_and_highest_flac_representation():
    text = mpd('<SegmentTemplate timescale="1" initialization="$RepresentationID$/init" media="$RepresentationID$/$Number$"><SegmentTimeline><S d="2" r="-1"/></SegmentTimeline></SegmentTemplate><Representation id="low" audioSamplingRate="44100"/><Representation id="high" audioSamplingRate="96000"/>')
    plan = parse_mpd(text, "https://media.example/m.mpd", "HI_RES_LOSSLESS")
    assert len(plan.resources) == 4
    assert plan.resources[0].url == "https://media.example/high/init"


def test_segment_list_byte_ranges():
    text = mpd('<Representation id="1"><BaseURL>audio.mp4</BaseURL><SegmentList><Initialization range="0-99"/><SegmentURL mediaRange="100-299"/></SegmentList></Representation>')
    plan = parse_mpd(text, "https://media.example/m.mpd", "LOSSLESS")
    assert plan.resources[1].byte_range == "100-299"
    assert plan.resources[1].url == "https://media.example/audio.mp4"


def test_encrypted_and_lossy_manifests_rejected():
    with pytest.raises(SyncError, match="encrypted_audio_unsupported"):
        parse_mpd(mpd('<ContentProtection/><Representation id="1"/>'), "https://media.example/m", "LOSSLESS")
    with pytest.raises(SyncError, match="lossless_representation_unavailable"):
        parse_mpd(mpd('<Representation id="1" codecs="mp4a.40.2"/>'), "https://media.example/m", "LOSSLESS")


def test_malicious_xml_rejected():
    with pytest.raises(SyncError):
        parse_mpd('<!DOCTYPE x [<!ENTITY x SYSTEM "file:///secrets">]><MPD>&x;</MPD>', "https://media.example/m", "LOSSLESS")


def test_direct_manifest_and_bts_mirror_urls():
    raw = {"codecs": "flac", "encryptionType": "NONE", "urls": ["https://media.example/a.flac", "https://media.example/b.flac"]}
    encoded = base64.b64encode(json.dumps(raw).encode()).decode()
    assert len(decode_manifest(encoded, "https://api.example/track", "LOSSLESS").resources) == 1
    raw["encryptionType"] = "OLD_AES"
    with pytest.raises(SyncError, match="encrypted_audio_unsupported"):
        decode_manifest(raw, "https://api.example", "LOSSLESS")


def test_nested_search_shapes_and_bad_shape():
    t = {"id": 1, "title": "Song", "artist": {"name": "Artist"}}
    for shape in ([t], {"tracks": {"items": [t]}}, {"data": {"items": [t]}}):
        assert search_items(shape)[0]["id"] == "1"
    assert search_items({"data": {"items": []}}) == []
    with pytest.raises(SyncError, match="provider_search_shape_changed"):
        search_items({"error": "not found"})


def test_rate_limit_numeric_and_http_date():
    assert retry_after("3600") == 3600
    assert retry_after("not a date") == 300
    assert retry_after("Wed, 01 Jan 2031 00:00:00 GMT") > 300


def test_manifest_response_drm_and_preview_rejected(tmp_path):
    provider = Monochrome(tmp_path)
    with pytest.raises(SyncError, match="encrypted_audio_unsupported"):
        provider.parse_response({"data": {"attributes": {"drmData": {"license": "secret"}}}}, "https://api.example", "LOSSLESS")
    with pytest.raises(SyncError, match="preview_audio_rejected"):
        provider.parse_response({"assetPresentation": "PREVIEW"}, "https://api.example", "LOSSLESS")


def test_malformed_200_response_fails_over(tmp_path, monkeypatch):
    provider = Monochrome(tmp_path)
    provider.instances = {"api": ["https://bad.example", "https://good.example"], "streaming": []}
    def fake(session, url, params):
        return {"error": "oops"} if "bad" in url else {"items": [{"id": 1, "title": "Song"}]}
    monkeypatch.setattr("ytlikes.provider.get_json", fake)
    assert provider.search({"title": "Song"})[0]["id"] == "1"
    assert provider.instances["api"][0] == "https://good.example"
