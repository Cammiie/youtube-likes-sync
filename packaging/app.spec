# Build from a clean checkout; no developer runtime files are included.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

root = Path(SPECPATH).parent
data = [(str(root/'extension'), 'extension'), (str(root/'schedule.ps1'), '.'),
        (str(root/'build-assets/browser-engine'), 'browser-engine'),
        (str(root/'build-assets/licenses'), 'licenses')]
binaries, hidden = [], []
for module in ('ytmusicapi', 'pystray', 'imageio_ffmpeg', 'mutagen'):
    package_data, package_binaries, package_hidden = collect_all(module)
    data += package_data; binaries += package_binaries; hidden += package_hidden
data += copy_metadata('ytmusicapi')
a = Analysis([str(root/'packaging/entry.py')], pathex=[str(root)], binaries=binaries,
             datas=data, hiddenimports=hidden + ['tkinter','PIL._tkinter_finder'],
             excludes=['pytest','IPython','matplotlib','numpy'], noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='YouTubeLikesSync',
          console=False, icon=str(root/'packaging/app.ico'), disable_windowed_traceback=True)
console = EXE(pyz, a.scripts, [], exclude_binaries=True, name='YouTubeLikesSync.Console',
              console=True, icon=str(root/'packaging/app.ico'))
coll = COLLECT(gui, console, a.binaries, a.datas, name='YouTubeLikesSync')
