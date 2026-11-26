"""Match Twitch VOD OCR results to replay battles.

The matching core is shared with findScreenshotBattles.py. Twitch has its own
metadata file, video table, and link table; its results are rebuilt from the
current metadata each run, which keeps the separate tables consistent without
touching the YouTube cache.
"""

import argparse
from contextlib import closing
import concurrent.futures
import json
import sqlite3
import time
from pathlib import Path

import findScreenshotBattles as matching
from json_files import write_json
from twitch_retention import is_recent_upload, retention_cutoff
from updateSchema import ensure_twitch_schema


DB_NAME = "data/game_battles.db"
SCREENSHOT_FILE = "data/twitch_data.json"
OUTPUT_JSON_FILE = "data/twitch_matches.json"
MAX_WORKERS = 4
# A screenshot can only show a battle happening around that point in the VOD.
MATCH_WINDOW_HOURS = 12


def _load_videos(filename):
    path = Path(filename)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Twitch screenshot metadata must be an object")
    return data


def purge_expired_twitch_videos(conn, cutoff):
    """Remove links and VOD rows outside the retention window."""
    conn.execute("""
        DELETE FROM twitch_battle_videos
        WHERE video_id IN (
            SELECT video_id FROM twitch_videos
            WHERE upload_date IS NULL OR length(upload_date) < 8
               OR upload_date < ?
        )
    """, (cutoff,))
    return conn.execute("""
        DELETE FROM twitch_videos
        WHERE upload_date IS NULL OR length(upload_date) < 8
           OR upload_date < ?
    """, (cutoff,)).rowcount


def main(*, db_name=DB_NAME, screenshot_file=SCREENSHOT_FILE,
         output_file=OUTPUT_JSON_FILE, max_workers=MAX_WORKERS, now=None):
    videos = _load_videos(screenshot_file)
    cutoff = retention_cutoff(now)
    # Live-prototype frames use stream IDs, not VOD IDs, and cannot be
    # deep-linked as recordings. VOD capture stores live_status="was_live".
    tasks = [(video_id, info) for video_id, info in videos.items()
             if isinstance(info, dict) and info.get("screenshots")
             and not info.get("is_live")
             and info.get("live_status") not in ("is_live", "is_upcoming", "post_live")
             and is_recent_upload(info.get("upload_date"), cutoff)]
    task_ids = {video_id for video_id, _ in tasks}
    started = time.perf_counter()
    with closing(sqlite3.connect(db_name, timeout=60)) as conn:
        ensure_twitch_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            purged = purge_expired_twitch_videos(conn, cutoff)
            if tasks:
                inverted_index, battle_data = matching.load_data_from_db(
                    conn, tasks, match_window_hours=MATCH_WINDOW_HOURS)
                workers = min(max_workers, max(1, len(tasks) // 32))
                if workers == 1:
                    matching.init_worker(
                        inverted_index, battle_data, MATCH_WINDOW_HOURS)
                    results = map(matching.process_video_task, tasks)
                    results = list(results)
                else:
                    with concurrent.futures.ProcessPoolExecutor(
                            max_workers=workers,
                            initializer=matching.init_worker,
                            initargs=(inverted_index, battle_data,
                                      MATCH_WINDOW_HOURS)) as executor:
                        results = list(executor.map(
                            matching.process_video_task, tasks, chunksize=8))
            else:
                results = []

            conn.execute("DELETE FROM twitch_battle_videos")
            all_matches, metadata_rows, output_data = [], [], {}
            for video_id, info in videos.items():
                if video_id not in task_ids:
                    continue
                output_data[video_id] = dict(info)
            for video_id, _, matches, screenshots in results:
                info = videos[video_id]
                output_data[video_id] = dict(info, screenshots=screenshots)
                metadata_rows.append((
                    video_id, info.get("upload_date"), info.get("title"),
                    info.get("uploader"), info.get("thumbnail"),
                ))
                all_matches.extend(matches)

            conn.executemany("""
                INSERT INTO twitch_videos
                (video_id, upload_date, title, uploader, thumbnail)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    upload_date=excluded.upload_date, title=excluded.title,
                    uploader=excluded.uploader, thumbnail=excluded.thumbnail
            """, metadata_rows)
            conn.executemany("""
                INSERT INTO twitch_battle_videos
                (battle_id, video_id, video_timestamp_sec, match_score,
                 ocr_player_count, battle_player_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, all_matches)
            ids = [row[0] for row in metadata_rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM twitch_videos WHERE video_id NOT IN ({placeholders})",
                    ids)
            else:
                conn.execute("DELETE FROM twitch_videos")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    write_json(output_file, output_data, indent=4)
    summary = {"videos": len(results), "matches": len(all_matches),
               "purged": purged, "seconds": time.perf_counter() - started}
    print(f"Twitch matching complete: {summary}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Match Twitch VODs to replays")
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--screenshot-file", default=SCREENSHOT_FILE)
    parser.add_argument("--output-file", default=OUTPUT_JSON_FILE)
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    main(**vars(parser.parse_args()))
