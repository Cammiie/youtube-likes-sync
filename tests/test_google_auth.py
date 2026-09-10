import json
import threading
import time
from urllib.parse import parse_qs,urlsplit,urlencode

import pytest
import requests

from ytlikes import google_auth as auth
from ytlikes.common import SyncError,load_auth,save_auth,config
from ytlikes.youtube import YouTube
from ytlikes.state import State
from ytlikes.onboarding import installation_settings,complete_setup

CLIENT={'client_id':'synthetic-client.apps.googleusercontent.com','client_secret':'synthetic-only'}
TOKEN={'access_token':'synthetic-access','refresh_token':'synthetic-refresh','expires_in':3600,'token_type':'Bearer','scope':auth.SCOPE}


def test_client_import_is_encrypted_and_web_clients_rejected(tmp_path):
    path=tmp_path/'client.json'
    path.write_text(json.dumps({'installed':CLIENT}))
    auth.import_client(tmp_path,path)
    assert auth.load_client(tmp_path)==CLIENT
    assert b'synthetic-only' not in (tmp_path/'google-client.dpapi').read_bytes()
    with pytest.raises(SyncError,match='google_desktop_client_required'):
        auth.client_config({'web':CLIENT})


def test_google_redirect_checks_state_uses_pkce_and_never_writes_plaintext(tmp_path,monkeypatch):
    checked=[]
    replies=[]
    callbacks=[]
    def open_browser(url):
        query=parse_qs(urlsplit(url).query)
        assert url.startswith('https://accounts.google.com/')
        assert query['scope']==[auth.SCOPE]
        assert query['code_challenge_method']==['S256']
        assert 'client_secret' not in query
        redirect=query['redirect_uri'][0]
        def callback():
            replies.append(requests.get(redirect,params={'state':'wrong','code':'stolen'},timeout=5).status_code)
            replies.append(requests.get(redirect,params={'state':query['state'][0],'code':'synthetic-code'},timeout=5).status_code)
        callbacks.append(threading.Thread(target=callback))
        callbacks[-1].start()
        return True
    def exchange(client,payload):
        checked.append(payload)
        assert client==CLIENT and len(payload['code_verifier'])>=43
        assert payload['code']=='synthetic-code'
        return TOKEN.copy()
    monkeypatch.setattr(auth,'token_request',exchange)
    flow=auth.GoogleSignIn(CLIENT,opener=open_browser,timeout=5)
    result=flow.run()
    callbacks[0].join(timeout=5)
    assert result['kind']=='google_oauth' and result['tokens']['refresh_token']=='synthetic-refresh'
    assert replies==[400,200] and len(checked)==1
    assert flow.code is None and flow.verifier is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('case,expected',[('denied','google_access_denied'),('cancelled','setup_cancelled'),('timeout','google_signin_timeout'),('no_browser','google_browser_open_failed')])
def test_signin_failure_does_not_exchange_or_replace_auth(case,expected,tmp_path,monkeypatch):
    save_auth(tmp_path,{'cookie':'existing-synthetic'})
    original=(tmp_path/'auth.dpapi').read_bytes()
    monkeypatch.setattr(auth,'token_request',lambda *a:pytest.fail('unexpected token exchange'))
    def open_browser(url):
        if case=='no_browser': return False
        if case=='cancelled': flow.cancel()
        if case=='denied':
            query=parse_qs(urlsplit(url).query)
            threading.Thread(target=lambda:requests.get(query['redirect_uri'][0],params={'state':query['state'][0],'error':'access_denied'},timeout=3)).start()
        return True
    flow=auth.GoogleSignIn(CLIENT,opener=open_browser,timeout=.05)
    with pytest.raises(SyncError,match=expected): flow.run()
    assert (tmp_path/'auth.dpapi').read_bytes()==original


def test_refresh_persists_with_dpapi_and_replaces_no_other_connection(tmp_path,monkeypatch):
    record={'kind':'google_oauth','client':CLIENT,'tokens':auth.finish_tokens(TOKEN)}
    save_auth(tmp_path,record)
    value={**TOKEN,'access_token':'refreshed-synthetic'}
    value.pop('refresh_token')
    monkeypatch.setattr(auth,'token_request',lambda *a:value)
    credentials=auth.ProtectedCredentials(record,tmp_path)
    credentials.refresh_token('synthetic-refresh')
    assert load_auth(tmp_path)['tokens']['access_token']=='refreshed-synthetic'
    assert b'refreshed-synthetic' not in (tmp_path/'auth.dpapi').read_bytes()
    save_auth(tmp_path,{'cookie':'new-connection'})
    credentials.refresh_token('synthetic-refresh')
    assert load_auth(tmp_path)=={'cookie':'new-connection'}


def test_real_ytmusic_client_uses_oauth_and_refreshes_securely(tmp_path,monkeypatch):
    record={'kind':'google_oauth','client':CLIENT,'tokens':{**auth.finish_tokens(TOKEN),'expires_at':0}}
    save_auth(tmp_path,record)
    monkeypatch.setattr(auth,'token_request',lambda *a:{**TOKEN,'access_token':'fresh-synthetic'})
    youtube=YouTube(record,root=tmp_path)
    assert youtube.client.headers['authorization']=='Bearer fresh-synthetic'
    assert load_auth(tmp_path)['tokens']['access_token']=='fresh-synthetic'
    assert youtube.client._token.local_cache is None


class Response:
    def __init__(self,value,status=200): self.value,self.status_code=value,status; self.ok=200<=status<300
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def iter_content(self,n): yield json.dumps(self.value).encode()


@pytest.mark.parametrize('value,status,error',[
    ({'error':'invalid_grant','error_description':'SECRET'},400,'youtube_auth_required'),
    ({'error':'invalid_client','error_description':'SECRET'},401,'google_client_invalid'),
    ({'error':'anything','error_description':'SECRET'},429,'google_rate_limited'),
    ({'error':'unknown','error_description':'SECRET'},400,'google_signin_failed'),
    ({**TOKEN,'scope':'email'},200,'google_permission_required')])
def test_fixed_token_errors_and_endpoint_scoping(value,status,error):
    class Session:
        def post(self,url,**kwargs):
            assert url==auth.TOKEN_URL and kwargs['allow_redirects'] is False
            return Response(value,status)
    with pytest.raises(SyncError,match=error) as caught:
        auth.token_request(CLIENT,{},session=Session())
    assert 'SECRET' not in str(caught.value)


def test_complete_setup_baseline_only_and_reconnect_preserves_it(tmp_path,monkeypatch):
    from ytlikes import cli
    class FakeYouTube:
        def __init__(self,*a,**k): pass
        def fetch(self): return ([{'video_id':'existing'}],'same-account')
    monkeypatch.setattr(cli,'YouTube',FakeYouTube)
    record={'kind':'google_oauth','client':CLIENT,'tokens':auth.finish_tokens(TOKEN)}
    first=complete_setup(tmp_path,record,str(tmp_path/'music'),automatic=False)
    assert first['baseline_created'] and first['downloads_started']==0
    state=State(tmp_path); baseline=state.get('baseline_at'); state.close()
    second=complete_setup(tmp_path,record,str(tmp_path/'music'),automatic=False)
    state=State(tmp_path)
    assert state.get('baseline_at')==baseline and not state.status()['jobs']
    state.close()
    assert not second['baseline_created']
    assert config(tmp_path)['download_engine']=='monochrome'


def test_existing_runtime_settings_preserved(tmp_path):
    original={'output':'C:/custom','download_engine':'monochrome','auto_api_renewal':False}
    (tmp_path/'config.json').write_text(json.dumps(original))
    assert all(installation_settings(tmp_path)[k]==v for k,v in original.items())


def test_html_rate_limit_is_retryable_without_reading_body():
    class Limited(Response):
        def iter_content(self,n): pytest.fail('rate limit body must not be read')
    class Session:
        def post(self,*a,**k): return Limited(None,429)
    with pytest.raises(SyncError,match='google_rate_limited'):
        auth.token_request(CLIENT,{},session=Session())


@pytest.mark.parametrize('redirects',[None,['http://localhost.example/callback'],['https://localhost']])
def test_desktop_redirect_validation(redirects):
    with pytest.raises(SyncError,match='google_desktop_client_required'):
        auth.client_config({**CLIENT,'redirect_uris':redirects})


def test_explicit_account_switch_preserves_baselines_downloads_and_seen_history(tmp_path,monkeypatch):
    from ytlikes import cli
    state=State(tmp_path)
    state.baseline([{'video_id':'old-like'}],'old-account')
    old_time=state.get('baseline_at')
    state.ingest([{'video_id':'already-downloaded'}])
    state.completed('already-downloaded','catalog-1',tmp_path/'saved.flac')
    save_auth(tmp_path,{'cookie':'old-auth'})
    class NewAccount:
        def __init__(self,*a,**k): pass
        def fetch(self): return ([{'video_id':'new-existing'}],'new-account')
    monkeypatch.setattr(cli,'YouTube',NewAccount)
    with pytest.raises(SyncError,match='different_youtube_account'):
        cli.connect_account(state,tmp_path,{'cookie':'new-auth'})
    assert load_auth(tmp_path)=={'cookie':'old-auth'}
    connected=cli.connect_account(state,tmp_path,{'cookie':'new-auth'},allow_account_change=True)
    assert connected['baseline_created'] and connected['downloads_started']==0
    assert state.get('account')=='new-account'
    assert state.status()['counts']=={'completed':1}
    assert not state.unseen([{'video_id':v} for v in ('old-like','already-downloaded','new-existing')])
    assert state.db.execute('SELECT baseline_at FROM account_baselines WHERE account=?',('old-account',)).fetchone()[0]==old_time
    assert state.downloaded('catalog-1')==tmp_path/'saved.flac'
    assert state.baseline([{'video_id':'liked-while-away'}],'old-account',allow_account_change=True) is False
    assert state.get('baseline_at')==old_time
    assert state.unseen([{'video_id':'liked-while-away'}])
    state.close()


def test_switch_failed_fetch_preserves_current_account_and_credentials(tmp_path,monkeypatch):
    from ytlikes import cli
    state=State(tmp_path); state.baseline([{'video_id':'existing'}],'old-account')
    save_auth(tmp_path,{'cookie':'old-auth'})
    before=state.status()
    class FailedAccount:
        def __init__(self,*a,**k): pass
        def fetch(self): raise SyncError('incomplete_likes_snapshot')
    monkeypatch.setattr(cli,'YouTube',FailedAccount)
    with pytest.raises(SyncError,match='incomplete_likes_snapshot'):
        cli.connect_account(state,tmp_path,{'cookie':'new-auth'},allow_account_change=True)
    assert state.status()==before and state.get('account')=='old-account'
    assert load_auth(tmp_path)=={'cookie':'old-auth'}
    state.close()
