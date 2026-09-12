"""Bundled app entry point and installer integration. No runtime data is packaged."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile

from .common import atomic_write, config, data_dir
from .runtime import app_dir, assets_dir, command, engine_dir


def install_integration():
    from .onboarding import installation_settings
    from .extension_host import register
    from .cli import scheduler
    root = data_dir()
    if not (root/'config.json').exists():
        atomic_write(root/'config.json', json.dumps(installation_settings(root), indent=2).encode())
    register(root)
    scheduler('Install')
    return 0


def uninstall_integration():
    """Remove only registration/scheduling owned by this app. Preserve all user data."""
    import subprocess
    import winreg
    from .extension_host import HOST
    manifest = data_dir()/'native-messaging/host.json'
    launcher = manifest.with_name('host.cmd')
    try:
        owned = str(app_dir()/'YouTubeLikesSync.Console.exe').lower() in launcher.read_text(encoding='utf-8').lower()
    except OSError:
        owned = False
    if owned:
        for browser in ('Google\\Chrome', 'Microsoft\\Edge', 'BraveSoftware\\Brave-Browser'):
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                path = f'Software\\{browser}\\NativeMessagingHosts\\{HOST}'
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | view) as key:
                        value = winreg.QueryValueEx(key, '')[0]
                    if Path(value) == manifest:
                        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, path, view, 0)
                except FileNotFoundError:
                    pass
    # The script checks the executable before removing the task. Other installs stay intact.
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        str(assets_dir()/'schedule.ps1'), '-Action', 'Remove', '-Executable', str(app_dir()/'YouTubeLikesSync.exe')],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode


def self_test():
    """Offline acceptance against synthetic audio and an isolated temporary database."""
    import tkinter
    from .common import dpapi
    from .extension_host import extension_id, serve, read_message, write_message
    from .media import run_ffmpeg, validate
    from .state import State
    with tempfile.TemporaryDirectory(prefix='ytlikes-self-test-') as folder:
        root = Path(folder)
        token = b'local-synthetic-credential'
        assert dpapi(dpapi(token), decrypt=True) == token
        state = State(root); state.baseline([{'video_id':'synthetic'}], 'synthetic-account')
        assert state.status()['counts'] == {}; state.close()
        audio = root/'tone.flac'
        result = run_ffmpeg(['-y','-f','lavfi','-i','sine=frequency=440:sample_rate=44100',
                             '-t','1','-c:a','flac',str(audio)])
        assert result.returncode == 0
        validate(audio,1)
        incoming, outgoing = io.BytesIO(), io.BytesIO()
        write_message(incoming, {'type':'hello'}); write_message(incoming, {'type':'invalid'})
        incoming.seek(0)
        serve(f'chrome-extension://{extension_id()}/', incoming, outgoing, root)
        outgoing.seek(0); assert read_message(outgoing)['type'] == 'ready'
        assert read_message(outgoing)['type'] == 'error'
        assert (assets_dir()/'extension/popup.html').is_file()
        from .antra import AntraCatalog
        assert AntraCatalog.track({'id': '1', 'title': 'Synthetic', 'artist': 'Test'})['id'] == '1'
        # Tk initialization validates the packaged Tcl/Tk without showing a window.
        window = tkinter.Tk(); window.withdraw(); window.update(); window.destroy()
    print(json.dumps({'result':'passed','checks':['dpapi','sqlite','flac_decode','native_messaging','native_provider','tk']}))
    return 0


def main():
    args = sys.argv[1:]
    route = args.pop(0) if args else 'setup'
    if route == 'native-host':
        from .extension_host import main as native_main
        sys.argv = [sys.argv[0], *args]
        return native_main()
    if route == 'browser-host':
        from .browser_host import main as browser_main
        return browser_main()
    if route == 'install-integration': return install_integration()
    if route == 'uninstall-integration': return uninstall_integration()
    if route == 'self-test': return self_test()
    if route == 'download-status':
        from .download_status import show
        return show()
    from .cli import main as cli_main
    return cli_main(args if route == 'cli' else [route, *args])


if __name__ == "__main__":
    raise SystemExit(main())
