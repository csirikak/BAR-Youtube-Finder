"""Run replay sync, YouTube/Twitch capture, OCR, matching, and export."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
import json
import multiprocessing as mp
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import struct
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

import console as ui
import scrape
import scrape_twitch
import processScreenshotsRapidOCR as ocr
import rename_resolver
import updateBattleDB


logger = ui.get("update")


ROOT = Path(__file__).resolve().parent
CHANNELS = [
    "https://www.youtube.com/channel/UC-QkFO7qGgPv5J3c8pGOpIQ/recent",
    "https://www.youtube.com/@BetterStrategy/videos",
    "https://www.youtube.com/@JAWSMUNCH304/videos",
    "https://www.youtube.com/@simplygraceful1/videos",
    "https://www.youtube.com/@dskinnerify/videos",
    "https://www.youtube.com/@BrightWorksTV/videos",
    "https://www.youtube.com/@MoreDrongo/videos",
    "https://www.youtube.com/@SuperKitowiec2/videos",
]


class StageTimes:
    def __init__(self):
        self.seconds = {}
        self.lock = threading.Lock()

    def run(self, name, function, *args, **kwargs):
        start = time.perf_counter()
        logger.info("[cyan]▸[/cyan] %s", name)
        try:
            result = function(*args, **kwargs)
            if result is False:
                raise RuntimeError(f"{name} reported failure")
            return result
        finally:
            elapsed = time.perf_counter() - start
            with self.lock:
                self.seconds[name] = elapsed
            logger.info("[green]✓[/green] %s · %s", name, ui.duration(elapsed))


RESOLUTION_BUCKETS = ("4K", "2K", "1080p", "720p", "480p", "Other")


def png_dimensions(filename):
    """Read a PNG's dimensions from its IHDR without decoding the image."""
    try:
        with open(filename, "rb") as image:
            header = image.read(24)
    except OSError:
        return None
    if (len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n"
            or header[12:16] != b"IHDR"):
        return None
    return struct.unpack(">II", header[16:24])


def resolution_bucket(width, height):
    """Map a screenshot's pixel dimensions to a display-oriented bucket."""
    if width >= 3840 or height >= 2160:
        return "4K"
    if width >= 2560 or height >= 1440:
        return "2K"
    if width >= 1920 or height >= 1080:
        return "1080p"
    if width >= 1280 or height >= 720:
        return "720p"
    if width >= 854 or height >= 480:
        return "480p"
    return "Other"


class ScreenshotResolutions:
    """Thread-safe counts of captured screenshots by resolution bucket."""

    def __init__(self):
        self.counts = {bucket: 0 for bucket in RESOLUTION_BUCKETS}
        self.lock = threading.Lock()

    def add(self, filename):
        dimensions = png_dimensions(filename)
        if dimensions is None:
            return
        bucket = resolution_bucket(*dimensions)
        with self.lock:
            self.counts[bucket] += 1

    def snapshot(self):
        with self.lock:
            return dict(self.counts)


class FrameStream:
    """Queue file paths, never full images; flush small OCR batches promptly."""

    def __init__(self, batch_size=8, flush_seconds=0.2, stop_event=None):
        if batch_size < 1:
            raise ValueError("OCR batch size must be positive")
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        self.queue = Queue()
        self.seen = set()
        self.lock = threading.Lock()
        self.closed = False
        self.stop_event = stop_event or threading.Event()

    def put(self, path):
        path = Path(path)
        match = ocr.FRAME_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"Not a completed screenshot filename: {path}")
        key = match.groups()
        with self.lock:
            if self.closed:
                raise RuntimeError("Screenshot arrived after capture completed")
            if key not in self.seen:
                self.seen.add(key)
                self.queue.put((path, *key))

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.queue.put(None)

    def batches(self, *, flush_idle=False):
        """Yield an empty batch after a lull so OCR can collect in-flight work."""
        finished = False
        batch_in_flight = False
        while not finished and not self.stop_event.is_set():
            try:
                frame = self.queue.get(timeout=0.2)
            except Empty:
                if flush_idle and batch_in_flight:
                    batch_in_flight = False
                    yield []
                continue
            if frame is None:
                return
            batch = [frame]
            deadline = time.monotonic() + self.flush_seconds
            while len(batch) < self.batch_size:
                try:
                    frame = self.queue.get(timeout=max(0, deadline - time.monotonic()))
                except Empty:
                    break
                if frame is None:
                    finished = True
                    break
                batch.append(frame)
            batch_in_flight = True
            yield batch


def scrape_channels(channel_urls, screenshots_dir, metadata_file, stream,
                    channel_workers=2, capture_workers=4, stop_event=None,
                    stats=None, on_screenshot=None):
    claimed_ids = set()
    failures = []
    try:
        # One shared capture pool prevents N channels from creating N * 4 FFmpeg
        # workers. Each channel keeps its own serialized yt-dlp client.
        with ThreadPoolExecutor(max_workers=capture_workers) as captures:
            with ThreadPoolExecutor(max_workers=channel_workers) as sources:
                futures = {
                    sources.submit(
                        scrape.get_channel_screenshots, channel, screenshots_dir,
                        capture_executor=captures, claimed_ids=claimed_ids,
                        on_screenshot=on_screenshot or stream.put,
                        metadata_file=metadata_file,
                        stop_event=stop_event, stats=stats
                    ): channel for channel in channel_urls
                }
                for future in as_completed(futures):
                    if not future.result():
                        failures.append(futures[future])
        if failures:
            raise RuntimeError(f"Could not scrape {len(failures)} channel(s): {failures}")
    finally:
        # Both pools have drained, including all screenshot callbacks.
        stream.close()


def run_ingestion(*, channel_urls, screenshots_dir, metadata_file, player_database,
                  channel_workers=2, capture_workers=4, ocr_batch_size=8, times=None,
                  stats=None, ocr_workers=None, resolutions=None):
    if min(channel_workers, capture_workers, ocr_batch_size) < 1:
        raise ValueError("Worker counts and OCR batch size must be positive")
    times = times or StageTimes()
    stats = stats or scrape.IngestionStats()
    resolutions = resolutions or ScreenshotResolutions()
    stop_event = threading.Event()
    stream = FrameStream(ocr_batch_size, stop_event=stop_event)

    def queue_frame(path):
        resolutions.add(path)
        stream.put(path)

    initial = ocr.load_metadata(metadata_file)
    for path, _, _ in ocr.pending_frames(screenshots_dir, initial):
        stream.put(path)

    with ThreadPoolExecutor(max_workers=3) as stages:
        database = stages.submit(times.run, "replay sync", updateBattleDB.main,
                                 db_name=player_database, stop_event=stop_event)
        capture = stages.submit(
            times.run, "YouTube capture", scrape_channels, channel_urls,
            screenshots_dir, metadata_file, stream, channel_workers, capture_workers,
            stop_event, stats, on_screenshot=queue_frame
        )

        def consume():
            # Read the latest catalog only after the writer commits. This also
            # avoids SQLite read/write contention during a large replay sync.
            database.result()
            return times.run(
                "OCR", ocr.main, screenshots_dir=screenshots_dir,
                output_file=metadata_file, player_database=player_database,
                batch_size=ocr_batch_size, frame_batches=stream.batches(flush_idle=True),
                metadata_lock=scrape.DB_LOCK, ocr_workers=ocr_workers
            )

        recognition = stages.submit(consume)
        try:
            for future in as_completed([database, capture, recognition]):
                future.result()  # Any failed prerequisite prevents matching/export.
        except BaseException:
            # Stop scheduling further downloads on failure/Ctrl-C. In-flight
            # requests finish within their timeouts; OCR checkpoints completed work.
            stop_event.set()
            raise
    return times


def run_twitch_ingestion(*, limit, capture_workers=2, ocr_batch_size=8,
                         times=None, stats=None, ocr_workers=None, resolutions=None):
    """Capture category VODs and OCR them into the separate Twitch metadata."""
    if limit < 1:
        raise ValueError("Twitch VOD limit must be positive")
    if capture_workers < 1:
        raise ValueError("Twitch capture worker count must be positive")
    times = times or StageTimes()
    stats = stats or scrape.IngestionStats()
    resolutions = resolutions or ScreenshotResolutions()

    def queue_frame(path):
        resolutions.add(path)

    times.run("Twitch VOD capture", scrape_twitch.capture_category,
              limit=limit, workers=capture_workers, stats=stats,
              on_screenshot=queue_frame)
    times.run("Twitch OCR", ocr.main,
              screenshots_dir=scrape_twitch.SCREENSHOT_DIR,
              output_file=scrape_twitch.METADATA_FILE,
              player_database=updateBattleDB.DB_NAME,
              batch_size=ocr_batch_size, ocr_workers=ocr_workers)
    return times


def provider_ready():
    try:
        with urlopen("http://127.0.0.1:4416/ping", timeout=1) as response:
            return bool(json.load(response).get("version"))
    except (OSError, URLError, ValueError):
        return False


@contextmanager
def network_services(use_warp=True, use_provider=True):
    provider = None
    warp_started = False
    try:
        if use_warp:
            warp_started = True
            subprocess.run(["warp-cli", "disconnect"], check=False, timeout=30)
            subprocess.run(["warp-cli", "connect"], check=True, timeout=30)
        if use_provider and not provider_ready():
            provider = subprocess.Popen(
                ["node", str(ROOT / "bgutil-ytdlp-pot-provider/server/build/main.js")],
                cwd=ROOT
            )
            deadline = time.monotonic() + 20
            while not provider_ready():
                if provider.poll() is not None:
                    raise RuntimeError("PO token provider exited during startup")
                if time.monotonic() >= deadline:
                    raise TimeoutError("PO token provider did not become ready")
                time.sleep(0.1)
        # Keep the network route stable for both concurrent download branches.
        yield
    finally:
        try:
            if provider is not None and provider.poll() is None:
                provider.terminate()
                try:
                    provider.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    provider.kill()
                    provider.wait(timeout=5)
        finally:
            if warp_started:
                subprocess.run(["warp-cli", "disconnect"], check=False, timeout=30)


def publish_changes():
    """Retain the existing updater's commit/push behavior; propagate failures."""
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
    changes = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if changes.returncode == 1:
        subprocess.run(
            ["git", "commit", "-m", f"update {datetime.now():%Y-%m-%d}"],
            cwd=ROOT, check=True
        )
    elif changes.returncode != 0:
        changes.check_returncode()
    subprocess.run(["git", "push", "origin"], cwd=ROOT, check=True)


def main(*, channel_urls=None, channel_workers=2, capture_workers=8,
         ocr_batch_size=8, ocr_workers=None, rename_requests=300,
         twitch_limit=scrape_twitch.MAX_VIDEO_PAGE, twitch_full=False,
         twitch_capture_workers=16,
         use_warp=True, use_provider=True, publish=True, cleanup=True,
         verbose=False):
    if min(channel_workers, capture_workers, ocr_batch_size) < 1:
        raise ValueError("Worker counts and OCR batch size must be positive")
    if rename_requests < 0:
        raise ValueError("Rename lookup budget must not be negative")
    if twitch_limit < 0:
        raise ValueError("Twitch VOD limit must not be negative")
    if twitch_full:
        # Discovery already stops at the 50-day retention cutoff; the GQL
        # maximum makes the request cover that whole window. This is also the
        # default, and the flag remains an explicit override.
        twitch_limit = scrape_twitch.MAX_VIDEO_PAGE
    if twitch_capture_workers < 1:
        raise ValueError("Twitch capture worker count must be positive")
    ui.setup(verbose)
    started = time.perf_counter()
    times = StageTimes()
    stats = scrape.IngestionStats()
    youtube_resolutions = ScreenshotResolutions()
    twitch_resolutions = ScreenshotResolutions()
    sources = CHANNELS if channel_urls is None else channel_urls
    twitch_label = ("off" if not twitch_limit else
                    "full 50-day" if twitch_limit >= scrape_twitch.MAX_VIDEO_PAGE
                    else twitch_limit)
    ui.banner("BAR update", f"{len(sources)} source{'s' if len(sources) != 1 else ''} · "
                            f"{channel_workers} channel workers · "
                            f"{capture_workers} capture workers · OCR batch {ocr_batch_size} · "
                            f"Twitch VODs {twitch_label} · "
                            f"WARP {'on' if use_warp else 'off'} · PO provider "
                            f"{'on' if use_provider else 'off'}")
    try:
        with network_services(use_warp, use_provider):
            run_ingestion(
                channel_urls=sources,
                screenshots_dir=scrape.SCREENSHOT_DIR,
                metadata_file=scrape.SCREENSHOT_DATA,
                player_database=updateBattleDB.DB_NAME,
                channel_workers=channel_workers, capture_workers=capture_workers,
                ocr_batch_size=ocr_batch_size, times=times, stats=stats,
                ocr_workers=ocr_workers, resolutions=youtube_resolutions
            )
            if twitch_limit:
                run_twitch_ingestion(
                    limit=twitch_limit, capture_workers=twitch_capture_workers,
                    ocr_batch_size=ocr_batch_size, times=times, stats=stats,
                    ocr_workers=ocr_workers, resolutions=twitch_resolutions
                )
        # A fresh interpreter keeps the matching pool's workers independent of
        # the updater's GPU context and avoids importing its OCR model stack.
        times.run("replay matching", subprocess.run,
                  [sys.executable, str(ROOT / "findScreenshotBattles.py")],
                  cwd=ROOT, check=True)
        if twitch_limit:
            times.run("Twitch matching", subprocess.run,
                      [sys.executable, str(ROOT / "findTwitchBattles.py")],
                      cwd=ROOT, check=True)
        times.run("rename lookup", rename_resolver.resolve,
                  db_name=updateBattleDB.DB_NAME, max_requests=rename_requests)
        from exportForFrontend import export_data
        times.run("frontend export", export_data)
        if publish:
            times.run("Git publish", publish_changes)
        if cleanup:
            directories = [scrape.SCREENSHOT_DIR]
            if twitch_limit:
                directories.append(scrape_twitch.SCREENSHOT_DIR)
            for directory in directories:
                if Path(directory).exists():
                    # shutil.rmtree(directory)
                    continue
        return times.seconds
    finally:
        report_summary(stats, times, screenshot_resolutions={
            "YouTube": youtube_resolutions.snapshot(),
            "Twitch": twitch_resolutions.snapshot(),
        })
        logger.info("Total wall time %s · stage times overlap, so do not add them",
                    ui.duration(time.perf_counter() - started))


def report_summary(stats, times, screenshot_resolutions=None):
    """Print run totals after success or failure, without empty tables."""
    counters, failures = stats.snapshot()
    if counters["sources"]:
        rows = [
            ("Sources", counters["sources"]),
            ("Videos scanned", counters["videos"]),
            ("Completed", f"{counters['processed']} video(s)"),
            ("Frames", f"{counters['frames']:,}"),
        ]
        if counters["retries"]:
            rows.append(("Retries", f"{counters['retries']} attempt(s)"))
        if counters["refreshes"]:
            rows.append(("URL refreshes", counters["refreshes"]))
        if counters["recovered"]:
            rows.append(("Recovered frames", counters["recovered"]))
        rows += [
            ("Deferred", counters["deferred"]),
            ("Skipped", counters["skipped"]),
            ("Failed", counters["failed"]),
        ]
        ui.stats_table("Ingestion", rows)
        if failures:
            ui.failures_table(failures)
    if times.seconds:
        ui.stats_table("Stages", [(name, ui.duration(seconds))
                                  for name, seconds in times.seconds.items()])
    if screenshot_resolutions:
        rows = []
        for platform, counts in screenshot_resolutions.items():
            parts = [f"{bucket} {count:,}"
                     for bucket, count in (counts or {}).items() if count]
            if parts:
                rows.append((platform, " · ".join(parts)))
        if rows:
            ui.stats_table("Screenshot resolutions", rows)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--channel-workers", type=int, default=2)
    parser.add_argument("--capture-workers", type=int, default=4)
    parser.add_argument("--ocr-batch-size", type=int, default=8)
    parser.add_argument("--ocr-workers", type=int, default=None,
                        help="parallel OCR readers (default: 3 on GPU, 1 on CPU)")
    parser.add_argument("--rename-requests", type=int, default=300,
                        help="maximum Gex name lookups per update (0 disables)")
    parser.add_argument("--twitch-limit", type=int,
                        default=scrape_twitch.MAX_VIDEO_PAGE,
                        help="maximum category-feed VODs per update (0 disables; "
                             "1000/default: walk the full 50-day window)")
    parser.add_argument("--twitch-full", action="store_true",
                        help="capture every available VOD in the 50-day retention "
                             "window (overrides --twitch-limit)")
    parser.add_argument("--twitch-capture-workers", type=int, default=2)
    parser.add_argument("--no-warp", dest="use_warp", action="store_false")
    parser.add_argument("--no-provider", dest="use_provider", action="store_false")
    parser.add_argument("--no-publish", dest="publish", action="store_false")
    parser.add_argument("--keep-screenshots", dest="cleanup", action="store_false")
    args = parser.parse_args()
    os.chdir(ROOT)
    mp.freeze_support()
    main(**vars(args))


if __name__ == "__main__":
    cli()
