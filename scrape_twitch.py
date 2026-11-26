"""Twitch category VOD capture plus the live-stream prototype.

Twitch directory pages are not yt-dlp playlists, so both discovery paths use the
public GQL endpoint behind the category page. Each VOD/channel is then resolved
with yt-dlp and captured with FFmpeg. VOD frames and metadata are kept in their
own Twitch files so the YouTube archive cannot be polluted.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

import requests
import yt_dlp

import console as ui
import scrape
from json_files import write_json
from twitch_retention import retention_cutoff


CATEGORY_URL = "https://www.twitch.tv/directory/category/beyond-all-reason"
VIDEO_CATEGORY_URL = (
    "https://www.twitch.tv/directory/category/beyond-all-reason/videos/all")
GQL_URL = "https://gql.twitch.tv/gql"
# Public web client ID used by Twitch's own directory page.
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_USER_AGENT = "youtube-finder (discord: cubicleannihilator)"
SCREENSHOT_DIR = "data/TwitchScreenshots"
METADATA_FILE = "data/twitch_data.json"
REQUEST_TIMEOUT = (10, 30)
# Twitch's category feed is capped at roughly 100 results per request. It only
# lists the last few days of VODs, so channel archives provide the older history.
MAX_VIDEO_PAGE = 1000
CHANNEL_VIDEO_PAGE = 100
CHANNEL_VIDEOS_OPERATION = "FilterableVideoTower_Videos"
CHANNEL_VIDEOS_HASH = (
    "67004f7881e65c297936f32c75246470629557a393788fb5a69d6d9a25a8fd5f")
VOD_PREFIX = "twitch_"
# Matches can be shorter than YouTube's 12-minute sampling gap, so Twitch VODs
# are sampled every seven minutes. Existing records keep their stored interval.
TWITCH_INTERVAL_SEC = 7 * 60

logger = ui.get("twitch")

STREAMS_QUERY = """
query CategoryStreams($slug: String!, $first: Int!) {
  game(slug: $slug) {
    name
    streams(first: $first) {
      edges {
        node {
          id
          title
          viewersCount
          createdAt
          broadcaster { login displayName }
        }
      }
    }
  }
}
"""

VIDEOS_QUERY = """
query CategoryVideos($slug: String!, $first: Int!, $after: Cursor) {
  game(slug: $slug) {
    name
    videos(first: $first, after: $after, types: [ARCHIVE], sort: TIME) {
      pageInfo { hasNextPage endCursor }
      edges {
        node {
          id
          title
          lengthSeconds
          createdAt
          viewCount
          owner { login displayName }
        }
      }
    }
  }
}
"""


def category_slug(category_url):
    parts = [part for part in urlparse(category_url).path.split("/") if part]
    if len(parts) < 3 or parts[0] != "directory" or parts[1] != "category":
        raise ValueError(f"Expected a Twitch category URL: {category_url}")
    return parts[2]


def _category_headers(category_url):
    return {
        "Client-ID": TWITCH_CLIENT_ID,
        "Content-Type": "application/json",
        "User-Agent": TWITCH_USER_AGENT,
        "Origin": "https://www.twitch.tv",
        "Referer": category_url,
    }


def _gql(category_url, query, first, after=None, session=None):
    variables = {"slug": category_slug(category_url), "first": first}
    if after is not None:
        variables["after"] = after
    response = (session or requests).post(
        GQL_URL,
        headers=_category_headers(category_url),
        json={"query": query, "variables": variables},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return (response.json().get("data") or {}).get("game")


def discover_streams(category_url=CATEGORY_URL, limit=None, session=None):
    """Return live streams from the category page's GQL backend."""
    first = min(limit if limit is not None else 100, 100)
    game = _gql(category_url, STREAMS_QUERY, first, session=session)
    if not game:
        raise ValueError(f"Twitch category not found: {category_slug(category_url)}")
    streams = []
    for edge in (game.get("streams") or {}).get("edges") or []:
        node = (edge or {}).get("node") or {}
        broadcaster = node.get("broadcaster") or {}
        if not node.get("id") or not broadcaster.get("login"):
            continue
        streams.append({
            "stream_id": str(node["id"]),
            "login": broadcaster["login"],
            "display_name": broadcaster.get("displayName") or broadcaster["login"],
            "title": node.get("title") or "",
            "viewers": int(node.get("viewersCount") or 0),
            "started_at": node.get("createdAt"),
        })
    streams.sort(key=lambda stream: (-stream["viewers"], stream["login"]))
    return streams


def _channel_videos(login, *, slug, since=None, session=None):
    """Return one channel's archive VODs, including history the category feed omits."""
    response = (session or requests).post(
        GQL_URL,
        headers=_category_headers(VIDEO_CATEGORY_URL),
        json={
            "operationName": CHANNEL_VIDEOS_OPERATION,
            "variables": {
                "limit": CHANNEL_VIDEO_PAGE,
                "channelOwnerLogin": login,
                "broadcastType": "ARCHIVE",
                "videoSort": "TIME",
            },
            "extensions": {"persistedQuery": {
                "version": 1,
                "sha256Hash": CHANNEL_VIDEOS_HASH,
            }},
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    user = ((response.json().get("data") or {}).get("user") or {})
    page = user.get("videos") or {}
    videos, seen = [], set()
    for edge in page.get("edges") or []:
        node = (edge or {}).get("node") or {}
        owner = node.get("owner") or {}
        game = node.get("game") or {}
        video_id = str(node.get("id") or "")
        created_at = node.get("publishedAt") or node.get("createdAt")
        if (not video_id or video_id in seen
                or game.get("slug") != slug
                or (since and str(created_at or "").replace("-", "")[:8] < since)):
            continue
        seen.add(video_id)
        videos.append({
            "video_id": video_id,
            "title": node.get("title") or "",
            "duration": int(node.get("lengthSeconds") or 0),
            "created_at": created_at,
            "views": int(node.get("viewCount") or 0),
            "login": owner.get("login") or login,
            "display_name": owner.get("displayName") or login,
        })
    return videos


def discover_videos(category_url=VIDEO_CATEGORY_URL, limit=None, since=None,
                    session=None):
    """Return category VODs, then walk active channels back to the cutoff."""
    requested = None if limit is None else int(limit)
    if requested is not None and requested < 1:
        return []
    # A full-window request keeps paging until the cutoff or the last page;
    # MAX_VIDEO_PAGE is the GQL page size, not a total cap.
    target = (None if requested is None or requested >= MAX_VIDEO_PAGE
              else requested)
    videos, seen, after = [], set(), None
    channel_logins = set()
    while target is None or len(videos) < target:
        first = (MAX_VIDEO_PAGE if target is None
                 else min(MAX_VIDEO_PAGE, target - len(videos)))
        game = _gql(category_url, VIDEOS_QUERY, first,
                    after=after, session=session)
        if not game:
            raise ValueError(f"Twitch category not found: {category_slug(category_url)}")
        page = game.get("videos") or {}
        page_has_recent = False
        for edge in page.get("edges") or []:
            node = (edge or {}).get("node") or {}
            owner = node.get("owner") or {}
            video_id = str(node.get("id") or "")
            if not video_id or not owner.get("login") or video_id in seen:
                continue
            created_at = node.get("createdAt")
            if since and str(created_at or "").replace("-", "")[:8] < since:
                continue
            page_has_recent = True
            seen.add(video_id)
            channel_logins.add(owner["login"])
            videos.append({
                "video_id": video_id,
                "title": node.get("title") or "",
                "duration": int(node.get("lengthSeconds") or 0),
                "created_at": created_at,
                "views": int(node.get("viewCount") or 0),
                "login": owner["login"],
                "display_name": owner.get("displayName") or owner["login"],
            })
            if target is not None and len(videos) >= target:
                break
        page_info = page.get("pageInfo") or {}
        next_cursor = page_info.get("endCursor")
        if ((target is not None and len(videos) >= target)
                or (since and not page_has_recent)
                or not page_info.get("hasNextPage")
                or not next_cursor or next_cursor == after):
            break
        after = next_cursor

    # The category feed only surfaces the last few days. Expand each channel it
    # found through the retention window so older VODs are not silently lost.
    if since and target is None and channel_logins:
        logger.info("Twitch · expanding %d channel archive(s)", len(channel_logins))
        slug = category_slug(category_url)
        for login in sorted(channel_logins):
            try:
                history = _channel_videos(
                    login, slug=slug, since=since, session=session)
            except Exception as error:
                logger.warning("Twitch · %s history failed: %s",
                               ui.escape(login), ui.escape(str(error)))
                continue
            for video in history:
                if video["video_id"] in seen:
                    continue
                seen.add(video["video_id"])
                videos.append(video)
    videos.sort(key=lambda video: video.get("created_at") or "", reverse=True)
    return videos


def extract_live_stream(login):
    """Resolve one live Twitch channel to HLS formats with yt-dlp."""
    options = {
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "format": "best[protocol=m3u8_native]/best",
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 2,
        "logger": scrape.YTDLP_LOGGER,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(f"https://www.twitch.tv/{login}", download=False)


def extract_vod(video_id):
    """Resolve one Twitch VOD to seekable HLS formats with yt-dlp."""
    options = {
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "format": "best[protocol=m3u8_native]/best",
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 2,
        "logger": scrape.YTDLP_LOGGER,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(
            f"https://www.twitch.tv/videos/{video_id}", download=False)


def _parse_started(value):
    if not value:
        return None
    try:
        start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start


def _normalize_vod(video, info):
    """Make a Twitch VOD use the archive's seekable video contract."""
    info = dict(info or {})
    video_id = f"{VOD_PREFIX}{video['video_id']}"
    info["id"] = video_id
    info["source"] = "twitch"
    # yt-dlp labels some Twitch VODs as live; duration makes the intent clear.
    info["is_live"] = False
    info["live_status"] = "was_live"
    info["webpage_url"] = f"https://www.twitch.tv/videos/{video['video_id']}"
    if video.get("title"):
        info["title"] = video["title"]
    if video.get("display_name"):
        info["uploader"] = video["display_name"]
    if not info.get("duration"):
        info["duration"] = video.get("duration")
    started = _parse_started(video.get("created_at"))
    if started:
        info["upload_date"] = started.strftime("%Y%m%d")
        info["started_at"] = started.isoformat().replace("+00:00", "Z")
    return info


def _load_metadata(filename):
    path = Path(filename)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid Twitch metadata: {filename}")
    return data


def update_metadata(filename, key, entry):
    """Atomically merge one live stream while preserving OCR results."""
    with scrape.DB_LOCK:
        data = _load_metadata(filename)
        previous = data.get(key) or {}
        data[key] = {
            **previous,
            **entry,
            "screenshots": previous.get("screenshots") or {},
        }
        write_json(filename, data, indent=4)


def capture_stream(stream, *, output_dir=SCREENSHOT_DIR,
                   metadata_file=METADATA_FILE, now=None, extractor=None,
                   screenshotter=None):
    """Capture one current frame and record stream-start-relative metadata."""
    now = now or datetime.now(timezone.utc)
    started = _parse_started(stream.get("started_at"))
    offset = max(0, int((now - started).total_seconds())) if started else 0
    video_id = f"twitch_{stream['stream_id']}"
    output = Path(output_dir) / f"{video_id}_{offset}s.png"
    if output.exists():
        return {"status": "skipped", "video_id": video_id, "offset": offset,
                "message": f"Frame already exists at {offset}s"}

    extractor = extractor or extract_live_stream
    screenshotter = screenshotter or scrape.capture_screenshot
    try:
        info = extractor(stream["login"])
    except Exception as error:
        return {"status": "failed", "video_id": video_id, "offset": offset,
                "message": f"yt-dlp: {error}"}
    if not info:
        return {"status": "failed", "video_id": video_id, "offset": offset,
                "message": "yt-dlp returned no stream information"}
    if info.get("live_status") not in (None, "is_live"):
        return {"status": "deferred", "video_id": video_id, "offset": offset,
                "message": f"Stream is {info.get('live_status')}"}

    formats = scrape.screenshot_formats(info)
    if not formats:
        return {"status": "deferred", "video_id": video_id, "offset": offset,
                "message": "No seekable video stream available"}

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    errors = []
    for stream_format in formats:
        try:
            error = screenshotter(stream_format, 0, str(output))
        except FileNotFoundError:
            return {"status": "failed", "video_id": video_id, "offset": offset,
                    "message": "ffmpeg not found"}
        if error is None:
            break
        errors.append(f"{stream_format.get('format_id', '?')}: {error}")
    else:
        return {"status": "failed", "video_id": video_id, "offset": offset,
                "message": " | ".join(errors)}

    start_date = started.strftime("%Y%m%d") if started else (
        info.get("upload_date") or now.strftime("%Y%m%d"))
    update_metadata(metadata_file, video_id, {
        "title": stream.get("title") or info.get("title"),
        "upload_date": start_date,
        "duration": None,
        "uploader": stream.get("display_name") or info.get("uploader"),
        "tags": ["Beyond All Reason", "twitch"],
        "thumbnail": info.get("thumbnail"),
        "live_status": "is_live",
        "source": "twitch",
        "url": info.get("webpage_url") or f"https://www.twitch.tv/{stream['login']}",
        "stream_id": stream["stream_id"],
        "started_at": stream.get("started_at"),
        "screenshot_offset_sec": offset,
    })
    return {"status": "captured", "video_id": video_id, "offset": offset,
            "message": f"Captured {output.name}"}


def capture_vod(video, *, output_dir=SCREENSHOT_DIR,
                metadata_file=METADATA_FILE, extractor=None, stop_event=None,
                stats=None, on_screenshot=None):
    """Capture the 90s/5m schedule for one category VOD."""
    video_id = f"{VOD_PREFIX}{video['video_id']}"
    stored = _load_metadata(metadata_file)
    existing = stored.get(video_id) or {}
    # The GQL creation time is the broadcast start. Backfill it for entries
    # that predate this field so matching can use the exact stream moment.
    started = _parse_started(video.get("created_at"))
    started_at = started.isoformat().replace("+00:00", "Z") if started else None
    if (started_at and video_id in stored
            and existing.get("started_at") != started_at):
        update_metadata(metadata_file, video_id, {"started_at": started_at})
        existing["started_at"] = started_at
    # Records captured before per-video intervals existed used 12-minute
    # sampling; keep that schedule instead of backfilling them all.
    if existing.get("screenshot_interval_sec"):
        interval = existing["screenshot_interval_sec"]
    elif existing.get("screenshots"):
        interval = scrape.INTERVAL_SEC
    else:
        interval = TWITCH_INTERVAL_SEC
    if (existing.get("duration") and not scrape.missing_timestamps(
            dict(existing, id=video_id), output_dir)):
        return {"status": "skipped", "video_id": video_id, "frames": 0,
                "message": f"Skipped '{existing.get('title') or video_id}' "
                           "(all screenshots captured or OCR processed)"}

    extractor = extractor or extract_vod
    try:
        info = _normalize_vod(video, extractor(video["video_id"]))
    except Exception as error:
        return {"status": "failed", "video_id": video_id, "frames": 0,
                "message": f"yt-dlp: {error}"}
    info["screenshot_interval_sec"] = interval
    if not info.get("formats"):
        return {"status": "deferred", "video_id": video_id, "frames": 0,
                "message": "No seekable video stream available"}
    if not scrape.expected_timestamps(info):
        return {"status": "skipped", "video_id": video_id, "frames": 0,
                "message": f"Skipped '{info.get('title') or video_id}' "
                           "(No duration or too short)"}

    processed = existing.get("screenshots") or {}
    if not scrape.missing_timestamps(info, output_dir, processed):
        return {"status": "skipped", "video_id": video_id, "frames": 0,
                "message": f"Skipped '{info.get('title') or video_id}' "
                           "(All screenshots captured or OCR processed)"}

    os.makedirs(output_dir, exist_ok=True)
    if scrape.update_video_database([info], metadata_file) == (0, 0):
        return {"status": "failed", "video_id": video_id, "frames": 0,
                "message": "Could not save Twitch metadata"}

    _, message, frames = scrape.process_video_screenshots(
        info, output_dir, processed_timestamps=processed,
        refresh_video=lambda: _normalize_vod(video, extractor(video["video_id"])),
        on_screenshot=on_screenshot, stop_event=stop_event, stats=stats)
    status = message.split(" ", 1)[0].lower()
    return {"status": status, "video_id": video_id, "frames": frames,
            "message": message}


def capture_category(*, category_url=VIDEO_CATEGORY_URL, limit=None,
                     output_dir=SCREENSHOT_DIR, metadata_file=METADATA_FILE,
                     workers=2, stop_event=None, stats=None,
                     on_screenshot=None):
    """Discover recent category VODs and capture their missing frames."""
    if workers < 1:
        raise ValueError("Worker count must be positive")
    videos = discover_videos(category_url, limit=limit,
                             since=retention_cutoff())
    if stats is not None:
        stats.increment("sources")
        stats.increment("videos", len(videos))
    counts = {"videos": len(videos), "processed": 0, "skipped": 0,
              "deferred": 0, "failed": 0, "frames": 0}
    if not videos:
        logger.info("Twitch · no category VODs found")
        return counts

    def run(video):
        if stop_event is not None and stop_event.is_set():
            return {"status": "failed", "video_id": video["video_id"],
                    "frames": 0, "message": "Cancelled"}
        try:
            return capture_vod(
                video, output_dir=output_dir, metadata_file=metadata_file,
                stop_event=stop_event, stats=stats,
                on_screenshot=on_screenshot)
        except Exception as error:
            return {"status": "failed", "video_id": video["video_id"],
                    "frames": 0, "message": f"worker error: {error}"}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run, video): video for video in videos}
        for future in as_completed(futures):
            result = future.result()
            status = result["status"]
            counts[status] = counts.get(status, 0) + 1
            counts["frames"] += result["frames"]
            if stats is not None:
                stats.increment("frames", result["frames"])
                if status == "failed":
                    stats.record_failure(result["video_id"], result["message"])
                else:
                    stats.increment(status)
            log = logger.info if status in ("processed", "deferred") else logger.warning
            log("%s · %s", result["video_id"], ui.escape(result["message"]))
    logger.info(
        "Twitch · %d VOD(s) · %d processed · %d frames · %d skipped · "
        "%d deferred · %d failed", counts["videos"], counts["processed"],
        counts["frames"], counts["skipped"], counts["deferred"], counts["failed"])
    return counts


def run_round(*, category_url=CATEGORY_URL, output_dir=SCREENSHOT_DIR,
              metadata_file=METADATA_FILE, limit=None, session=None,
              extractor=None):
    streams = discover_streams(category_url, limit=limit, session=session)
    counts = {"streams": len(streams), "captured": 0, "skipped": 0,
              "deferred": 0, "failed": 0}
    for stream in streams:
        result = capture_stream(
            stream, output_dir=output_dir, metadata_file=metadata_file,
            extractor=extractor)
        status = result["status"]
        counts[status] += 1
        log = logger.info if status in ("captured", "deferred") else logger.warning
        log("%s · %s", ui.escape(stream["login"]),
            ui.escape(result["message"]))
    logger.info(
        "Twitch · %d live stream(s) · %d captured · %d skipped · "
        "%d deferred · %d failed", counts["streams"], counts["captured"],
        counts["skipped"], counts["deferred"], counts["failed"])
    return counts


def main(*, category_url=None, output_dir=SCREENSHOT_DIR,
         metadata_file=METADATA_FILE, limit=25, list_only=False,
         interval=0, live=False):
    if limit is not None and limit < 1:
        raise ValueError("Limit must be positive")
    if interval < 0:
        raise ValueError("Interval must not be negative")
    url = category_url or (CATEGORY_URL if live else VIDEO_CATEGORY_URL)
    while True:
        try:
            if list_only and live:
                streams = discover_streams(url, limit=limit)
                for stream in streams:
                    print(f"{stream['login']:<24} {stream['viewers']:>6}  "
                          f"{stream['title']}")
                logger.info("Twitch · %d live stream(s)", len(streams))
            elif list_only:
                videos = discover_videos(url, limit=limit,
                                         since=retention_cutoff())
                for video in videos:
                    print(f"{video['video_id']:<12} {video['duration']:>6}s  "
                          f"{video['display_name']:<24} {video['title']}")
                logger.info("Twitch · %d VOD(s)", len(videos))
            elif live:
                run_round(category_url=url, output_dir=output_dir,
                          metadata_file=metadata_file, limit=limit)
            else:
                capture_category(category_url=url, limit=limit,
                                 output_dir=output_dir,
                                 metadata_file=metadata_file)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            logger.error("Twitch discovery failed: %s", ui.escape(str(error)))
        if interval <= 0 or not live:
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Twitch BAR VOD/live-stream capture")
    parser.add_argument("--category-url", default=None)
    parser.add_argument("--output-dir", default=SCREENSHOT_DIR)
    parser.add_argument("--metadata-file", default=METADATA_FILE)
    parser.add_argument("--limit", type=int, default=1000,
                        help="maximum category results per run (1000/default: "
                             "walk the full 50-day window)")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="list videos/streams without capturing")
    parser.add_argument("--live", action="store_true",
                        help="use live streams instead of category VODs")
    parser.add_argument("--interval", type=float, default=0,
                        help="seconds between live capture rounds")
    main(**vars(parser.parse_args()))
