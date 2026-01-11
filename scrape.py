import yt_dlp
import subprocess
import os
import threading
from yt_dlp.utils import DateRange
import curl_cffi
from yt_dlp.networking import impersonate
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import datetime as dt
import json
import re

# --- CONFIGURATION ---
# Max number of videos to download/process in parallel
MAX_WORKERS = 30 
# Time of the first screenshot in seconds
START_TIME_SEC = 90  # 1:30
# Interval between screenshots in seconds
INTERVAL_SEC = 12 * 60 # 12 minutes
# --- --- --- --- ---
SCREENSHOT_DIR = "data/AllBarScreenshots"

FETCH_FROM = "20240601"

SCREENSHOT_DATA = "data/screenshot_data.json"
DB_LOCK = threading.Lock()

def populateHaveSet():
    unique_ids = set()

    # 1. Process files in the Screenshot Directory
    if os.path.exists(SCREENSHOT_DIR) and os.path.isdir(SCREENSHOT_DIR):
        for entry in os.listdir(SCREENSHOT_DIR):
            full_path = os.path.join(SCREENSHOT_DIR, entry)
            if os.path.isfile(full_path):
                # Remove suffix to get the raw ID (e.g. "abc_30s.png" -> "abc")
                video_id = re.sub(r"_\d+s\.png$", '', entry)
                unique_ids.add(video_id.strip())
    
    # 2. Process keys in the JSON Data file
    if os.path.exists(SCREENSHOT_DATA) and os.path.isfile(SCREENSHOT_DATA):
        try:
            with open(SCREENSHOT_DATA, 'r') as f:
                data = json.load(f)
                # Iterate through keys to strip whitespace, ensuring " id " matches "id"
                for video_id in data.keys():
                    unique_ids.add(str(video_id).strip())
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error reading {SCREENSHOT_DATA}: {e}")

    # 3. Write sorted, unique IDs to file
    with open("/tmp/haveScreenshots.txt", 'w') as f:
        # sorted() makes the output deterministic and easier to read
        for line in sorted(unique_ids):
            f.write(f"youtube {line}\n")

populateHaveSet()
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
            # Ensure it's a dictionary
            if not isinstance(video_db, dict):
                print(f"Warning: '{db_filepath}' was not a valid dictionary. Starting fresh.")
                video_db = {}
        except (FileNotFoundError, json.JSONDecodeError):
            print(f"No existing database found at '{db_filepath}'. Creating a new one.")
            video_db = {}
        except Exception as e:
            print(f"Error reading database file: {e}. Starting with an empty DB.")
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
                'thumbnail': video.get('thumbnail')
            }

            # Check if it's an addition or update
            if video_id in video_db:
                updates_count += 1
            else:
                additions_count += 1
            
            # Add or update the entry
            video_db[video_id] = video_data

        # 3. Write the updated database back to the file
        try:
            with open(db_filepath, 'w', encoding='utf-8') as f:
                # Use indent=4 for readability, ensure_ascii=False for special chars
                json.dump(video_db, f, indent=4, ensure_ascii=False)
        except IOError as e:
            print(f"\n[ERROR] Could not write database to '{db_filepath}': {e}")
            # Return 0,0 if save fails
            return (0, 0)
        
        # 4. Return the result
        return (additions_count, updates_count)

def fetch_and_process_video(ydl_instance, video_url, output_dir, game_tag):
    """
    Helper function for Stage 2.
    Fetches full metadata for a single video URL and then
    passes that metadata to the screenshot processing function.
    """
    try:
        # This is the network call to get stream URLs
        
        
        # Now we have the full info, update the database
        
        
        # And process the screenshots
        return process_video_screenshots(full_video_info, output_dir, game_tag)
        
    except Exception as e:
        # This error is for the *individual* video fetch
        print(f"[WARN] Failed to fetch full metadata for {video_url}: {e}")
        return None, f"Skipped (Failed full fetch: {e})", 0

def process_video_screenshots(video, output_dir, game_tag=""):
    """
    Processes a single video entry:
    - Filters by tag
    - Checks if all screenshots *already exist* before processing.
    - Takes screenshots at START_TIME_SEC and every INTERVAL_SEC thereafter.
    - This function is designed to be run in a thread pool.
    """
    if video is None:
        return None, "Skipped (None entry)", 0

    video_id = video.get('id')
    video_title = video.get('title', 'N/A')
    tags = video.get('tags')

    # --- PYTHON TAG FILTER ---
    if game_tag: # Only filter if a game_tag is provided
        if not isinstance(tags, list):
            return video_id, f"Skipped '{video_title}' (No tags)", 0
        
        if game_tag.lower() not in [tag.lower() for tag in tags]:
            return video_id, f"Skipped '{video_title}' (Tag not found)", 0

    # --- DURATION & COMPLETION CHECK ---
    duration = video.get('duration')
    if not duration:
        return video_id, f"Skipped '{video_title}' (No duration info)", 0
        
    if duration < START_TIME_SEC:
        return video_id, f"Skipped '{video_title}' (Shorter than {START_TIME_SEC}s)", 0

    # --- "ALL-OR-NOTHING" CHECK ---
    expected_timestamps = list(range(START_TIME_SEC, math.floor(duration), INTERVAL_SEC))
    
    all_files_exist = True
    if not expected_timestamps:
        pass
    
    for timestamp_sec in expected_timestamps:
        output_filename = os.path.join(output_dir, f"{video_id}_{timestamp_sec}s.png")
        if not os.path.exists(output_filename):
            all_files_exist = False
            break
    
    if all_files_exist:
        return video_id, f"Skipped '{video_title}' (All screenshots exist)", 0
    # --- END NEW CHECK ---


    # --- PROCESS VIDEO (BECAUSE FILES ARE MISSING) ---
    
    try:
        stream_url = video['requested_formats'][0]['url']
        http_headers = video['requested_formats'][0]['http_headers']
    except:
        stream_url = video.get('url')
        http_headers = video.get('http_headers')
    if not stream_url:
        return video_id, f"Skipped '{video_title}' (Could not get stream URL)", 0
    else:
        print(f"Format: {video.get('format_note')} (ID: {video_id})")
    header_string = ""
    if http_headers:
        header_string = "".join(
            f"{key}: {value}\r\n" for key, value in http_headers.items()
        )

    screenshots_taken = 0
    
    for timestamp_sec in expected_timestamps:
        
        output_filename = os.path.join(output_dir, f"{video_id}_{timestamp_sec}s.png")
        
        if os.path.exists(output_filename):
            continue

        # 6. Use ffmpeg to grab the screenshot
        ffmpeg_command = ['ffmpeg']
        
        if header_string:
            ffmpeg_command.extend(['-headers', header_string])

        ffmpeg_command.extend([
            '-hwaccel', 'vaapi',                 # 1. Force VAAPI
            '-hwaccel_device', '/dev/dri/renderD128', # 2. Select the AMD GPU
            '-hwaccel_output_format', 'yuv420p', # 3. Copy frame from GPU to CPU
            '-ss', str(timestamp_sec), 
            '-i', stream_url,
            '-vframes', '1',
            '-reconnect', '1',
            '-y',
            output_filename
        ])
        try:
            subprocess.run(
                ffmpeg_command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            screenshots_taken += 1
        except subprocess.CalledProcessError as e:
            # Print the error code for easier debugging
            print(f"--- FAILED (ffmpeg) for '{video_title}' at {timestamp_sec}s. Error: {e}")
        except FileNotFoundError:
            print("\n[ERROR] 'ffmpeg' command not found.")
            return video_id, "Failed (ffmpeg not found)", screenshots_taken
    return video_id, f"Processed '{video_title}, format: {video.get("format")}'", screenshots_taken


def get_channel_screenshots(channel_url, output_dir, game_tag=""):
    """
    Downloads screenshots from all videos in a channel using a 
    two-stage, library-only method to avoid rate-limiting.
    """
    
    os.makedirs(output_dir, exist_ok=True)
    print(f"Saving screenshots to: {os.path.abspath(output_dir)}")

    # --- STAGE 1: Fast "flat" fetch ---
    # We fetch *only* the playlist structure, not the
    # deep metadata for every single video. This is fast
    # and avoids rate-limiting.
    
    print(f"Stage 1: Fetching flat video list for channel: {channel_url}...")
    
    # 'extract_flat': 'in_playlist' is the key.
    # It returns a list of video entries immediately.
    flat_opts = {
        'extract_flat': 'in_playlist',
        'daterange': DateRange(start=FETCH_FROM),
        'download_archive': '/tmp/haveScreenshots.txt',
        'impersonate': impersonate.ImpersonateTarget("safari", "26.0"),
        'extractor_args': {"youtube":{"player_client": ["tv_simply"]}},
        'js_runtimes': {'node':{}},
        'quiet': False,
        'ignoreerrors': True,
    }

    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            # This call is now very fast and returns a flat list
            # of all videos that passed the daterange/archive filters.
            channel_info = ydl.extract_info(channel_url, download=False)
    
    except yt_dlp.utils.DownloadError as e:
        print(f"\n[ERROR] A yt-dlp error occurred during flat fetch: {e}")
        return False
    except Exception as e:
        print(f"\n[ERROR] An unexpected error occurred: {e}")
        return False

    if not channel_info or 'entries' not in channel_info or not channel_info['entries']:
        print("No new videos found matching your criteria.")
        return True

    # The videos are already flat, so we just grab the list
    videos_to_process = channel_info['entries']
    
    # We didn't use 'break_on_reject', so we have all videos
    # since FETCH_FROM. We can't stop early, but this single
    # flat fetch is far less taxing than your original method.
    print(f"Stage 1 Complete: Found {len(videos_to_process)} new videos.")

    # --- STAGE 2: Fetch Full Metadata & Process in Parallel ---
    # Now we loop through our filtered list and fetch the *full*
    # metadata (with stream URLs) only for these few videos.
    
    print("Stage 2: Submitting videos to processing pool...")
    
    # These are the options needed to get stream URLs
    full_fetch_opts = {
        'format': 'bestvideo*+bestaudio/best',
        'impersonate': impersonate.ImpersonateTarget("safari", "26.0"),
        'extractor_args': {"youtube":{"player_client": ["tv_simply"]}},
        'js_runtimes': {'node':{}},
        'quiet': False,
    }
    print("Stage 2: Fetching full metadata for new videos sequentially...")
    full_videos_to_process = []
    
    # Use a single ydl instance to fetch metadata sequentially
    try:
        with yt_dlp.YoutubeDL(full_fetch_opts) as ydl:
            for i, video_stub in enumerate(videos_to_process, 1):
                if not video_stub or not video_stub.get('url'):
                    print(f"[WARN] Skipping video stub {i}/{len(videos_to_process)} (no URL).")
                    continue
                
                print(f"Fetching metadata for video {i}/{len(videos_to_process)}: {video_stub.get('id')}")
                try:
                    # 1. FETCH SEQUENTIALLY
                    full_video_info = ydl.extract_info(video_stub['url'], download=False)
                    
                    if not full_video_info:
                        print(f"[WARN] Failed to fetch full info for {video_stub.get('id')}.")
                        continue
                        
                    # 2. UPDATE DATABASE SEQUENTIALLY
                    update_video_database([full_video_info], SCREENSHOT_DATA)
                    
                    # 3. ADD TO LIST FOR PARALLEL PROCESSING
                    full_videos_to_process.append(full_video_info)
                    
                except yt_dlp.utils.DownloadError as e:
                    print(f"[WARN] Failed to fetch {video_stub.get('id')}: {e}. Video may be private/deleted.")
                except Exception as e:
                    print(f"[ERROR] Unexpected error fetching {video_stub.get('id')}: {e}")

    except Exception as e:
        print(f"[ERROR] Failed to initialize YoutubeDL for Stage 2: {e}")
        return False

    print(f"\nMetadata fetch complete. Found {len(full_videos_to_process)} processable videos.")
    print("Submitting screenshot processing to parallel thread pool...")

    # Now, process the screenshots in parallel using the full info we just gathered
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_video_id = {}
        
        for video_info in full_videos_to_process:
            future = executor.submit(
                process_video_screenshots, 
                video_info, 
                output_dir, 
                game_tag
            )
            future_to_video_id[future] = video_info.get('id', 'unknown')

        print(f"Submitted {len(future_to_video_id)} videos to {MAX_WORKERS} workers.")
        
        for future in as_completed(future_to_video_id):
            video_id_from_future = future_to_video_id[future]
            try:
                # This will be (vid_id, message, count)
                result = future.result()
                if not result:
                    continue
                    
                vid_id, message, count = result
                
                if count > 0:
                    print(f"+++ SUCCESS (ID: {vid_id}): Grabbed {count} new screenshot(s) from '{message.replace('Processed ', '')}'")
                elif "Skipped" in message:
                    print(f"--- INFO (ID: {vid_id}): {message}")
                    pass 
                    
            except Exception as e:
                print(f"[ERROR] Thread for video ID {video_id_from_future} failed: {e}")
    return True
# --- --- --- --- ---
#      RUN SCRIPT
# --- --- --- --- ---
if __name__ == "__main__":
    channels = [
        "https://www.youtube.com/channel/UC-QkFO7qGgPv5J3c8pGOpIQ/recent",
        "https://www.youtube.com/@BetterStrategy/videos",
        "https://www.youtube.com/@JAWSMUNCH304/videos",
        "https://www.youtube.com/@simplygraceful1/videos",
        "https://www.youtube.com/@dskinnerify/videos",
        "https://www.youtube.com/@BrightWorksTV/videos",
        "https://www.youtube.com/@MoreDrongo/videos",
        "https://www.youtube.com/@SuperKitowiec2/videos",
    ]
    for channel in channels:
        get_channel_screenshots(
            channel_url=channel, 
            output_dir=SCREENSHOT_DIR,
            game_tag=""  
        )
    print("\nScript finished.")