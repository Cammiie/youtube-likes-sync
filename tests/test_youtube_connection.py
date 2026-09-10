import json
import pytest
from ytmusicapi import YTMusic
from ytlikes.common import SyncError
from ytlikes.youtube import LikesAudit, CompleteYTMusic, YouTube, parse_headers


ROW = {'musicResponsiveListItemRenderer': {}}


def page(items):
    return {'contents': {'twoColumnBrowseResultsRenderer': {'secondaryContents': {
        'sectionListRenderer': {'contents': [{'musicPlaylistShelfRenderer': {'contents': items}}]}}}}}


def token(value):
    return {'continuationItemRenderer': {'continuationEndpoint': {'continuationCommand': {'token': value}}}}


def continuation(items):
    return {'onResponseReceivedActions': [{'appendContinuationItemsAction': {'continuationItems': items}}]}


def test_completed_accessible_snapshot_can_have_stale_header_count(monkeypatch):
    monkeypatch.setattr(YTMusic, '_send_request', lambda *args: page([ROW]))
    def liked(client, limit):
        assert limit is None
        client._send_request('browse', {'browseId': 'VLLM'})
        return {'trackCount': 36, 'tracks': [{'videoId': 'existing', 'title': 'Fixture'}]}
    monkeypatch.setattr(YTMusic, 'get_liked_songs', liked)
    client=CompleteYTMusic()
    client.get_account_info=lambda: {'channelHandle': '@fixture'}
    songs,_=YouTube({},client).fetch()
    assert len(songs)==1 and client.likes_snapshot_complete


def test_audit_requires_terminal_page_and_every_parsed_row():
    audit=LikesAudit()
    audit.observe({'browseId':'VLLM'},page([ROW,token('next')]))
    with pytest.raises(SyncError): audit.validate([{}])
    audit.observe({'continuation':'next'},continuation([ROW]))
    with pytest.raises(SyncError): audit.validate([{}])
    audit.validate([{},{}])


@pytest.mark.parametrize('response',[{},continuation([]),continuation([token('next')])])
def test_missing_empty_or_looping_continuation_is_rejected(response):
    audit=LikesAudit()
    audit.observe({'browseId':'VLLM'},page([ROW,token('next')]))
    with pytest.raises(SyncError): audit.observe({'continuation':'next'},response)


def test_unknown_or_malformed_items_cannot_prove_completeness():
    for item in [{'changedRenderer':{}},{'continuationItemRenderer':{}}]:
        with pytest.raises(SyncError): LikesAudit().observe({'browseId':'VLLM'},page([item]))


def test_signed_out_likes_page_requires_reconnection(monkeypatch):
    response = {'contents': {'buttonRenderer': {'navigationEndpoint': {'signInEndpoint': {}}}}}
    monkeypatch.setattr(YTMusic, '_check_auth', lambda self: None)
    monkeypatch.setattr(YTMusic, '_send_request', lambda *args: response)
    with pytest.raises(SyncError, match='youtube_auth_required'):
        CompleteYTMusic().get_liked_songs()


def test_missing_account_header_checks_for_expired_session(monkeypatch):
    monkeypatch.setattr(YTMusic, 'get_account_info', lambda self: {}['missing'])
    monkeypatch.setattr(YTMusic, '_send_request', lambda *args: {'signInEndpoint': {}})
    with pytest.raises(SyncError, match='youtube_auth_required'):
        YouTube({}, CompleteYTMusic()).fetch()


def test_unknown_account_shape_is_not_assumed_expired(monkeypatch):
    monkeypatch.setattr(YTMusic, 'get_account_info', lambda self: {}['missing'])
    monkeypatch.setattr(YTMusic, '_send_request', lambda *args: {'unrecognizedRenderer': {}})
    with pytest.raises(SyncError, match='youtube_response_changed'):
        YouTube({}, CompleteYTMusic()).fetch()


def test_sign_in_link_does_not_override_real_playlist(monkeypatch):
    response = page([ROW])
    response['optionalAction'] = {'signInEndpoint': {}}
    monkeypatch.setattr(YTMusic, '_send_request', lambda *args: response)
    assert CompleteYTMusic()._send_request('browse', {'browseId': 'VLLM'}) == response


def fetch_copy(url='https://music.youtube.com/youtubei/v1/browse?prettyPrint=false',cookie=True):
    headers={'authorization':'discard-me'}
    if cookie: headers['cookie']='SAPISID=synthetic;'
    return 'fetch('+json.dumps(url)+', '+json.dumps({'headers':headers,'method':'POST'})+');'


def test_node_fetch_copy_is_parsed_without_running_code():
    assert parse_headers(fetch_copy())=={'cookie':'SAPISID=synthetic;'}


@pytest.mark.parametrize('raw',[fetch_copy(cookie=False),fetch_copy('https://foreign.example/youtubei/v1/browse'),
                              'fetch("https://music.youtube.com/youtubei/v1/browse", stealCredentials());'])
def test_incomplete_foreign_or_executable_copy_rejected(raw):
    with pytest.raises(SyncError,match='invalid_youtube_request_headers'): parse_headers(raw)
