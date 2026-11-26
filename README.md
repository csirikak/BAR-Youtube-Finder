# BAR Video Battle Finder

This repository contains a complete pipeline designed to find and link Beyond All Reason (BAR) gameplay moments from YouTube videos and Twitch VODs to their specific, corresponding battle replays.

It works by scraping YouTube channels and Twitch category VODs for BAR content, using computer vision (YOLO) to find player lists in screenshots, performing OCR to extract player names, and then fuzzy-matching those names against a comprehensive SQLite database of all official BAR replays.

The final result is a searchable web frontend that allows you to find videos of a specific player or fuzzy-search for a player name and get a direct link to the source video and timestamp where they appeared.

## Features

  * **YouTube Scraper**: Downloads video metadata and screenshots from specified channels using `yt-dlp`.
  * **Twitch VOD Pipeline**: Discovers recent BAR category VODs, captures frames through yt-dlp + FFmpeg, and stores all Twitch metadata and matches in separate SQL tables.
  * **50-Day Twitch Retention**: Twitch links older than 50 days are hidden by the frontend and removed from SQL during each Twitch matching update.
  * **Replay Database Ingestion**: Pulls all battle replay metadata from the official `api.bar-rts.com` into a local SQLite database.
  * **Custom CV Model**: Includes a complete tool (`bbox.py`) to label, train, and run a YOLO model to detect the in-game player UI panel.
  * **High-Performance OCR**: Uses `RapidOCR` to extract player names from the detected UI panels.
  * **Incremental Matching**: Uses `rapidfuzz` to link OCR player lists to replays, with a persistent cache that recomputes only videos affected by OCR or replay changes.
  * **Rename-Aware Players**: Resolves historical usernames to BAR accounts through [Gex](https://gex.honu.pw/), so searching any previous name returns the same videos.
  * **Web Frontend**: A responsive static site with background autocomplete, player/map search, channel and upload-date filters, a YouTube/Twitch/both platform filter, shareable searches, and paginated video links.

## How It Works

This project is a multi-stage pipeline. The scripts must be run in a specific order to correctly build the dataset.

### One-Time Setup: Training the CV Model

Before you can process screenshots, you need to train the YOLO model to find the player list.

1.  **Collect Screenshots**: Manually gather a few hundred screenshots from BAR videos and place them in the `training_screenshots` folder, use `scrape.py` to make this easier.
2.  **Label Data**: Run `python bbox.py label`. This opens an OpenCV window. Click the top-left corner of the player UI panel. The bottom-right is assumed to be the edge of the screen. Press `SPACE` to save and advance.
3.  **Train Model**: Run `python bbox.py train`. This uses the labels you just created to train a YOLO model. It will save the best model as `ui_detector.pt`.

-----

### Main Pipeline: Running the Process

Once you have a trained `ui_detector.pt` model, you can run the main pipeline.

1.  **Update DB Schema**: (Run once)
    `python updateSchema.py`

      * This adds the YouTube `videos`/`battle_videos` tables and the separate Twitch `twitch_videos`/`twitch_battle_videos` tables used to link replays to source videos.

2.  **Populate Replay DB**:
    `python updateBattleDB.py`

      * This script connects to the `api.bar-rts.com` and downloads the metadata for *all* battles back to a particular date, including player names for each battle. It stores this in `game_battles.db`. This can be run periodically to fetch new replays.

3.  **Scrape YouTube Videos**:
    `python scrape.py`

      * This script reads the list of YouTube channels and uses `yt-dlp` to fetch video metadata (saving to `data/screenshot_data.json`).
      * It uses `ffmpeg` to take screenshots at 1:30 and every 12 minutes thereafter, saving them to `data/AllBarScreenshots/`.
      * Rerunning fills missing timestamps while preserving existing screenshots and OCR results. A video is complete only when every expected timestamp has a complete PNG or a saved OCR result (including an empty player list).
      * Captures start as metadata arrives, with four concurrent workers. Failed streams try alternate formats, and a rejected stream URL (HTTP 401/403/410) is refreshed up to three times per video; recovered frames are reported instead of failed ones, and incomplete captures remain eligible for the next run. The console includes FFmpeg errors for frames that could not be captured and a per-source summary.
      * Ongoing livestreams and videos with only segmented DASH streams are deferred. Rerun after YouTube makes a seekable recording available.

4.  **Run OCR on Screenshots**:
    `python processScreenshotsRapidOCR.py`

      * Uses batched YOLO panel detection and PP-OCRv6 through RapidOCR's PyTorch backend. AMD ROCm uses the same `cuda` device interface as NVIDIA. CPU is selected automatically if no GPU is available.
      * On GPU, three OCR readers run on separate streams and overlap with decoding and panel detection for the next batch. Each reader selects the best character scores on the GPU before copying results to the CPU. Use `--ocr-workers N` to override the reader count (1 on CPU by default).
      * Uses larger recognition batches, skips rotation classification for upright screenshots, and tunes detection for small, dim player names. The local `players` table helps correct unambiguous OCR typos while preserving digits, clan tags, and Unicode names.
      * Updates `data/screenshot_data.json` with atomic checkpoints every 50 frames and at completion/interruption. Completed frames are skipped before models load. Valid empty results are saved; failed frames remain eligible for retry and stop the enclosing pipeline before screenshot cleanup.
      * Override defaults with `--device cpu`, `--device cuda:0`, `--batch-size 4`, or `--checkpoint-every 10`. Use `--reprocess` to replace saved results for screenshots still on disk, or `--reprocess-empty` to revisit only frames whose saved player list is empty. Rerun with the same flag if a reprocessing attempt fails.
      * For an isolated evaluation: `python processScreenshotsRapidOCR.py --screenshots-dir path/to/samples --output-file data/ocr-evaluation.json`. See [OCR benchmark results](docs/ocr-benchmark.md) for the model comparison and limitations.

5.  **Match OCR to Replays**:
    `python findScreenshotBattles.py`

      * Reads OCR player lists from `data/screenshot_data.json` and reuses matching results stored in `data/game_battles.db`. Unchanged runs skip loading the battle history entirely.
      * New OCR results and changes to battles within a video's eight-month search window invalidate that video's cache. Database triggers track replay insertions, corrections, roster edits, and deletions; newer battles leave older uploads cached.
      * Small updates use an indexed player lookup to load only candidate battles and their rosters. The supporting index is built once, on the first small update.
      * It uses a parallelized process and `rapidfuzz`'s `token_set_ratio` to find the best `battle_id` that matches the list of players in each screenshot.
      * It saves these matches (e.g., "Video X at timestamp Y matches Battle ID Z") into the `battle_videos` table in `game_battles.db` and also creates `matches_output.json`.
      * Twitch VODs use the same matching core through `findTwitchBattles.py`, but write only to `twitch_videos` and `twitch_battle_videos`. They are rebuilt from the current Twitch metadata each run, so Twitch IDs never enter the YouTube tables or YouTube cache.
      * The Twitch matcher applies the 50-day VOD retention window: it deletes expired `twitch_videos`/`twitch_battle_videos` rows before rebuilding the current links.
      * Use `--force` to rebuild every match, or `--max-workers 4` to limit worker processes. The first run populates the cache; changing matching settings also invalidates it.

6.  **Resolve Player Renames**:
    `python rename_resolver.py`

      * Maps historical player names to stable BAR account IDs through the Gex API. Only names in battles already linked to a video are resolved.
      * Current usernames come from BAR's user list; historical names use Gex with the required `youtube-finder (discord: cubicleannihilator)` user agent and a conservative 300-requests-per-five-minutes limiter.
      * Lookups are cached in `player_rename_cache`, so reruns are resumable and repeat runs make no requests for known names. Use `--max-requests N` to bound a run, `--workers N` to overlap requests (default 4), and `--force` to recheck cached names. Ambiguous results are never merged.
      * The update pipeline runs this stage with a 300-lookup budget after matching; `--rename-requests 0` disables it for a run.

7.  **Export for Frontend**:
    `python exportForFrontend.py`

      * Reads linked YouTube and Twitch data directly from a consistent SQLite snapshot, with no scraping or OCR needed.
      * Writes a compact `frontend_files/catalog.<hash>.json`, storing each video and battle once, with a platform marker and optional thumbnail. `frontend_files/frontend_data.json` is now a small manifest pointing to that catalog.
      * Unchanged catalogs keep the same filename for browser caching. The manifest is replaced atomically after the catalog is ready, and the previous catalog is retained for tabs loading during an update. Deploy the manifest and catalog files together.

8.  **View Results**:

      * Serve the repository folder with a simple HTTP server (e.g., `python -m http.server`) and open `index.html` in your browser. Use HTTP rather than opening the file directly: search runs in a module Web Worker.
      * Data loading, indexing, autocomplete, and filtering run in the worker. The page receives at most eight suggestions or 24 results at a time and loads thumbnails as they approach the viewport.
      * The platform selector switches between all results, YouTube-only results, and Twitch-only results. Twitch VOD links deep-link to the matched timestamp; YouTube links use the existing timestamp URL format. Twitch entries older than 50 days are hidden even if a catalog still contains them.
      * Player names retain clan tags and Unicode. Search supports case-insensitive names and typo suggestions, with keyboard navigation. Platform, channel, upload-date, sort, and page selections are included in copied search links; existing `?playerName=...` links still work.
      * The original BAR SVG logo, favicon, Poppins typeface, and blue/charcoal styling are retained. Branding and font files are served locally; asset sources and the font license are in [frontend_files/assets](frontend_files/assets/README.md).

## Twitch VODs

`scrape_twitch.py` discovers recent BAR VODs from `https://www.twitch.tv/directory/category/beyond-all-reason/videos/all`. yt-dlp cannot parse Twitch directory pages, so discovery uses the public GQL endpoint behind the category page; each VOD is then resolved with yt-dlp and captured with FFmpeg starting at 1:30 and sampled every 7 minutes, since matches can be shorter than YouTube's 12-minute gap. VODs captured before the 7-minute schedule keep their original interval, so changing it does not backfill the archive.

Frames go to `data/TwitchScreenshots/` and metadata to `data/twitch_data.json`, separate from the YouTube archive. Twitch video IDs are prefixed with `twitch_` internally so they cannot collide with YouTube IDs.

Only the last 50 days of Twitch VODs are retained. The category feed only exposes the last few days, so discovery uses it to find active channels and then walks each channel's archive back to the 50-day cutoff, keeping only Beyond All Reason VODs. Channels with no recent category VOD cannot be discovered from that feed. `findTwitchBattles.py` removes expired Twitch rows from SQL, and the frontend independently hides stale older entries.

Twitch matching places each screenshot in real time from the VOD's broadcast start plus the screenshot timestamp, then considers only battles within ±12 hours of that moment.

```bash
# List recent BAR category VODs without capturing.
python scrape_twitch.py --list --limit 10

# Capture every VOD in the 50-day window (the default).
python scrape_twitch.py --limit 1000
```

The update pipeline requests the full 50-day window by default.
`--twitch-full` remains an explicit alias; use `--twitch-limit N` for a smaller cap.

```bash
# OCR the Twitch frames into their separate metadata file.
python processScreenshotsRapidOCR.py \
    --screenshots-dir data/TwitchScreenshots \
    --output-file data/twitch_data.json

# Match the OCR results into the separate Twitch tables.
python findTwitchBattles.py
```

The Twitch matcher writes `twitch_videos` and `twitch_battle_videos`; it never touches `videos`, `battle_videos`, or the YouTube matching cache. Export reads both link tables, and the frontend filters by `Both platforms`, `YouTube`, or `Twitch`. Run `findTwitchBattles.py` on every update to refresh links and purge expired Twitch rows.

The original live-capture mode remains available for current streams:

```bash
# List or capture live BAR streams instead of VODs.
python scrape_twitch.py --live --list
python scrape_twitch.py --live --limit 2

# Capture a frame every 12 minutes (Ctrl-C to stop).
python scrape_twitch.py --live --interval 720
```

## Setup & Installation

1.  **Clone Repository**

    ```bash
    git clone https://github.com/csirikak/BAR-Youtube-Finder.git
    cd BAR-Youtube-Finder
    ```

2.  **Create Python Environment**

    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install Python Dependencies**
    ```bash
    pip install -U "yt-dlp[default]" "bgutil-ytdlp-pot-provider>=2.0.0" curl_cffi requests python-dateutil rich
    pip install opencv-python numpy
    pip install ultralytics "rapidocr==3.9.2" rapidfuzz
    ```
    Install a PyTorch build appropriate for your GPU before these commands. For AMD, keep `torch`, `torchvision`, and `triton` on compatible ROCm builds; do not replace a working AMD installation with generic PyPI wheels. OCR uses the `rapidocr` package and its Torch backend, not the older `rapidocr-onnxruntime` package. RapidOCR downloads its model weights on first use. The player-name database is optional; without it, names are returned without catalog corrections.

4.  **Install External Dependencies**

      * **FFmpeg**: You must have `ffmpeg` installed and available in your system's `PATH`. This is required by `scrape.py` for taking screenshots.
      * **YouTube JavaScript support**: Install a supported Deno or Node.js runtime (Node 22+ for the PO provider) and keep `yt-dlp[default]` current so its EJS challenge solver is installed. The scraper enables both runtimes. See the [yt-dlp EJS setup guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
      * **YouTube PO tokens**: Some streams require a token provider. Install `bgutil-ytdlp-pot-provider` in the same Python environment and follow its [provider setup instructions](https://github.com/Brainicism/bgutil-ytdlp-pot-provider#installation). Keep the Python plugin and provider checkout on matching releases; use [2.0.0 or newer for its HTTP server security fixes](https://github.com/Brainicism/bgutil-ytdlp-pot-provider/releases/tag/2.0.0). After updating the checkout, rebuild it with `npm ci` and `npx tsc` in its `server` directory. The scraper uses its HTTP server when available, or its script when built at `bgutil-ytdlp-pot-provider/server/build/generate_once.js` inside this repository. See the [yt-dlp PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide).
      * **(Optional)** `exiftool` or `imagemagick`: The `delete.sh` script uses these.

## Usage (Pipeline Order)

For the complete joint YouTube + Twitch update, run `python update_pipeline.py`.
The local `update.py` entry point delegates to the same runner. It preserves the
existing commit/push and screenshot-cleanup steps after a successful update.
Twitch matching runs automatically and applies the 50-day purge.

The default already requests every VOD in the 50-day window. `--twitch-full`
remains an explicit alias for the GQL maximum (1000):

```bash
python update_pipeline.py --twitch-full --twitch-capture-workers 4
```

```mermaid
flowchart LR
    Start --> Sync[Replay database sync]
    Start --> Capture[YouTube channel capture]
    Sync --> OCR[Streaming GPU OCR]
    Capture -->|Completed screenshots| OCR
    Capture --> Match[YouTube replay matching]
    OCR --> Match
    OCR --> TwitchCapture[Twitch VOD capture]
    TwitchCapture --> TwitchOCR[Twitch OCR]
    TwitchOCR --> TwitchMatch[Twitch replay matching]
    Match --> Renames[Player rename lookup]
    TwitchMatch --> Renames
    Renames --> Export[Frontend export]
    Export --> Publish[Commit and push]
    Publish --> Cleanup[Remove processed screenshots]
```

Replay sync runs alongside YouTube channel discovery and screenshot capture.
Once the database commits, OCR reads the current player catalog and processes
screenshots as they arrive. Twitch VOD capture and OCR run as a separate stage
after YouTube ingestion, then both matching stages feed rename resolution and
export. A shared metadata lock and merged OCR checkpoints preserve both new
video metadata and recognition results. Duplicate YouTube video IDs are claimed
once per run, and all channels share one bounded capture pool.

The runner prints one line per source and per completed video, then summary
tables for ingestion counters (videos, frames, retries, refreshes, deferred,
skipped, failed), captured screenshot resolutions for YouTube and Twitch
(4K, 2K, 1080p, 720p, 480p, other), and stage times, followed by any failed
videos with reasons.
Repeated yt-dlp and per-frame messages only appear with `--verbose`/`-v`.
Concurrent stage times include waits and should not be added together. Matching
and export wait for their inputs; failed prerequisites, exports, or pushes stop
cleanup. On cancellation, new work stops and in-flight requests finish within
their timeouts. Successful OCR results remain checkpointed for the next run.

```bash
# Run the update while retaining screenshots and omitting commit/push.
python update_pipeline.py --no-publish --keep-screenshots

# Adjust concurrency and Twitch VOD volume; defaults shown.
python update_pipeline.py --channel-workers 2 --capture-workers 4 \
    --twitch-limit 1000 --twitch-capture-workers 2 --ocr-batch-size 8

# Explicitly request the entire retained Twitch window (the default).
python update_pipeline.py --twitch-full --twitch-capture-workers 4

# Disable Twitch work and its retention purge for a YouTube-only run.
python update_pipeline.py --twitch-limit 0

# Show yt-dlp extraction detail and every recognized frame.
python update_pipeline.py -v
```

Use `--no-warp` to leave the current network connection alone, or
`--no-provider` when managing the PO provider separately. An existing healthy
provider is reused; a provider started by this runner is stopped on exit.
Configure the curated channel list in `update_pipeline.py`.

Regression tests (no YouTube requests or GPU required):

```bash
python -m unittest discover -s tests -v
node --test tests/search.test.mjs
```

See [performance measurements and cache behavior](docs/performance.md) for
the update and browser benchmarks, including an optional browser smoke test.

The individual stages can also be run in order:

```bash
# --- ONE-TIME SETUP ---
# 1. Manually add screenshots, then label them
python bbox.py label

# 2. Train the YOLO model
python bbox.py train

# 3. Add the new tables to the database
python updateSchema.py

# --- REGULAR PIPELINE RUN ---
# 1. Update the battle replay database
python updateBattleDB.py

# 2. Scrape YouTube for new videos and screenshots
python scrape.py

# 3. Process new screenshots with OCR
python processScreenshotsRapidOCR.py

# 4. Run the matching logic
python findScreenshotBattles.py

# 5. Resolve previous player names (cached and rate-limited)
python rename_resolver.py

# 6. Export the final data for the website
python exportForFrontend.py

# 7. Serve the frontend
python -m http.server 8000
# ...then open http://localhost:8000 in your browser
```

## 📂 Project Structure

```
.
├── data/
│   ├── AllBarScreenshots/       # (Generated) Directory for all YouTube screenshots
│   ├── TwitchScreenshots/       # (Generated) Directory for all Twitch VOD screenshots
│   ├── game_battles.db          # (Generated) SQLite DB of all BAR replays
│   ├── matches_output.json      # (Generated) Intermediate JSON of video/battle matches
│   ├── screenshot_data.json     # (Generated) JSON DB of video metadata and OCR results
│   ├── twitch_data.json         # (Generated) Twitch VOD metadata and OCR results
│   └── twitch_matches.json      # (Generated) Twitch video/battle matches
├── frontend_files/
│   ├── app.js               # JavaScript for the frontend
│   ├── frontend_data.json   # (Generated) Small catalog manifest
│   ├── catalog.<hash>.json  # (Generated) Compact shared data
│   ├── search-core.js      # Search indexes and pure query logic
│   ├── search-worker.js    # Background loading, autocomplete, and filtering
│   └── style.css            # CSS for the frontend
├── yolo_dataset/            # (Generated) Staging area for YOLO training
├── yolo_labels/             # (Generated) Labels from bbox.py
│
├── bbox.py                  # Tool for labeling, training, and inferring with YOLO
├── exportForFrontend.py     # Exports final data to frontend_files/frontend_data.json
├── findScreenshotBattles.py # **Core Logic**: Matches OCR results to the DB
├── match_cache.py           # Persistent matching cache and replay change tracking
├── rename_resolver.py       # Maps historical names to BAR account IDs
├── scrape_twitch.py         # Twitch category VOD/live discovery and capture
├── findTwitchBattles.py     # Twitch-only SQL matching and retention purge
├── twitch_retention.py      # Shared 50-day Twitch VOD retention rule
├── json_files.py            # Atomic JSON publishing
├── index.html               # The web frontend UI
├── processScreenshotsRapidOCR.py # Runs YOLO + OCR on all screenshots
├── scrape.py                # Scrapes YouTube channels for videos & screenshots
├── updateBattleDB.py        # Populates game_battles.db from the BAR API
├── update_pipeline.py       # Concurrent capture, replay sync, and streaming OCR
├── updateSchema.py          # Adds video/match tables to the DB
│
└── ui_detector.pt           # (Generated) The trained YOLO model
```
