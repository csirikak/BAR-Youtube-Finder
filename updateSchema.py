import sqlite3

DB_NAME = 'data/game_battles.db'


def ensure_twitch_schema(conn):
    """Create Twitch-only tables without touching the YouTube tables."""
    conn.execute('''
    CREATE TABLE IF NOT EXISTS twitch_videos (
        video_id TEXT PRIMARY KEY,
        upload_date TEXT,
        title TEXT,
        uploader TEXT,
        thumbnail TEXT
    )
    ''')
    conn.execute('''
    CREATE TABLE IF NOT EXISTS twitch_battle_videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        battle_id TEXT,
        video_id TEXT,
        video_timestamp_sec INTEGER,
        match_score REAL,
        ocr_player_count INTEGER,
        battle_player_count INTEGER,
        FOREIGN KEY (battle_id) REFERENCES battles (battle_id),
        FOREIGN KEY (video_id) REFERENCES twitch_videos (video_id),
        UNIQUE(battle_id, video_id, video_timestamp_sec)
    )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS twitch_battle_videos_battle '
                 'ON twitch_battle_videos(battle_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS twitch_battle_videos_video '
                 'ON twitch_battle_videos(video_id)')


def add_new_tables(db_name):
    """
    Adds the YouTube and Twitch video tables to the database.
    """
    conn = sqlite3.connect(db_name, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    cursor = conn.cursor()

    # --- YouTube videos ---
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS videos (
        video_id TEXT PRIMARY KEY,
        upload_date TEXT,
        title TEXT,
        uploader TEXT
    )
    ''')
    print("Created 'videos' table (if not exists).")

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

    # --- Twitch VODs (kept separate from YouTube) ---
    ensure_twitch_schema(cursor)
    print("Created Twitch video tables (if not exists).")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    print("Updating database schema...")
    add_new_tables(DB_NAME)
    print("Schema update complete.")
