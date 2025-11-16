import sqlite3
import json
from collections import defaultdict

# --- CONFIGURATION ---
DB_NAME = 'data/game_battles.db'
MATCHES_JSON = 'data/matches_output.json'
FRONTEND_DATA_OUTPUT = 'frontend_files/frontend_data.json'

# --- END CONFIGURATION ---

def export_data():
    """
    Exports all necessary data from the DB and JSON files
    into a single, client-consumable JSON file.
    """
    print(f"Connecting to database: {DB_NAME}")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. Build Player -> Battle ID Index
    print("Building player index (player_name -> battle_ids)...")
    print("  -> (Filtering to only include battles with video matches)")
    player_index = defaultdict(list)
    
    # --- UPDATED QUERY ---
    # This query joins participants with the battle_videos table.
    # The INNER JOIN ensures that only (player, battle) pairs
    # are selected where the battle_id *also* exists in battle_videos.
    # The GROUP BY ensures each battle_id is listed only once per player.
    filtered_query = """
        SELECT
            p.player_name,
            p.battle_id
        FROM
            battle_participants p
        INNER JOIN
            battle_videos bv ON p.battle_id = bv.battle_id
        WHERE
            p.player_name IS NOT NULL
        GROUP BY
            p.player_name, p.battle_id
    """
    cursor.execute(filtered_query)
    
    for row in cursor.fetchall():
        player_name, battle_id = row
        # No 'if' needed, query already filtered
        player_index[player_name].append(battle_id)
            
    # 2. Build Battle -> Video Match Index
    print("Building battle match index (battle_id -> video_matches)...")
    battle_matches = defaultdict(list)
    
    # This query is unchanged
    query = """
        SELECT
            bv.battle_id,
            bv.video_id,
            MIN(bv.video_timestamp_sec) as timestamp,
            v.title,
            v.upload_date,
            v.uploader
        FROM battle_videos bv
        JOIN videos v ON bv.video_id = v.video_id
        GROUP BY bv.battle_id, bv.video_id
    """
    cursor.execute(query)
    for row in cursor.fetchall():
        battle_id, video_id, timestamp, title, upload_date, uploader = row
        battle_matches[battle_id].append({
            "video_id": video_id,
            "timestamp": timestamp,
            "title": title or "Unknown Title",
            "upload_date": upload_date or "N/A",
            "uploader": uploader or "N/A"
        })

    # 3. Build Map Index
    print("Building map index (map_name -> video_matches)...")
    map_index = defaultdict(list)
    
    # This query joins battles (for map_name) with battle_videos and videos
    # It finds all videos that contain a battle on a specific map.
    map_query = """
        SELECT
            b.map_name,
            bv.video_id,
            MIN(bv.video_timestamp_sec) as timestamp,
            v.title,
            v.upload_date,
            v.uploader,
            bv.battle_id
        FROM battles b
        JOIN battle_videos bv ON b.battle_id = bv.battle_id
        JOIN videos v ON bv.video_id = v.video_id
        WHERE
            b.map_name IS NOT NULL
        GROUP BY b.map_name, bv.video_id, bv.battle_id
        ORDER BY b.map_name, v.upload_date DESC
    """
    cursor.execute(map_query)
    
    unique_map_names = set()
    
    for row in cursor.fetchall():
        map_name, video_id, timestamp, title, upload_date, uploader, battle_id = row
        
        unique_map_names.add(map_name)
        
        map_index[map_name].append({
            "video_id": video_id,
            "timestamp": timestamp,
            "title": title or "Unknown Title",
            "upload_date": upload_date or "N/A",
            "uploader": uploader or "N/A",
            "battle_id": battle_id
        })
        
    conn.close()
    print("Database processing complete.")

    # 4. Build OCR Search Index
    print(f"Loading {MATCHES_JSON} to build OCR index...")
    ocr_index = []
    last_battle = 0
    try:
        with open(MATCHES_JSON, 'r') as f:
            matches_data = json.load(f)
            
        for video_id, video_info in matches_data.items():
            video_title = video_info.get("title", "Unknown Title")
            upload_date = video_info.get("upload_date", "")
            if upload_date != "" and int(upload_date) > last_battle:
                last_battle = int(upload_date)
            uploader = video_info.get("uploader", "N/A")
            for timestamp, data in video_info.get("screenshots", {}).items():
                ocr_players = data.get("players_ocr", [])
                for player_name in ocr_players:
                    if player_name:
                        ocr_index.append({
                            "ocr_name": player_name,
                            "video_id": video_id,
                            "timestamp": int(timestamp),
                            "title": video_title,
                            "upload_date": upload_date,
                            "uploader": uploader
                        })
                        
    except Exception as e:
        print(f"Error processing {MATCHES_JSON}: {e}")

    # 5. Compile and save all data
    print(f"Saving compiled data to {FRONTEND_DATA_OUTPUT}...")
    
    all_player_names = list(player_index.keys())
    all_map_names = sorted(list(unique_map_names))
    
    frontend_data = {
        "player_index": player_index,       # For Search 1 (data)
        "all_player_names": all_player_names, # For Search 1 (autocomplete)
        "battle_matches": battle_matches,   # For Search 1 (data)
        "ocr_index": ocr_index,             # For Search 2
        "map_index": map_index,             # For Search 3 (data)
        "all_map_names": all_map_names,     # For Search 3 (autocomplete)
        "last_battle": last_battle
    }
    
    with open(FRONTEND_DATA_OUTPUT, 'w') as f:
        json.dump(frontend_data, f)
        
    print("--- Export Complete ---")
    print(f"Total players indexed: {len(player_index)}")
    print(f"Total battles with video matches: {len(battle_matches)}")
    print(f"Total OCR names indexed: {len(ocr_index)}")
    print(f"Total unique maps indexed: {len(all_map_names)}")

if __name__ == "__main__":
    export_data()