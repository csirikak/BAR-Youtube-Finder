// Pure search logic shared by the browser worker and Node regression tests.
export const normalize = value => String(value).normalize('NFKC').toLowerCase().trim();

function distanceWithin(a, b, limit) {
    if (Math.abs(a.length - b.length) > limit) return limit + 1;
    let previous = Array.from({length: b.length + 1}, (_, i) => i);
    let beforePrevious = null;
    for (let i = 1; i <= a.length; i++) {
        const row = [i];
        let minimum = i;
        for (let j = 1; j <= b.length; j++) {
            row[j] = Math.min(row[j - 1] + 1, previous[j] + 1,
                previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
            if (beforePrevious && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
                row[j] = Math.min(row[j], beforePrevious[j - 2] + 1);
            }
            minimum = Math.min(minimum, row[j]);
        }
        if (minimum > limit) return limit + 1;
        beforePrevious = previous;
        previous = row;
    }
    return previous[b.length];
}

export class SearchIndex {
    constructor(data) {
        if (data.version !== 2) throw new Error('Unsupported catalog version. Reload the page.');
        this.data = data;
        this.players = new Map(Object.entries(data.players));
        this.mapBattles = new Map(data.maps.map(name => [name, []]));
        this.rows = [];
        data.battles.forEach(([battleId, mapId, battleDate, links], battleIndex) => {
            const map = data.maps[mapId] || 'Unknown map';
            this.mapBattles.get(map)?.push(battleIndex);
            for (const [videoIndex, timestamp] of links) {
                const [videoId, title, uploaderIndex, uploadDate] = data.videos[videoIndex];
                this.rows.push({battleIndex, battleId, battleDate, map, videoId, title,
                    uploader: data.uploaders[uploaderIndex], uploadDate, timestamp});
            }
        });
        this.rows.sort((a, b) => (Number(b.uploadDate) || 0) - (Number(a.uploadDate) || 0)
            || b.battleDate.localeCompare(a.battleDate) || a.videoId.localeCompare(b.videoId)
            || a.timestamp - b.timestamp || a.battleId.localeCompare(b.battleId));
        this.names = {};
        this.folded = {};
        for (const [kind, source] of [['player', this.players], ['map', this.mapBattles]]) {
            this.folded[kind] = new Map();
            this.names[kind] = Array.from(source, ([name, battles]) => {
                const folded = normalize(name);
                const spellings = this.folded[kind].get(folded) || [];
                spellings.push(name);
                this.folded[kind].set(folded, spellings);
                return {name, folded, bare: folded.replace(/^\[[^\]]+\]/, ''), count: battles.length};
            }).sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
        }
        this.cachedResults = null;
    }

    suggest(kind, query, limit = 8) {
        kind = kind === 'map' ? 'map' : 'player';
        const q = normalize(query);
        const names = this.names[kind];
        if (!q) return names.slice(0, limit).map(({name, count}) => ({name, count}));
        const exact = [], prefix = [], contains = [];
        for (const item of names) {
            if (item.folded === q) exact.push(item);
            else if (item.folded.startsWith(q) || item.bare.startsWith(q)) prefix.push(item);
            else if (item.folded.includes(q)) contains.push(item);
        }
        const selected = [...exact, ...prefix, ...contains].slice(0, limit);
        // Fuzzy fallback runs only in the worker and only if direct hits are sparse.
        if (selected.length < limit && q.length >= 3) {
            const seen = new Set(selected.map(item => item.name));
            const tolerance = q.length >= 6 ? 2 : 1;
            const fuzzy = [];
            for (const item of names) {
                if (seen.has(item.name)) continue;
                const distance = Math.min(distanceWithin(q, item.folded, tolerance),
                    item.bare === item.folded ? tolerance + 1 : distanceWithin(q, item.bare, tolerance));
                if (distance <= tolerance) fuzzy.push({...item, distance});
            }
            fuzzy.sort((a, b) => a.distance - b.distance || b.count - a.count || a.name.localeCompare(b.name));
            selected.push(...fuzzy.slice(0, limit - selected.length));
        }
        return selected.map(({name, count}) => ({name, count}));
    }

    search({kind = 'player', query = '', channel = '', period = 'all',
            sort = 'newest', page = 1, pageSize = 24} = {}) {
        kind = kind === 'map' ? 'map' : 'player';
        const source = kind === 'player' ? this.players : this.mapBattles;
        query = query.trim();
        if (query && !source.has(query)) {
            const spellings = this.folded[kind].get(normalize(query)) || [];
            if (spellings.length === 1) query = spellings[0];
        }
        const key = JSON.stringify([kind, query, channel, period, sort]);
        if (this.cachedResults?.key !== key) {
            const selected = query ? new Set(source.get(query) || []) : null;
            let cutoff = '';
            if (['30', '90', '365'].includes(period)) {
                const date = new Date();
                date.setUTCDate(date.getUTCDate() - Number(period));
                cutoff = date.toISOString().slice(0, 10).replaceAll('-', '');
            }
            const rows = this.rows.filter(row =>
                (!selected || selected.has(row.battleIndex))
                && (!channel || row.uploader === channel)
                && (!cutoff || row.uploadDate >= cutoff));
            if (sort === 'oldest') rows.reverse();
            this.cachedResults = {key, rows};
        }
        const rows = this.cachedResults.rows;
        pageSize = Math.max(1, Math.min(48, Number(pageSize) || 24));
        const pages = Math.max(1, Math.ceil(rows.length / pageSize));
        page = Math.max(1, Math.min(pages, Math.floor(Number(page) || 1)));
        const start = (page - 1) * pageSize;
        return {query, count: rows.length, page, pages, start,
            items: rows.slice(start, start + pageSize)};
    }
}
