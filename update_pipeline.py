"""Run replay sync, YouTube capture, and streaming OCR with bounded concurrency."""

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
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

import scrape
import processScreenshotsRapidOCR as ocr
import updateBattleDB


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
        print(f"[update] Starting {name}", flush=True)
        try:
            result = function(*args, **kwargs)
            if result is False:
                raise RuntimeError(f"{name} reported failure")
            return result
        finally:
            elapsed = time.perf_counter() - start
            with self.lock:
                self.seconds[name] = elapsed
            print(f"[update] {name}: {elapsed:.2f}s elapsed", flush=True)


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

    def batches(self):
        finished = False
        while not finished and not self.stop_event.is_set():
            try:
                frame = self.queue.get(timeout=0.2)
            except Empty:
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
            yield batch


def scrape_channels(channel_urls, screenshots_dir, metadata_file, stream,
                    channel_workers=2, capture_workers=4, stop_event=None):
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
                        on_screenshot=stream.put, metadata_file=metadata_file,
                        stop_event=stop_event
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
                  channel_workers=2, capture_workers=4, ocr_batch_size=8, times=None):
    if min(channel_workers, capture_workers, ocr_batch_size) < 1:
        raise ValueError("Worker counts and OCR batch size must be positive")
    times = times or StageTimes()
    stop_event = threading.Event()
    stream = FrameStream(ocr_batch_size, stop_event=stop_event)
    initial = ocr.load_metadata(metadata_file)
    for path, _, _ in ocr.pending_frames(screenshots_dir, initial):
        stream.put(path)

    with ThreadPoolExecutor(max_workers=3) as stages:
        database = stages.submit(times.run, "replay sync", updateBattleDB.main,
                                 db_name=player_database, stop_event=stop_event)
        capture = stages.submit(
            times.run, "YouTube capture", scrape_channels, channel_urls,
            screenshots_dir, metadata_file, stream, channel_workers, capture_workers, stop_event
        )

        def consume():
            # Read the latest catalog only after the writer commits. This also
            # avoids SQLite read/write contention during a large replay sync.
            database.result()
            return times.run(
                "OCR", ocr.main, screenshots_dir=screenshots_dir,
                output_file=metadata_file, player_database=player_database,
                batch_size=ocr_batch_size, frame_batches=stream.batches(),
                metadata_lock=scrape.DB_LOCK
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


def main(*, channel_urls=None, channel_workers=2, capture_workers=4,
         ocr_batch_size=8, use_warp=True, use_provider=True,
         publish=True, cleanup=True):
    if min(channel_workers, capture_workers, ocr_batch_size) < 1:
        raise ValueError("Worker counts and OCR batch size must be positive")
    started = time.perf_counter()
    times = StageTimes()
    try:
        with network_services(use_warp, use_provider):
            run_ingestion(
                channel_urls=CHANNELS if channel_urls is None else channel_urls,
                screenshots_dir=scrape.SCREENSHOT_DIR,
                metadata_file=scrape.SCREENSHOT_DATA,
                player_database=updateBattleDB.DB_NAME,
                channel_workers=channel_workers, capture_workers=capture_workers,
                ocr_batch_size=ocr_batch_size, times=times
            )
        # A fresh interpreter keeps the matching pool's workers independent of
        # the updater's GPU context and avoids importing its OCR model stack.
        times.run("replay matching", subprocess.run,
                  [sys.executable, str(ROOT / "findScreenshotBattles.py")],
                  cwd=ROOT, check=True)
        from exportForFrontend import export_data
        times.run("frontend export", export_data)
        if publish:
            times.run("Git publish", publish_changes)
        if cleanup and Path(scrape.SCREENSHOT_DIR).exists():
            shutil.rmtree(scrape.SCREENSHOT_DIR)
        return times.seconds
    finally:
        print(f"[update] Total wall time: {time.perf_counter() - started:.2f}s", flush=True)
        print("[update] Stage times overlap and include waits; do not add them together.", flush=True)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-workers", type=int, default=2)
    parser.add_argument("--capture-workers", type=int, default=4)
    parser.add_argument("--ocr-batch-size", type=int, default=8)
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
