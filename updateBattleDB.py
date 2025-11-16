import sqlite3
import requests
import time
import json
from datetime import datetime, timedelta

# --- CONFIGURATION: SET THESE VALUES ---

# The name of your local SQLite database file
DB_NAME = 'data/game_battles.db'

# The base URL of the game's API
API_BASE_URL = 'https://api.bar-rts.com/replays'

LIMIT_PER_PAGE = 1024

# How many seconds to wait between API calls to avoid rate-limiting
RATE_LIMIT_DELAY = 1  # 1 second


# Stop fetching battles older than this date (YYYY-MM-DDTHH:MM:SS.mmmZ)
# This is useful for the first sync to avoid fetching all of history.
# Set to None to fetch all history.
EARLIEST_BATTLE_TIMESTAMP = '2024-01-01T00:00:00.000Z' # Example: Stop at Jan 1, 2024

# --- END CONFIGURATION ---


def setup_database(db_name):
    """
    Creates the database and necessary tables if they don't exist.
    We use a schema adapted to the API's available data.

    NOTE: The API provides 'player_name' but no stable 'player_id'.
    We are forced to use 'player_name' as the primary key for players.
    """
    # Connect to the SQLite database
    # This will create the file if it doesn't exist
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    # --- Create battles table ---
    # Stores one row per battle
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS battles (
        battle_id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        map_name TEXT
    )
    ''')

    # --- Create players table ---
    # Stores one row per unique player name
    # We use player_name as the Primary Key as it's all the API provides
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS players (
        player_name TEXT PRIMARY KEY
    )
    ''')

    # --- Create battle_participants junction table ---
    # Links player names to battles.
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS battle_participants (
        battle_id TEXT,
        player_name TEXT,
        PRIMARY KEY (battle_id, player_name),
        FOREIGN KEY (battle_id) REFERENCES battles (battle_id),
        FOREIGN KEY (player_name) REFERENCES players (player_name)
    )
    ''')

    print(f"Database '{db_name}' initialized and tables ensured.")
    conn.commit()
    return conn


def get_last_sync_timestamp(conn):
    """
    Finds the most recent battle timestamp (startTime) in our database.
    We will only ask the API for battles *after* this time.
    """
    cursor = conn.cursor()
    # The API returns newest first, so we find the MAX (most recent) timestamp
    cursor.execute("SELECT MAX(timestamp) FROM battles")
    result = cursor.fetchone()

    if result and result[0]:
        print(f"Last sync found. Newest battle is from: {result[0]}")
        return result[0]
    else:
        print("No previous sync data found. Will perform a full sync.")
        return None


def fetch_battles_from_api(since_timestamp=None):
    """
    Fetches battle data from the API.
    This is a generator, yielding battles one by one to save memory.
    It handles pagination automatically.
    
    It fetches pages until it finds a battle timestamp that is
    older than or equal to 'since_timestamp'.
    """
    
    # Start with parameters.
    # The API uses 'page' and 'limit'. 'page' starts at 1.
    params = {
        'page': 1,
        'limit': LIMIT_PER_PAGE,
        'endedNormally': 'true'
    }
    
    # This is our pagination loop
    while True:
        print(f"Requesting: {API_BASE_URL} with params: {params}")
        try:
            response = requests.get(API_BASE_URL, params=params)
            # Raise an exception for bad status codes (4xx, 5xx)
            response.raise_for_status() 
            data = response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching from API: {e}")
            break
        # --- END REAL API CALL ---

        # --- PARSE THE RESPONSE ---
        # The battle data is an object with numeric keys: {"0": {...}, "1": {...}}
        battles_dict = data.get('data', {})
        
        # Stop pagination if the 'data' object is empty
        if not battles_dict:
            print("No more battles found on this page. Fetch complete.")
            break

        # Yield each battle individually
        # We sort by the keys as strings to process them in order, just in case
        for battle in battles_dict:
            
            try:
                battle_timestamp = battle['startTime']
                
                # --- SYNC LOGIC 1: NEW BATTLES ---
                # If we have a 'since_timestamp' (from a previous run) and this battle
                # is older or the same, we've seen it. Stop the entire fetch process.
                if since_timestamp and battle_timestamp and battle_timestamp <= since_timestamp:
                    print(f"Encountered battle {battle['id']} from {battle_timestamp}, "
                          "which is at or before our last sync. Stopping fetch.")
                    return  # This stops the generator
                
                # --- SYNC LOGIC 2: HISTORICAL LIMIT ---
                # If we have a 'EARLIEST_BATTLE_TIMESTAMP' (for first sync) and this
                # battle is older than that, stop the fetch.
                if EARLIEST_BATTLE_TIMESTAMP and battle_timestamp and battle_timestamp < EARLIEST_BATTLE_TIMESTAMP:
                    print(f"Encountered battle {battle['id']} from {battle_timestamp}, "
                          f"which is before the configured earliest date ({EARLIEST_BATTLE_TIMESTAMP}).")
                    print("Stopping historical sync.")
                    return # This stops the generator
                
                # If it's a new battle and within our desired date range, yield it
                yield battle

            except KeyError as e:
                print(f"Skipping battle due to missing key: {e}. Data: {battle}")
                continue


        # --- PAGINATION ---
        # Go to the next page
        params['page'] += 1
        
        # Be a good citizen and don't spam the API
        time.sleep(RATE_LIMIT_DELAY)


def process_and_insert_data(conn, battle_generator):
    """
    Takes battles from the generator and inserts them into the
    database in a single transaction.
    """
    cursor = conn.cursor()
    battles_processed = 0
    players_updated = 0
    links_created = 0

    try:
        # Start a transaction for much faster inserts
        cursor.execute("BEGIN TRANSACTION")

        for battle in battle_generator:
            
            # --- Extract data from the battle object ---
            try:
                battle_id = battle['id']
                timestamp = battle['startTime']
                # Use .get() for safe nested dictionary access
                map_name = battle.get('Map', {}).get('scriptName')
                ally_teams = battle.get('AllyTeams', [])
                
                if not ally_teams:
                    print(f"Skipping battle {battle_id} as it has no AllyTeams data.")
                    continue
                    
            except KeyError as e:
                print(f"Skipping battle due to missing key: {e}. Data: {battle}")
                continue
            # --- END EXTRACTION ---

            # 1. Insert the battle
            # INSERT OR IGNORE skips if the PRIMARY KEY (battle_id) already exists
            cursor.execute('''
            INSERT OR IGNORE INTO battles (battle_id, timestamp, map_name)
            VALUES (?, ?, ?)
            ''', (battle_id, timestamp, map_name))
            
            if cursor.rowcount > 0:
                battles_processed += 1

            # 2. Loop through participants to update players and links
            for team in ally_teams:
                for player in team.get('Players', []):
                    try:
                        player_name = player['name']
                        if not player_name:
                            continue # Skip if name is empty
                    except KeyError:
                        # Skip player if 'name' key doesn't exist
                        continue

                    # 2a. Insert the player into the 'players' table
                    # INSERT OR IGNORE skips if player_name (Primary Key) already exists
                    cursor.execute('''
                    INSERT OR IGNORE INTO players (player_name)
                    VALUES (?)
                    ''', (player_name,))
                    
                    if cursor.rowcount > 0:
                        players_updated += 1 # Counts new players

                    # 2b. Link the player to the battle in the junction table
                    # INSERT OR IGNORE skips if the link (battle_id, player_name) already exists
                    cursor.execute('''
                    INSERT OR IGNORE INTO battle_participants (battle_id, player_name)
                    VALUES (?, ?)
                    ''', (battle_id, player_name))

                    if cursor.rowcount > 0:
                        links_created += 1

        # Commit the transaction
        conn.commit()
        print("\n--- Sync Complete ---")
        print(f"New battles added: {battles_processed}")
        print(f"New unique players found: {players_updated}")
        print(f"New player-battle links created: {links_created}")
        if battles_processed == 0:
            print("Database is already up-to-date.")

    except Exception as e:
        print(f"An error occurred during database insertion: {e}")
        print("Rolling back transaction...")
        conn.rollback()
    

def main():
    """Main function to run the update process."""
    print("--- Starting Database Update Script ---")
    
    # 1. Connect to DB and create tables if they don't exist
    conn = setup_database(DB_NAME)
    
    # 2. Find out where we left off
    last_sync = get_last_sync_timestamp(conn)
    
    # 3. Get a generator for new battles from the API
    battle_generator = fetch_battles_from_api(last_sync)
    
    # 4. Process all battles from the generator and insert into DB
    process_and_insert_data(conn, battle_generator)
    
    # 5. Close the database connection
    conn.close()
    print("--- Database Update Script Finished ---")


if __name__ == "__main__":
    main()