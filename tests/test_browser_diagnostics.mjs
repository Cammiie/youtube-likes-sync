import assert from 'node:assert/strict';
import test from 'node:test';
import {verificationFailureCode as code, unsupportedAudio} from '../browser/diagnostics.mjs';

test('verification failures distinguish stages before track lookup', () => {
    assert.equal(code(new Error('Failed to load Turnstile')), 'monochrome_verification_script_failed');
    assert.equal(code(new Error('Turnstile timed out')), 'monochrome_verification_timeout');
    assert.equal(code(new Error('Turnstile failed')), 'monochrome_verification_widget_failed');
    assert.equal(code(new Error('Unified Playback Turnstile exchange failed: 403')), 'monochrome_verification_exchange_failed');
    assert.equal(code(new Error('Unified Playback Turnstile exchange returned no JWT')), 'monochrome_verification_response_invalid');
});
test('unknown upstream errors never expose URLs or credentials', () => {
    assert.equal(code(new Error('https://example.test/?token=secret')), 'monochrome_verification_failed');
    assert.equal(code(new Error('Unified Playback Turnstile exchange failed: 403 token=secret')), 'monochrome_verification_failed');
});
test('encrypted audio remains disabled unless the exact supported path is opted in', () => {
    const encrypted = {externalProvider:'amazon',externalDecryptionKey:'synthetic',externalStreamType:'cenc'};
    assert.equal(unsupportedAudio(encrypted),true);
    assert.equal(unsupportedAudio(encrypted,true),false);
    assert.equal(unsupportedAudio({...encrypted,externalProvider:'unknown'},true),true);
    assert.equal(unsupportedAudio({...encrypted,externalDecryptionKey:null},true),true);
    assert.equal(unsupportedAudio({...encrypted,isVideo:true},true),true);
    assert.equal(unsupportedAudio({externalProvider:'tidal'}),false);
});
