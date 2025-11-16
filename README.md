# BAR Video Battle Finder

This repository contains a complete data engineering pipeline designed to find and link "Beyond All Reason" (BAR) gameplay moments from YouTube videos to their specific, corresponding battle replays.

It works by scraping YouTube channels for BAR content, using computer vision (YOLO) to find player lists in screenshots, performing OCR to extract player names, and then fuzzy-matching those names against a comprehensive SQLite database of all official BAR replays.

The final result is a searchable web frontend that allows you to find videos of a specific player or fuzzy-search for a player name and get a direct link to the YouTube video and timestamp where they appeared.

## 🚀 Features

  * **YouTube Scraper**: Downloads video metadata and screenshots from specified channels using `yt-dlp`.
  * **Replay Database Ingestion**: Pulls all battle replay metadata from the official `api.bar-rts.com` into a local SQLite database.
  * **Custom CV Model**: Includes a complete tool (`bbox.py`) to label, train, and run a YOLO model to detect the in-game player UI panel.
  * **High-Performance OCR**: Uses `RapidOCR` to extract player names from the detected UI panels.
  * **Fuzzy-Matching Core**: Implements a parallelized, high-speed fuzzy matching algorithm (`rapidfuzz`) to link a list of OCR'd players from a screenshot to a specific battle ID.
  * **Web Frontend**: Provides a simple, fast, all-client-side search page (`index.html`) to query the final, linked data.

## ⚙️ How It Works: The Data Pipeline

This project is a multi-stage pipeline. The scripts must be run in a specific order to correctly build the dataset.

### One-Time Setup: Training the CV Model

Before you can process screenshots, you need to train the YOLO model to find the player list.

1.  **Collect Screenshots**: Manually gather a few hundred screenshots from BAR videos and place them in the `training_screenshots` folder.
2.  **Label Data**: Run `python bbox.py label`. This opens an OpenCV window. Click the top-left corner of the player UI panel. The bottom-right is assumed to be the edge of the screen. Press `SPACE` to save and advance.
3.  **Train Model**: Run `python bbox.py train`. This uses the labels you just created to train a YOLO model. It will save the best model as `ui_detector.pt`.

-----

### Main Pipeline: Running the Process

Once you have a trained `ui_detector.pt` model, you can run the main pipeline.

1.  **Update DB Schema**: (Run once)
    `python updateSchema.py`

      * This adds the `videos` and `battle_videos` tables to the database, which are needed to link replays to videos.

2.  **Populate Replay DB**:
    `python updateBattleDB.py`

      * This script connects to the `api.bar-rts.com` and downloads the metadata for *all* battles back to a particular date, including player names for each battle. It stores this in `game_battles.db`. This can be run periodically to fetch new replays.

3.  **Scrape YouTube Videos**:
    `python scrape.py`

      * This script reads the list of YouTube channels and uses `yt-dlp` to fetch video metadata (saving to `screenshot_data.json`).
      * It then uses `ffmpeg` to take screenshots at a set interval (e.g., every 12 minutes) and saves them to `AllBarScreenshots/`.

4.  **Run OCR on Screenshots**:
    `python processScreenshotsRapidOCR.py`

      * This script loads the `ui_detector.pt` model.
      * It scans all screenshots, finds the player UI panel, and runs `RapidOCR` to extract the list of player names.
      * It updates `screenshot_data.json` with the new OCR results, linking them to the video ID and timestamp.

5.  **Match OCR to Replays**:
    `python findScreenshotBattles.py`

      * This is the core logic. It loads the OCR'd player lists from `screenshot_data.json` and the complete battle history from `game_battles.db`.
      * It uses a parallelized process and `rapidfuzz`'s `token_set_ratio` to find the best `battle_id` that matches the list of players in each screenshot.
      * It saves these matches (e.g., "Video X at timestamp Y matches Battle ID Z") into the `battle_videos` table in `game_battles.db` and also creates `matches_output.json`.

6.  **Export for Frontend**:
    `python exportForFrontend.py`

      * This script reads the final, linked data from the database and `matches_output.json`.
      * It formats this data into a single, optimized JSON file: `frontend_files/frontend_data.json`.

7.  **View Results**:

      * Serve the repository folder with a simple HTTP server (e.g., `python -m http.server`) and open `index.html` in your browser. The page will load `frontend_data.json` and provide a search interface.

## 🛠️ Setup & Installation

1.  **Clone Repository**

    ```bash
    git clone https://github.com/your-username/bar-video-finder.git
    cd bar-video-finder
    ```

2.  **Create Python Environment**

    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install Python Dependencies**
    ```bash
    pip install yt-dlp curl_cffi requests python-dateutil
    pip install opencv-python numpy
    pip install ultralytics torch
    pip install rapidocr-onnxruntime rapidfuzz
    ```
    There may be more, pip install them if you get an error.

4.  **Install External Dependencies**

      * **FFmpeg**: You must have `ffmpeg` installed and available in your system's `PATH`. This is required by `scrape.py` for taking screenshots.
      * **yt-dlp**: This needs some special setup like node for the JS exection provider, or a PO server. Check the repo for more info.
      * **(Optional)** `exiftool` or `imagemagick`: The `delete.sh` script uses these.

## 🏃 Usage (Pipeline Order)

```bash
# --- ONE-TIME SETUP ---
# 1. Manually add screenshots, then label them
python bbox.py label

# 2. Train the YOLO model
python bbox.py train

# 3. Add the new tables to the database
python updateSchema.py

# --- REGULAR PIPELINE RUN ---
# 1. Update the battle replay database
python updateBattleDB.py

# 2. Scrape YouTube for new videos and screenshots
python scrape.py

# 3. Process new screenshots with OCR
python processScreenshotsRapidOCR.py

# 4. Run the matching logic
python findScreenshotBattles.py

# 5. Export the final data for the website
python exportForFrontend.py

# 6. Serve the frontend
python -m http.server 8000
# ...then open http://localhost:8000 in your browser
```

## 📂 Project Structure

```
.
├── data/
│   ├── AllBarScreenshots/       # (Generated) Directory for all .png screenshots
│   ├── game_battles.db          # (Generated) SQLite DB of all BAR replays
│   ├── matches_output.json      # (Generated) Intermediate JSON of video/battle matches
│   └── screenshot_data.json     # (Generated) JSON DB of video metadata and OCR results
├── frontend_files/
│   ├── app.js               # JavaScript for the frontend
│   ├── frontend_data.json   # (Generated) The final JSON for the UI
│   └── style.css            # CSS for the frontend
├── utils/
|   ├── delete.ps1               # (Utility) PowerShell script to delete small images
|   └── delete.sh                # (Utility) Bash script to delete small images
├── yolo_dataset/            # (Generated) Staging area for YOLO training
├── yolo_labels/             # (Generated) Labels from bbox.py
│
├── bbox.py                  # Tool for labeling, training, and inferring with YOLO
├── exportForFrontend.py     # Exports final data to frontend_files/frontend_data.json
├── findScreenshotBattles.py # **Core Logic**: Matches OCR results to the DB
├── index.html               # The web frontend UI
├── processScreenshotsRapidOCR.py # Runs YOLO + OCR on all screenshots
├── scrape.py                # Scrapes YouTube channels for videos & screenshots
├── updateBattleDB.py        # Populates game_battles.db from the BAR API
├── updateSchema.py          # Adds video/match tables to the DB
│
└── ui_detector.pt           # (Generated) The trained YOLO model
```