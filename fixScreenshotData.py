import yt_dlp
import json
import os
import re
import sys
from yt_dlp.networking import impersonate

# --- CONFIGURATION ---
# Point these to your data
SCREENSHOT_DIR = "data/AllBarScreenshots"
DB_FILEPATH = "data/screenshot_data.json"
# --- --- --- --- ---

def get_ids_from_screenshot_dir(directory):
    """
    Scans the screenshot directory and returns a set of
    all unique video IDs found in the filenames.
    """
    file_ids = set()
    if not os.path.exists(directory) or not os.path.isdir(directory):
        print(f"[ERROR] Screenshot directory not found: {directory}")
        return file_ids

    for entry in os.listdir(directory):
        if entry.endswith('.png'):
            # Use regex to strip off the timestamp suffix
            # e.g., "bO4DytVhO8Q_90s.png" -> "bO4DytVhO8Q"
            video_id = re.sub(r"_\d+s\.png$", '', entry)
            file_ids.add(video_id.strip())
            
    return file_ids

def sync_database(db_filepath, screenshot_dir):
    """
    Synchronizes the JSON database with the screenshot directory.
    - Finds screenshots with no entry in the JSON.
    - Finds JSON entries with missing metadata.
    - Fetches info for all and saves the complete database.
    """
    
    # 1. Load the database
    video_db = {}
    if os.path.exists(db_filepath):
        try:
            with open(db_filepath, 'r', encoding='utf-8') as f:
                video_db = json.load(f)
            if not isinstance(video_db, dict):
                raise ValueError("Database is not a valid JSON object.")
        except Exception as e:
            print(f"Error loading JSON from '{db_filepath}': {e}")
            print("Starting with an empty database in memory.")
            video_db = {}
    else:
        print(f"No database found at '{db_filepath}'. A new one will be created.")

    # 2. Get all IDs from file system
    ids_from_files = get_ids_from_screenshot_dir(screenshot_dir)
    if not ids_from_files:
        print("No screenshots found. Exiting.")
        return
        
    print(f"Found {len(ids_from_files)} unique video IDs in screenshot directory.")
    
    # 3. Find what's missing or incomplete
    ids_in_json = set(video_db.keys())
    
    # Find IDs that are in files but not in the JSON
    missing_from_json = ids_from_files - ids_in_json
    print(f"Found {len(missing_from_json)} IDs in files that are missing from the JSON.")

    # Find IDs in the JSON that are incomplete
    incomplete_in_json = set()
    for video_id, data in video_db.items():
        if not data:
            incomplete_in_json.add(video_id)
            continue
        # Check for missing/empty values
        if (not data.get('title') or 
            not data.get('upload_date') or 
            not data.get('uploader')):
            incomplete_in_json.add(video_id)
            
    print(f"Found {len(incomplete_in_json)} existing JSON entries with incomplete metadata.")
    
    # 4. Combine into a single list of IDs to fetch
    ids_to_fetch = missing_from_json.union(incomplete_in_json)
    
    if not ids_to_fetch:
        print("Database is already in sync. No work to do. Exiting.")
        return

    print(f"\nTotal videos to fetch/update: {len(ids_to_fetch)}. Starting...")

    # 5. Create a backup
    backup_filepath = db_filepath + '.sync-bak'
    try:
        with open(backup_filepath, 'w', encoding='utf-8') as f:
            json.dump(video_db, f, indent=4, ensure_ascii=False)
        print(f"Created backup at {backup_filepath}")
    except Exception as e:
        print(f"Warning: Could not create backup file. {e}")
        
    # 6. Set up yt-dlp to fetch info
    ydl_opts = {
        'quiet': True,
        'ignoreerrors': True,
        'no_download': True,
        'impersonate': impersonate.ImpersonateTarget("safari", "26.0"),
        'extractor_args': {"youtube": {"player_client": ["tv_simply"]}},
        'js_runtimes': {'node':{}},
    }

    updates_count = 0
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for video_id in ids_to_fetch:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            print(f"Fetching: {video_id}...")
            
            try:
                info = ydl.extract_info(video_url, download=False)
                
                if not info:
                    print(f"  -> Failed (no info returned): {video_id}")
                    continue

                # Create the data payload (same as in your main script)
                video_data = {
                    'title': info.get('title'),
                    'upload_date': info.get('upload_date'),
                    'duration': info.get('duration'),
                    'uploader': info.get('uploader'),
                    'tags': info.get('tags'),
                    'thumbnail': info.get('thumbnail')
                }
                
                # Add or update the entry in our in-memory db
                video_db[video_id] = video_data
                print(f"  -> Synced: {info.get('title')}")
                updates_count += 1

            except yt_dlp.utils.DownloadError as e:
                print(f"  -> Failed (DownloadError): {video_id}. Video likely private/deleted.")
                # Add placeholder data so we don't try again
                if video_id not in video_db or not video_db[video_id].get('title'):
                    video_db[video_id] = {
                        'title': 'N/A (Fetch Failed - Private/Deleted)',
                        'upload_date': 'N/A', 'duration': 0,
                        'uploader': 'N/A', 'tags': [], 'thumbnail': ''
                    }
                updates_count += 1
            
            except Exception as e:
                print(f"  -> Failed (Unexpected Error): {video_id}. {e}")

    # 7. Save the updated database
    if updates_count > 0:
        print(f"\nTotal updates made: {updates_count}.")
        print(f"Saving synchronized database to {db_filepath}...")
        try:
            with open(db_filepath, 'w', encoding='utf-8') as f:
                json.dump(video_db, f, indent=4, ensure_ascii=False)
            print("Save complete.")
        except Exception as e:
            print(f"FATAL ERROR: Could not write updates to '{db_filepath}'. {e}")
            print(f"Your original data is safe in '{backup_filepath}'")
    else:
        print("\nNo updates were successfully made.")

if __name__ == "__main__":
    sync_database(DB_FILEPATH, SCREENSHOT_DIR)