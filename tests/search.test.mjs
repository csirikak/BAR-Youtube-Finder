import {test} from 'node:test';
import assert from 'node:assert/strict';
import {SearchIndex} from '../frontend_files/search-core.js';

function fixture() {
    return {version: 4, aliases: {}, accounts: {},
        players: JSON.parse('{"Graceful":[0,1],"[Clan]Player":[0],"玩家名字":[1],"__proto__":[0]}'),
        maps: ['Map A', 'Map B'], uploaders: ['Channel A', 'Channel B'],
        videos: [['v1', 'First', 0, '20260901', 'youtube', ''],
            ['v2', '<script>title</script>', 1, '20261120', 'twitch', 'https://thumb']],
        battles: [['b1', 0, '2026-09-01', [[0, 90], [1, 810]]],
            ['b2', 1, '2026-08-01', [[1, 1530]]]]};
}
const NOW = new Date('2026-11-23T00:00:00Z');
const makeIndex = data => new SearchIndex(data, NOW);
test('exact, case insensitive, clan and Unicode suggestions preserve spelling', () => {
    const index = makeIndex(fixture());
    assert.equal(index.suggest('player', 'graceful')[0].name, 'Graceful');
    assert.equal(index.suggest('player', 'Player')[0].name, '[Clan]Player');
    assert.equal(index.suggest('player', '玩家')[0].name, '玩家名字');
    assert.equal(index.suggest('player', '__proto__')[0].name, '__proto__');
});
test('typos still get suggestions, including transposed letters', () => {
    const index = makeIndex(fixture());
    assert.equal(index.suggest('player', 'Grceful')[0].name, 'Graceful');
    assert.equal(index.suggest('player', 'Garceful')[0].name, 'Graceful');
});
test('search uses complete player names and retains multiple battles per video', () => {
    const index = makeIndex(fixture());
    assert.equal(index.search({query: 'graceful'}).count, 3);
    assert.equal(index.search({query: 'Grace'}).count, 0);
    assert.equal(index.search({query: '__proto__'}).count, 2);
});
test('platform filtering can show YouTube, Twitch, or both', () => {
    const index = makeIndex(fixture());
    assert.equal(index.search({query: 'graceful', platform: 'youtube'}).count, 1);
    assert.equal(index.search({query: 'graceful', platform: 'twitch'}).count, 2);
    assert.equal(index.search({query: 'graceful', platform: 'both'}).count, 3);
    assert.ok(index.search({query: 'graceful', platform: 'twitch'}).items
        .every(item => item.platform === 'twitch'));
});
test('Twitch links older than 60 days are hidden from every platform filter', () => {
    const data = fixture();
    data.videos[1][3] = '20260923';
    const expired = makeIndex(data);
    assert.equal(expired.search({query: 'graceful', platform: 'twitch'}).count, 0);
    assert.equal(expired.search({query: 'graceful', platform: 'both'}).count, 1);
    data.videos[1][3] = '20260924';
    assert.equal(makeIndex(data).search({query: 'graceful', platform: 'twitch'}).count, 2);
});
test('previous names resolve to one account and keep every battle', () => {
    const data = fixture();
    data.players = {Old: [0], New: [1], Solo: [0]};
    data.aliases = {Old: '42', New: '42'};
    data.accounts = {'42': 'New'};
    const index = makeIndex(data);
    assert.equal(index.search({query: 'Old'}).count, 3);
    assert.equal(index.search({query: 'New'}).count, 3);
    assert.equal(index.search({query: 'Old'}).displayName, 'New');
    const suggestion = index.suggest('player', 'Old')[0];
    assert.equal(suggestion.name, 'New');
    assert.equal(suggestion.query, 'Old');
    assert.equal(suggestion.count, 2);
});
test('map, channel, sort, and paging combine without changing the catalog', () => {
    const data = fixture();
    const before = JSON.stringify(data);
    const index = makeIndex(data);
    assert.equal(index.search({kind: 'map', query: 'Map A', channel: 'Channel B'}).count, 1);
    const oldest = index.search({sort: 'oldest', pageSize: 1});
    assert.equal(oldest.items[0].videoId, 'v1');
    assert.equal(oldest.pages, 3);
    assert.equal(index.search({sort: 'oldest', pageSize: 1, page: 100}).page, 3);
    assert.equal(index.search({kind: 'map', query: 'Missing', page: 100}).page, 1);
    assert.equal(JSON.stringify(data), before);
});
test('short queries and suggestion results stay bounded', () => {
    const data = fixture();
    data.players = Object.fromEntries(Array.from({length: 200}, (_, i) => ['Player' + i, [0]]));
    const index = makeIndex(data);
    assert.equal(index.suggest('player', '').length, 8);
    assert.equal(index.suggest('player', 'P').length, 8);
});
test('real exported dataset returns at most one page of rows', async () => {
    const {readFile} = await import('node:fs/promises');
    const root = new URL('../frontend_files/', import.meta.url);
    const manifest = JSON.parse(await readFile(new URL('frontend_data.json', root)));
    const index = makeIndex(JSON.parse(await readFile(new URL(manifest.catalog, root))));
    const result = index.search({kind: 'map', query: 'Supreme Isthmus v2.1'});
    assert.ok(result.count > 1000);
    assert.equal(result.items.length, 24);
    assert.equal(index.suggest('player', 'Graceful')[0].name, 'Graceful');
});
