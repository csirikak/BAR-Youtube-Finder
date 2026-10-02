import {SearchIndex} from './search-core.js';

let index;
self.onmessage = async ({data: message}) => {
    const {id, type, payload} = message;
    try {
        let result;
        if (type === 'init') {
            const manifestResponse = await fetch(new URL('frontend_data.json', import.meta.url), {cache: 'no-cache'});
            if (!manifestResponse.ok) throw new Error('The video index could not be loaded.');
            const manifest = await manifestResponse.json();
            if (manifest.version !== 2 || !/^catalog\.[a-f0-9]+\.json$/.test(manifest.catalog)) {
                throw new Error('The video index is being updated. Please retry.');
            }
            const response = await fetch(new URL(manifest.catalog, import.meta.url), {cache: 'force-cache'});
            if (!response.ok) throw new Error('The video catalog could not be loaded.');
            index = new SearchIndex(await response.json());
            result = {stats: manifest.stats, generatedAt: manifest.generated_at,
                uploaders: index.data.uploaders, popularMaps: index.suggest('map', '', 5)};
        } else if (!index) {
            throw new Error('The video index is still loading.');
        } else if (type === 'suggest') {
            result = index.suggest(payload.kind, payload.query);
        } else if (type === 'search') {
            result = index.search(payload);
        } else {
            throw new Error('Unknown search request.');
        }
        self.postMessage({id, result});
    } catch (error) {
        self.postMessage({id, error: error.message});
    }
};
