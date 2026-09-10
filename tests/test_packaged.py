from pathlib import Path
import sys

from ytlikes import runtime


def test_frozen_commands_use_bundled_executables_and_resources(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'YouTubeLikesSync.exe'))
    monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path/'_internal'),raising=False)
    assert runtime.app_dir() == tmp_path
    assert runtime.assets_dir() == tmp_path/'_internal'
    assert runtime.engine_dir(tmp_path/'user-data') == tmp_path/'_internal/browser-engine'
    assert runtime.command('ytlikes.browser_host') == [str(tmp_path/'YouTubeLikesSync.exe'),'browser-host']
    assert runtime.command('ytlikes.cli','sync','--quiet') == [str(tmp_path/'YouTubeLikesSync.exe'),'cli','sync','--quiet']
    assert runtime.command('ytlikes.extension_host',windowless=False) == [str(tmp_path/'YouTubeLikesSync.Console.exe'),'native-host']


def test_source_commands_and_engine_keep_current_layout(monkeypatch,tmp_path):
    monkeypatch.delattr(sys,'frozen',raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'python.exe'))
    assert runtime.command('ytlikes.cli','sync') == [str(tmp_path/'pythonw.exe'),'-m','ytlikes.cli','sync']
    assert runtime.engine_dir(tmp_path) == tmp_path/'monochrome/ytlikes-dist'


def test_frozen_scheduler_uses_its_own_executable(monkeypatch,tmp_path):
    from ytlikes import cli
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'YouTubeLikesSync.Console.exe'))
    monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path/'_internal'),raising=False)
    calls=[]
    class Result: returncode=0
    monkeypatch.setattr(cli.subprocess,'run',lambda args,**kw: calls.append(args) or Result())
    cli.scheduler('Install')
    assert '-Executable' in calls[0]
    assert str(tmp_path/'YouTubeLikesSync.exe') in calls[0]
    assert str(tmp_path/'_internal/schedule.ps1') in calls[0]


def test_packaged_native_launcher_has_no_python_dependency(monkeypatch,tmp_path):
    from ytlikes import extension_host as host
    import winreg
    monkeypatch.setattr(host,'bundled',lambda:True)
    monkeypatch.setattr(host,'app_dir',lambda:tmp_path)
    (tmp_path/'YouTubeLikesSync.Console.exe').write_bytes(b'synthetic')
    class Key:
        def __enter__(self):return self
        def __exit__(self,*a):pass
    monkeypatch.setattr(winreg,'CreateKeyEx',lambda *a:Key())
    monkeypatch.setattr(winreg,'SetValueEx',lambda *a:None)
    host.register(tmp_path/'runtime')
    text=(tmp_path/'runtime/native-messaging/host.cmd').read_text()
    assert 'YouTubeLikesSync.Console.exe' in text and 'native-host %*' in text
    assert '.venv' not in text and '-m ytlikes' not in text
