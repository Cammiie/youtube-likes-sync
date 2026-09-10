"""Apply a narrow raw-audio callback option and bundle the upstream engine."""
import os
from pathlib import Path
import shutil
import subprocess
import re
from ytlikes.common import data_dir
from ytlikes.media import run_ffmpeg

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = (data_dir() / "monochrome").resolve()


def main():
    api = RUNTIME / "js/api.js"
    backup = RUNTIME / "js/api.js.upstream"
    if not backup.exists():
        shutil.copyfile(api, backup)
    original = backup.read_text(encoding="utf-8")
    anchor = "            if (!isVideo) {\n                blob = await applyAudioPostProcessing"
    if original.count(anchor) != 1:
        raise RuntimeError("Pinned upstream download method changed")
    replacement = """            // YouTubeLikesSync: validate original bytes natively; never transcode a lossy source.
            if (options.ytlikesRawDownload) return blob;

            if (!isVideo) {
                blob = await applyAudioPostProcessing"""
    modified = original.replace(anchor, replacement)
    encrypted_codec = "                        preserveAtmos ? 'copy' : 'flac',"
    if modified.count(encrypted_codec) != 1:
        raise RuntimeError('Pinned upstream encrypted-audio branch changed')
    # Opt-in encrypted FLAC must be stream-copied, never encoded from a lossy source.
    modified = modified.replace(encrypted_codec,
        "                        preserveAtmos || options.ytlikesRawDownload ? 'copy' : 'flac',")
    api.write_text(modified, encoding="utf-8")
    for source, target in [('worker.html','worker.html'),('worker.js','ytlikes-worker.js'),('diagnostics.mjs','ytlikes-diagnostics.mjs'),('vite.config.mjs','ytlikes.vite.config.mjs'),
                           ('self-test.html','self-test.html'),('self-test.js','ytlikes-self-test.js'),
                           ('verify-api.html','verify-api.html'),('verify-api.js','ytlikes-verify-api.js')]:
        shutil.copyfile(PROJECT / 'browser' / source, RUNTIME / target)
    html = (RUNTIME / 'worker.html').read_text(encoding='utf-8')
    css = re.search(r'<style>(.*?)</style>', html, flags=re.S).group(1)
    (RUNTIME / 'ytlikes-worker.css').write_text(css, encoding='utf-8')
    (RUNTIME / 'worker.html').write_text(re.sub(r'<style>.*?</style>', '<link rel="stylesheet" href="/ytlikes-worker.css">', html, flags=re.S), encoding='utf-8')
    subprocess.run([shutil.which('node'), str(RUNTIME / 'node_modules/vite/bin/vite.js'), 'build', '--config', 'ytlikes.vite.config.mjs'], cwd=RUNTIME, check=True)
    fixtures = RUNTIME / 'ytlikes-dist/ytlikes-fixtures'
    fixtures.mkdir(exist_ok=True)
    for file,codec in [('fixture.flac','flac'),('fixture.m4a','aac')]:
        result = run_ffmpeg(['-y','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','1','-c:a',codec,str(fixtures/file)])
        if result.returncode:
            raise RuntimeError('Could not generate local verification tones')
    print('Monochrome browser engine bundled.', flush=True)


if __name__ == '__main__':
    main()
