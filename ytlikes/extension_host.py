"""One-shot Chromium native messaging host. Never log request bodies or credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import sys

from .common import SyncError, atomic_write, data_dir
from .runtime import app_dir, assets_dir, bundled

HOST = 'com.youtubelikes.sync'
PROJECT = assets_dir()
MAX_MESSAGE = 128 * 1024


def extension_id():
    key = json.loads((PROJECT/'extension/manifest.json').read_text(encoding='utf-8'))['key']
    digest = hashlib.sha256(base64.b64decode(key, validate=True)).hexdigest()[:32]
    return ''.join(chr(ord('a') + int(c, 16)) for c in digest)


def read_message(stream):
    header = stream.read(4)
    if len(header) != 4:
        raise SyncError('extension_disconnected')
    length = struct.unpack('<I', header)[0]
    if not 0 < length <= MAX_MESSAGE:
        raise SyncError('extension_invalid_message')
    body = bytearray()
    while len(body) < length:
        part = stream.read(length-len(body))
        if not part:
            raise SyncError('extension_disconnected')
        body.extend(part)
    try:
        value = json.loads(body)
    except (UnicodeError, ValueError):
        raise SyncError('extension_invalid_message') from None
    if not isinstance(value, dict):
        raise SyncError('extension_invalid_message')
    return value


def write_message(stream, value):
    raw = json.dumps(value, ensure_ascii=True).encode('utf-8')
    stream.write(struct.pack('<I', len(raw)) + raw)
    stream.flush()


def captured_headers(value):
    from .youtube import parse_headers
    if not isinstance(value, dict) or len(value) > 12:
        raise SyncError('invalid_youtube_request_headers')
    allowed = {'cookie', 'x-goog-authuser', 'x-goog-visitor-id', 'user-agent', 'origin', 'x-origin'}
    if any(k not in allowed or not isinstance(v, str) or len(v) > 65536 or '\r' in v or '\n' in v
           for k, v in value.items()):
        raise SyncError('invalid_youtube_request_headers')
    if value.get('origin', 'https://music.youtube.com') != 'https://music.youtube.com' or value.get('x-origin', 'https://music.youtube.com') != 'https://music.youtube.com':
        raise SyncError('invalid_youtube_request_headers')
    if not value.get('x-goog-authuser', '0').isdigit():
        raise SyncError('invalid_youtube_request_headers')
    # Cookies are used only against Music; the library creates fresh SAPISIDHASH values.
    return parse_headers(json.dumps({**value, 'origin': 'https://music.youtube.com'}))


def handle(message, root, send):
    from .onboarding import complete_setup, installation_settings
    operation = message.get('type')
    if operation == 'hello':
        return {'type': 'ready', 'output': installation_settings(root)['output'],
                'connected': (root/'auth.dpapi').exists()}
    if operation != 'connect' or not isinstance(message.get('switchAccount'), bool):
        raise SyncError('extension_invalid_message')
    output = message.get('output')
    if not isinstance(output, str) or not output.strip() or len(output) > 2048:
        raise SyncError('output_folder_unavailable')
    headers = captured_headers(message.get('headers'))
    send({'type': 'progress', 'phase': 'checking'})
    result = complete_setup(root, headers, output.strip(), allow_account_change=message['switchAccount'])
    return {'type': 'connected', 'likes': result['current_likes'], 'output': result['output'],
            'schedulerWarning': bool(result.get('scheduler_warning')), 'baselineCreated': result['baseline_created']}


def serve(origin, stream_in, stream_out, root):
    if origin != f'chrome-extension://{extension_id()}/':
        return 1
    send = lambda value: write_message(stream_out, value)
    try:
        message = read_message(stream_in)
        # hello keeps the port open while the user-selected tab reloads.
        if message.get('type') == 'hello':
            send(handle(message, root, send))
            message = read_message(stream_in)
        send(handle(message, root, send))
    except SyncError as error:
        # Only project-owned fixed codes can cross this boundary.
        send({'type': 'error', 'code': error.code})
    except Exception:
        try:
            send({'type': 'error', 'code': 'extension_connection_failed'})
        except Exception:
            pass
    return 0


def register(root=None):
    import winreg
    root = root or data_dir()
    folder = root/'native-messaging'
    python = app_dir()/'YouTubeLikesSync.Console.exe' if bundled() else PROJECT/'.venv/Scripts/python.exe'
    if not python.is_file():
        raise SyncError('extension_install_required')
    # Batch values are quoted and percent-escaped, never derived from browser input.
    executable = str(python).replace('%', '%%')
    project = str(PROJECT).replace('%', '%%')
    launcher = folder/'host.cmd'
    arguments = 'native-host' if bundled() else '-m ytlikes.extension_host'
    atomic_write(launcher, (f'@echo off\r\nsetlocal DisableDelayedExpansion\r\nchcp 65001 >nul\r\ncd /d "{project}"\r\n"{executable}" {arguments} %*\r\n').encode('utf-8'))
    manifest = folder/'host.json'
    atomic_write(manifest, json.dumps({'name': HOST, 'description': 'YouTube Likes Sync local connector',
        'path': str(launcher), 'type': 'stdio', 'allowed_origins': [f'chrome-extension://{extension_id()}/']}).encode())
    # Brave supports its own registration; Chrome registration also covers compatible browsers.
    for browser in ('Google\\Chrome', 'Microsoft\\Edge', 'BraveSoftware\\Brave-Browser'):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f'Software\\{browser}\\NativeMessagingHosts\\{HOST}',
                                    0, winreg.KEY_WRITE | view) as key:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, str(manifest))
    return {'extension_id': extension_id(), 'folder': str(PROJECT/'extension')}


def main():
    if sys.argv[1:] == ['--register']:
        print(json.dumps(register()))
        return 0
    if os.name == 'nt':
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    return serve(sys.argv[1] if len(sys.argv)>1 else '', sys.stdin.buffer, sys.stdout.buffer, data_dir())


if __name__ == '__main__':
    raise SystemExit(main())
