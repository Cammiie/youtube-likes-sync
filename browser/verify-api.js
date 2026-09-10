// Explicit one-time verification only. The scheduled native engine never loads this page.
console.log = console.warn = console.error = console.debug = console.info = () => {};
const status = document.getElementById('status');
let turnstileCode = null;
let failureCode = 'monochrome_verification_failed';
try {
    const {LosslessAPI} = await import('./js/api.js');
    const {apiSettings} = await import('./js/storage.js');
    const {verificationFailureCode} = await import('./ytlikes-diagnostics.mjs');
    const api = new LosslessAPI(apiSettings);
    const originalLoad = api.loadTurnstile.bind(api);
    api.loadTurnstile = async () => {
        const sdk = await originalLoad();
        return {
            render:(container, options) => sdk.render(container, {...options, 'error-callback':(code) => {
                if (/^\d{6}$/.test(String(code))) turnstileCode = String(code);
                return options['error-callback']?.(code);
            }}),
            execute:sdk.execute.bind(sdk), remove:sdk.remove?.bind(sdk)
        };
    };
    status.textContent = 'Requesting fresh verification. Complete any prompt below.';
    let response;
    try { response = await api.getUnifiedTurnstileResponse(); }
    catch (error) { failureCode = verificationFailureCode(error); throw error; }
    if (!response) throw new Error('no_proof');
    const result = await fetch('/migration/exchange', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({response})});
    if (!result.ok) { failureCode = 'api_verification_exchange_failed'; throw new Error('exchange_failed'); }
    status.textContent = result.ok ? 'Native API session saved securely. Testing direct downloads next.' : 'The native API session exchange was rejected.';
} catch {
    status.textContent = 'Verification did not complete. No browser download was started.';
    await fetch('/migration/failure', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({code:failureCode,turnstile_code:turnstileCode})}).catch(()=>{});
}
