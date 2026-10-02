const $ = id => document.getElementById(id);
const input = $('search-input');
const suggestions = $('suggestions');
const number = value => Number(value).toLocaleString();
const state = {kind: 'player', query: '', channel: '', period: 'all', sort: 'newest', page: 1};
let worker, ready = false, nextId = 0, requestEpoch = 0, suggestionEpoch = 0;
let debounce, options = [], activeOption = -1, composing = false;
const pending = new Map();

function stored(key, value) {
    try {
        if (value !== undefined) localStorage.setItem(key, value);
        else return localStorage.getItem(key) || '';
    } catch { /* Search also works with browser storage disabled. */ }
    return '';
}

function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
}

function externalLink(className, text, href) {
    const link = node('a', className, text);
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
}

function request(type, payload = {}) {
    return new Promise((resolve, reject) => {
        const id = ++nextId;
        pending.set(id, {resolve, reject});
        worker.postMessage({id, type, payload});
    });
}

function closeSuggestions() {
    clearTimeout(debounce);
    suggestionEpoch++;
    suggestions.hidden = true;
    suggestions.replaceChildren();
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    options = [];
    activeOption = -1;
}

async function suggest() {
    if (!ready || composing) return;
    const epoch = ++suggestionEpoch;
    const query = input.value;
    try {
        const found = await request('suggest', {kind: state.kind, query});
        if (epoch !== suggestionEpoch || document.activeElement !== input || input.value !== query) return;
        options = found;
        activeOption = -1;
        suggestions.replaceChildren();
        found.forEach((item, index) => {
            const option = node('li');
            option.id = 'suggestion-' + index;
            option.setAttribute('role', 'option');
            option.setAttribute('aria-selected', 'false');
            option.append(node('span', '', item.name), node('small', '', number(item.count) + ' battles'));
            option.addEventListener('click', () => selectQuery(item.name));
            suggestions.append(option);
        });
        suggestions.hidden = !found.length;
        input.setAttribute('aria-expanded', String(Boolean(found.length)));
        input.removeAttribute('aria-activedescendant');
    } catch (error) { showError(error); }
}

function scheduleSuggestions() {
    clearTimeout(debounce);
    suggestionEpoch++; // Invalidate an in-flight answer immediately, before debouncing.
    suggestions.hidden = true;
    suggestions.replaceChildren();
    options = [];
    activeOption = -1;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    $('clear-search').hidden = !input.value;
    if (!composing) debounce = setTimeout(suggest, 80);
}

function updateMode() {
    document.querySelectorAll('[data-kind]').forEach(button => {
        const selected = button.dataset.kind === state.kind;
        button.classList.toggle('active', selected);
        button.setAttribute('aria-selected', String(selected));
        button.tabIndex = selected ? 0 : -1;
    });
    $('search-panel').setAttribute('aria-labelledby', state.kind + '-tab');
    $('search-heading').textContent = state.kind === 'player' ? 'Search by Player Name' : 'Browse by Map';
    $('search-label').textContent = state.kind === 'player' ? 'Player name' : 'Map name';
    $('search-tip').textContent = state.kind === 'player'
        ? 'Find videos of battles a player took part in. Choose a name from the suggestions.'
        : 'Find videos of battles played on a specific map.';
    input.placeholder = state.kind === 'player' ? 'Enter player name…' : 'Enter map name…';
    input.value = state.query;
    $('clear-search').hidden = !state.query;
}

function readLocation(restore = false) {
    const params = new URLSearchParams(location.search);
    state.kind = params.has('map') || params.get('mode') === 'map' ? 'map' : 'player';
    state.query = params.get(state.kind === 'player' ? 'playerName' : 'map') || '';
    if (restore && !location.search) state.query = stored('lastPlayerQuery');
    state.channel = params.get('channel') || '';
    state.period = ['30', '90', '365'].includes(params.get('period')) ? params.get('period') : 'all';
    state.sort = params.get('sort') === 'oldest' ? 'oldest' : 'newest';
    state.page = Math.max(1, parseInt(params.get('page'), 10) || 1);
    $('channel-filter').value = state.channel;
    $('period-filter').value = state.period;
    $('sort-filter').value = state.sort;
    updateMode();
}

function updateLocation(push = false) {
    const params = new URLSearchParams();
    if (state.query) params.set(state.kind === 'player' ? 'playerName' : 'map', state.query);
    else if (state.kind === 'map') params.set('mode', 'map');
    if (state.channel) params.set('channel', state.channel);
    if (state.period !== 'all') params.set('period', state.period);
    if (state.sort !== 'newest') params.set('sort', state.sort);
    if (state.page > 1) params.set('page', String(state.page));
    const url = location.pathname + (params.size ? '?' + params.toString() : '');
    if (url !== location.pathname + location.search) history[push ? 'pushState' : 'replaceState']({}, '', url);
}

function selectQuery(query, kind = state.kind) {
    state.kind = kind;
    state.query = query.trim();
    state.page = 1;
    stored(kind === 'player' ? 'lastPlayerQuery' : 'lastMapQuery', state.query);
    closeSuggestions();
    updateMode();
    updateLocation(true);
    runSearch();
}

function formatTime(seconds) {
    seconds = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds / 60) % 60;
    const remaining = String(seconds % 60).padStart(2, '0');
    return hours ? hours + ':' + String(minutes).padStart(2, '0') + ':' + remaining : minutes + ':' + remaining;
}

function displayDate(value) {
    if (!/^\d{8}$/.test(value)) return 'Date unavailable';
    const iso = value.slice(0, 4) + '-' + value.slice(4, 6) + '-' + value.slice(6, 8);
    const date = new Date(iso + 'T12:00:00Z');
    if (Number.isNaN(date.getTime())) return 'Date unavailable';
    return date.toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC'});
}

function card(item) {
    const article = node('article', 'video-card');
    const videoUrl = 'https://www.youtube.com/watch?v=' + encodeURIComponent(item.videoId) + '&t=' + item.timestamp + 's';
    const thumb = externalLink('video-thumb', '', videoUrl);
    thumb.setAttribute('aria-label', 'Watch ' + item.title + ' at ' + formatTime(item.timestamp));
    const image = node('img');
    image.src = 'https://i.ytimg.com/vi/' + encodeURIComponent(item.videoId) + '/mqdefault.jpg';
    image.alt = '';
    image.loading = 'lazy';
    image.decoding = 'async';
    image.width = 320;
    image.height = 180;
    thumb.append(image, node('span', 'timestamp', formatTime(item.timestamp)));
    const details = node('div', 'video-details');
    const metadata = node('div', 'video-meta');
    const date = node('time', '', displayDate(item.uploadDate));
    if (/^\d{8}$/.test(item.uploadDate)) date.dateTime = item.uploadDate.slice(0, 4) + '-' + item.uploadDate.slice(4, 6) + '-' + item.uploadDate.slice(6, 8);
    metadata.append(node('span', '', item.uploader), date);
    const title = node('h3', 'video-title');
    title.append(externalLink('', item.title, videoUrl));
    const map = node('button', 'video-map', item.map);
    map.type = 'button';
    map.addEventListener('click', () => selectQuery(item.map, 'map'));
    details.append(metadata, title, map);
    const actions = node('div', 'card-actions');
    actions.append(externalLink('watch-button', 'Watch ↗', videoUrl),
        externalLink('replay-link', 'Replay ↗', 'https://www.beyondallreason.info/replays?gameId=' + encodeURIComponent(item.battleId)));
    article.append(thumb, details, actions);
    return article;
}

async function runSearch() {
    if (!ready) return;
    const epoch = ++requestEpoch;
    $('results').setAttribute('aria-busy', 'true');
    try {
        const result = await request('search', {...state});
        if (epoch !== requestEpoch) return;
        state.query = result.query;
        state.page = result.page;
        updateLocation();
        $('results-title').textContent = state.query || 'Recent battles';
        $('results-eyebrow').textContent = state.query ? (state.kind === 'player' ? 'PLAYER APPEARANCES' : 'ON THE BATTLEFIELD') : 'FROM THE ARCHIVE';
        $('result-count').textContent = result.count
            ? 'Showing ' + number(result.start + 1) + '–' + number(result.start + result.items.length) + ' of ' + number(result.count) + ' video links'
            : 'No matching videos';
        $('results').replaceChildren();
        if (result.items.length) {
            const fragment = document.createDocumentFragment();
            result.items.forEach(item => fragment.append(card(item)));
            $('results').append(fragment);
        } else {
            const empty = node('div', 'empty-state');
            empty.append(node('h3', '', 'No videos found'),
                node('p', '', 'Try a name from the suggestions, or widen your channel and date filters.'));
            $('results').append(empty);
        }
        $('pagination').hidden = result.pages <= 1;
        $('page-label').textContent = 'Page ' + result.page + ' of ' + result.pages;
        $('previous-page').disabled = result.page <= 1;
        $('next-page').disabled = result.page >= result.pages;
        $('barstats-link').href = 'http://bar-stats.pro/playerstats' +
            (state.kind === 'player' && state.query ? '?playerName=' + encodeURIComponent(state.query) : '');
        $('results').setAttribute('aria-busy', 'false');
    } catch (error) {
        if (epoch === requestEpoch) showError(error);
    }
}

function showError(error) {
    $('load-status').hidden = false;
    $('load-status').classList.add('error');
    $('load-status').textContent = error.message || 'Something went wrong. Please retry.';
    $('retry-load').hidden = false;
    $('results').setAttribute('aria-busy', 'false');
}

async function load() {
    ready = false;
    closeSuggestions();
    requestEpoch++;
    for (const task of pending.values()) task.reject(new Error('Reloading the archive.'));
    pending.clear();
    worker?.terminate();
    $('load-status').hidden = false;
    $('load-status').classList.remove('error');
    $('load-status').textContent = 'Loading players and videos…';
    $('retry-load').hidden = true;
    try {
        worker = new Worker(new URL('./search-worker.js', import.meta.url), {type: 'module'});
        worker.onmessage = ({data}) => {
            const task = pending.get(data.id);
            if (!task) return;
            pending.delete(data.id);
            if (data.error) task.reject(new Error(data.error));
            else task.resolve(data.result);
        };
        worker.onerror = () => {
            ready = false;
            const error = new Error('Search could not start. Please retry loading the archive.');
            for (const task of pending.values()) task.reject(error);
            pending.clear();
            showError(error);
        };
        const summary = await request('init');
        for (const key of ['players', 'battles', 'videos']) $('stat-' + key).textContent = number(summary.stats[key]);
        const filter = $('channel-filter');
        filter.replaceChildren(new Option('All channels', ''));
        summary.uploaders.forEach(name => filter.append(new Option(name, name)));
        if (!summary.uploaders.includes(state.channel)) state.channel = '';
        filter.value = state.channel;
        $('popular-maps').replaceChildren(node('span', '', 'POPULAR MAPS'));
        summary.popularMaps.forEach(({name}) => {
            const button = node('button', 'map-shortcut', name);
            button.type = 'button';
            button.addEventListener('click', () => selectQuery(name, 'map'));
            $('popular-maps').append(button);
        });
        $('last-updated').textContent = 'Archive updated ' + new Date(summary.generatedAt).toLocaleDateString(undefined,
            {year: 'numeric', month: 'short', day: 'numeric'});
        ready = true;
        $('load-status').hidden = true;
        $('retry-load').hidden = true;
        await runSearch();
        if (document.activeElement === input) suggest();
    } catch (error) { showError(error); }
}

input.addEventListener('input', scheduleSuggestions);
input.addEventListener('compositionstart', () => { composing = true; closeSuggestions(); });
input.addEventListener('compositionend', () => { composing = false; scheduleSuggestions(); });
input.addEventListener('focus', suggest);
input.addEventListener('blur', closeSuggestions);
suggestions.addEventListener('pointerdown', event => event.preventDefault());
input.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Escape') { closeSuggestions(); return; }
    if (suggestions.hidden || !options.length) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        activeOption = activeOption < 0
            ? (event.key === 'ArrowDown' ? 0 : options.length - 1)
            : (activeOption + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
        [...suggestions.children].forEach((item, i) => item.setAttribute('aria-selected', String(i === activeOption)));
        const active = suggestions.children[activeOption];
        input.setAttribute('aria-activedescendant', active.id);
        active.scrollIntoView({block: 'nearest'});
    } else if (event.key === 'Enter') {
        event.preventDefault();
        selectQuery(options[activeOption >= 0 ? activeOption : 0].name);
    }
});
$('search-form').addEventListener('submit', event => { event.preventDefault(); selectQuery(input.value); });
$('clear-search').addEventListener('click', () => { selectQuery(''); input.focus(); });
document.querySelectorAll('[data-kind]').forEach(button => {
    button.addEventListener('click', () => { selectQuery('', button.dataset.kind); input.focus(); });
    button.addEventListener('keydown', event => {
        if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            event.preventDefault();
            const kind = event.key === 'Home' ? 'player' : event.key === 'End' ? 'map' : state.kind === 'map' ? 'player' : 'map';
            selectQuery('', kind);
            $(kind + '-tab').focus();
        }
    });
});
for (const [id, key] of [['channel-filter', 'channel'], ['period-filter', 'period'], ['sort-filter', 'sort']]) {
    $(id).addEventListener('change', () => { state[key] = $(id).value; state.page = 1; updateLocation(true); runSearch(); });
}
for (const [id, direction] of [['previous-page', -1], ['next-page', 1]]) {
    $(id).addEventListener('click', () => {
        state.page += direction;
        updateLocation(true);
        runSearch();
        $('results-title').scrollIntoView({block: 'start'});
    });
}
$('share-search').addEventListener('click', async () => {
    updateLocation();
    try {
        await navigator.clipboard.writeText(location.href);
        $('share-search').textContent = 'Link copied ✓';
        setTimeout(() => { $('share-search').textContent = 'Copy search link ↗'; }, 2000);
    } catch {
        $('share-search').textContent = 'Copy the link from your address bar';
    }
});
$('retry-load').addEventListener('click', load);
window.addEventListener('popstate', () => { closeSuggestions(); readLocation(); runSearch(); });
readLocation(true);
load();
