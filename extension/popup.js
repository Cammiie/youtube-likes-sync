const $ = id => document.getElementById(id);
const errors = {
  extension_install_required: 'Run Install.cmd in the Windows app first, then reopen this extension.',
  extension_connection_failed: 'The Windows app could not connect. Run Connect YouTube Music.cmd to repair the connector, then try again.',
  extension_interrupted: 'The browser connection stopped. Your saved downloads remain intact. Try connecting again.',
  extension_capture_timeout: 'No signed-in request was found. Open Music, sign in, visit Library, then try again.',
  music_tab_required: 'Open your signed-in YouTube Music tab, then click this extension again.',
  different_youtube_account: 'This is a different account. To switch, check the account option above and connect again. Existing downloads stay intact.',
  youtube_auth_required: 'YouTube could not read this account. Check that its Music library works in this tab, then try again.',
  incomplete_likes_snapshot: 'We could not read all your likes. Your connection is unchanged. Please try again.',
  already_running: 'A sync is finishing. Wait a moment, then connect again.',
  output_folder_unavailable: 'Enter an available, full Windows folder path.',
  youtube_network_error: 'Could not finish reading your likes. Check your connection and try again.',
  invalid_youtube_request_headers: 'The Music session could not be read. Reload your Music library, then try again.'
};
let dirty = false;
$('output').addEventListener('input', () => { dirty = true; });
function render(state) {
  if (!dirty && state.output) $('output').value = state.output;
  const busy = ['opening', 'capturing', 'checking'].includes(state.phase);
  $('connect').disabled = busy || !state.hostReady;
  $('output').disabled = busy;
  $('switchAccount').disabled = busy;
  $('open').disabled = busy;
  $('connect').textContent = state.phase === 'connected' ? 'Reconnect account' : busy ? 'Connecting…' : 'Connect YouTube Music';
  const text = {
    idle: 'Sign in to Music with the account you want to sync.',
    opening: 'Connecting to the Windows app…',
    capturing: 'Reading the session from your Music tab…',
    checking: 'Checking your complete liked-songs library. You can close this popup; reopen it to see the result.',
    connected: state.schedulerWarning ? 'Connected, but scheduling needs repair. Run Install.cmd to enable automatic checks.' :
      `Connected. ${state.likes} existing likes checked. New likes will download every five minutes while this PC is awake and you’re signed in.`
  };
  $('status').textContent = state.phase === 'error' ? errors[state.code] || 'The library check failed. Your existing connection is unchanged. Try again later.' : text[state.phase] || text.idle;
  $('status').dataset.error = String(state.phase === 'error');
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes.state) render(changes.state.newValue);
});
$('connect').addEventListener('click', async () => {
  $('connect').disabled = true;
  render(await chrome.runtime.sendMessage({type: 'connect', output: $('output').value, switchAccount: $('switchAccount').checked}));
});
$('open').addEventListener('click', () => chrome.runtime.sendMessage({type: 'openMusic'}));
render(await chrome.runtime.sendMessage({type: 'status'}));
