"""Copy only distributable engine assets and third-party notices into a build tree."""
import argparse
import importlib.metadata
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    target = args.project/'build-assets'
    if target.exists():
        raise SystemExit('Use a fresh build checkout; build-assets already exists.')
    licenses = target/'licenses'; licenses.mkdir(parents=True)
    shutil.copytree(args.project/'licenses', licenses/'project')
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.txt'):
        shutil.copy2(args.project/name, licenses/name)
    for dist in importlib.metadata.distributions():
        if dist.metadata['Name'].lower() in ('pip','pytest','pygments','git-filter-repo','iniconfig','pluggy'):
            continue
        for file in dist.files or []:
            if Path(str(file)).name.lower().startswith(('license','licence','copying','notice')) and Path(str(file)).suffix not in ('.py','.pyc','.pyo'):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    dest = licenses/dist.metadata['Name']/str(file).replace('../','').replace('..\\','')
                    dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, dest)
    python_license = Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.is_file(): shutil.copy2(python_license, licenses/'Python-LICENSE.txt')
    print('Prepared dependency notices; no browser or runtime data copied.')


if __name__ == '__main__': main()
