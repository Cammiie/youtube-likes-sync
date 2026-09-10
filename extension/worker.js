import {captureHeaders} from './capture.js';

const HOST = 'com.youtubelikes.sync';
let job = null;
let infoPort = null;
let state = {phase: 'idle'};
const restored = chrome.storage.session.get('state').then(saved => {
  if (saved.state) state = ['opening', 'capturing', 'checking'].includes(saved.state.phase)
    ? {phase: 'error', code: 'extension_interrupted'} : saved.state;
});

function publish(next) {
  state = {...state, ...next};
  // Status only. Cookies and request headers are never written to extension storage.
  chrome.storage.session.set({state});
  chrome.action.setBadgeText({text: state.phase === 'connected' ? 'OK' : state.phase === 'error' ? '!' : ''});
  chrome.action.setBadgeBackgroundColor({color: '#173b54'});
}
function finish(next) {
  const previous = job;
  job = null;
  if (previous) { clearTimeout(previous.timer); previous.port.disconnect(); }
  publish(next);
}
function nativeInfo() {
  if (job || infoPort) return;
  const port = infoPort = chrome.runtime.connectNative(HOST);
  port.onMessage.addListener(message => {
    if (message.type === 'ready') {
      publish({output: message.output, existingConnection: message.connected, hostReady: true});
      infoPort = null;
      port.disconnect();
    }
  });
  port.onDisconnect.addListener(() => {
    const failed = !!chrome.runtime.lastError;
    if (infoPort === port) {
      infoPort = null;
      publish({phase: 'error', code: failed ? 'extension_install_required' : 'extension_connection_failed', hostReady: false});
    }
  });
  port.postMessage({type: 'hello'});
}

async function start(options) {
  if (job) return;
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tab?.url?.startsWith('https://music.youtube.com/')) {
    publish({phase: 'error', code: 'music_tab_required'}); return;
  }
  if (infoPort) { const old = infoPort; infoPort = null; old.disconnect(); }
  const port = chrome.runtime.connectNative(HOST);
  const current = job = {port, tabId: tab.id, output: options.output,
    switchAccount: options.switchAccount === true, captured: false, timer: null};
  publish({phase: 'opening', code: null});
  current.timer = setTimeout(() => {
    if (job === current) finish({phase: 'error', code: 'extension_capture_timeout'});
  }, 60000);
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError; // Consume error without logging browser/native details.
    if (job === current) finish({phase: 'error', code: 'extension_connection_failed'});
  });
  port.onMessage.addListener(message => {
    if (job !== current) return;
    if (message.type === 'ready') {
      publish({phase: 'capturing', hostReady: true});
      // A reload preserves the selected account; do not switch to a guessed account URL.
      chrome.tabs.reload(current.tabId).catch(() => finish({phase: 'error', code: 'music_tab_required'}));
    } else if (message.type === 'progress') {
      publish({phase: 'checking'});
    } else if (message.type === 'connected') {
      finish({phase: 'connected', likes: message.likes, output: message.output,
        schedulerWarning: message.schedulerWarning, existingConnection: true});
    } else if (message.type === 'error') {
      finish({phase: 'error', code: message.code});
    }
  });
  port.postMessage({type: 'hello'});
}

chrome.webRequest.onBeforeSendHeaders.addListener(details => {
  if (!job || job.captured || state.phase !== 'capturing') return;
  const headers = captureHeaders(details, job.tabId);
  if (!headers) return;
  job.captured = true;
  clearTimeout(job.timer);
  // The native app owns full pagination/commit once checking starts. Closing the popup
  // must not interrupt its transaction or imply that cancelling can undo a commit.
  publish({phase: 'checking'});
  job.port.postMessage({type: 'connect', headers, output: job.output, switchAccount: job.switchAccount});
}, {urls: ['https://music.youtube.com/youtubei/v1/browse*'], types: ['xmlhttprequest']}, ['requestHeaders', 'extraHeaders']);

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('popup.html')) return;
  restored.then(async () => {
    if (message.type === 'status') { nativeInfo(); respond(state); }
    else if (message.type === 'connect') { await start(message); respond(state); }
    else if (message.type === 'openMusic') { await chrome.tabs.create({url: 'https://music.youtube.com/library'}); respond({ok: true}); }
  }).catch(() => { publish({phase: 'error', code: 'extension_connection_failed'}); respond(state); });
  return true;
});
