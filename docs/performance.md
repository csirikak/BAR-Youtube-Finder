# Update and frontend performance

Measured locally on 2026-09-29 with an Intel i5-13600KF, Python 3.14.4,
1,838,185 replay records, and 6,147 videos with OCR metadata. These are
matching-stage measurements; network scraping and GPU OCR are separate stages.

| Matching workload | Time |
| --- | ---: |
| Full rebuild on an isolated database copy | 176.8 s |
| Unchanged repeat, all 6,147 videos cached | 0.50 s |
| Recompute the 50 most recent videos, reuse the other 6,097 | 28.0 s |
| First creation of the candidate lookup index | 18.7 s |

The 50-video measurement excludes the one-time index build. Its output exactly
matched the full rebuild, including replay IDs and scores. The index reduced the
loaded history to 137,808 candidate battles for that workload. Updates with up to
200 affected videos use this path; larger rebuilds load the complete history and
use the process pool. Small batches avoid copying the index to extra processes.

The working database was also rebuilt and its cache and candidate index populated.
Its subsequent unchanged run took 0.54 seconds. No new scraping was required.

## Cache correctness

Each video's cache key includes its OCR frames, upload date, matching settings,
algorithm version, and RapidFuzz version. SQLite triggers track changed replay
days, including participant edits and replay replacements. A video is invalidated
only when a changed day falls within its matching window. Unparseable dates are
handled conservatively. Metadata-only title/channel edits reuse matches while
updating the exported metadata. Removed videos and frames lose their old links.

Matching holds a consistent database transaction; failures preserve previous
links. JSON output uses atomic replacement. A cached run can regenerate output
after an interrupted file write. `python findScreenshotBattles.py --force`
rebuilds everything. Bump `CACHE_VERSION` when changing matching semantics.

Equal-scoring candidates now use a stable replay-ID tie break. Compared with the
previous run's arbitrary iteration order, 585 frames selected a different tied
replay; every frame retained the same score. This can slightly change archive
player/map counts. The new full and incremental runs agree exactly.

## Browser and data export

On an unchanged database snapshot, the compact export reduced the catalog from
7,711,275 to 1,605,154 bytes (79.2% smaller). Gzip size fell from 2,592,341 to
686,204 bytes (73.5% smaller). Reconstructing the old player and battle indexes
from the new representation preserved every player membership, video link,
timestamp, title, uploader, and upload date. The later matching rebuild produces
a similarly sized 1,606,791-byte catalog.

The small manifest is revalidated on each load. Catalog filenames contain a
content hash, so unchanged catalogs can be reused from the browser cache. A
Chromium repeat visit transferred zero catalog bytes. Local first results appeared
in 193 ms and repeat results in 110 ms; these localhost timings do not represent
internet download speeds.

JSON parsing, name indexing, typo matching, and result filtering run in a Web
Worker. Typing is debounced for 80 ms, and outdated suggestions are discarded.
The page renders up to eight suggestions or 24 result cards, with lazy thumbnails,
instead of building the entire result list. Fonts and search code have no CDN
dependency. See [MDN's worker documentation](https://developer.mozilla.org/en-US/docs/Web/API/Web_Workers_API/Using_web_workers).

The real-data Chromium smoke test covered desktop/mobile layouts, keyboard and
pointer autocomplete, composition input, pagination, filters, browser history,
deep links, literal rendering of HTML-like input, failed-download retry, and
blocked local storage. Typing `SentientWaffle` at 4× CPU throttling produced no
main-thread tasks over 50 ms. Screenshots and timing output are saved under the
ignored `data/` directory.

## Reproducing checks

```bash
python -m unittest discover -s tests -v
node --test tests/search.test.mjs
```

For the optional browser check, serve the repository at `http://127.0.0.1:8765/`
with `python -m http.server 8765 --bind 127.0.0.1`. In another terminal:

```bash
npm install --prefix /tmp/bar-browser-check playwright
/tmp/bar-browser-check/node_modules/.bin/playwright install chromium
PLAYWRIGHT_MODULE=/tmp/bar-browser-check/node_modules/playwright node tests/browser-smoke.cjs
```

Use `BASE_URL` for another server address and `BROWSER_EXECUTABLE` for an existing
Chromium binary. The browser smoke test uses the bundled real catalog and checks
known player/map searches. The Python and pure search tests need no browser.
