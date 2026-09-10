import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const captureSource = fs.readFileSync(new URL('../extension/capture.js', import.meta.url), 'utf8').replace('export function', 'function');
const workerSource = fs.readFileSync(new URL('../extension/worker.js', import.meta.url), 'utf8').replace(/^import .*;\r?\n/, '');
const event = () => ({listeners: [], addListener(fn) {this.listeners.push(fn);}, emit(...args) {for (const fn of this.listeners) fn(...args);}});

function harness() {
  const ports = [], saved = [], timers = new Map(), reloads = [];
  const chrome = {runtime: {id: 'test', getURL: file => `chrome-extension://test/${file}`, onMessage: event(),
    connectNative() {
      const port = {onMessage: event(), onDisconnect: event(), sent: [], postMessage(x) {this.sent.push(x);}, disconnect() {this.closed = true;}};
      ports.push(port); return port;
    }},
    storage: {session: {get: async () => ({}), set: value => {saved.push(structuredClone(value)); return Promise.resolve();}}},
    action: {setBadgeText() {}, setBadgeBackgroundColor() {}},
    tabs: {query: async () => [{id: 17, url: 'https://music.youtube.com/library'}], reload: async id => reloads.push(id), create: async () => ({})},
    webRequest: {onBeforeSendHeaders: event()}};
  const context = vm.createContext({chrome, URL, Set, clearTimeout: id => timers.delete(id), setTimeout: fn => {const id = Symbol(); timers.set(id, fn); return id;}});
  vm.runInContext(captureSource + '\n' + workerSource, context);
  async function send(value, url = 'chrome-extension://test/popup.html') {
    return new Promise(resolve => chrome.runtime.onMessage.emit(value, {id: 'test', url}, resolve));
  }
  const details = {tabId: 17, initiator: 'https://music.youtube.com', method: 'POST',
    url: 'https://music.youtube.com/youtubei/v1/browse?prettyPrint=false',
    requestHeaders: [{name: 'Cookie', value: '__Secure-3PAPISID=SECRET'}, {name: 'Authorization', value: 'SECRET-HASH'}, {name: 'X-Goog-AuthUser', value: '1'}]};
  return {chrome, ports, saved, timers, reloads, send, details, capture: d => context.captureHeaders(d, 17)};
}

test('captures only the chosen Music tab and excludes authorization', () => {
  const h = harness();
  assert.equal(h.capture({...h.details, tabId: 18}), null);
  assert.equal(h.capture({...h.details, initiator: 'https://evil.example'}), null);
  assert.equal(h.capture({...h.details, url: 'https://music.youtube.com.evil.example/youtubei/v1/browse'}), null);
  assert.equal(h.capture({...h.details, method: 'GET'}), null);
  const headers = h.capture(h.details);
  assert.equal(headers['x-goog-authuser'], '1');
  assert.equal(headers.authorization, undefined);
});

test('no capture before click; one transfer; secrets never reach storage; result survives popup closure', async () => {
  const h = harness();
  h.chrome.webRequest.onBeforeSendHeaders.emit(h.details);
  assert.equal(h.ports.length, 0);
  await h.send({type: 'connect', output: 'C:\\Music', switchAccount: true});
  const port = h.ports[0];
  port.onMessage.emit({type: 'ready'});
  assert.deepEqual(h.reloads, [17]);
  h.chrome.webRequest.onBeforeSendHeaders.emit(h.details);
  h.chrome.webRequest.onBeforeSendHeaders.emit(h.details);
  assert.equal(port.sent.filter(x => x.type === 'connect').length, 1);
  assert.equal(port.sent[1].switchAccount, true);
  assert.equal(h.timers.size, 0);
  assert.ok(!JSON.stringify(h.saved).includes('SECRET'));
  port.onMessage.emit({type: 'connected', likes: 32, output: 'C:\\Music'});
  assert.equal(h.saved.at(-1).state.phase, 'connected');
  assert.ok(port.closed);
});

test('missing host and capture timeout preserve retryable errors', async () => {
  const h = harness();
  await h.send({type: 'connect', output: 'C:\\Music'});
  h.ports[0].onDisconnect.emit();
  assert.equal(h.saved.at(-1).state.code, 'extension_connection_failed');
  await h.send({type: 'connect', output: 'C:\\Music'});
  [...h.timers.values()][0]();
  assert.equal(h.saved.at(-1).state.code, 'extension_capture_timeout');
  assert.equal(h.timers.size, 0);
});

test('ordinary web pages cannot trigger a native connection', async () => {
  const h = harness();
  h.chrome.runtime.onMessage.emit({type: 'connect'}, {id: 'test', url: 'https://music.youtube.com/'}, () => assert.fail('must ignore'));
  await Promise.resolve();
  assert.equal(h.ports.length, 0);
});
