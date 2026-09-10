"""Install a pinned, licensed upstream source tree outside OneDrive."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import requests
from ytlikes.common import data_dir

COMMIT = "60b76d0d9258b9aabc6a9ee83cb8a54bd76ce90b"
PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = data_dir() / "monochrome"


def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    marker = RUNTIME / ".upstream-commit"
    if not marker.exists():
        print("Fetching pinned Monochrome source...", flush=True)
        r = requests.get(f"https://codeload.github.com/monochrome-music/monochrome/zip/{COMMIT}", timeout=(15, 180))
        r.raise_for_status()
        digest = hashlib.sha256(r.content).hexdigest()
        prefix = f"monochrome-{COMMIT}/"
        with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 2 * 1024**3:
                raise ValueError("Unexpected archive size")
            for item in archive.infolist():
                if not item.filename.startswith(prefix):
                    raise ValueError("Unexpected archive prefix")
                relative = item.filename[len(prefix):]
                if not relative:
                    continue
                dest = (RUNTIME / relative).resolve()
                if not dest.is_relative_to(RUNTIME.resolve()):
                    raise ValueError("Archive path escape")
                if item.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(archive.read(item))
        marker.write_text(COMMIT)
        (PROJECT / "monochrome-source.json").write_text(json.dumps({
            "repository": "https://github.com/monochrome-music/monochrome", "commit": COMMIT,
            "archive_sha256": digest, "license": "Apache-2.0"}, indent=2))
    elif marker.read_text().strip() != COMMIT:
        raise ValueError("Different upstream revision already installed")
    lock = PROJECT / "monochrome-package-lock.json"
    if lock.exists():
        shutil.copyfile(lock, RUNTIME / "package-lock.json")
    npm = shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("Node.js/npm is required")
    print("Installing upstream dependencies with lifecycle scripts disabled...", flush=True)
    subprocess.run([npm, "ci" if lock.exists() else "install", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=RUNTIME, check=True)
    if not lock.exists():
        shutil.copyfile(RUNTIME / "package-lock.json", lock)
    print("Pinned Monochrome source installed.", flush=True)


if __name__ == "__main__":
    main()
