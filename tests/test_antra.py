import json
import time
from pathlib import Path

import pytest
import requests

from ytlikes import antra
from ytlikes.common import SyncError, config
from ytlikes.matching import choose
from ytlikes.media import run_ffmpeg, validate
from ytlikes.migration import migrate
from ytlikes.state import State


class Response:
    def __init__(self, body=b'', status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {}
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.closed = True
    def iter_content(self, size):
        yield self.body


@pytest.fixture
def provider(tmp_path):
    catalog = antra.AntraCatalog(tmp_path)
    catalog._access = {'endpoint': 'https://example.invalid', 'key': 'synthetic-local-fixture'}
    yield catalog
    catalog.close()


@pytest.mark.parametrize('status,code', [(401,'api_access_required'), (403,'api_access_required'),
    (429,'provider_rate_limited'), (503,'provider_unavailable'), (302,'provider_unavailable'),
    (404,'lossless_unavailable'), (422,'lossless_unavailable')])
def test_safe_http_failures(provider, monkeypatch, status, code):
    response = Response(status=status, headers={'Retry-After': '7200'})
    monkeypatch.setattr(provider.session, 'get', lambda *a, **k: response)
    with pytest.raises(SyncError, match=code): provider.request('/api/stream/1', audio=True)
    assert response.closed
    if status == 429:
        assert antra.access_status(provider.root)['retry_at'] > time.time()+7100
        with pytest.raises(SyncError, match=code): provider.request('/search/')


def test_credentials_scoped_no_redirect(provider, monkeypatch):
    calls=[]
    monkeypatch.setattr(provider.session, 'get', lambda *a, **k: calls.append((a,k)) or Response())
    with provider.request('/search/'): pass
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['headers'] == {'X-API-Key': 'synthetic-local-fixture'}
    assert 'X-API-Key' not in provider.session.headers


@pytest.mark.parametrize('endpoint', ['http://example.com', 'https://name:password@example.com',
    'https://example.com/?secret=x', 'https://example.com/#token', 'invalid'])
def test_bad_configuration(endpoint):
    with pytest.raises(SyncError): antra.checked_access({'endpoint':endpoint, 'key':'test'})


def test_bounded_and_malformed_json():
    with pytest.raises(SyncError): antra.AntraCatalog.read_json(Response(b'12345'), 4)
    with pytest.raises(SyncError): antra.AntraCatalog.read_json(Response(b'<html>'))


def test_metadata_identity_and_malformed(provider, monkeypatch):
    monkeypatch.setattr(provider, 'data', lambda *a: {'id':'2','title':'Song','artist':'Artist'})
    with pytest.raises(SyncError, match='provider_recording_mismatch'): provider.metadata({'id':'1'})
    for row in ({}, {'id':'../1','title':'Song','artist':'Artist'},
                {'id':'1','title':'Song','artist':'Artist','duration':float('nan')}):
        with pytest.raises(SyncError): provider.track(row)


def test_matching_preserves_numeric_ids_and_isrc(provider, monkeypatch):
    source={'title':'Song','artists':['Artist'],'isrc':'EXACT'}
    monkeypatch.setattr(provider, 'data', lambda *a: {'data':{'items':[
        {'id':'2','title':'Song','artist':'Artist'}, {'id':'3','title':'Song','artist':'Artist'}]}})
    monkeypatch.setattr(provider,'metadata',lambda row: dict(row,isrc='EXACT' if row['id']=='3' else 'OTHER'))
    assert choose(source,provider.search(source))['id']=='3'


def test_search_empty_vs_failure(provider, monkeypatch):
    monkeypatch.setattr(provider,'data', lambda *a: {'items':[]})
    assert provider.search({'title':'song'}) == []
    monkeypatch.setattr(provider,'data', lambda *a: {'error':'unavailable'})
    with pytest.raises(SyncError): provider.search({'title':'song'})


@pytest.fixture
def audio(tmp_path):
    path=tmp_path/'synthetic.flac'
    assert run_ffmpeg(['-y','-f','lavfi','-i','sine=frequency=500:sample_rate=44100',
        '-t','1','-c:a','flac',str(path)]).returncode==0
    return path.read_bytes()


def test_transfer_fallback_tagging_and_dedup(provider, tmp_path, monkeypatch, audio):
    calls=[]
    def request(*a,**k):
        calls.append((a,k))
        if len(calls)==1: raise SyncError('lossless_unavailable')
        return Response(audio, headers={'Content-Length':str(len(audio))})
    monkeypatch.setattr(provider,'request',request)
    downloader=antra.AntraDownloader(provider,config(tmp_path))
    target=tmp_path/'output'/'track.flac'
    track={'id':'123','title':'Song','artists':['Artist'],'duration':1}
    downloader.download(track,'video-id',target)
    tagged=validate(target,1)
    assert tagged['youtube_video_id']==['video-id']
    assert tagged['monochrome_track_id']==['123']
    assert tagged['download_transport']==['antra_tidal']
    original=target.read_bytes()
    downloader.download(track,'other-video',target)
    assert target.read_bytes()==original and len(calls)==2
    assert calls[0][0][1]=={'strict_24':'1'} and calls[1][0][1]=={'prefer_16':'1'}
    assert not target.with_suffix('.part').exists()


@pytest.mark.parametrize('failure',['interrupted','lossy','duration','size','disk','encrypted'])
def test_transfer_failure_no_completed_file(provider,tmp_path,monkeypatch,audio,failure):
    class Broken(Response):
        def iter_content(self,size):
            yield audio[:100]
            raise requests.ConnectionError('private-url-must-not-escape')
    response=Broken() if failure=='interrupted' else Response(b'not-flac' if failure=='lossy' else audio,
        headers={'Content-Type':'application/json'} if failure=='encrypted' else {})
    monkeypatch.setattr(provider,'request',lambda *a,**k:response)
    settings=config(tmp_path)
    if failure=='size': settings['max_download_bytes']=1
    downloader=antra.AntraDownloader(provider,settings)
    if failure=='disk': monkeypatch.setattr(antra.os,'rename',lambda *a: (_ for _ in ()).throw(OSError()))
    target=tmp_path/'output.flac'
    with pytest.raises(SyncError) as caught:
        downloader.download({'id':'123','title':'Song','duration':100 if failure=='duration' else 1},'video',target)
    assert 'private' not in str(caught.value)
    assert not target.exists() and not target.with_suffix('.part').exists()


def test_migration_preserves_history_selection_and_rate_limit(tmp_path,monkeypatch):
    monkeypatch.setattr('ytlikes.migration.stop_legacy_host',lambda root:None)
    state=State(tmp_path)
    state.baseline([{'video_id':'old'}],'account')
    state.ingest([{'video_id':'new','title':'Song'}, {'video_id':'limited','title':'Other'}])
    state.selected('new',{'id':'123','title':'Song'},Path('saved.flac'))
    state.fail('new',SyncError('monochrome_verification_failed',1800))
    state.fail('limited',SyncError('provider_rate_limited',7200))
    state.set('paused',True)
    before={key:state.get(key) for key in ['account','baseline_at','paused']}
    selected=state.db.execute("SELECT candidate,path FROM jobs WHERE video_id='new'").fetchone()
    limited=state.db.execute("SELECT next_try FROM jobs WHERE video_id='limited'").fetchone()[0]
    settings=migrate(state,tmp_path,dict(config(tmp_path),download_engine='monochrome'))
    migrate(state,tmp_path,settings)
    assert settings['download_engine']=='antra_tidal' and not settings['browser_launch_allowed']
    assert before=={key:state.get(key) for key in before}
    assert tuple(selected)==tuple(state.db.execute("SELECT candidate,path FROM jobs WHERE video_id='new'").fetchone())
    assert state.db.execute("SELECT next_try FROM jobs WHERE video_id='new'").fetchone()[0]==0
    assert state.db.execute("SELECT next_try FROM jobs WHERE video_id='limited'").fetchone()[0]==limited
    state.close()


def test_legacy_host_block_does_not_mutate(tmp_path,monkeypatch):
    monkeypatch.setattr('ytlikes.migration.stop_legacy_host',lambda root: (_ for _ in ()).throw(SyncError('legacy_helper_quit_required')))
    state=State(tmp_path)
    with pytest.raises(SyncError): migrate(state,tmp_path,config(tmp_path))
    assert not (tmp_path/'config.json').exists() and state.get('browserless_migration') is None
    state.close()


def test_existing_conflict_never_replaced(provider,tmp_path,monkeypatch,audio):
    target=tmp_path/'existing.flac';target.write_bytes(audio)
    downloader=antra.AntraDownloader(provider,config(tmp_path))
    monkeypatch.setattr(provider,'request',lambda *a,**k:pytest.fail('must not request audio'))
    with pytest.raises(SyncError,match='existing_file_conflict'):
        downloader.download({'id':'123','title':'Song','duration':1},'video',target)
    assert target.read_bytes()==audio


def test_bootstrap_dpapi_and_failure_not_plaintext(tmp_path,monkeypatch):
    provider=antra.AntraCatalog(tmp_path)
    manifest={'mirrors':{'tidal':'https://example.invalid'},'api_key':'synthetic-service-value'}
    monkeypatch.setattr(provider.session,'get',lambda *a,**k:Response(json.dumps(manifest).encode()))
    assert provider.access()['key']==manifest['api_key']
    assert manifest['api_key'].encode() not in (tmp_path/'mirror-access.dpapi').read_bytes()
    restarted=antra.AntraCatalog(tmp_path)
    monkeypatch.setattr(restarted.session,'get',lambda *a,**k:pytest.fail('cached access expected'))
    assert restarted.access()==provider.access()
    provider.close();restarted.close()


def test_likes_ingested_while_provider_access_fails(tmp_path):
    from ytlikes.service import sync
    state=State(tmp_path);state.baseline([], 'account')
    class YouTube:
        def fetch(self):return [{'video_id':'new','title':'Song','artists':['Artist']}],'account'
    class Provider:
        def search(self,source):raise SyncError('api_access_required')
    report=sync(state,YouTube(),Provider(),object(),config(tmp_path))
    assert report['new_likes']==1 and report['pending_failures']==1
    assert state.status()['jobs'][0]['error']=='api_access_required'
    assert state.get('auth_required',False) is False
    state.close()
