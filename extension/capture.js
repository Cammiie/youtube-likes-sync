// Pure validation shared by the worker and protocol tests.
export function captureHeaders(details, tabId) {
  if (details.tabId !== tabId || details.method !== 'POST' ||
      details.initiator !== 'https://music.youtube.com') return null;
  let url;
  try { url = new URL(details.url); } catch { return null; }
  if (url.origin !== 'https://music.youtube.com' || url.pathname !== '/youtubei/v1/browse') return null;
  const allowed = new Set(['cookie', 'x-goog-authuser', 'x-goog-visitor-id', 'user-agent']);
  const headers = {origin: 'https://music.youtube.com'};
  for (const {name, value} of details.requestHeaders || []) {
    const key = name.toLowerCase();
    if (allowed.has(key) && typeof value === 'string') headers[key] = value;
  }
  if (!/(?:^|;\s*)(?:__Secure-3PAPISID|SAPISID)=.+/.test(headers.cookie || '')) return null;
  return headers;
}
