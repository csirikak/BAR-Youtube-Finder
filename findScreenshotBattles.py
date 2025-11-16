import sqlite3
import json
from datetime import datetime
from collections import defaultdict
import concurrent.futures
import os
import time

# Use the much faster rapidfuzz library (drop-in replacement for thefuzz)
# pip install rapidfuzz
from rapidfuzz import fuzz

# Used for 6-month date calculation
# pip install python-dateutil
from dateutil.relativedelta import relativedelta

# --- CONFIGURATION ---
DB_NAME = 'data/game_battles.db'
SCREENSHOT_JSON_FILE = 'data/screenshot_data.json'
OUTPUT_JSON_FILE = 'data/matches_output.json'

# A battle must have a score of at least this to be considered a match.
# (0-100 scale from rapidfuzz)
MINIMUM_MATCH_THRESHOLD = 30

# Number of processes to use for matching. Defaults to your system's CPU count.
# Using ProcessPoolExecutor, so this is now # of processes, not threads.
MAX_WORKERS = os.cpu_count() or 20

# How far back from the video upload date to search for battles
MAX_DATE_RANGE_MONTHS = 6

# Mininum number of OCR recognitions to perform a compare.
MIN_LEN = 6

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


def load_data_from_db(conn):
    """
    Loads data by fetching battles first, then iterating through
    all participants once to build both data structures.
    """
    print("Loading data from database...")
    cursor = conn.cursor()

    inverted_index = defaultdict(list)
    battle_data = {}

    # 1. Load all battle metadata first. This is fast.
    print("  Loading battle metadata...")
    cursor.execute("SELECT battle_id, timestamp FROM battles")
    for battle_id, timestamp in cursor.fetchall():
        battle_data[battle_id] = {
            'timestamp': timestamp,
            'players': set()  # Initialize with an empty set
        }

    # 2. Load all participants ONCE and build both structures
    print("  Building player inverted index and populating battle data...")
    cursor.execute("SELECT battle_id, player_name FROM battle_participants")
    
    missing_battles = 0
    for battle_id, player_name in cursor.fetchall():
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


def find_best_match(ocr_player_list, video_upload_date_str, inverted_index, battle_data):
    """
    Finds the best battle_id for a given list of OCR'd players.
    Applies 6-month date filter. Uses raw, case-sensitive strings.
    (This function is unchanged, as it's just pure logic)
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
    best_score = -1
    best_battle_id = None
    
    # Convert the ocr_name_set to a list ONCE for rapidfuzz
    ocr_name_list = list(ocr_name_set)
    
    for battle_id in valid_candidates:
        clean_player_set = battle_data[battle_id]['players']
        
        # rapidfuzz requires a list, not a set.
        clean_player_list = list(clean_player_set)
        
        # token_set_ratio is excellent for this.
        # It compares two sets of strings, ignoring order and duplicates,
        # and handles partial matches well.
        score = fuzz.token_set_ratio(ocr_name_list, clean_player_list)
        
        if score > best_score:
            best_score = score
            best_battle_id = battle_id

    if best_score >= MINIMUM_MATCH_THRESHOLD:
        return best_battle_id, best_score
    else:
        # Match was not good enough
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
    
    print(f"  ... Processing Video: {video_id} (worker {os.getpid()})")
    
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


def main():
    """Main function to run the matching process."""
    
    # 1. Connect to DB and load all data into memory
    # This is done ONCE in the main thread.
    conn = sqlite3.connect(DB_NAME)
    # ***MODIFIED***
    # Load data into local variables that will be passed to the initializer
    inverted_index, battle_data = load_data_from_db(conn)
    conn.close() 
    
    # 2. Load the screenshot JSON
    try:
        with open(SCREENSHOT_JSON_FILE, 'r') as f:
            videos_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Could not find '{SCREENSHOT_JSON_FILE}'.")
        return
    except json.JSONDecodeError:
        print(f"Error: Could not parse '{SCREENSHOT_JSON_FILE}'. Check for JSON errors.")
        return

    print(f"Loaded {len(videos_data)} videos from '{SCREENSHOT_JSON_FILE}'.")
    
    # This will be our new JSON structure for output
    output_data = videos_data
    
    # Lists to aggregate results from threads
    all_matches_to_insert = []
    all_videos_to_insert = []
    
    # 3. Process each video and screenshot using a ProcessPool
    print(f"\n--- Starting parallel processing with {MAX_WORKERS} processes ---")
    start_time = time.perf_counter()
    
    # ***MODIFIED***
    # Create a list of tasks. Each task is a tuple of arguments
    # for our process_video_task function.
    # We now ONLY pass the small, video-specific data.
    tasks = [
        (vid, vinfo) 
        for vid, vinfo in videos_data.items()
    ]
    
    # ***MODIFIED***
    # Switch to ProcessPoolExecutor
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=MAX_WORKERS,
        initializer=init_worker,
        initargs=(inverted_index, battle_data) # Pass large data to initializer
    ) as executor:
        
        # Use executor.map to process tasks in parallel
        # It returns results in the order the tasks were submitted
        results = executor.map(process_video_task, tasks)
        
        # Process results as they complete
        for result in results:
            video_id, video_db_tuple, matches_db_list, screenshots_json_dict = result
            
            # Aggregate results safely in the main thread
            all_videos_to_insert.append(video_db_tuple)
            all_matches_to_insert.extend(matches_db_list)
            
            # Update the main JSON structure
            if video_id in output_data:
                output_data[video_id]['screenshots'] = screenshots_json_dict
    
    end_time = time.perf_counter()
    print(f"--- Parallel processing finished in {end_time - start_time:.2f} seconds ---")
                
    # 4. Save results to Database
    print("\n--- Saving all matches to database ---")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        # Insert all videos
        cursor.executemany('''
        INSERT OR REPLACE INTO videos (video_id, upload_date, title, uploader)
        VALUES (?, ?, ?, ?)
        ''', all_videos_to_insert)
        print(f"Inserted or replaced {len(all_videos_to_insert)} video entries.")
        
        # Insert all battle/video links
        cursor.executemany('''
        INSERT OR REPLACE INTO battle_videos 
        (battle_id, video_id, video_timestamp_sec, match_score, ocr_player_count, battle_player_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', all_matches_to_insert)
        print(f"Inserted or replaced {len(all_matches_to_insert)} screenshot matches.")
        
        conn.commit()
    except Exception as e:
        print(f"Error saving to database: {e}")
        conn.rollback()
    finally:
        conn.close()

    # 5. Save the modified JSON to a new file
    with open(OUTPUT_JSON_FILE, 'w') as f:
        json.dump(output_data, f, indent=4)
        
    print(f"\n--- Process Complete ---")
    print(f"Updated JSON with match data saved to '{OUTPUT_JSON_FILE}'.")


if __name__ == "__main__":
    main()