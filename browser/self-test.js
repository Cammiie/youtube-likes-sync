import {LosslessAPI} from './js/api.js';
import {apiSettings} from './js/storage.js';
console.error = console.warn = console.log = () => {};
const result = document.getElementById('result');
const digest = async blob => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()))).join(',');
try {
    const api = new LosslessAPI(apiSettings);
    for (const file of ['fixture.flac', 'fixture.m4a']) {
        const url = new URL('/ytlikes-fixtures/'+file, location.href).href;
        const source = await (await fetch(url)).blob();
        const enrichedTrack = {id:0,title:'Synthetic test tone',artists:[{name:'Test'}],album:{title:'Fixture'}};
        const output = await api.downloadTrack(0, 'LOSSLESS', undefined, {
            triggerDownload:false, ytlikesRawDownload:true,
            enriched:{isVideo:false, enrichedTrack, externalStreamUrl:url, externalProvider:'monochrome',
                lookup:{info:{audioQuality:'LOSSLESS'}}}
        });
        if (await digest(source) !== await digest(output)) throw new Error('bytes_changed');
        const magic = String.fromCharCode(...new Uint8Array(await output.slice(0,4).arrayBuffer()));
        if ((file.endsWith('.flac')) !== (magic === 'fLaC')) throw new Error('format_changed');
    }
    result.textContent='PASS: the real Monochrome engine preserves FLAC bytes and leaves AAC unconverted for the native rejection gate.';
} catch {
    result.textContent='FAIL: Monochrome engine fixture check failed.';
}
