import json
import time
from types import SimpleNamespace

import pytest

from ytlikes import browser_host, browser_desktop
from ytlikes.browser_host import Host, browser_status
from ytlikes.browser_download import BrowserDownloader
from ytlikes.common import SyncError, config
from ytlikes.state import State
from test_media import Provider, track


def test_attention_persists_backoff_and_does_not_renotify(tmp_path):
    host = Host(tmp_path,launch_browser=False)
    host.require_attention('monochrome_verification_required')
    assert host.policy['next_try'] >= time.time()+1799
    host.policy['notified'] = True
    host.save_policy()
    restarted = Host(tmp_path,launch_browser=False)
    restarted.require_attention('monochrome_verification_widget_failed')
    assert restarted.policy['notified'] is True
    restarted.verified()
    assert not browser_status(tmp_path)['attention']
    restarted.require_attention('monochrome_verification_required')
    assert not restarted.policy['notified']


def test_rate_cooldown_and_no_browser_during_backoff(tmp_path,monkeypatch):
    (tmp_path/'config.json').write_text(json.dumps({'browser_launch_allowed':True}))
    host = Host(tmp_path)
    monkeypatch.setattr(host,'open_browser',lambda **kw:pytest.fail('browser launched during backoff'))
    host.require_attention('provider_rate_limited',7200)
    with pytest.raises(ValueError,match='provider_rate_limited'):
        host.submit(track(),100,1000)
    assert host.policy['next_try'] > time.time()+7199
    assert not host.jobs


def test_verified_retries_only_verification_jobs(tmp_path):
    state = State(tmp_path)
    state.baseline([], 'account')
    state.ingest([{'video_id':v} for v in ['verify','missing','rate']])
    for video,code in [('verify','monochrome_verification_required'),('missing','catalog_match_not_found'),('rate','provider_rate_limited')]:
        state.fail(video,SyncError(code,86400))
    host = Host(tmp_path,launch_browser=False)
    host.require_attention('monochrome_verification_required')
    host.verified()
    due = [r['video_id'] for r in state.due()]
    assert due == ['verify']
    state.close()


def test_pause_resume_and_quit_persist(tmp_path):
    host = Host(tmp_path,launch_browser=False)
    state = State(tmp_path)
    host.action('pause')
    assert state.get('paused')
    host.action('resume')
    assert not state.get('paused') and host.kick_at
    host.action('quit')
    assert state.get('paused') and host.stopping.is_set()
    state.close()


def test_unvalidated_mode_leaves_download_pending(tmp_path,monkeypatch):
    settings = config(tmp_path)
    settings['browser_downloads_ready'] = False
    downloader = BrowserDownloader(Provider(),settings)
    with pytest.raises(SyncError,match='browser_mode_validation_required'):
        downloader.download(track(),'video',tmp_path/'audio.flac')
    assert not list(tmp_path.glob('*.flac'))


def test_profile_ownership_is_exact_and_ignores_renderers(tmp_path,monkeypatch):
    class Process:
        info = {'name':'msedge.exe'}
        def __init__(self,args): self.args=args
        def cmdline(self): return self.args
    owned = Process([f'--user-data-dir={tmp_path}'])
    other = Process([f'--user-data-dir={tmp_path}-personal'])
    renderer = Process([f'--user-data-dir={tmp_path}','--type=renderer'])
    monkeypatch.setattr(browser_desktop.psutil,'process_iter',lambda *a:[owned,other,renderer])
    assert browser_desktop.profile_processes(tmp_path) == [owned]


def test_reused_pid_is_not_owned():
    window = object.__new__(browser_desktop.BrowserWindow)
    window.created = 100
    window.process = SimpleNamespace(is_running=lambda:True,create_time=lambda:200)
    assert not window.alive()
    assert window.handles() == []
    window.close()  # Must not terminate the replacement process.


@pytest.mark.parametrize('mode,flag',[('tray',0),('minimized',7)])
def test_background_states_never_activate(mode,flag):
    calls = []
    window = object.__new__(browser_desktop.BrowserWindow)
    window.handles = lambda:[123]
    window.user = SimpleNamespace(IsWindowVisible=lambda h:True,IsIconic=lambda h:False,
        ShowWindowAsync=lambda h,f:calls.append(f),SetForegroundWindow=lambda h:pytest.fail('focus stolen'))
    assert window.apply(mode)
    assert calls == ([7,0] if mode == 'tray' else [flag])


def test_tray_notification_click_and_explorer_recovery(monkeypatch):
    import pystray
    actions = []
    tray = browser_desktop.TrayController(actions.append)
    tray.icon._on_notify(0,0x405)
    assert actions == ['open']
    restored = []
    monkeypatch.setattr(tray.icon,'_show',lambda:restored.append(True))
    tray.icon._visible = True
    tray.icon._on_taskbarcreated(0,0)
    assert restored == [True]
    tray.icon._visible = False


def test_normal_browser_launch_is_minimized_without_headless(tmp_path,monkeypatch):
    (tmp_path/'config.json').write_text(json.dumps({'browser_launch_allowed':True,'browser_window_mode':'tray'}))
    launches = []
    class Window:
        def __init__(self,profile): self.desired=None
        def alive(self): return False
        def recover(self): return False
        def bind(self,p): pass
    monkeypatch.setattr(browser_desktop,'BrowserWindow',Window)
    monkeypatch.setattr(browser_desktop.psutil,'Process',lambda pid:object())
    monkeypatch.setattr(browser_host.subprocess,'Popen',lambda args,**kw: launches.append((args,kw)) or SimpleNamespace(pid=123))
    host = Host(tmp_path)
    host.open_browser()
    assert len(launches) == 1
    args, options = launches[0]
    assert '--start-minimized' in args
    assert not any('headless' in a for a in args)
    assert options['startupinfo'].wShowWindow == 7
    assert host.mode == 'tray'


def test_closed_browser_relaunches_even_with_recent_heartbeat(tmp_path,monkeypatch):
    (tmp_path/'config.json').write_text(json.dumps({'browser_launch_allowed':True}))
    host = Host(tmp_path)
    host.desktop = SimpleNamespace(alive=lambda:False)
    host.last_worker = time.time()
    launches = []
    monkeypatch.setattr(host,'open_browser',lambda:launches.append(True))
    host.submit(track(),100,1000)
    assert launches == [True]


def test_cli_retry_clears_persistent_and_live_cooldown(tmp_path,monkeypatch):
    from ytlikes import browser_download
    host = Host(tmp_path,launch_browser=False)
    host.require_attention('monochrome_verification_required')
    calls = []
    monkeypatch.setattr(browser_download,'bridge_session',lambda root:SimpleNamespace(
        post=lambda url,**kw:calls.append(url)))
    browser_download.reset_browser_retry(tmp_path)
    assert browser_status(tmp_path)['next_try'] == 0
    assert calls == [browser_download.BASE+'/worker/retry']
