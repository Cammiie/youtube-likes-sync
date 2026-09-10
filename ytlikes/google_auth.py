"""Google desktop OAuth: external browser, PKCE, loopback callback, DPAPI."""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import requests
from ytmusicapi import OAuthCredentials

from .common import SyncError, atomic_write, dpapi, load_auth, save_auth

# Match ytmusicapi's scope for YouTube Music; setup explains Google's broad grant.
SCOPE = 'https://www.googleapis.com/auth/youtube'
TOKEN_URL = 'https://oauth2.googleapis.com/token'


def client_config(value):
    if not isinstance(value,dict) or 'web' in value:
        raise SyncError('google_desktop_client_required')
    data = value.get('installed',value)
    if not isinstance(data,dict):
        raise SyncError('google_client_invalid')
    identity, secret = data.get('client_id'), data.get('client_secret','')
    if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com',identity):
        raise SyncError('google_client_invalid')
    if not isinstance(secret,str) or len(secret)>4096:
        raise SyncError('google_client_invalid')
    if 'redirect_uris' in data:
        redirects=data['redirect_uris']
        if not isinstance(redirects,list) or not any(isinstance(u,str) and
                urlsplit(u).scheme=='http' and urlsplit(u).hostname in ('localhost','127.0.0.1')
                for u in redirects):
            raise SyncError('google_desktop_client_required')
    return {'client_id':identity,'client_secret':secret}


def import_client(root, path):
    try:
        if Path(path).stat().st_size>65536:
            raise ValueError()
        value=client_config(json.loads(Path(path).read_text(encoding='utf-8-sig')))
    except SyncError:
        raise
    except (OSError,ValueError,TypeError):
        raise SyncError('google_client_invalid') from None
    atomic_write(root/'google-client.dpapi',dpapi(json.dumps(value).encode()))
    return value


def load_client(root):
    try:
        return client_config(json.loads(dpapi((root/'google-client.dpapi').read_bytes(),decrypt=True)))
    except FileNotFoundError:
        from .runtime import app_dir
        supplied=app_dir()/'google-client.json'
        if supplied.is_file():
            return import_client(root,supplied)
        raise SyncError('google_client_required') from None
    except (OSError,ValueError,TypeError):
        raise SyncError('google_client_invalid') from None


def token_request(client, payload, *, session=None):
    session=session or requests.Session()
    session.trust_env=False
    data={**payload,'client_id':client['client_id']}
    if client.get('client_secret'):
        data['client_secret']=client['client_secret']
    try:
        with session.post(TOKEN_URL,data=data,timeout=(10,30),allow_redirects=False,stream=True) as response:
            if response.status_code==429:
                raise SyncError('google_rate_limited',1800)
            raw=bytearray()
            for chunk in response.iter_content(8192):
                raw.extend(chunk)
                if len(raw)>65536:
                    raise SyncError('google_response_invalid')
            value=json.loads(raw)
            if not response.ok or value.get('error'):
                error=value.get('error')
                code={'invalid_grant':'youtube_auth_required','invalid_client':'google_client_invalid',
                      'unauthorized_client':'google_desktop_client_required','access_denied':'google_access_denied'}.get(error,'google_signin_failed')
                raise SyncError(code)
            if not isinstance(value.get('access_token'),str) or not value['access_token'] or str(value.get('token_type','')).lower()!='bearer':
                raise SyncError('google_response_invalid')
            ttl=int(value['expires_in'])
            if not 0<ttl<=86400:
                raise SyncError('google_response_invalid')
            scopes=value.get('scope',SCOPE).split()
            if SCOPE not in scopes:
                raise SyncError('google_permission_required')
            return value
    except SyncError:
        raise
    except requests.RequestException:
        raise SyncError('google_network_error') from None
    except (ValueError,KeyError,TypeError,AttributeError):
        raise SyncError('google_response_invalid') from None


def finish_tokens(value, previous=None):
    previous=previous or {}
    refresh=value.get('refresh_token') or previous.get('refresh_token')
    if not isinstance(refresh,str) or not refresh:
        raise SyncError('google_offline_access_required')
    return {'access_token':value['access_token'],'refresh_token':refresh,'token_type':'Bearer',
            'scope':value.get('scope',previous.get('scope',SCOPE)),
            'expires_at':int(time.time())+int(value['expires_in']),'expires_in':int(value['expires_in'])}


class GoogleSignIn:
    def __init__(self, client, *, opener=webbrowser.open, timeout=300):
        self.client=client_config(client)
        self.opener,self.timeout=opener,timeout
        self.cancelled=threading.Event()
        self.received=threading.Event()
        self.state=secrets.token_urlsafe(32)
        self.verifier=secrets.token_urlsafe(64)
        self.code=None
        self.error=None
        self.server=None
        self.url=None

    def cancel(self):
        self.cancelled.set()

    def run(self, on_waiting=lambda:None):
        flow=self
        class Callback(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                uri=urlsplit(self.path)
                query=parse_qs(uri.query)
                expected=f'127.0.0.1:{self.server.server_port}'
                valid=(self.headers.get('Host')==expected and uri.path=='/oauth/callback'
                       and len(query.get('state',[]))==1 and secrets.compare_digest(query['state'][0],flow.state)
                       and not flow.received.is_set() and not flow.cancelled.is_set())
                if not valid:
                    self.send_error(400,'Invalid sign-in response')
                    return
                codes=query.get('code',[])
                if 'error' in query:
                    flow.error='google_access_denied'
                elif len(codes)==1 and 0<len(codes[0])<4096:
                    flow.code=codes[0]
                else:
                    self.send_error(400,'Invalid sign-in response')
                    return
                body=b'<!doctype html><meta charset="utf-8"><title>YouTube Likes Sync</title><p>You can close this tab and return to YouTube Likes Sync.</p>'
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(body)))
                self.send_header('Cache-Control','no-store')
                self.send_header('Referrer-Policy','no-referrer')
                self.send_header('Content-Security-Policy',"default-src 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                finally:
                    flow.received.set()
        class LocalServer(HTTPServer):
            def get_request(self):
                sock,address=super().get_request()
                sock.settimeout(5)
                return sock,address
            def handle_error(self,*args): pass
        self.server=LocalServer(('127.0.0.1',0),Callback)
        self.server.timeout=.25
        redirect=f'http://127.0.0.1:{self.server.server_port}/oauth/callback'
        challenge=base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b'=').decode()
        self.url='https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({
            'client_id':self.client['client_id'],'redirect_uri':redirect,'response_type':'code','scope':SCOPE,
            'state':self.state,'code_challenge':challenge,'code_challenge_method':'S256',
            'access_type':'offline','prompt':'consent select_account'})
        try:
            if not self.opener(self.url):
                raise SyncError('google_browser_open_failed')
            on_waiting()
            deadline=time.monotonic()+self.timeout
            while not self.received.is_set():
                if self.cancelled.is_set():
                    raise SyncError('setup_cancelled')
                if time.monotonic()>deadline:
                    raise SyncError('google_signin_timeout')
                self.server.handle_request()
            if self.error:
                raise SyncError(self.error)
            if self.cancelled.is_set():
                raise SyncError('setup_cancelled')
            value=token_request(self.client,{'grant_type':'authorization_code','code':self.code,
                'redirect_uri':redirect,'code_verifier':self.verifier})
            if self.cancelled.is_set():
                raise SyncError('setup_cancelled')
            return {'kind':'google_oauth','client':self.client,'tokens':finish_tokens(value)}
        finally:
            self.server.server_close()
            self.code=None
            self.verifier=None
            self.url=None


class ProtectedCredentials(OAuthCredentials):
    def __init__(self, record, root=None):
        self.record,self.root=record,root
        client=client_config(record['client'])
        super().__init__(client['client_id'],client['client_secret'])

    def refresh_token(self, refresh_token):
        value=token_request(self.record['client'],{'grant_type':'refresh_token','refresh_token':refresh_token})
        self.record['tokens']=finish_tokens(value,self.record['tokens'])
        if self.root is not None:
            current=load_auth(self.root)
            if current.get('kind')=='google_oauth' and current.get('tokens',{}).get('refresh_token')==refresh_token and current.get('client')==self.record['client']:
                save_auth(self.root,self.record)
        return value
