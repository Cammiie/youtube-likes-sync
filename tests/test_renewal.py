import base64
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from ytlikes import renewal
from ytlikes.common import config, SyncError
from ytlikes.unified import save_session, UnifiedDownloader
from ytlikes.service import sync
from ytlikes.state import State
from test_media import Provider


def credential(root, expires, *, required=True):
    payload=base64.urlsafe_b64encode(json.dumps({'exp':expires}).encode()).decode().rstrip('=')
    save_session(root,{'base':'https://api.example','token':'fixture',
        'jwt':'fixture.'+payload+'.signature','verification_required':required})


@pytest.fixture
def enabled(tmp_path):
    return {**config(tmp_path),'auto_api_renewal':True}


def test_disabled_valid_and_independent_credentials_never_launch(tmp_path,enabled,monkeypatch):
    monkeypatch.setattr(renewal,'run_verification',lambda _:pytest.fail('unexpected browser launch'))
    assert renewal.renew_if_due(tmp_path,config(tmp_path))['status']=='disabled'
    assert renewal.renew_if_due(tmp_path,enabled)['status']=='api_configuration_required'
    credential(tmp_path,time.time()+3600)
    assert renewal.renew_if_due(tmp_path,enabled)['status']=='token_valid'
    credential(tmp_path,0,required=False)
    assert renewal.renew_if_due(tmp_path,enabled)['status']=='verification_not_required'


def test_near_expiry_renews_once_then_future_ticks_skip(tmp_path,enabled,monkeypatch):
    now=time.time();credential(tmp_path,now+599);calls=[]
    def verify(root):
        calls.append(1);credential(root,now+3600)
    monkeypatch.setattr(renewal,'run_verification',verify)
    assert renewal.renew_if_due(tmp_path,enabled,now=now)['status']=='renewed'
    assert renewal.renew_if_due(tmp_path,enabled,now=now+300)['status']=='token_valid'
    assert calls==[1]


def test_rejected_access_renews_even_before_expiry(tmp_path,enabled,monkeypatch):
    now=time.time();credential(tmp_path,now+3000)
    (tmp_path/'unified-status.json').write_text('{"status":"api_access_required"}')
    monkeypatch.setattr(renewal,'run_verification',lambda root:credential(root,now+3600))
    assert renewal.renew_if_due(tmp_path,enabled,now=now)['status']=='renewed'


def test_exchange_and_validation_reuse_downloader_client(tmp_path,enabled,monkeypatch):
    now=time.time();credential(tmp_path,now+100);client=object();calls=[]
    def verify(root,*,api):
        assert api is client;calls.append('exchange');credential(root,now+3600)
    def check(root,settings,*,api):
        assert api is client;calls.append('validate')
    monkeypatch.setattr(renewal,'run_verification',verify)
    monkeypatch.setattr(renewal,'validate_access',check)
    assert renewal.renew_if_due(tmp_path,enabled,now=now,api=client)['status']=='renewed'
    assert calls==['exchange','validate']


def test_successful_renewal_still_limits_reopening_on_early_rejection(tmp_path,enabled,monkeypatch):
    now=time.time();credential(tmp_path,now+100);calls=[]
    def verify(root):calls.append(1);credential(root,now+3600)
    monkeypatch.setattr(renewal,'run_verification',verify)
    renewal.renew_if_due(tmp_path,enabled,now=now)
    (tmp_path/'unified-status.json').write_text('{"status":"api_access_required"}')
    assert renewal.renew_if_due(tmp_path,enabled,now=now+300)['status']=='waiting_to_renew'
    assert calls==[1]


def test_failure_restores_session_and_does_not_reopen_every_five_minutes(tmp_path,enabled,monkeypatch):
    now=time.time();credential(tmp_path,now+500)
    before=(tmp_path/'unified-api.dpapi').read_bytes()
    old_status=(tmp_path/'unified-status.json').read_bytes();calls=[]
    def fail(root):
        calls.append(1);credential(root,now+3600)
        raise SyncError('monochrome_verification_widget_failed')
    monkeypatch.setattr(renewal,'run_verification',fail)
    result=renewal.renew_if_due(tmp_path,enabled,now=now)
    assert result['next_try']==now+1800
    assert (tmp_path/'unified-api.dpapi').read_bytes()==before
    assert (tmp_path/'unified-status.json').read_bytes()==old_status
    for seconds in (300,600,1500):renewal.renew_if_due(tmp_path,enabled,now=now+seconds)
    assert len(calls)==1
    renewal.renew_if_due(tmp_path,enabled,now=now+1801)
    assert len(calls)==2


def test_restart_retains_attempt_lease(tmp_path,enabled,monkeypatch):
    now=time.time();credential(tmp_path,now+100)
    def interrupted(root):raise SystemExit()
    monkeypatch.setattr(renewal,'run_verification',interrupted)
    with pytest.raises(SystemExit):renewal.renew_if_due(tmp_path,enabled,now=now)
    monkeypatch.setattr(renewal,'run_verification',lambda _:pytest.fail('restart reopened browser'))
    assert renewal.renew_if_due(tmp_path,enabled,now=now+300)['status']=='waiting_to_renew'


def test_issued_but_rejected_token_is_not_reported_as_renewed(tmp_path,enabled,monkeypatch):
    now=time.time();credential(tmp_path,now+500)
    before=(tmp_path/'unified-api.dpapi').read_bytes()
    monkeypatch.setattr(renewal,'run_verification',lambda root:credential(root,now+3600))
    def rejected(*args):raise SyncError('api_access_required')
    monkeypatch.setattr(renewal,'validate_access',rejected)
    result=renewal.renew_if_due(tmp_path,enabled,now=now)
    assert result['status']=='api_access_required'
    assert (tmp_path/'unified-api.dpapi').read_bytes()==before


def test_dry_run_and_pause_never_prepare_renewal(tmp_path):
    state=State(tmp_path);state.baseline([],'account')
    yt=SimpleNamespace(fetch=lambda:([],'account'))
    dl=SimpleNamespace(prepare_session=lambda s:pytest.fail('unexpected renewal'))
    assert sync(state,yt,Provider(),dl,config(tmp_path),dry_run=True)['status']=='dry_run'
    state.set('paused',True)
    assert sync(state,yt,Provider(),dl,config(tmp_path))['status']=='paused'
    state.close()


def test_renewal_only_retries_jobs_blocked_by_api_access(tmp_path,monkeypatch):
    state=State(tmp_path);state.baseline([],'account')
    state.ingest([{'video_id':'access'},{'video_id':'missing'}])
    state.fail('access',SyncError('api_access_required',86400))
    state.fail('missing',SyncError('catalog_match_not_found',86400))
    original=state.db.execute("SELECT next_try FROM jobs WHERE video_id='missing'").fetchone()[0]
    monkeypatch.setattr(renewal,'renew_if_due',lambda *a,**k:{'status':'renewed'})
    UnifiedDownloader(Provider(),config(tmp_path),tmp_path).prepare_session(state)
    assert state.db.execute("SELECT next_try FROM jobs WHERE video_id='access'").fetchone()[0]==0
    assert state.db.execute("SELECT next_try FROM jobs WHERE video_id='missing'").fetchone()[0]==original
    state.close()


def test_verification_closes_owned_window_and_gate(tmp_path,monkeypatch):
    (tmp_path/'monochrome/ytlikes-dist').mkdir(parents=True)
    (tmp_path/'monochrome/ytlikes-dist/verify-api.html').write_text('fixture')
    binary=tmp_path/'Microsoft/Edge/Application/msedge.exe'
    binary.parent.mkdir(parents=True);binary.touch()
    monkeypatch.setenv('PROGRAMFILES(X86)',str(tmp_path))
    monkeypatch.setattr(renewal,'PORT',0)
    calls=[]
    def run(args,**kwargs):calls.append(args);return SimpleNamespace(returncode=0)
    monkeypatch.setattr(renewal.subprocess,'run',run)
    class Browser:
        pid=12345
        def poll(self):return None
        def wait(self,timeout):return 0
    def launch(args,**kwargs):
        assert not any('headless' in a for a in args)
        assert args[-1].endswith('/verify-api.html')
        (tmp_path/'unified-verification-attempt.json').write_text('{"status":"ok"}')
        return Browser()
    monkeypatch.setattr(renewal.subprocess,'Popen',launch)
    renewal.run_verification(tmp_path)
    assert ['taskkill.exe','/PID','12345','/T','/F'] in calls
    assert config(tmp_path)['api_migration_pending'] is False
    assert config(tmp_path)['browser_launch_allowed'] is False
