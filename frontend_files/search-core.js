// Pure search logic shared by the browser worker and Node regression tests.
export const normalize = value => String(value).normalize('NFKC').toLowerCase().trim();
export const TWITCH_RETENTION_DAYS = 50;

function retentionCutoff(now = new Date()) {
    let date = new Date(now);
    if (Number.isNaN(date.getTime())) date = new Date();
    date.setUTCDate(date.getUTCDate() - TWITCH_RETENTION_DAYS);
    return date.toISOString().slice(0, 10).replaceAll('-', '');
}

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

function matchAlias(item, query) {
    let prefix = null, contains = null;
    for (let i = 0; i < item.foldedAliases.length; i++) {
        const folded = item.foldedAliases[i];
        const bare = item.bareAliases[i];
        const spelling = item.nameAliases[i];
        if (folded === query) return {tier: 0, alias: spelling};
        if (prefix === null && (folded.startsWith(query) || bare.startsWith(query))) {
            prefix = {tier: 1, alias: spelling};
        }
        if (contains === null && folded.includes(query)) {
            contains = {tier: 2, alias: spelling};
        }
    }
    return prefix || contains;
}

export class SearchIndex {
    constructor(data, now = new Date()) {
        if (data.version !== 4) throw new Error('Unsupported catalog version. Reload the page.');
        this.data = data;
        this.twitchCutoff = retentionCutoff(now);
        this.players = new Map(Object.entries(data.players));
        this.mapBattles = new Map(data.maps.map(name => [name, []]));
        this.rows = [];
        data.battles.forEach(([battleId, mapId, battleDate, links], battleIndex) => {
            const map = data.maps[mapId] || 'Unknown map';
            this.mapBattles.get(map)?.push(battleIndex);
            for (const [videoIndex, timestamp] of links) {
                const [videoId, title, uploaderIndex, uploadDate, platform, thumbnail] = data.videos[videoIndex];
                const rowPlatform = platform || 'youtube';
                if (rowPlatform === 'twitch'
                    && (!/^\d{8}$/.test(uploadDate) || uploadDate < this.twitchCutoff)) continue;
                this.rows.push({battleIndex, battleId, battleDate, map, videoId, title,
                    uploader: data.uploaders[uploaderIndex], uploadDate, timestamp,
                    platform: rowPlatform, thumbnail: thumbnail || ''});
            }
        });
        this.rows.sort((a, b) => (Number(b.uploadDate) || 0) - (Number(a.uploadDate) || 0)
            || b.battleDate.localeCompare(a.battleDate) || a.videoId.localeCompare(b.videoId)
            || a.timestamp - b.timestamp || a.battleId.localeCompare(b.battleId));

        // aliases maps a historical spelling to an account ID. Names without an
        // alias remain their own group, so a missing lookup never hides results.
        const aliases = new Map(Object.entries(data.aliases || {}));
        const accountNames = data.accounts || {};
        this.playerGroups = new Map();
        this.playerKeyByName = new Map();
        this.playerFolded = new Map();
        for (const [name, battles] of this.players) {
            const account = aliases.get(name);
            const key = account !== undefined ? 'a:' + account : 'n:' + name;
            this.playerKeyByName.set(name, key);
            let group = this.playerGroups.get(key);
            if (!group) {
                group = {
                    key,
                    label: account !== undefined ? (accountNames[account] || name) : name,
                    names: [], battles: new Set(),
                };
                this.playerGroups.set(key, group);
            }
            group.names.push(name);
            for (const battle of battles) group.battles.add(battle);
        }
        for (const group of this.playerGroups.values()) {
            group.battles = Array.from(group.battles).sort((a, b) => a - b);
            group.count = group.battles.length;
            group.query = this.playerKeyByName.has(group.label)
                ? group.label : group.names[0];
            for (const name of group.names) {
                const folded = normalize(name);
                let keys = this.playerFolded.get(folded);
                if (!keys) this.playerFolded.set(folded, keys = new Set());
                keys.add(group.key);
            }
        }

        const entry = (name, aliasesForName, count, query) => ({
            name, query, count,
            nameAliases: aliasesForName,
            foldedAliases: aliasesForName.map(normalize),
            bareAliases: aliasesForName.map(n => normalize(n).replace(/^\[[^\]]+\]/, '')),
        });
        this.playerEntries = Array.from(this.playerGroups.values(), group =>
            entry(group.label, group.names, group.count, group.query))
            .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));

        this.mapKeyByFolded = new Map();
        for (const map of this.mapBattles.keys()) {
            const folded = normalize(map);
            const spellings = this.mapKeyByFolded.get(folded) || [];
            spellings.push(map);
            this.mapKeyByFolded.set(folded, spellings);
        }
        this.mapEntries = Array.from(this.mapBattles, ([name, battles]) =>
            entry(name, [name], battles.length, name))
            .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
        this.cachedResults = null;
    }

    suggest(kind, query, limit = 8) {
        const entries = kind === 'map' ? this.mapEntries : this.playerEntries;
        const q = normalize(query);
        if (!q) {
            return entries.slice(0, limit).map(item =>
                ({name: item.name, query: item.query, count: item.count}));
        }
        const buckets = [[], [], []];
        for (const item of entries) {
            const match = matchAlias(item, q);
            if (match) buckets[match.tier].push({item, alias: match.alias});
        }
        const rank = (a, b) => b.item.count - a.item.count
            || a.item.name.localeCompare(b.item.name);
        for (const bucket of buckets) bucket.sort(rank);
        const selected = buckets.flat().slice(0, limit);

        if (selected.length < limit && q.length >= 3) {
            const seen = new Set(selected.map(({item}) => item.key || item.name));
            const tolerance = q.length >= 6 ? 2 : 1;
            const fuzzy = [];
            for (const item of entries) {
                if (seen.has(item.key || item.name)) continue;
                let best = {distance: tolerance + 1, alias: null};
                item.foldedAliases.forEach((folded, i) => {
                    const bare = item.bareAliases[i];
                    const distance = Math.min(distanceWithin(q, folded, tolerance),
                        bare === folded ? tolerance + 1 : distanceWithin(q, bare, tolerance));
                    if (distance < best.distance) {
                        best = {distance, alias: item.nameAliases[i]};
                    }
                });
                if (best.distance <= tolerance) fuzzy.push({item, ...best});
            }
            fuzzy.sort((a, b) => a.distance - b.distance
                || b.item.count - a.item.count || a.item.name.localeCompare(b.item.name));
            selected.push(...fuzzy.slice(0, limit - selected.length));
        }
        return selected.map(({item, alias}) =>
            ({name: item.name, query: alias || item.query, count: item.count}));
    }

    search({kind = 'player', query = '', channel = '', period = 'all',
            platform = 'both', sort = 'newest', page = 1, pageSize = 24} = {}) {
        kind = kind === 'map' ? 'map' : 'player';
        query = query.trim();
        let selected = null, displayName;
        if (kind === 'map' && query) {
            let resolved = query;
            if (!this.mapBattles.has(resolved)) {
                const spellings = this.mapKeyByFolded.get(normalize(resolved)) || [];
                if (spellings.length === 1) resolved = spellings[0];
            }
            query = resolved;
            selected = new Set(this.mapBattles.get(resolved) || []);
        } else if (kind === 'player' && query) {
            let key = this.playerKeyByName.get(query);
            if (key === undefined) {
                const keys = this.playerFolded.get(normalize(query));
                if (keys && keys.size === 1) key = [...keys][0];
            }
            if (key === undefined) {
                selected = new Set();
            } else {
                const group = this.playerGroups.get(key);
                selected = new Set(group.battles);
                displayName = group.label;
            }
        }
        const key = JSON.stringify([kind, query, channel, period, platform, sort]);
        if (this.cachedResults?.key !== key) {
            let cutoff = '';
            if (['30', '90', '365'].includes(period)) {
                const date = new Date();
                date.setUTCDate(date.getUTCDate() - Number(period));
                cutoff = date.toISOString().slice(0, 10).replaceAll('-', '');
            }
            const rows = this.rows.filter(row =>
                (!selected || selected.has(row.battleIndex))
                && (!channel || row.uploader === channel)
                && (!platform || platform === 'both' || row.platform === platform)
                && (!cutoff || row.uploadDate >= cutoff));
            if (sort === 'oldest') rows.reverse();
            this.cachedResults = {key, rows};
        }
        const rows = this.cachedResults.rows;
        pageSize = Math.max(1, Math.min(48, Number(pageSize) || 24));
        const pages = Math.max(1, Math.ceil(rows.length / pageSize));
        page = Math.max(1, Math.min(pages, Math.floor(Number(page) || 1)));
        const start = (page - 1) * pageSize;
        return {query, displayName, count: rows.length, page, pages, start,
            items: rows.slice(start, start + pageSize)};
    }
}
