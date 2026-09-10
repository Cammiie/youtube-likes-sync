import json
from pathlib import Path
import threading
import time

import pytest
import requests

from ytlikes import browser_host, browser_download
from ytlikes.browser_host import Host, Handler, ThreadingHTTPServer
from ytlikes.common import SyncError, config
from ytlikes.media import destination, validate
from test_media import audio_files, track, Provider


@pytest.fixture
def host(tmp_path, monkeypatch):
    root=tmp_path/'bridge'
    engine=Host(root, launch_browser=False)
    engine.dist.mkdir(parents=True)
    (engine.dist/'worker.html').write_text('<h1>Fixture</h1>')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    server.host=engine
    port=server.server_port
    base=f'http://localhost:{port}'
    monkeypatch.setattr(browser_host,'PORT',port)
    monkeypatch.setattr(browser_host,'BASE',base)
    monkeypatch.setattr(browser_download,'BASE',base)
    monkeypatch.setattr(browser_download,'data_dir',lambda:root)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    session=requests.Session()
    session.trust_env=False
    session.headers['X-Bridge-Key']=engine.key
    yield engine,base,session
    server.shutdown()
    server.server_close()


def test_auth_origin_and_path_traversal(host):
    engine,base,session=host
    assert requests.get(base+'/health').status_code==403
    assert session.get(base+'/health').json()['engine']=='upstream'
    assert session.post(base+'/jobs',headers={'Origin':'https://foreign.example'},json={}).status_code==403
    assert session.get(base+'/%2e%2e/state.sqlite3').status_code==404
    r=requests.get(base+'/worker.html')
    assert 'HttpOnly' in r.headers['Set-Cookie']
    assert 'SameSite=Strict' in r.headers['Set-Cookie']


def test_host_restart_preserves_browser_cookie_key(tmp_path):
    first=Host(tmp_path,launch_browser=False)
    restarted=Host(tmp_path,launch_browser=False)
    assert first.key==restarted.key
    assert first.key.encode() not in (tmp_path/'bridge-key.dpapi').read_bytes()


def test_job_claim_upload_result_and_cleanup(host):
    engine,base,session=host
    id=session.post(base+'/jobs',json={'track':track(),'max_bytes':100,'timeout_ms':1000}).json()['id']
    job=session.get(base+'/worker/next').json()
    assert job['id']==id
    assert 'path' not in job and 'key' not in job
    assert session.post(base+'/worker/started/'+id,json={'delivery_id':job['delivery_id']}).ok
    assert session.post(base+'/worker/result/'+id,data=b'fLaCfixture').status_code==200
    assert session.get(base+'/jobs/'+id).json()['state']=='done'
    assert session.get(base+'/result/'+id).content==b'fLaCfixture'
    session.delete(base+'/jobs/'+id)
    assert not list(engine.spool.glob('*.audio'))


def test_encrypted_lossless_permission_is_owned_by_local_configuration(host):
    engine,base,session=host
    body={'track':track(),'max_bytes':100,'timeout_ms':1000,'allow_encrypted_lossless':True}
    id=session.post(base+'/jobs',json=body).json()['id']
    assert session.get(base+'/worker/next').json()['allow_encrypted_lossless'] is False
    session.delete(base+'/jobs/'+id)
    (engine.root/'config.json').write_text(json.dumps({'allow_encrypted_lossless':True}))
    body['allow_encrypted_lossless']=False
    id=session.post(base+'/jobs',json=body).json()['id']
    assert session.get(base+'/worker/next').json()['allow_encrypted_lossless'] is True
    session.delete(base+'/jobs/'+id)


def test_upload_limit_cancelled_result_and_one_job(host):
    _,base,session=host
    body={'track':track(),'max_bytes':4,'timeout_ms':1000}
    id=session.post(base+'/jobs',json=body).json()['id']
    assert session.post(base+'/jobs',json=body).status_code==400
    job=session.get(base+'/worker/next').json()
    session.post(base+'/worker/started/'+id,json={'delivery_id':job['delivery_id']})
    assert session.post(base+'/worker/result/'+id,data=b'too many bytes').status_code==413
    session.delete(base+'/jobs/'+id)
    assert session.post(base+'/worker/result/'+id,data=b'test').status_code==404


def test_unknown_error_cannot_persist_transport_secrets(host):
    engine,base,session=host
    id=session.post(base+'/jobs',json={'track':track(),'max_bytes':100,'timeout_ms':1000}).json()['id']
    session.post(base+'/worker/error/'+id,json={'code':'https://secret.example/signed?token=secret',
        'diagnostic':{'track_request_sent':False,'url':'https://secret.example/signed?token=secret'}})
    assert session.get(base+'/jobs/'+id).json()['code']=='monochrome_download_failed'
    assert session.get(base+'/health').json()['last_diagnostic']=={
        'code':'monochrome_download_failed','track_request_sent':False}


def test_abandoned_job_lease_releases_queue_and_spool(host):
    engine,base,session=host
    body={'track':track(),'max_bytes':100,'timeout_ms':1000}
    old=session.post(base+'/jobs',json=body).json()['id']
    session.get(base+'/worker/next')
    abandoned=engine.jobs[old]
    abandoned['path'].write_bytes(b'interrupted')
    abandoned['created']-=62
    new=session.post(base+'/jobs',json=body).json()['id']
    assert abandoned['cancelled']
    assert not abandoned['path'].exists()
    assert old not in engine.jobs
    assert session.get(base+'/worker/next').json()['id']==new
    assert session.post(base+'/worker/result/'+old,data=b'late').status_code==404
    session.delete(base+'/jobs/'+new)


@pytest.mark.parametrize('file,expected', [('lossless.flac',None),('lossy.m4a','lossy_audio_rejected')])
def test_real_http_bridge_native_audio_gate(host,tmp_path,audio_files,monkeypatch,file,expected):
    engine,base,session=host
    monkeypatch.setattr(browser_download,'ensure_host',lambda:session)
    def worker():
        job=session.get(base+'/worker/next',timeout=15).json()
        session.post(base+'/worker/started/'+job['id'],json={'delivery_id':job['delivery_id']})
        session.post(base+'/worker/result/'+job['id'],data=(audio_files/file).read_bytes(),timeout=15)
    thread=threading.Thread(target=worker)
    thread.start()
    settings=config(tmp_path)
    settings['max_job_seconds']=10
    target=destination(tmp_path/'out',track())
    dl=browser_download.BrowserDownloader(Provider(),settings)
    if expected:
        with pytest.raises(SyncError,match=expected):
            dl.download(track(),'synthetic',target)
        assert not target.exists()
    else:
        dl.download(track(),'synthetic',target)
        assert validate(target,1)['monochrome_track_id']==['123']
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert not list(engine.spool.glob('*.audio'))
    assert not list(tmp_path.rglob('*.part'))


def test_closed_browser_cannot_strand_unacknowledged_delivery(host):
    engine,base,session=host
    id=session.post(base+'/jobs',json={'track':track(),'max_bytes':100,'timeout_ms':60000}).json()['id']
    old=session.get(base+'/worker/next').json()
    assert engine.jobs[id]['state']=='claimed'
    engine.jobs[id]['claimed_at']-=16
    new=session.get(base+'/worker/next').json()
    assert new['id']==id and new['delivery_id']!=old['delivery_id']
    assert session.post(base+'/worker/started/'+id,json={'delivery_id':old['delivery_id']}).status_code==409
    assert session.post(base+'/worker/started/'+id,json={'delivery_id':new['delivery_id']}).ok
    assert session.post(base+'/worker/result/'+id,data=b'new browser result').ok
    session.delete(base+'/jobs/'+id)


def test_interrupted_browser_upload_removes_partial_audio(host):
    import socket
    from urllib.parse import urlsplit
    engine,base,session=host
    id=session.post(base+'/jobs',json={'track':track(),'max_bytes':100,'timeout_ms':60000}).json()['id']
    job=session.get(base+'/worker/next').json()
    session.post(base+'/worker/started/'+id,json={'delivery_id':job['delivery_id']})
    address=urlsplit(base)
    with socket.create_connection(('127.0.0.1',address.port),timeout=5) as connection:
        header=(f'POST /worker/result/{id} HTTP/1.1\r\nHost: {address.netloc}\r\n'
                f'X-Bridge-Key: {engine.key}\r\nContent-Length: 20\r\n\r\n')
        connection.sendall(header.encode()+b'partial')
        connection.shutdown(socket.SHUT_WR)
        connection.recv(1024)
    assert session.get(base+'/jobs/'+id).json()['state']=='failed'
    assert not list(engine.spool.glob('*.audio'))
    session.delete(base+'/jobs/'+id)
    assert session.post(base+'/jobs',json={'track':track(),'max_bytes':100,'timeout_ms':1000}).ok
