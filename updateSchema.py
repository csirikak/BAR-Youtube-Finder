import sqlite3

DB_NAME = 'data/game_battles.db'

def add_new_tables(db_name):
    """
    Adds the 'videos' and 'battle_videos' tables to the database
    to store the many-to-many relationship between battles and videos.
    """
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    # --- Create videos table ---
    # Stores one row per YouTube video
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS videos (
        video_id TEXT PRIMARY KEY,
        upload_date TEXT,
        title TEXT,
        uploader TEXT
    )
    ''')
    print("Created 'videos' table (if not exists).")

    # --- Create battle_videos junction table ---
    # Links a specific battle to a video at a specific time.
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS battle_videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        battle_id TEXT,
        video_id TEXT,
        video_timestamp_sec INTEGER,
        match_score REAL,
        ocr_player_count INTEGER,
        battle_player_count INTEGER,
        FOREIGN KEY (battle_id) REFERENCES battles (battle_id),
        FOREIGN KEY (video_id) REFERENCES videos (video_id),
        UNIQUE(battle_id, video_id, video_timestamp_sec)
    )
    ''')
    print("Created 'battle_videos' junction table (if not exists).")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    print("Updating database schema...")
    add_new_tables(DB_NAME)
    print("Schema update complete.")