// Thin caller of the pinned upstream download engine. Transport URLs and tokens
// remain inside this browser profile and are never sent to the Python watcher.
import {verificationFailureCode, unsupportedAudio} from './ytlikes-diagnostics.mjs';
const status = document.getElementById('status');
const display = (message) => { status.textContent = message; };
// Upstream diagnostics can include signed URLs. Do not retain them in console logs.
console.log = console.warn = console.error = console.debug = console.info = () => {};
const errorCode = (error) => {
    const message = String(error?.message || '').toLowerCase();
    if (/ytlikes_encrypted/.test(message)) return 'encrypted_audio_unsupported';
    if (/turnstile|verif|captcha/.test(message)) return 'monochrome_verification_required';
    if (/quality|lossless/.test(message)) return 'monochrome_lossless_unavailable';
    return 'monochrome_download_failed';
};
const send = (path, payload) => fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
document.getElementById('retry').onclick = async () => {
    const r = await send('/worker/retry', {});
    display(r.ok ? 'Pending tracks will retry on the next check.' : 'Could not request a retry.');
};
try {
    const {LosslessAPI} = await import('./js/api.js');
    const {apiSettings, preferDolbyAtmosSettings, losslessContainerSettings} = await import('./js/storage.js');
    preferDolbyAtmosSettings.setEnabled(false);
    losslessContainerSettings.setContainer('flac');
    const api = new LosslessAPI(apiSettings);
    // The source metadata has already been matched by the watcher. Reuse it
    // instead of repeating another network catalog lookup before every download.
    const originalMetadata = api.getTrackMetadata.bind(api);
    let activeTrack = null;
    let allowEncryptedLossless = false;
    let verificationFailure = null;
    let trackRequestSent = false;
    let turnstileCode = null;
    let downloadDiagnostic = {};
    let enrichFailure = null;
    let rateLimitedUntil = 0;
    const originalLoadTurnstile = api.loadTurnstile.bind(api);
    api.loadTurnstile = async () => {
        const sdk = await originalLoadTurnstile();
        return {
            render:(container, options) => sdk.render(container, {...options, 'error-callback':(code) => {
                if (/^\d{6}$/.test(String(code))) turnstileCode = String(code);
                return options['error-callback']?.(code);
            }}),
            execute:sdk.execute.bind(sdk),
            remove:sdk.remove?.bind(sdk)
        };
    };
    const originalFetch = api.fetchWithTimeout.bind(api);
    api.fetchWithTimeout = async (url, ...args) => {
        if (rateLimitedUntil > Date.now()) throw new Error('ytlikes_rate_limited');
        const isTrack = new URL(url).pathname.replace(/\/$/, '') === '/api/v2/track';
        if (isTrack) { trackRequestSent = true; downloadDiagnostic.stage = 'track_lookup'; }
        const response = await originalFetch(url, ...args);
        if (response.status === 429) {
            const raw = response.headers.get('Retry-After');
            const seconds = /^\d+$/.test(raw || '') ? Number(raw) : (Date.parse(raw) - Date.now()) / 1000;
            rateLimitedUntil = Date.now() + Math.max(1800, Number.isFinite(seconds) ? seconds : 1800) * 1000;
            throw new Error('ytlikes_rate_limited');
        }
        if (isTrack) downloadDiagnostic.track_http_status = response.status;
        return response;
    };
    const originalEnvelope = api.fetchUnifiedPlaybackEnvelope.bind(api);
    api.fetchUnifiedPlaybackEnvelope = async (...args) => {
        const envelope = await originalEnvelope(...args);
        if (envelope) {
            const resource = api.getUnifiedPlaybackResource(envelope);
            const source = String(resource?.source || envelope.selected_source || '').toLowerCase();
            downloadDiagnostic.audio_source = ['tidal','amazon','mono','monochrome','deezer','qobuz'].includes(source) ? source : 'unknown';
            downloadDiagnostic.stage = resource ? 'resource_received' : 'resource_missing';
        }
        return envelope;
    };
    const originalVerification = api.getUnifiedTurnstileJwt.bind(api);
    let verificationTimer;
    api.getUnifiedTurnstileJwt = async (...args) => {
        if (verificationFailure || rateLimitedUntil) throw new Error('Turnstile retry deferred');
        verificationTimer = setTimeout(() => {
            void send('/worker/attention', {code:'monochrome_verification_required'});
        }, 30000);
        try {
            const jwt = await originalVerification(...args);
            if (jwt) await send('/worker/verified', {});
            checkingVerification = false;
            return jwt;
        }
        catch (error) {
            verificationFailure = verificationFailureCode(error);
            await send('/worker/attention', {code:verificationFailure});
            throw error;
        }
        finally { clearTimeout(verificationTimer); }
    };
    api.getTrackMetadata = async (id) => activeTrack && String(id) === String(activeTrack.id)
        ? api.prepareTrack(activeTrack) : originalMetadata(id);
    const originalEnrich = api.enrichTrack.bind(api);
    api.enrichTrack = async (...args) => {
        let result;
        try { result = await originalEnrich(...args); }
        catch (error) { enrichFailure = errorCode(error); throw error; }
        if (unsupportedAudio(result, allowEncryptedLossless)) {
            enrichFailure = 'encrypted_audio_unsupported';
            throw new Error('ytlikes_encrypted');
        }
        return result;
    };
    let checkingVerification = false;
    new MutationObserver(() => {
        const panel = document.getElementById('unified-playback-turnstile-panel');
        if (panel && getComputedStyle(panel).display !== 'none' && !checkingVerification) {
            checkingVerification = true;
            display('Monochrome needs browser verification. Complete the prompt below.');
        }
    }).observe(document.body, {subtree:true, attributes:true, childList:true});
    let manuallyVerifying = false;
    const verifyLocally = async ({forceRefresh=false, retryAll=false} = {}) => {
        if (activeTrack || manuallyVerifying) return;
        manuallyVerifying = true;
        verificationFailure = null;
        rateLimitedUntil = 0;
        try {
            display('Checking Monochrome verification…');
            const jwt = await api.getUnifiedTurnstileJwt({forceRefresh});
            if (!jwt) throw new Error('verification unavailable');
            if (retryAll) await send('/worker/retry', {});
            display('Ready. Pending tracks will resume.');
        } catch {
            display('Verification needs attention. Click Retry to try again when ready.');
        } finally { manuallyVerifying = false; }
    };
    document.getElementById('retry').onclick = () => verifyLocally({retryAll:true});
    setInterval(async () => {
        if (activeTrack || manuallyVerifying) return;
        try {
            const response = await fetch('/worker/control', {cache:'no-store'});
            if (response.ok) {
                const control = await response.json();
                if (control.verify) await verifyLocally({forceRefresh:control.force_refresh});
            }
        } catch { /* Local host restarts are retried without logging. */ }
    }, 2000);
    display('Ready. Waiting for a new liked song.');
    while (true) {
        let job;
        try {
            const response = await fetch('/worker/next', {cache:'no-store'});
            if (!response.ok) throw new Error('connection');
            job = await response.json();
        } catch {
            display('Waiting for the local sync service…');
            await new Promise(r => setTimeout(r, 5000));
            continue;
        }
        if (!job) continue;
        while (manuallyVerifying) await new Promise(r => setTimeout(r, 100));
        const accepted = await send(`/worker/started/${job.id}`, {delivery_id:job.delivery_id});
        if (!accepted.ok) continue;
        const t = job.track;
        allowEncryptedLossless = job.allow_encrypted_lossless === true;
        activeTrack = {id:Number(t.id), title:t.title, version:t.version || '', type:'track', duration:t.duration,
            artists:(t.artists || []).map(name => ({name})), artist:{name:(t.artists || [])[0] || 'Unknown Artist'},
            album:{title:t.album, cover:t.cover}, isrc:t.isrc, trackNumber:t.track_number, volumeNumber:t.disc_number};
        checkingVerification = false;
        verificationFailure = null;
        trackRequestSent = false;
        turnstileCode = null;
        downloadDiagnostic = {stage:'resolving'};
        enrichFailure = null;
        rateLimitedUntil = 0;
        display(`Downloading ${t.title}…`);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), job.timeout_ms);
        let lastError;
        try {
            for (const quality of ['HI_RES_LOSSLESS', 'LOSSLESS']) {
                try {
                    // This is the same upstream method used by downloads.js.
                    // The narrow ytlikesRawDownload option returns the original
                    // audio before any conversion; Python verifies/remuxes it.
                    const blob = await api.downloadTrack(t.id, quality, undefined, {
                        track:activeTrack, triggerDownload:false, calculateDashBytes:false,
                        ytlikesRawDownload:true, signal:controller.signal,
                        onProgress:(p) => {
                            if (p?.receivedBytes > job.max_bytes) controller.abort();
                        }
                    });
                    if (!blob || blob.size > job.max_bytes) throw new Error('size_limit');
                    downloadDiagnostic.stage = 'local_upload';
                    const uploaded = await fetch(`/worker/result/${job.id}`, {method:'POST',
                        headers:{'Content-Type':'application/octet-stream'}, body:blob, signal:controller.signal});
                    if (!uploaded.ok) throw new Error('local_save_failed');
                    display(`Downloaded ${t.title}. The watcher is validating the audio.`);
                    lastError = null;
                    break;
                } catch (e) {
                    lastError = e;
                    if (controller.signal.aborted || checkingVerification || verificationFailure || rateLimitedUntil) break;
                }
            }
            if (lastError) {
                const code = rateLimitedUntil ? 'provider_rate_limited' : checkingVerification ? 'monochrome_verification_required' :
                    verificationFailure && !trackRequestSent ? verificationFailure : enrichFailure || errorCode(lastError);
                await send(`/worker/error/${job.id}`, {code, retry_after:Math.max(1800,(rateLimitedUntil-Date.now())/1000), diagnostic:{...downloadDiagnostic, track_request_sent:trackRequestSent, turnstile_code:turnstileCode}});
                display(code === 'monochrome_verification_required'
                    ? 'Browser verification is needed. Click Retry after completing verification.'
                    : code.startsWith('monochrome_verification_') ? 'Monochrome’s browser verification failed before the track lookup. This track remains pending.'
                    : `Monochrome could not download ${t.title}. It remains pending for retry.`);
            }
        } finally {
            clearTimeout(timer);
            activeTrack = null;
        }
    }
} catch {
    display('The Monochrome engine could not load. Run the helper installer again.');
    void send('/worker/attention', {code:'monochrome_engine_load_failed'});
}
