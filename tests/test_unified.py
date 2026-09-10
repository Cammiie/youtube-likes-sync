import base64
import json
import shutil
import time

import pytest

from ytlikes.common import config, dpapi, SyncError
from ytlikes.unified import UnifiedAPI, UnifiedDownloader, UnifiedPlan, lookup_params, save_session, session_status
from ytlikes.provider import Resource
from ytlikes.media import destination, run_ffmpeg, validate
from ytlikes.state import State
from ytlikes.service import sync
from ytlikes import browser_host, browser_download
from test_media import audio_files, track, Provider
from test_browser_bridge import host


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code=status
        self.payload=payload
        self.headers=headers or {}
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def iter_content(self,size): yield json.dumps(self.payload).encode()


def envelope(**changes):
    resource={'kind':'audio','delivery':'direct','source':'amazon','url':'https://audio.example/file',
              'lossless':True,'decryption_key':'00112233445566778899aabbccddeeff'}
    resource.update(changes)
    return {'schema_version':'2.0','playback':[resource]}


@pytest.fixture
def api(tmp_path):
    save_session(tmp_path,{'base':'https://api.example','token':'fixture-secret'})
    return UnifiedAPI(tmp_path,{**config(tmp_path),'allow_encrypted_lossless':True})


def test_lookup_matches_upstream_version_artist_and_duration():
    t={**track(),'version':'Live','artists':[' A ','B'],'duration':123500,'isrc':' abcd '}
    assert lookup_params(t,'LOSSLESS')=={'track':'Test Tone (Live)','artist':'A, B','album':'Test Album',
        'duration':'124','isrc':'ABCD','intent':'download','quality':'LOSSLESS'}


def test_credentials_are_protected_and_endpoint_updates_drop_old_session(tmp_path):
    save_session(tmp_path,{'base':'https://old.example','token':'secret-old','jwt':'old-jwt'})
    assert b'secret-old' not in (tmp_path/'unified-api.dpapi').read_bytes()
    save_session(tmp_path,{'base':'https://new.example','token':'secret-new'})
    saved=json.loads(dpapi((tmp_path/'unified-api.dpapi').read_bytes(),decrypt=True))
    assert saved['base']=='https://new.example' and saved['jwt']==''


def test_request_headers_stay_scoped_and_resource_has_original_key(api,monkeypatch):
    calls=[]
    def get(url,**kwargs):
        calls.append((url,kwargs));return Response(payload=envelope())
    monkeypatch.setattr(api.session,'get',get)
    plan=api.resolve(track(),'HI_RES_LOSSLESS')
    assert plan.decryption_key=='00112233445566778899aabbccddeeff'
    assert plan.decryption_key not in repr(plan)
    assert calls[0][0]=='https://api.example/api/v2/track/'
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['params']['intent']=='download'
    assert calls[0][1]['headers']['Authorization']=='Bearer fixture-secret'
    assert 'Accept' not in calls[0][1]['headers']  # Preserve the native exchange session's Accept.
    assert 'Authorization' not in api.audio_session.headers


@pytest.mark.parametrize('status,code',[(401,'api_access_required'),(403,'api_access_required'),
    (428,'api_access_required'),(429,'provider_rate_limited'),(404,'catalog_audio_unavailable'),
    (502,'catalog_audio_unavailable'),(302,'unified_api_request_failed')])
def test_api_failures_are_sanitized_and_access_rate_failures_stop_requests(api,monkeypatch,status,code):
    calls=[]
    def get(*args,**kwargs):
        calls.append(1)
        return Response(status,{'secret':'must-never-persist'},{'Retry-After':'1800'})
    monkeypatch.setattr(api.session,'get',get)
    with pytest.raises(SyncError,match=code) as error: api.resolve(track(),'LOSSLESS')
    if status==429: assert error.value.delay>=1800
    if status in (401,403,428,429):
        with pytest.raises(SyncError,match=code): api.resolve(track(),'LOSSLESS')
        assert len(calls)==1
    assert 'must-never-persist' not in (api.root/'unified-status.json').read_text()


def test_expired_verification_never_contacts_api_or_browser(api,monkeypatch):
    payload=base64.urlsafe_b64encode(json.dumps({'exp':time.time()-60}).encode()).decode().rstrip('=')
    save_session(api.root,{'base':'https://api.example','token':'fixture-secret',
                          'jwt':'header.'+payload+'.signature','verification_required':True})
    monkeypatch.setattr(api.session,'get',lambda *a,**k:pytest.fail('expired session used'))
    assert session_status(api.root)['status']=='api_access_required'
    with pytest.raises(SyncError,match='api_access_required'): api.resolve(track(),'LOSSLESS')


@pytest.mark.parametrize('changes,code',[
    ({'source':'unknown'},'unified_source_unsupported'),
    ({'decryption_key':'invalid'},'encrypted_audio_unsupported'),
    ({'decryption_key':None,'drm':{'type':'unsupported'}},'encrypted_audio_unsupported'),
    ({'decryption_key':None,'encrypted':True},'encrypted_audio_unsupported'),
    ({'delivery':'hls'},'hls_audio_unsupported'),
    ({'delivery':'dash'},'encrypted_segments_unsupported'),
    ({'lossless':False},'lossy_audio_rejected'),
])
def test_unsupported_resources_remain_pending(api,monkeypatch,changes,code):
    monkeypatch.setattr(api.session,'get',lambda *a,**k:Response(payload=envelope(**changes)))
    with pytest.raises(SyncError,match=code): api.resolve(track(),'LOSSLESS')


@pytest.mark.parametrize('filename,accepted',[('lossless.flac',True),('lossy.m4a',False)])
def test_native_encrypted_download_validates_original_codec_and_deduplicates(tmp_path,audio_files,filename,accepted):
    key='00112233445566778899aabbccddeeff'
    encrypted=tmp_path/'encrypted.mp4'
    result=run_ffmpeg(['-y','-v','error','-i',str(audio_files/filename),'-c:a','copy','-strict','-2',
        '-encryption_scheme','cenc-aes-ctr','-encryption_key',key,
        '-encryption_kid','ffeeddccbbaa99887766554433221100',str(encrypted)])
    assert result.returncode==0
    dl=UnifiedDownloader(Provider(),config(tmp_path),tmp_path)
    calls=[]
    dl.resolve=lambda t,q:UnifiedPlan([Resource('https://audio.example/file')],q,key)
    def transfer(plan,target,deadline):
        calls.append(1);shutil.copyfile(encrypted,target)
    dl.transfer=transfer
    target=destination(tmp_path/'music',track())
    if accepted:
        dl.download(track(),'video',target)
        assert validate(target,1).info.sample_rate==44100
        before=target.read_bytes()
        dl.download(track(),'video',target)
        assert target.read_bytes()==before and len(calls)==1
    else:
        with pytest.raises(SyncError,match='lossy_audio_rejected'):dl.download(track(),'video',target)
        assert not target.exists()
    assert not list((tmp_path/'music').rglob('*.part'))


def test_interrupted_native_download_cleans_partial_files(tmp_path):
    dl=UnifiedDownloader(Provider(),config(tmp_path),tmp_path)
    dl.resolve=lambda t,q:UnifiedPlan([Resource('https://audio.example/file')],q)
    def transfer(plan,target,deadline):
        target.write_bytes(b'partial');raise SyncError('download_network_error')
    dl.transfer=transfer
    target=destination(tmp_path/'music',track())
    with pytest.raises(SyncError,match='download_network_error'):dl.download(track(),'video',target)
    assert not target.exists() and not list((tmp_path/'music').rglob('*.part'))


def test_polling_keeps_new_likes_when_api_access_fails(tmp_path):
    state=State(tmp_path)
    state.baseline([{'video_id':'baseline'}],'account')
    class YouTube:
        songs=[]
        def fetch(self):return self.songs,'account'
    yt=YouTube()
    dl=UnifiedDownloader(Provider(),config(tmp_path),tmp_path)
    for id in ('one','two'):
        yt.songs.append({'video_id':id,'title':'Test Tone','artists':['Test Artist'],'duration':1})
        assert sync(state,yt,Provider(),dl,config(tmp_path))['new_likes']==1
    assert state.status()['baseline_and_seen']==3
    assert state.status()['counts']=={'pending':2}
    assert not state.get('auth_required',False)
    state.close()


def test_browser_launch_blocked_even_when_explicitly_called(tmp_path,monkeypatch):
    monkeypatch.setattr(browser_host.subprocess,'Popen',lambda *a,**k:pytest.fail('browser launched'))
    browser_host.Host(tmp_path).open_browser(visible=True)
    with pytest.raises(SyncError,match='browser_disabled'):browser_download.ensure_host(tmp_path)


def test_migration_requires_bridge_auth_and_closes_after_one_transfer(host):
    engine,base,session=host
    settings=config(engine.root);settings['api_migration_pending']=True
    (engine.root/'config.json').write_text(json.dumps(settings))
    body={'base':'https://api.example','token':'fixture-secret','verification_required':False}
    import requests
    assert requests.post(base+'/migration/session',json=body).status_code==403
    assert session.post(base+'/migration/session',json=body).status_code==200
    assert session.post(base+'/migration/session',json=body).status_code==409
    assert session_status(engine.root)['configured'] is True


def test_native_verification_exchange_keeps_proof_ephemeral_and_uses_own_client(api,monkeypatch):
    payload=base64.urlsafe_b64encode(json.dumps({'exp':time.time()+3600}).encode()).decode().rstrip('=')
    token='fixture.'+payload+'.signature'
    calls=[]
    def post(url,**kwargs):
        calls.append((url,kwargs));return Response(payload={'access_token':token})
    monkeypatch.setattr(api.session,'post',post)
    api.exchange_verification('one-time-fixture-proof')
    assert calls[0][0]=='https://api.example/api/auth/turnstile'
    assert calls[0][1]['json']=={'turnstile_token':'one-time-fixture-proof'}
    assert calls[0][1]['allow_redirects'] is False
    assert set(calls[0][1]['headers'])=={'Authorization','Content-Type'}
    protected=(api.root/'unified-api.dpapi').read_bytes()
    assert token.encode() not in protected
    saved=json.loads(dpapi(protected,decrypt=True))
    assert saved['jwt']==token and saved['verification_required'] is True
    assert 'one-time-fixture-proof' not in json.dumps(saved)
    assert session_status(api.root)['status']=='configured'


def test_rejected_verification_exchange_preserves_credentials(api,monkeypatch):
    before=(api.root/'unified-api.dpapi').read_bytes()
    monkeypatch.setattr(api.session,'post',lambda *a,**k:Response(403,{'error':'sensitive-fixture'}))
    with pytest.raises(SyncError,match='api_verification_exchange_failed'):
        api.exchange_verification('one-time-fixture-proof')
    assert (api.root/'unified-api.dpapi').read_bytes()==before


def test_native_exchange_bridge_is_authenticated_and_one_time(host,monkeypatch):
    engine,base,session=host
    (engine.root/'config.json').write_text(json.dumps({'api_migration_pending':True}))
    calls=[]
    monkeypatch.setattr(UnifiedAPI,'exchange_verification',lambda self,proof:calls.append(proof))
    import requests
    assert requests.post(base+'/migration/exchange',json={'response':'fixture'}).status_code==403
    assert session.post(base+'/migration/exchange',json={'response':'fixture'}).status_code==200
    assert session.post(base+'/migration/exchange',json={'response':'fixture'}).status_code==409
    assert calls==['fixture']


def test_verification_failure_preserves_current_session_and_sanitizes_details(host):
    engine,base,session=host
    save_session(engine.root,{'base':'https://api.example','token':'fixture-secret'})
    before=(engine.root/'unified-api.dpapi').read_bytes()
    (engine.root/'config.json').write_text(json.dumps({'api_migration_pending':True}))
    response=session.post(base+'/migration/failure',json={'code':'monochrome_verification_widget_failed',
        'turnstile_code':'600010','token':'never-persist','details':'https://example.test/?secret=hidden'})
    assert response.status_code==200
    result=json.loads((engine.root/'unified-verification-attempt.json').read_text())
    assert result['status']=='monochrome_verification_widget_failed' and result['turnstile_code']=='600010'
    assert set(result)=={'status','turnstile_code','at'}
    assert (engine.root/'unified-api.dpapi').read_bytes()==before
    assert config(engine.root)['api_migration_pending'] is False
    assert session.post(base+'/migration/failure',json={}).status_code==409
