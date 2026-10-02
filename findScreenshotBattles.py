import sqlite3
import json
from datetime import datetime
from collections import defaultdict
import concurrent.futures
import os
import time
import argparse
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from json_files import write_json
from match_cache import digest, ensure_schema, replay_scope_hash

# Use the much faster rapidfuzz library (drop-in replacement for thefuzz)
# pip install rapidfuzz
from rapidfuzz import fuzz, __version__ as RAPIDFUZZ_VERSION

# Used for the replay date window calculation
# pip install python-dateutil
from dateutil.relativedelta import relativedelta

# --- CONFIGURATION ---
DB_NAME = 'data/game_battles.db'
SCREENSHOT_JSON_FILE = 'data/screenshot_data.json'
OUTPUT_JSON_FILE = 'data/matches_output.json'

# A battle must have a score of at least this to be considered a match.
# (0-100 scale from rapidfuzz)
MINIMUM_MATCH_THRESHOLD = 50

# Maximum number of processes used for uncached videos.
MAX_WORKERS = 8

# How far back from the video upload date to search for battles
MAX_DATE_RANGE_MONTHS = 8

# Mininum number of OCR recognitions to perform a compare.
MIN_LEN = 6
CACHE_VERSION = 1  # Bump when matching semantics change.

# --- END CONFIGURATION ---


# --- Globals for Worker Processes ---
# These will be populated by the init_worker function
# to avoid passing large data with every task.
g_inverted_index = None
g_battle_data = None


def init_worker(inverted_index, battle_data):
    """
    Initializer for each worker process.
    This runs ONCE per process, loading the large read-only
    data into the process's global scope.
    """
    global g_inverted_index, g_battle_data
    g_inverted_index = inverted_index
    g_battle_data = battle_data
    print(f"Worker process {os.getpid()} initialized with data.")


def load_data_from_db(conn, tasks=None):
    """
    Load full history for rebuilds, or only possible candidates for small updates.
    """
    if tasks is not None and len(tasks) <= 200:
        return load_candidate_data(conn, tasks)
    print("Loading data from database...")
    cursor = conn.cursor()

    inverted_index = defaultdict(list)
    battle_data = {}

    # 1. Load all battle metadata first. This is fast.
    print("  Loading battle metadata...")
    cursor.execute("SELECT battle_id, timestamp FROM battles")
    for battle_id, timestamp in cursor:
        battle_data[battle_id] = {
            'timestamp': timestamp,
            'players': set()  # Initialize with an empty set
        }

    # 2. Load all participants ONCE and build both structures
    print("  Building player inverted index and populating battle data...")
    cursor.execute("SELECT battle_id, player_name FROM battle_participants")
    
    missing_battles = 0
    for battle_id, player_name in cursor:
        if not player_name:
            continue
        
        # A. Build the inverted index
        inverted_index[player_name].append(battle_id)
        
        # B. Populate the battle_data dictionary
        if battle_id in battle_data:
            battle_data[battle_id]['players'].add(player_name)
        else:
            # This can happen if a participant is linked to a non-existent battle
            missing_battles += 1

    if missing_battles > 0:
        print(f"  Warning: Found {missing_battles} participant entries for battles not in the 'battles' table.")
        
    print(f"Loaded {len(battle_data)} battles and {len(inverted_index)} unique players.")
    return inverted_index, battle_data


def load_candidate_data(conn, tasks):
    """Use indexed name lookups and date bounds before reading entire rosters."""
    names, dates = set(), []
    for _, info in tasks:
        try:
            dates.append(datetime.strptime(info.get('upload_date'), '%Y%m%d').date())
        except (ValueError, TypeError):
            dates.append(None)
        for players in info.get('screenshots', {}).values():
            recognized = {name for name in players if name}
            if len(recognized) >= MIN_LEN and not any('(AI)' in n or '(Al)' in n for n in recognized):
                names.update(recognized)
    inverted_index, battle_data = defaultdict(list), {}
    if not names:
        return inverted_index, battle_data
    # Created once; the covering index avoids scanning millions of unrelated rows.
    conn.execute('CREATE INDEX IF NOT EXISTS match_participant_names '
                 'ON battle_participants(player_name, battle_id)')
    conn.execute('CREATE TEMP TABLE match_names (name TEXT PRIMARY KEY)')
    conn.executemany('INSERT INTO match_names VALUES (?)', [(name,) for name in sorted(names)])
    conn.execute('CREATE TEMP TABLE match_candidates (battle_id TEXT PRIMARY KEY)')
    conn.execute('''INSERT OR IGNORE INTO match_candidates
        SELECT p.battle_id FROM match_names n
        CROSS JOIN battle_participants p INDEXED BY match_participant_names
        ON p.player_name = n.name''')
    bounded = dates and all(date is not None for date in dates)
    if bounded:
        start = min(dates) - relativedelta(months=MAX_DATE_RANGE_MONTHS)
        end = max(dates)
    for bid, timestamp in conn.execute('''SELECT b.battle_id, b.timestamp
            FROM match_candidates c CROSS JOIN battles b ON b.battle_id = c.battle_id'''):
        if bounded:
            try:
                date = datetime.fromisoformat(timestamp.split('.')[0].replace('Z', '')).date()
                if date < start or date > end:
                    continue
            except (ValueError, TypeError, AttributeError):
                pass  # Match the full matcher's treatment of unknown dates.
        battle_data[bid] = {'timestamp': timestamp, 'players': set()}
    conn.execute('DELETE FROM match_candidates')
    conn.executemany('INSERT INTO match_candidates VALUES (?)', [(bid,) for bid in battle_data])
    for bid, name in conn.execute('''SELECT p.battle_id, p.player_name
            FROM match_candidates c CROSS JOIN battle_participants p ON p.battle_id = c.battle_id'''):
        if name:
            battle_data[bid]['players'].add(name)
            if name in names:
                inverted_index[name].append(bid)
    conn.execute('DROP TABLE match_candidates')
    conn.execute('DROP TABLE match_names')
    print(f'Loaded {len(battle_data)} candidate battles for {len(tasks)} changed videos.')
    return inverted_index, battle_data


def find_best_match(ocr_player_list, video_upload_date_str, inverted_index, battle_data):
    """
    Finds the best battle_id for a given list of OCR'd players.
    Applies the configured date filter. Uses raw, case-sensitive strings.
    """
    
    # Use raw OCR'd names
    ocr_name_set = set(p for p in ocr_player_list if p)
    if not ocr_name_set:
        return None, 0 # No valid players in screenshot
    
    if len(ocr_name_set) < MIN_LEN:
        return None, 0
    
    # 2. FILTER: Find candidate battles
    candidate_scores = defaultdict(int)
    
    # Use the inverted index to find potential matches
    for ocr_name in ocr_name_set:
        # Find battles this player was in (case-sensitive)
        matched_battles = inverted_index.get(ocr_name, [])
        for battle_id in matched_battles:
            # Add 1 to this battle's "candidate score"
            candidate_scores[battle_id] += 1
        # Skip AI games
        if "(AI)" in ocr_name or "(Al)" in ocr_name:
            return None, 0 

    if not candidate_scores:
        # No player was recognized in the index
        return None, 0

    # 3. FILTER: Apply date filter
    # A battle *must* have occurred before the video was uploaded
    # and *not* be older than MAX_DATE_RANGE_MONTHS.
    try:
        # Parse the 'YYYYMMDD' date. This creates a naive datetime.
        upload_dt = datetime.strptime(video_upload_date_str, '%Y%m%d')
        # Calculate the earliest allowed battle date
        earliest_allowed_dt = upload_dt - relativedelta(months=MAX_DATE_RANGE_MONTHS)
    except (ValueError, TypeError):
        # print(f"Warning: Skipping date filter due to invalid upload_date: {video_upload_date_str}")
        upload_dt = None
        earliest_allowed_dt = None
        
    valid_candidates = []
    for battle_id in candidate_scores:
        battle_info = battle_data.get(battle_id)
        if not battle_info:
            continue # Should not happen if DB is consistent

        # Date Check
        if upload_dt:
            try:
                # Parse battle timestamp, removing timezone info to compare with naive upload_dt
                # This assumes battle timestamps are UTC, but compares them all consistently.
                battle_dt = datetime.fromisoformat(battle_info['timestamp'].split('.')[0].replace('Z', ''))
                
                # Check 1: Battle must be ON OR BEFORE the video upload
                # (Allowing same-day)
                if battle_dt.date() > upload_dt.date():
                    continue
                    
                # Check 2: Battle must NOT be older than the allowed range
                if battle_dt.date() < earliest_allowed_dt.date():
                    continue
            except Exception as e:
                # print(f"Warning: Could not parse battle timestamp {battle_info['timestamp']}. Error: {e}")
                pass # Skip this battle if timestamp is bad
                
        valid_candidates.append(battle_id)
        
    if not valid_candidates:
        return None, 0
        
    # 4. SCORE: Find the best match among the valid candidates
    # Convert set to list for stability
    best_score = -1
    best_battle_id = None
    
    # Pre-convert OCR list to a single string for token algorithms
    # This is faster and more robust for token_sort_ratio
    ocr_str = " ".join(ocr_player_list)
    
    for battle_id in sorted(valid_candidates):
        clean_player_set = battle_data[battle_id]['players']
        clean_player_list = list(clean_player_set)
        
        # Join the battle roster into a string
        battle_str = " ".join(clean_player_list)
        
        # 1. Set Ratio: Checks if OCR is a valid SUBSET of Battle
        # Returns 100 for (a,b) vs (a,b,c,d)
        score_set = fuzz.token_set_ratio(ocr_str, battle_str)
        
        # 2. Sort Ratio: Checks if the TOTAL CONTENT matches
        # Returns ~50 for (a,b) vs (a,b,c,d) because of length difference
        score_sort = fuzz.token_sort_ratio(ocr_str, battle_str)
        
        # 3. Hybrid Score: Average them
        # (a,b) -> (100 + 50) / 2 = 75
        # (a,b,c,d) -> (100 + 100) / 2 = 100
        score = (score_set + score_sort) / 2
        
        if score > best_score:
            best_score = score
            best_battle_id = battle_id

    if best_score >= MINIMUM_MATCH_THRESHOLD:
        return best_battle_id, best_score
    else:
        return None, best_score


def process_video_task(task_args):
    """
    A single unit of work for the PROCESS pool.
    Processes one video and all its screenshots.
    Returns results to be aggregated by the main thread.
    
    task_args is now just (video_id, video_info)
    """
    # ***MODIFIED***
    # Access the large data from the worker's global scope
    global g_inverted_index, g_battle_data
    
    # Unpack the lightweight task-specific data
    video_id, video_info = task_args
    
    
    video_db_tuple = (
        video_id,
        video_info.get('upload_date'),
        video_info.get('title'),
        video_info.get('uploader')
    )
    
    matches_db_list = []
    screenshots_json_dict = {}
    
    screenshots = video_info.get('screenshots', {})
    for timestamp_sec, ocr_player_list in screenshots.items():
        
        # ***MODIFIED***
        # Pass the process-global data to the matching function
        battle_id, score = find_best_match(
            ocr_player_list,
            video_info.get('upload_date'),
            g_inverted_index,
            g_battle_data
        )
        
        if battle_id:
            # Add to our batch for DB insertion
            matches_db_list.append((
                battle_id,
                video_id,
                int(timestamp_sec),
                score,
                len(ocr_player_list),
                len(g_battle_data[battle_id]['players']) # Use global data
            ))
            
            # Update the output JSON structure
            screenshots_json_dict[timestamp_sec] = {
                "players_ocr": ocr_player_list,
                "matched_battle_id": battle_id,
                "match_score": round(score, 2)
            }
        else:
            screenshots_json_dict[timestamp_sec] = {
                "players_ocr": ocr_player_list,
                "matched_battle_id": None,
                "match_score": round(score, 2)
            }
            
    return (video_id, video_db_tuple, matches_db_list, screenshots_json_dict)


def main(*, db_name=DB_NAME, screenshot_file=SCREENSHOT_JSON_FILE,
         output_file=OUTPUT_JSON_FILE, force=False, max_workers=MAX_WORKERS):
    """Reuse unchanged video matches; recompute only affected replay windows."""
    if max_workers < 1:
        raise ValueError("Worker count must be positive")
    started = time.perf_counter()
    videos_data = json.loads(Path(screenshot_file).read_text(encoding="utf-8"))
    if not isinstance(videos_data, dict):
        raise ValueError("Screenshot metadata must be an object")
    with closing(sqlite3.connect(db_name, timeout=60)) as conn:
        ensure_schema(conn)
        # The updater already waits for replay sync. A consistent transaction also
        # prevents a standalone writer changing rosters underneath this snapshot.
        conn.execute("BEGIN IMMEDIATE")
        try:
            revisions = list(conn.execute("SELECT day, revision FROM match_day_revisions ORDER BY day"))
            cached = {row[0]: row[1:] for row in conn.execute(
                "SELECT video_id, input_hash, replay_hash, result_json FROM video_match_cache")}
            settings = [CACHE_VERSION, MIN_LEN, MINIMUM_MATCH_THRESHOLD,
                        MAX_DATE_RANGE_MONTHS, RAPIDFUZZ_VERSION]

            @lru_cache(maxsize=None)
            def scope(date):
                return replay_scope_hash(revisions, date, MAX_DATE_RANGE_MONTHS)

            pending, keys, results = [], {}, {}
            reused = 0
            for video_id, info in videos_data.items():
                key = digest([settings, info.get("upload_date"), info.get("screenshots", {})])
                replay_key = scope(info.get("upload_date"))
                keys[video_id] = (key, replay_key)
                previous = cached.get(video_id)
                if not force and previous and previous[:2] == (key, replay_key):
                    try:
                        result = json.loads(previous[2])
                        if (not isinstance(result, list) or len(result) != 4
                                or result[0] != video_id
                                or not isinstance(result[2], list)
                                or not isinstance(result[3], dict)):
                            raise ValueError("Invalid cached result")
                        results[video_id] = result
                        reused += 1
                        continue
                    except (ValueError, TypeError):
                        pass  # Rebuild a damaged cache entry from authoritative inputs.
                pending.append((video_id, info))
            print(f"Replay matches: {reused} videos cached, {len(pending)} to recompute")

            if pending:
                inverted_index, battle_data = load_data_from_db(conn, pending)
                # Small updates finish faster without copying an index to many processes.
                workers = min(max_workers, max(1, len(pending) // 32))
                if workers == 1:
                    init_worker(inverted_index, battle_data)
                    computed = map(process_video_task, pending)
                    for result in computed:
                        results[result[0]] = result
                else:
                    with concurrent.futures.ProcessPoolExecutor(
                        max_workers=workers, initializer=init_worker,
                        initargs=(inverted_index, battle_data)
                    ) as executor:
                        for result in executor.map(process_video_task, pending, chunksize=8):
                            results[result[0]] = result

            # Reconstruct outputs from both reused and new results. This also
            # removes stale links for removed frames/videos without a full rematch.
            conn.execute("DELETE FROM battle_videos")
            all_matches, metadata_rows = [], []
            output_data = {}
            for video_id, info in videos_data.items():
                result = results[video_id]
                all_matches.extend(result[2])
                metadata_rows.append((video_id, info.get("upload_date"),
                                      info.get("title"), info.get("uploader")))
                output_data[video_id] = dict(info, screenshots=result[3])
            conn.executemany("""
                INSERT INTO videos(video_id, upload_date, title, uploader) VALUES (?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET upload_date=excluded.upload_date,
                title=excluded.title, uploader=excluded.uploader
            """, metadata_rows)
            conn.executemany("""
                INSERT INTO battle_videos
                (battle_id, video_id, video_timestamp_sec, match_score,
                 ocr_player_count, battle_player_count) VALUES (?, ?, ?, ?, ?, ?)
            """, all_matches)
            for video_id, _ in pending:
                conn.execute("INSERT OR REPLACE INTO video_match_cache VALUES (?, ?, ?, ?)",
                             (video_id, *keys[video_id], json.dumps(results[video_id], ensure_ascii=False)))
            conn.executemany("DELETE FROM video_match_cache WHERE video_id = ?",
                             [(vid,) for vid in cached if vid not in videos_data])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    # If publishing fails, the next run can recover from the committed cache.
    write_json(output_file, output_data, indent=4)
    summary = {"cached": reused, "computed": len(pending),
               "matches": len(all_matches), "seconds": time.perf_counter() - started}
    print(f"Replay matching complete: {summary}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Incremental OCR-to-replay matching")
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--screenshot-file", default=SCREENSHOT_JSON_FILE)
    parser.add_argument("--output-file", default=OUTPUT_JSON_FILE)
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--force", action="store_true", help="Recompute every match and rebuild the cache")
    main(**vars(parser.parse_args()))
