import {defineConfig} from 'vite';
import path from 'node:path';
import svgUse from './vite-plugin-svg-use.ts';
import blobAssetPlugin from './vite-plugin-blob.ts';
export default defineConfig({
    root:import.meta.dirname,
    base:'/',
    define:{__COMMIT_HASH__:JSON.stringify('60b76d0'),__VITEST__:false,'process.env.CI':JSON.stringify('')},
    resolve:{alias:{'!lucide':'/node_modules/lucide-static/icons','!simpleicons':'/node_modules/simple-icons/icons',
        '!':'/node_modules',events:'/node_modules/events/events.js',pocketbase:'/node_modules/pocketbase/dist/pocketbase.es.js',
        stream:path.resolve(import.meta.dirname,'stream-stub.js')}},
    worker:{format:'es'},
    plugins:[blobAssetPlugin(),svgUse()],
    build:{outDir:'ytlikes-dist',emptyOutDir:true,sourcemap:false,reportCompressedSize:false,
        rollupOptions:{input:{worker:path.resolve(import.meta.dirname,'worker.html'),selfTest:path.resolve(import.meta.dirname,'self-test.html'),verifyApi:path.resolve(import.meta.dirname,'verify-api.html')}}}
});
