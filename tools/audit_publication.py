"""Inspect Git history and unpacked releases without printing private matched text.

Set YTLIKES_PRIVATE_MARKERS to JSON strings identifying local/private values to
exclude. Keep that environment value and raw build logs outside release assets.
Use alongside a secret scanner; this is an additional targeted privacy check.
"""
import argparse
import json
import marshal
import os
from pathlib import Path
import subprocess
import types


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release-dir', type=Path)
    args = parser.parse_args()
    markers = json.loads(os.environ.get('YTLIKES_PRIVATE_MARKERS', '[]'))
    if not markers or not all(isinstance(x,str) and len(x)>3 for x in markers):
        raise SystemExit('Supply private markers locally through YTLIKES_PRIVATE_MARKERS.')
    needles = [value.lower().encode(encoding) for value in markers for encoding in ('utf-8','utf-16le')]
    findings = []
    def check(data, location):
        if any(needle in data.lower() for needle in needles): findings.append(location)
    def git(*args):return subprocess.check_output(['git',*args])
    revisions = git('rev-list','--all').decode().split()
    objects = set()
    for rev in revisions:
        check(git('cat-file','commit',rev), f'commit:{rev[:12]}')
        for row in git('ls-tree','-r',rev).splitlines():
            meta,path = row.split(b'\t',1)
            mode,kind,identity = meta.split()
            if kind == b'blob' and identity not in objects:
                objects.add(identity);check(git('cat-file','blob',identity.decode()), f'blob:{identity.decode()[:12]}')
    if args.release_dir:
        for path in args.release_dir.rglob('*'):
            if path.is_file():check(path.read_bytes(), str(path.relative_to(args.release_dir)))
        from PyInstaller.archive.readers import CArchiveReader
        def code_scan(value, name):
            if isinstance(value, types.CodeType):
                check(value.co_filename.encode(), name)
                for item in value.co_consts:code_scan(item,name)
            elif isinstance(value,str):check(value.encode(),name)
            elif isinstance(value,bytes):check(value,name)
        for exe in args.release_dir.glob('YouTubeLikesSync*.exe'):
            archive = CArchiveReader(str(exe))
            for name in archive.toc:
                if name.endswith('.pyz'):
                    pyz=archive.open_embedded_archive(name)
                    for module in pyz.toc:code_scan(pyz.extract(module),f'{exe.name}:{module}')
                else:
                    try:code_scan(marshal.loads(archive.extract(name)), f'{exe.name}:{name}')
                    except (ValueError,TypeError,EOFError):pass
    print(json.dumps({'commits':len(revisions),'unique_blobs':len(objects),'private_marker_findings':sorted(set(findings))}))
    return 1 if findings else 0


if __name__ == '__main__':raise SystemExit(main())
