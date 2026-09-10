"""Locations and child commands shared by source and bundled installations."""
from pathlib import Path
import sys


def bundled():
    return bool(getattr(sys, 'frozen', False))


def app_dir():
    return Path(sys.executable).resolve().parent if bundled() else Path(__file__).resolve().parents[1]


def assets_dir():
    return Path(sys._MEIPASS) if bundled() else app_dir()


def engine_dir(root):
    return assets_dir()/'browser-engine' if bundled() else root/'monochrome/ytlikes-dist'


def command(module, *args, windowless=True):
    if bundled():
        executable = 'YouTubeLikesSync.exe' if windowless else 'YouTubeLikesSync.Console.exe'
        route = {'ytlikes.cli': 'cli', 'ytlikes.browser_host': 'browser-host',
                 'ytlikes.extension_host': 'native-host'}[module]
        return [str(app_dir()/executable), route, *args]
    executable = Path(sys.executable)
    if windowless:
        executable = executable.with_name('pythonw.exe')
    return [str(executable), '-m', module, *args]
