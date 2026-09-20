import yt_dlp
import subprocess
import os
import threading
from yt_dlp.utils import DateRange
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import math
import json
import re
import tempfile
from pathlib import Path

# --- CONFIGURATION ---
# Max number of videos to download/process in parallel
MAX_WORKERS = 4
FFMPEG_TIMEOUT_SEC = 120
MAX_FORMAT_ATTEMPTS = 3
# Time of the first screenshot in seconds
START_TIME_SEC = 90  # 1:30
# Interval between screenshots in seconds
INTERVAL_SEC = 12 * 60 # 12 minutes
# --- --- --- --- ---
SCREENSHOT_DIR = "data/AllBarScreenshots"

FETCH_FROM = "20240601"

SCREENSHOT_DATA = "data/screenshot_data.json"
DB_LOCK = threading.Lock()

def expected_timestamps(video):
    duration = video.get('duration')
    if not isinstance(duration, (int, float)) or not math.isfinite(duration):
        return []
    return list(range(START_TIME_SEC, math.floor(duration), INTERVAL_SEC))


def is_complete_png(filename):
    """Reject empty or truncated FFmpeg output before counting it as captured."""
    try:
        with open(filename, 'rb') as image:
            if image.read(8) != b'\x89PNG\r\n\x1a\n':
                return False
            image.seek(-12, os.SEEK_END)
            return image.read() == b'\x00\x00\x00\x00IEND\xaeB`\x82'
    except (OSError, ValueError):
        return False


def load_video_database(db_filepath):
    try:
        with DB_LOCK, open(db_filepath, encoding='utf-8') as database:
            data = json.load(database)
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        return data
    except FileNotFoundError:
        return {}


def missing_timestamps(video, output_dir, processed_timestamps=()):
    # OCR results survive removal of the PNG directory. An empty OCR result
    # for a timestamp still means that frame was successfully captured.
    processed = {str(t) for t in processed_timestamps}
    processed.update(str(t) for t in (video.get('screenshots') or {}))
    return [
        timestamp for timestamp in expected_timestamps(video)
        if str(timestamp) not in processed and not is_complete_png(
            os.path.join(output_dir, f"{video['id']}_{timestamp}s.png"))
    ]


def populateHaveSet(output_dir=None, video_db=None):
    """Archive only videos whose entire capture schedule is accounted for."""
    output_dir = SCREENSHOT_DIR if output_dir is None else output_dir
    video_db = load_video_database(SCREENSHOT_DATA) if video_db is None else video_db
    return {
        video_id for video_id, data in video_db.items()
        if data.get('live_status') not in ('is_live', 'is_upcoming', 'post_live')
        and expected_timestamps(data)
        and not missing_timestamps(dict(data, id=video_id), output_dir)
    }

def flatten_entries(entries):
    """Recursively flattens a list of entries (videos or playlists)."""
    if not entries:
        return
    for entry in entries:
        if entry is None:
            continue
        # If the entry is a playlist, recurse into its entries
        if entry.get('_type') == 'playlist' and 'entries' in entry:
            yield from flatten_entries(entry.get('entries'))
        # If it's a video, yield it
        elif entry.get('id'):
            yield entry

def update_video_database(video_list, db_filepath='screenshot_data.json'):
    """
    Loads, updates, and saves a JSON database of video metadata.

    Args:
        video_list (list): A list of video dictionaries from yt-dlp.
        db_filepath (str): Path to the JSON database file.

    Returns:
        tuple: A (additions_count, updates_count) tuple.
    """
    video_db = {}
    
    # 1. Try to load the existing database
    with DB_LOCK:
        try:
            with open(db_filepath, 'r', encoding='utf-8') as f:
                video_db = json.load(f)
            if not isinstance(video_db, dict):
                raise ValueError(f"'{db_filepath}' is not a JSON object")
        except FileNotFoundError:
            print(f"No existing database found at '{db_filepath}'. Creating a new one.")
            video_db = {}

        updates_count = 0
        additions_count = 0

        # 2. Iterate through fetched videos and update the db
        for video in video_list:
            if not video or not video.get('id'):
                continue  # Skip invalid entries
            
            video_id = video.get('id')

            # Create the data payload with requested fields
            video_data = {
                'title': video.get('title'),
                'upload_date': video.get('upload_date'), # 'creation data'
                'duration': video.get('duration'),
                'uploader': video.get('uploader'),
                'tags': video.get('tags'),
                'thumbnail': video.get('thumbnail'),
                'live_status': video.get('live_status'),
            }

            # Check if it's an addition or update
            if video_id in video_db:
                updates_count += 1
            else:
                additions_count += 1
            
            # Add or update the entry
            # Retrying an incomplete video must preserve previously OCR'd frames.
            video_db.setdefault(video_id, {}).update(video_data)

        # 3. Write the updated database back to the file
        temporary_path = None
        try:
            directory = os.path.dirname(os.path.abspath(db_filepath))
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=directory, delete=False) as f:
                temporary_path = f.name
                json.dump(video_db, f, indent=4, ensure_ascii=False)
            os.replace(temporary_path, db_filepath)
        except IOError as e:
            print(f"\n[ERROR] Could not write database to '{db_filepath}': {e}")
            # Return 0,0 if save fails
            return (0, 0)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.remove(temporary_path)
        
        # 4. Return the result
        return (additions_count, updates_count)

def screenshot_formats(video):
    """Choose a few independent video streams that FFmpeg can seek directly."""
    selected = video.get('requested_formats') or [video]
    # yt-dlp sorts formats from worst to best. Keep its preferred stream first.
    candidates = []
    seen = set()
    for stream in [*selected, *reversed(video.get('formats') or [])]:
        url = stream.get('url')
        protocol = stream.get('protocol') or (url or '').split(':', 1)[0]
        if (not url or url in seen or stream.get('vcodec') in ('none', 'images')
                or stream.get('has_drm')
                or protocol not in ('http', 'https', 'm3u8', 'm3u8_native')):
            continue
        seen.add(url)
        headers = dict(video.get('http_headers') or {})
        headers.update(stream.get('http_headers') or {})
        candidates.append(dict(stream, http_headers=headers, protocol=protocol))

    if not candidates:
        return []
    # Try a different transport before more encodings from a failing CDN URL.
    first = candidates.pop(0)
    first_is_hls = first['protocol'].startswith('m3u8')
    alternate = next((f for f in candidates
                      if f['protocol'].startswith('m3u8') != first_is_hls), None)
    result = [first]
    if alternate is not None:
        candidates.remove(alternate)
        result.append(alternate)
    return (result + candidates)[:MAX_FORMAT_ATTEMPTS]


def ffmpeg_error(stderr):
    """Keep useful diagnostics without dumping signed media URLs or tokens."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    message = re.sub(r'https?://\S+', '<stream URL>', stderr or '')
    return '\n'.join(message.strip().splitlines()[-8:])[-2000:]


def capture_screenshot(stream, timestamp_sec, output_filename):
    """Publish only a complete PNG; return an error message on capture failure."""
    # A separate file prevents crashes/timeouts from leaving an apparent success.
    with tempfile.NamedTemporaryFile(
            dir=os.path.dirname(output_filename) or '.', suffix='.part.png',
            delete=False) as temporary:
        temporary_path = temporary.name
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin']
    headers = ''.join(f'{key}: {value}\r\n'
                      for key, value in stream.get('http_headers', {}).items())
    if headers:
        command.extend(['-headers', headers])
    command.extend([
        '-reconnect', '1', '-reconnect_streamed', '1',
        '-reconnect_delay_max', '5', '-rw_timeout', '30000000',
        '-threads', '2', '-ss', str(timestamp_sec), '-i', stream['url'],
        '-map', '0:v:0', '-frames:v', '1', '-an', '-threads', '1',
        '-y', temporary_path,
    ])
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT_SEC)
        if result.returncode:
            return ffmpeg_error(result.stderr) or f'FFmpeg exited with code {result.returncode}'
        if not is_complete_png(temporary_path):
            return 'FFmpeg exited without producing a complete PNG'
        os.replace(temporary_path, output_filename)
        return None
    except subprocess.TimeoutExpired as error:
        detail = ffmpeg_error(error.stderr)
        return f'FFmpeg timed out after {FFMPEG_TIMEOUT_SEC}s. {detail}'.strip()
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def process_video_screenshots(video, output_dir, game_tag="",
                              processed_timestamps=(), refresh_video=None):
    """Capture missing frames, trying alternate formats and one URL refresh."""
    if not video:
        return None, "Skipped (None entry)", 0
    video_id = video.get('id')
    video_title = video.get('title', 'N/A')
    if not video_id:
        return None, "Skipped (No video ID)", 0
    if game_tag and game_tag.lower() not in [
            tag.lower() for tag in (video.get('tags') or [])]:
        return video_id, f"Skipped '{video_title}' (Tag not found)", 0
    if video.get('is_live') or video.get('live_status') in ('is_live', 'is_upcoming'):
        return video_id, f"Deferred '{video_title}' (Livestream is not finished)", 0
    if not expected_timestamps(video):
        return video_id, f"Skipped '{video_title}' (No duration or too short)", 0

    missing = missing_timestamps(video, output_dir, processed_timestamps)
    if not missing:
        return video_id, f"Skipped '{video_title}' (All screenshots captured or OCR processed)", 0
    streams = screenshot_formats(video)
    if not streams:
        # A just-ended livestream may expose only fragment URLs. Passing one
        # to FFmpeg as a whole video cannot work; leave it eligible for a rerun.
        return video_id, f"Deferred '{video_title}' (No seekable video stream yet; retry later)", 0

    os.makedirs(output_dir, exist_ok=True)
    screenshots_taken = 0
    refreshed = False
    for timestamp_sec in missing:
        output_filename = os.path.join(output_dir, f"{video_id}_{timestamp_sec}s.png")
        while True:
            for stream in streams:
                try:
                    error = capture_screenshot(stream, timestamp_sec, output_filename)
                except FileNotFoundError:
                    return video_id, "Failed (ffmpeg not found)", screenshots_taken
                if error is None:
                    screenshots_taken += 1
                    # Reuse a working fallback for subsequent timestamps.
                    streams = [stream] + [f for f in streams if f is not stream]
                    break
                print(f"--- FAILED (ID: {video_id}, {timestamp_sec}s, "
                      f"format {stream.get('format_id', '?')}): {error}")
            else:
                if refresh_video is not None and not refreshed:
                    refreshed = True
                    print(f"Refreshing stream URLs for {video_id}...")
                    try:
                        fresh_video = refresh_video()
                        streams = screenshot_formats(fresh_video or {})
                    except Exception as error:
                        print(f"[WARN] Could not refresh {video_id}: {ffmpeg_error(str(error))}")
                        streams = []
                    if streams:
                        continue
                remaining = len(missing) - screenshots_taken
                return video_id, (
                    f"Failed '{video_title}' ({remaining} screenshot(s) still missing; "
                    "will retry on the next run)"), screenshots_taken
            break
    return video_id, f"Processed '{video_title}'", screenshots_taken


# Relevance-gate keyword sets. Matched (case-insensitive) against a video's
# title/description; strong tags are matched exactly. Kept intentionally tight
# so the gate has high precision on search-discovery results.
BAR_TITLE_DESC_KEYS = ("beyond all reason", "beyondallreason", "bar-rts")
BAR_STRONG_TAGS = ("beyond all reason", "bar")


def is_bar_relevant(info):
    """Cheap relevance gate on FULL metadata (title/description/tags).

    Applied ONLY to untrusted discovery sources, and only in Stage 2 -- i.e.
    AFTER the sequential metadata fetch but BEFORE the costly ffmpeg screenshot
    grab and the downstream OCR pass. Curated channels bypass this entirely,
    because BAR uploads there frequently have meme titles and bare descriptions
    (e.g. only a twitch/discord link) that this gate would wrongly reject.
    """
    if not info:
        return False
    title = (info.get('title') or '').lower()
    desc = (info.get('description') or '').lower()
    tags = [t.lower() for t in (info.get('tags') or [])]
    if any(k in title for k in BAR_TITLE_DESC_KEYS):
        return True
    if any(k in desc for k in BAR_TITLE_DESC_KEYS):
        return True
    if any(t in BAR_STRONG_TAGS for t in tags):
        return True
    return False


def youtube_options():
    """Shared extraction settings for initial requests and refreshed URLs."""
    extractor_args = {"youtube": {"player_client": ['default', 'mweb']}}
    # update.py runs the HTTP provider. When scrape.py is used on its own,
    # allow the installed plugin to fall back to the repository's local script.
    provider = Path(__file__).resolve().parent / 'bgutil-ytdlp-pot-provider' / 'server'
    if (provider / 'build' / 'generate_once.js').is_file():
        extractor_args['youtubepot-bgutilscript'] = {'server_home': [str(provider)]}
    return {
        # Screenshots need video only. Prefer seekable HTTPS, then HLS.
        # Retain a final fallback so post-live DASH-only videos can be deferred
        # explicitly instead of failing during metadata extraction.
        'format': ('bestvideo[protocol=https]/best[protocol=https]/'
                   'bestvideo[protocol=m3u8_native]/best[protocol=m3u8_native]/'
                   'bestvideo/best'),
        'extractor_args': extractor_args,
        'js_runtimes': {'deno': {}, 'node': {}},
        'socket_timeout': 30,
        'retries': 2,
        'extractor_retries': 2,
        'noplaylist': True,
        'quiet': False,
    }


def get_channel_screenshots(channel_url, output_dir, game_tag="", require_bar_relevance=False):
    """Discover videos, fetch metadata sequentially, and capture with bounded workers.

    The return value indicates whether the source could be processed. Individual
    unavailable/deferred/failed videos are reported and remain eligible for retry.
    """
    os.makedirs(output_dir, exist_ok=True)
    print(f"Saving screenshots to: {os.path.abspath(output_dir)}")
    try:
        video_db = load_video_database(SCREENSHOT_DATA)
        completed_ids = populateHaveSet(output_dir, video_db)
    except (OSError, ValueError) as error:
        print(f"[ERROR] Could not read {SCREENSHOT_DATA}: {error}")
        return False

    print(f"Stage 1: Fetching flat video list for source: {channel_url}...")
    flat_opts = {
        **youtube_options(),
        'extract_flat': 'in_playlist',
        'daterange': DateRange(start=FETCH_FROM),
        'download_archive': {f'youtube {video_id}' for video_id in completed_ids},
        'ignoreerrors': True,
    }
    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            channel_info = ydl.extract_info(channel_url, download=False)
    except Exception as error:
        print(f"[ERROR] Could not fetch source: {error}")
        return False
    if channel_info is None:
        print("[ERROR] yt-dlp returned no source information.")
        return False

    entries = channel_info.get('entries') if 'entries' in channel_info else [channel_info]
    videos_to_process = []
    seen = set(completed_ids)
    for stub in flatten_entries(entries):
        if stub['id'] not in seen:
            seen.add(stub['id'])
            videos_to_process.append(stub)
    if not videos_to_process:
        print("No new or incomplete videos found matching your criteria.")
        return True
    print(f"Stage 1 complete: Found {len(videos_to_process)} new or incomplete videos.")

    pending = {}
    counts = {'processed': 0, 'failed': 0, 'deferred': 0, 'skipped': 0, 'screenshots': 0}
    metadata_lock = threading.Lock()

    def report_finished(futures):
        for future in futures:
            video_id = pending.pop(future)
            try:
                _, message, count = future.result()
                counts['screenshots'] += count
                status = message.split(' ', 1)[0].lower()
                counts[status] += 1
                print(f"[{status.upper()}] {video_id}: {message}; {count} new screenshot(s)")
            except Exception as error:
                counts['failed'] += 1
                print(f"[ERROR] Screenshot worker for {video_id}: {error}")

    try:
        # The executor shuts down before ydl, since workers may refresh URLs.
        with yt_dlp.YoutubeDL(youtube_options()) as ydl, \
                ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            def fetch_video(url):
                # Keep extraction sequential, including worker refreshes.
                with metadata_lock:
                    return ydl.extract_info(url, download=False)

            for index, stub in enumerate(videos_to_process, 1):
                # Do not queue an entire channel's expiring media URLs.
                if len(pending) >= MAX_WORKERS:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    report_finished(done)
                video_id = stub['id']
                url = stub.get('webpage_url') or stub.get('url')
                if not url or not url.startswith(('https://', 'http://')):
                    url = f'https://www.youtube.com/watch?v={video_id}'
                print(f"Fetching metadata {index}/{len(videos_to_process)}: {video_id}")
                try:
                    info = fetch_video(url)
                except yt_dlp.utils.DownloadError as error:
                    counts['failed'] += 1
                    print(f"[WARN] Metadata fetch failed for {video_id}: {error}")
                    continue
                if not info:
                    counts['failed'] += 1
                    print(f"[WARN] No metadata for {video_id}; will retry on the next run.")
                    continue
                # Flat playlist entries frequently have no upload_date, so the
                # date filter must also be checked against full metadata.
                upload_date = info.get('upload_date')
                if upload_date and upload_date not in DateRange(start=FETCH_FROM):
                    counts['skipped'] += 1
                    continue
                if require_bar_relevance and not is_bar_relevant(info):
                    counts['skipped'] += 1
                    print(f"[GATE] Skipping non-BAR '{info.get('title', 'N/A')}' ({video_id}).")
                    continue
                if game_tag and game_tag.lower() not in [
                        tag.lower() for tag in (info.get('tags') or [])]:
                    counts['skipped'] += 1
                    continue
                if update_video_database([info], SCREENSHOT_DATA) == (0, 0):
                    raise OSError(f'Could not save metadata for {video_id}')

                processed = video_db.get(video_id, {}).get('screenshots') or {}
                future = executor.submit(
                    process_video_screenshots, info, output_dir, game_tag, processed,
                    lambda video_url=url: fetch_video(video_url))
                pending[future] = video_id

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                report_finished(done)
    except Exception as error:
        print(f"[ERROR] Could not process source: {error}")
        return False

    print(f"Source complete: {counts['screenshots']} new screenshot(s), "
          f"{counts['processed']} videos completed, {counts['failed']} failed, "
          f"{counts['deferred']} deferred, {counts['skipped']} skipped.")
    return True
# --- --- --- --- ---
#      RUN SCRIPT
# --- --- --- --- ---
if __name__ == "__main__":
    # TRUSTED sources: curated BAR creators + the official channel. Scraped
    # WITHOUT a relevance gate -- BAR videos here often have meme titles and
    # bare descriptions that a metadata gate would wrongly discard.
    trusted_sources = [
        "https://www.youtube.com/channel/UC-QkFO7qGgPv5J3c8pGOpIQ/recent",
        "https://www.youtube.com/@BetterStrategy/videos",
        "https://www.youtube.com/@JAWSMUNCH304/videos",
        "https://www.youtube.com/@simplygraceful1/videos",
        "https://www.youtube.com/@dskinnerify/videos",
        "https://www.youtube.com/@BrightWorksTV/videos",
        "https://www.youtube.com/@MoreDrongo/videos",
        "https://www.youtube.com/@SuperKitowiec2/videos",
        "https://www.youtube.com/@BeyondAllReason/videos",  # official channel (UC8E-VzcrJTWIG_scVnaQ1uA)
    ]

    # DISCOVERY sources: keyword search + mixed-game channels that also post
    # BAR. These can surface irrelevant videos, so require_bar_relevance=True
    # gates each on full metadata BEFORE the costly ffmpeg grab + OCR.
    # NOTE: 'ytsearchdate' is unsupported by the active handlers; plain
    # 'ytsearch' works and orders by relevance.
    discovery_sources = [
        "ytsearch150:Beyond All Reason",
        "ytsearch150:Beyond All Reason gameplay",
        "ytsearch100:Beyond All Reason 8v8",
        "ytsearch80:Beyond All Reason 1v1",
        "https://www.youtube.com/@Wintergaming/videos",
        "https://www.youtube.com/@disnof/videos",
    ]

    for channel in trusted_sources:
        get_channel_screenshots(
            channel_url=channel,
            output_dir=SCREENSHOT_DIR,
            game_tag="",
            require_bar_relevance=False,
        )

    for source in discovery_sources:
        get_channel_screenshots(
            channel_url=source,
            output_dir=SCREENSHOT_DIR,
            game_tag="",
            require_bar_relevance=True,
        )
    print("\nScript finished.")
