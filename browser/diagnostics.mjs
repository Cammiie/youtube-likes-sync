// Only fixed codes leave the browser. Never return upstream exception text.
export function verificationFailureCode(error) {
    const message = String(error?.message || '');
    if (message === 'Failed to load Turnstile') return 'monochrome_verification_script_failed';
    if (message === 'Turnstile timed out') return 'monochrome_verification_timeout';
    if (message === 'Turnstile failed' || message === 'Turnstile expired') return 'monochrome_verification_widget_failed';
    if (/^Unified Playback Turnstile exchange failed: \d{3}$/.test(message)) return 'monochrome_verification_exchange_failed';
    if (message === 'Unified Playback Turnstile exchange returned no JWT') return 'monochrome_verification_response_invalid';
    return 'monochrome_verification_failed';
}

export function unsupportedAudio(result, allowEncryptedLossless = false) {
    if (result.isVideo) return true;
    const encrypted = Boolean(result.externalDecryptionKey || /cenc/i.test(result.externalStreamType || '') || result.lookup?.info?.drmData);
    return encrypted && !(allowEncryptedLossless && result.externalProvider === 'amazon' && result.externalDecryptionKey);
}
