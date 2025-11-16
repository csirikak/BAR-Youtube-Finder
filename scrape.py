import yt_dlp
import subprocess
import os
import sys
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
MAX_WORKERS = 20 
# Time of the first screenshot in seconds
START_TIME_SEC = 90  # 1:30
# Interval between screenshots in seconds
INTERVAL_SEC = 12 * 60 # 12 minutes
# --- --- --- --- ---
SCREENSHOT_DIR = "data/AllBarScreenshots"

FETCH_FROM = "20240601"

def populateHaveSet():
    file_names = set()
    if os.path.exists(SCREENSHOT_DIR) and os.path.isdir(SCREENSHOT_DIR):
        for entry in os.listdir(SCREENSHOT_DIR):
            full_path = os.path.join(SCREENSHOT_DIR, entry)
            if os.path.isfile(full_path):
                entry = re.sub(r"_\d+s\.png$", '', entry)
                file_names.add(entry.strip())
    
    with open("/tmp/haveScreenshots.txt", 'w') as f:
        for line in file_names:
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

        # --- --- --- --- --- --- --- --- --- --- --- --- ---
        # *** THE FIX IS HERE ***
        # By placing -i (input) *before* -ss (seek), we use "output seeking".
        # This is more reliable for complex streams (like live/DVR)
        # than "input seeking" (-ss before -i).
        #
        # OLD (Failing) ORDER:
        ffmpeg_command.extend([
            '-hwaccel', 'vaapi',                 # 1. Force VAAPI
            '-hwaccel_device', '/dev/dri/renderD128', # 2. Select the AMD GPU
            '-hwaccel_output_format', 'yuv420p', # 3. Copy frame from GPU to CPU
            '-ss', str(timestamp_sec), 
            '-i', stream_url,
            '-vframes', '1',
            '-y', 
            output_filename
        ])
        #
        # NEW (Reliable) ORDER:
        '''
        ffmpeg_command.extend([
            '-i', stream_url,
            '-ss', str(timestamp_sec), # Seek to the correct timestamp
            '-vframes', '1',
            '-y', 
            output_filename
        ])
        '''
        # --- --- --- --- --- --- --- --- --- --- --- --- ---

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
    Downloads screenshots from all videos in a channel.
    - Fetches all video info in a single pass.
    - Uses a ThreadPoolExecutor to process videos in parallel.
    """
    
    os.makedirs(output_dir, exist_ok=True)
    print(f"Saving screenshots to: {os.path.abspath(output_dir)}")
    ydl_opts = {
        'format': 'bestvideo*+bestaudio/best',
        'impersonate': impersonate.ImpersonateTarget("safari", "26.0"),
        'extractor_args': {"youtube":{"player_client": ["tv_simply"]}},
        #'sleep_interval_requests': 5,
        'quiet': False,
        'ignoreerrors': True, 
        'js_runtimes': {'node':{}},
        #'cookiefile': "cookies-youtube-com.txt",
        'daterange': DateRange(start=FETCH_FROM),
        'verbose': True,
        'download_archive': '/tmp/haveScreenshots.txt',
        'break_on_reject': True,

    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"Fetching video list for channel: {channel_url}...")
            print(f"Filtering for tag: '{game_tag}' and videos since 2024-06-01.")
            ydl.extract_info
            channel_info = ydl.extract_info(channel_url, download=False)
            '''
            if 'entries' not in channel_info or not channel_info['entries']:
                print("No videos found matching your date criteria.")
                return
            '''
            videos = list(flatten_entries(channel_info['entries']))
            if not videos:
                print("No video entries found after flattening playlists.")
                return
            
            additions, updates = update_video_database(videos, 'screenshot_data.json')
            print(f"Database: {additions} new videos added, {updates} existing videos updated.")
            print(f"Found {len(videos)} videos (post-date-filter). Submitting to processing pool...")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_video = {}
                for video in videos:
                    if video:
                        future = executor.submit(process_video_screenshots, video, output_dir, game_tag)
                        future_to_video[future] = video.get('id', 'unknown')

                print(f"Submitted {len(future_to_video)} videos to {MAX_WORKERS} workers.")
                
                for future in as_completed(future_to_video):
                    video_id_from_future = future_to_video[future]
                    try:
                        vid_id, message, count = future.result()
                        if count > 0:
                            print(f"+++ SUCCESS (ID: {vid_id}): Grabbed {count} new screenshot(s) from '{message.replace('Processed ', '')}'")
                        
                        elif "Skipped" in message:
                            print(f"--- INFO (ID: {vid_id}): {message}")
                            
                    except Exception as e:
                        # Print the exception to see what went wrong in the thread
                        print(f"[ERROR] Thread for video ID {video_id_from_future} failed: {e}")

    except yt_dlp.utils.DownloadError as e:
        print(f"\n[ERROR] A yt-dlp error occurred: {e}")
    except Exception as e:
        print(f"\n[ERROR] An unexpected error occurred: {e}")

# --- --- --- --- ---
#      RUN SCRIPT
# --- --- --- --- ---
if __name__ == "__main__":
    channels = [
        "https://www.youtube.com/@simplygraceful1/videos",
        "https://www.youtube.com/@dskinnerify/videos",
        "https://www.youtube.com/@BrightWorksTV/videos",
        "https://www.youtube.com/@MoreDrongo/videos",
        "https://www.youtube.com/@SuperKitowiec2/videos"
    ]
    for channel in channels:
        get_channel_screenshots(
            channel_url=channel, 
            output_dir=SCREENSHOT_DIR,
            game_tag=""  
        )
    print("\nScript finished.")