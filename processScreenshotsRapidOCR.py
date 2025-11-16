import cv2
from pathlib import Path
from rapidocr import RapidOCR
import re
import os
import json  # Added for JSON output
from ultralytics import YOLO
import torch
import multiprocessing as mp

DEBUG_MODE = False
# YOLO model path for UI panel detection (pretrained)
MODEL_PATH = "ui_detector.pt"  # change if your trained model is elsewhere
# YOLO confidence threshold
YOLO_CONF_THRESHOLD = 0.80
NUM_THREADS = 4

SCREENSHOTS_DIR = "data/AllBarScreenshots"
# Define the JSON output file
JSON_OUTPUT_FILE = "data/screenshot_data.json"


def strip_clan_tag(text):
    """
    Removes clan tags from usernames.
    Handles formats like: [Crd]username, (clan)username, {clan}username
    Returns: (stripped_username, clan_tag or None)
    """
    if not text:
        return text, None
    text = text.strip('*').strip()
    # Match clan tags at start of string like [tag]name, (tag)name, {tag}name
    pattern = r'^[\[\(\{]([^\]\)\}]+)[\]\)\}]\s*(.+)$'
    match = re.match(pattern, text)
    if match:
        clan_tag = match.group(1)
        username = match.group(2)
        return username.strip(), f"[{clan_tag}]"
    return text.strip(), None

def show_debug_image(image, title, box_coords=None):
    """
    Helper function to show a scaled-down debug image.
    """
    try:
        debug_img = image.copy()
        if box_coords:
            (startX, startY, W, H) = box_coords
            cv2.rectangle(debug_img, (startX, startY), (W - 1, H - 1), (0, 0, 255), 2)
        (img_H, img_W) = debug_img.shape[:2]
        scale = 800 / img_W
        if img_H * scale > 1000:
            scale = 900 / img_H
        display_scale = min(1.0, scale)
        if display_scale < 0.99:
            debug_img_resized = cv2.resize(debug_img, None, fx=display_scale, fy=display_scale, interpolation=cv2.INTER_LANCZOS4)
        else:
            debug_img_resized = debug_img
        cv2.imshow(title, debug_img_resized)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    except cv2.error as e:
        if "cvShowImage" in str(e):
            print("\n--- ERROR: OpenCV is headless. Cannot show debug window. ---")
        else:
            print(f"An error occurred in show_debug_image: {e}")
    except Exception as e:
        print(f"An error occurred in show_debug_image: {e}")

def find_ui_panel(screenshot, yolo_model):
    """
    Finds the right-side UI panel using YOLO.
    Returns: (startX, startY, endX, endY) or None if no detection with conf > 0.92
    """
    (H, W) = screenshot.shape[:2]
    # Run YOLO
    try:
        results = yolo_model(screenshot, verbose=False)
        if results and len(results) > 0:
            boxes = results[0].boxes
            if boxes is not None and len(boxes) > 0:
                # Pick the box with highest confidence
                best = None
                best_conf = -1.0
                for b in boxes:
                    conf = float(b.conf[0]) if hasattr(b, "conf") else 0.0
                    if conf > best_conf:
                        best_conf = conf
                        best = b
                if best is not None and best_conf > YOLO_CONF_THRESHOLD:
                    xyxy = best.xyxy[0].tolist()
                    x1, y1, x2, y2 = map(int, xyxy)
                    # Sanity check
                    x1 = max(0, min(x1, W - 1))
                    x2 = max(0, min(x2, W - 1))
                    y1 = max(0, min(y1, H - 1))
                    y2 = max(0, min(y2, H - 1))
                    # If the detected box is reasonably on the right side, accept
                    if x1 > W * 0.5:
                        print(f"UI Panel detected by YOLO at ({x1},{y1},{x2},{y2}) conf={best_conf:.3f}")
                        return (x1, y1, x2, y2)
                    else:
                        print("YOLO detected a box but it's not on the expected right-side region.")
                else:
                    print(f"No YOLO detection with confidence > {YOLO_CONF_THRESHOLD}.")
    except Exception as e:
        print(f"Warning: YOLO inference failed: {e}")
    return None

def is_gamertag_candidate(text):
    """
    Heuristic filter to decide whether OCR text is likely a gamertag.
    Rules:
      - Not empty, not purely numeric
      - Contains at least one letter
      - Length between 3 and 24 characters (after stripping weird punctuation)
      - Not in the stoplist (UI words)
    """
    if not text:
        return False
    raw = text.strip()
    # Reject obvious UI labels or short tokens
    stoplist = {
        'enemies', 'spectators', 'units', 'total', 'fps', 'units/', 'units:', 'total:', 'spectators:',
    }
    low = raw.lower()
    if low in stoplist:
        return False
    
    for element in stoplist:
        if element in low:
            return False
    # Clean to allow letters, digits, _ - [ ] ( ) and remove trailing punctuation
    cleaned = re.sub(r'[^A-Za-z0-9_\-\[\]\(\)]', '', raw)
    
    if len(cleaned) < 3 or len(cleaned) > 24:
        return False
    # Avoid things that look numeric (like "59" or "99")
    if cleaned.isdigit():
        return False
    if cleaned[:-1].isdigit():
        return False
    return True

def ocr_bottom_right_element(screenshot_path: str, debug=False, reader=None, yolo_model=None):
    """
    Finds the UI element using YOLO, runs PP on the ROI,
    filters OCR outputs to probable gamertags, then fuzzy matches results against the username database.
    
    MODIFIED: This version expects 'reader' to be a RapidOCR engine instance.
    """
    screenshot = cv2.imread(str(screenshot_path))
    if screenshot is None:
        raise FileNotFoundError(f"Screenshot not found or is corrupt at {screenshot_path}")
    (H, W) = screenshot.shape[:2]

    # --- Find UI Panel using YOLO ---
    ui_bounds = find_ui_panel(screenshot, yolo_model)
    if ui_bounds is None:
        print(f"Could not find UI panel in {screenshot_path}. Skipping.")
        if debug:
            show_debug_image(screenshot, f"Skipped (No UI): {Path(screenshot_path).name}")
        return None
    
    (startX, startY, endX, endY) = ui_bounds
    
    # Ensure valid ROI
    if startX >= endX or startY >= endY:
        print("Invalid UI bounds detected, skipping.")
        return None
    
    roi_color = screenshot[startY:endY, startX:endX]
    
    if roi_color.shape[0] < 30 or roi_color.shape[1] < 30:
        print("ROI too small, skipping.")
        return None

    # --- Run OCR on ROI (Modified for RapidOCR) ---
    roi_for_ocr = roi_color.copy()
    print(f"Running OCR on ROI of size {roi_for_ocr.shape}...")
    try:
        # --- NEW FIX ---
        # 1. Call the reader, which returns a single RapidOCROutput object
        ocr_result_object = reader(roi_for_ocr) 

        # 2. Check if the object is valid and has detected text
        if ocr_result_object and ocr_result_object.txts:
            
            # 3. Zip the parallel lists (boxes, txts, scores) into the 
            #    (bbox, text, prob) tuple format that the rest of the 
            #    script expects.
            results = list(zip(
                ocr_result_object.boxes, 
                ocr_result_object.txts, 
                ocr_result_object.scores
            ))
        else:
            # No text found, set results to an empty list
            results = []
        # --- End FIX ---
        
    except Exception as e:
        print(f"OCR failure: {e}")
        results = []

    if not results:
        print(f"No text found in {screenshot_path}. Skipping.")
        if debug:
            show_debug_image(screenshot, f"Skipped (No Text): {Path(screenshot_path).name}", box_coords=(startX, startY, endX, endY))
        return None
    
    print(f"Found {len(results)} text elements via OCR")
    
    # --- Filter OCR outputs for gamertag candidates ---
    # (This section will now work correctly)
    candidates = []
    for (bbox, text, prob) in results:
        # Normalize text
        t = text.strip()
        
        # Skip very low-confidence OCR hits
        if prob is not None and prob < 0.35:
            continue
            
        if is_gamertag_candidate(t):
            # Keep bounding boxes for possible debug visualization
            candidates.append((bbox, t, prob))

    print(f"After gamertag filtering: {len(candidates)} candidates")
    
    if not candidates:
        if debug:
            show_debug_image(roi_color, "No Gamertag Candidates", box_coords=(startX, startY, endX, endY))
        return None

    # Sort candidates top-to-bottom, left-to-right (by y then x)
    # (No changes needed here, bbox structure is compatible)
    def bbox_top_left(bbox):
        (tl, tr, br, bl) = bbox
        return (int(tl[1]), int(tl[0]))
    
    candidates.sort(key=lambda x: bbox_top_left(x[0]))

    if debug:
        for (bbox, dirty_text, prob) in candidates:
            (top_left, top_right, bot_right, bot_left) = bbox
            
            # These are already correct
            top_left = (int(top_left[0]), int(top_left[1]))
            bot_right = (int(bot_right[0]), int(bot_right[1]))          
            
            cv2.rectangle(roi_for_ocr, top_left, bot_right, (128, 128, 128), 1)
            
            # --- FIX IS HERE ---
            # Cast the 'top_right' coordinates to int for the text origin
            text_origin = (int(top_right[0]) + 8, int(top_right[1]))
            
            # Also, ensure text is ASCII-safe for putText, which can't
            # handle special characters (like the Chinese chars we saw).
            safe_text = dirty_text.encode('ascii', 'ignore').decode('ascii')

            cv2.putText(roi_for_ocr, safe_text, text_origin,
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 100), 1)
    final_text = []
    for candidate in candidates:
        final_text.append(re.sub(r'^[0-9\s\u4e00-\u9fff]*', '', candidate[1]))
    
    print(f"--- Found Gamertags in {screenshot_path} ---")
    print(final_text if final_text else "(No gamertags detected)")
    print("--------------------------------------------------\n")

    if debug:
        # (No changes needed here)
        try:
            display_scale = 2.0
            full_debug = screenshot.copy()
            cv2.rectangle(full_debug, (startX, startY), (endX-1, endY-1), (0, 255, 255), 3)
            show_debug_image(full_debug, f"Full Screenshot - UI Box: {Path(screenshot_path).name}")
            
            debug_img_resized = cv2.resize(roi_for_ocr, None, fx=display_scale, fy=display_scale, interpolation=cv2.INTER_LANCZOS4)
            cv2.imshow("OCR Gamertag Results (Green=Matched, Yellow=Unmatched candidate)", debug_img_resized)
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        except cv2.error as e:
            if "cvShowImage" in str(e):
                print("\n--- ERROR: OpenCV is headless. Cannot show debug window. ---")
            else:
                print(f"An error occurred: {e}")

    return final_text if final_text else None

def init_pool(initialData, lock):
    global yolo_model, reader, worker_data, worker_lock
    worker_data = initialData
    worker_lock = lock
    reader = RapidOCR()
    if Path(MODEL_PATH).exists():
        try:
            print(f"Loading YOLO model from {MODEL_PATH} in process {os.getpid()}...")
            yolo_model = YOLO(MODEL_PATH)
            yolo_model.to('cuda')
            print(f"YOLO model loaded in process {os.getpid()}.")
        except Exception as e:
            print(f"Warning: Failed to load YOLO model '{MODEL_PATH}' in process {os.getpid()}: {e}")
            yolo_model = None
    else:
        print(f"YOLO model not found at {MODEL_PATH} in process {os.getpid()}. Cannot proceed without YOLO.")
        yolo_model = None

def process_file(path, DEBUG_MODE):
    global reader, yolo_model, worker_data, worker_lock
    try:
        # Parse path to get video_id and timestamp
        pattern = re.compile(r"^(.*)_(\d+)s\.png$")
        match = pattern.match(path.name)

        if match:
            # Extract the captured groups
            video_id = match.group(1)
            timestamp = match.group(2)

            print(f"File: {str(path)}")
            print(f"  > Video ID: {video_id}")
            print(f"  > Timestamp: {timestamp}")
        else:
            print(f"File: {str(path)} (No match)")
        
        screenshots = (worker_data.get(video_id, {})).get('screenshots', {})
        if timestamp in screenshots:
            print(f"Skipping {path.name}: Already processed.")
            return
        if yolo_model is None:
            print(f"Skipping {path}: YOLO model not loaded.")
            return
            
        results = ocr_bottom_right_element(str(path), debug=DEBUG_MODE, reader=reader, yolo_model=yolo_model)
        
        if results:
            # Use lock to safely read/write to the JSON
            with worker_lock:
                # Read existing data
                data = {}
                if Path(JSON_OUTPUT_FILE).exists():
                    try:
                        with open(JSON_OUTPUT_FILE, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                    except json.JSONDecodeError:
                        print(f"Warning: {JSON_OUTPUT_FILE} is corrupt, starting fresh.")
                        data = {}
                
                # Update data structure
                if video_id not in data:
                    data[video_id] = {}
                if "screenshots" not in data[video_id]:
                    data[video_id]["screenshots"] = {} # Ensure screenshots dict exists
                    
                # Add the new OCR result
                data[video_id]["screenshots"][timestamp] = results
                
                # Write the updated data back to the file
                try:
                    with open(JSON_OUTPUT_FILE, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=4, sort_keys=True)
                except Exception as e:
                    print(f"CRITICAL: Failed to write to {JSON_OUTPUT_FILE}: {e}")

    except FileNotFoundError as e:
        print(e)
    except Exception as e:
        print(f"An error occurred on file {path}: {e}")

def initialize_json(filepath):
    """
    Initializes the JSON file with base data if it doesn't exist,
    and ensures all base entries have a 'screenshots' key.
    """
    data = {}
    if Path(filepath).exists():
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: {filepath} was corrupt. Initializing from base data.")
            data = {}
    
    # Write the (potentially) updated data back
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, sort_keys=True)
    except Exception as e:
        print(f"FATAL: Could not initialize JSON file {filepath}: {e}")
        raise # Stop execution if we can't write to our output file

    return data

# --- Example Usage (Main Execution Block) ---
def main():
    # Set DEBUG mode here (True to show windows, False for production)
    
    # Initialize the JSON database file
    initialData = None
    try:
        initialData = initialize_json(JSON_OUTPUT_FILE)
    except Exception as e:
        print(f"Failed to start due to JSON initialization error: {e}")
        return

    # Find all screenshots
    try:
        pathlist = Path(SCREENSHOTS_DIR).glob("**/*.png")
        files = list(pathlist)
        if not files:
            print(f"No .png files found in {SCREENSHOTS_DIR} directory.")
            return
        
        print(f"Found {len(files)} screenshots to process.")
        
        manager = mp.Manager()
        lock = manager.Lock()
        
        # Use NUM_THREADS, but not more than the number of files
        num_processes = min(NUM_THREADS, len(files))
        if num_processes <= 0:
             print("No files or threads to process.")
             return
        
        initargs_tuple = (initialData, lock)
        print(f"Starting processing pool with {num_processes} worker(s)...")
        with mp.Pool(processes=num_processes, initializer=init_pool, initargs=initargs_tuple) as pool:
            args_list = [(path, DEBUG_MODE) for path in files]
            pool.starmap(process_file, args_list)
            
        print("Processing complete.")
            
    except Exception as e:
        print(f"A fatal error occurred: {e}")

if __name__ == "__main__":
    mp.freeze_support() # Good practice for multiprocessing
    main()