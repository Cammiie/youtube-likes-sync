import io
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from ytlikes import extension_host as host
from ytlikes.common import SyncError, load_auth, save_auth
from ytlikes.state import State


def frame(value):
    raw = json.dumps(value).encode()
    return struct.pack('<I', len(raw)) + raw


@pytest.mark.parametrize('raw', [b'', b'\x01', struct.pack('<I', 0), struct.pack('<I', host.MAX_MESSAGE+1),
                                struct.pack('<I', 8)+b'{}', frame([]), frame('cookie=SECRET')])
def test_bounded_framing_rejects_truncation_and_wrong_shapes(raw):
    with pytest.raises(SyncError): host.read_message(io.BytesIO(raw))


def test_valid_partial_reads_and_utf8_roundtrip():
    class Fragmented(io.BytesIO):
        def read(self, n=-1): return super().read(min(n, 4))
    assert host.read_message(Fragmented(frame({'text': '\u2764'}))) == {'text': '\u2764'}
    output = io.BytesIO(); host.write_message(output, {'text': '\u2764'})
    assert host.read_message(io.BytesIO(output.getvalue())) == {'text': '\u2764'}


def test_foreign_extension_is_rejected_before_reading_credentials(tmp_path):
    output = io.BytesIO()
    assert host.serve('chrome-extension://untrusted/', io.BytesIO(frame({'headers': 'SECRET'})), output, tmp_path) == 1
    assert not output.getvalue()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('value', [None, {'cookie': 'SAPISID=SECRET\r\nInjected: x'},
    {'cookie': 'SAPISID=SECRET', 'origin': 'https://evil.example'},
    {'cookie': 'SAPISID=SECRET', 'authorization': 'Bearer SECRET'},
    {'cookie': 'SAPISID=SECRET', 'x-goog-authuser': 'other'}, {'cookie': 'no_session=1'}])
def test_header_boundary_excludes_injection_other_origins_and_copied_authorization(value):
    with pytest.raises(SyncError, match='invalid_youtube_request_headers') as error:
        host.captured_headers(value)
    assert 'SECRET' not in str(error.value)


def test_connect_validates_before_commit_and_deduplicates_reconnect(tmp_path, monkeypatch):
    from ytlikes import cli
    class Library:
        def __init__(self, headers, *a, **kw):
            assert headers['origin'] == 'https://music.youtube.com'
            assert 'authorization' not in headers
        def fetch(self): return ([{'video_id': 'existing'}], 'same-account')
    monkeypatch.setattr(cli, 'YouTube', Library)
    monkeypatch.setattr(cli, 'scheduler', lambda action: {})
    message = {'type': 'connect', 'headers': {'cookie': '__Secure-3PAPISID=synthetic'},
               'output': str(tmp_path/'music'), 'switchAccount': False}
    first = host.handle(message, tmp_path, lambda value: None)
    assert first['type'] == 'connected' and first['baselineCreated']
    state = State(tmp_path); stamp = state.get('baseline_at'); state.close()
    again = host.handle(message, tmp_path, lambda value: None)
    assert not again['baselineCreated']
    state = State(tmp_path)
    assert state.get('baseline_at') == stamp and not state.status()['jobs']
    assert b'synthetic' not in (tmp_path/'auth.dpapi').read_bytes()
    state.close()


def test_failed_library_check_preserves_old_connection_and_outputs_no_secrets(tmp_path, monkeypatch):
    from ytlikes import cli
    class Failed:
        def __init__(self, *a, **kw): pass
        def fetch(self): raise SyncError('incomplete_likes_snapshot')
    monkeypatch.setattr(cli, 'YouTube', Failed)
    save_auth(tmp_path, {'cookie': 'original'})
    state = State(tmp_path); state.baseline([{'video_id': 'old'}], 'old-account'); before = state.status(); state.close()
    message = {'type': 'connect', 'headers': {'cookie': '__Secure-3PAPISID=SECRET'},
               'output': str(tmp_path/'music'), 'switchAccount': True}
    output = io.BytesIO()
    host.serve(f'chrome-extension://{host.extension_id()}/', io.BytesIO(frame(message)), output, tmp_path)
    assert b'SECRET' not in output.getvalue()
    assert b'incomplete_likes_snapshot' in output.getvalue()
    assert load_auth(tmp_path) == {'cookie': 'original'}
    state = State(tmp_path); assert state.status() == before; state.close()


def test_boolean_switch_must_be_explicit(tmp_path):
    with pytest.raises(SyncError, match='extension_invalid_message'):
        host.handle({'type': 'connect', 'switchAccount': 'true'}, tmp_path, lambda value: None)


def test_real_python_stdio_protocol_does_not_emit_console_text():
    # Real subprocess checks Windows binary stdio, launcher argument shape, and public origin.
    process = subprocess.run([sys.executable, '-m', 'ytlikes.extension_host',
                              f'chrome-extension://{host.extension_id()}/', '--parent-window=0'],
        input=frame({'type': 'hello'})+frame({'type': 'unknown'}), capture_output=True, timeout=15)
    assert process.returncode == 0 and not process.stderr
    output = io.BytesIO(process.stdout)
    assert host.read_message(output)['type'] == 'ready'
    assert host.read_message(output) == {'type': 'error', 'code': 'extension_invalid_message'}
    assert not output.read()


def test_manifest_permissions_are_limited_to_music_and_native_bridge():
    manifest = json.loads((host.PROJECT/'extension/manifest.json').read_text())
    assert manifest['host_permissions'] == ['https://music.youtube.com/*']
    assert set(manifest['permissions']) == {'activeTab','webRequest','nativeMessaging','storage'}
    assert not manifest.get('content_scripts') and not manifest.get('externally_connectable')
    assert len(host.extension_id()) == 32 and set(host.extension_id()) <= set('abcdefghijklmnop')
