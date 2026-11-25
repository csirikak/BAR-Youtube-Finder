"""Export a compact, content-addressed catalog for the static site."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from json_files import write_json

DB_NAME = "data/game_battles.db"
MATCHES_JSON = "data/matches_output.json"  # Legacy path; export now reads SQLite directly.
FRONTEND_DATA_OUTPUT = "frontend_files/frontend_data.json"
CATALOG_VERSION = 4
TWITCH_PREFIX = "twitch_"


def _account_rows(conn):
    try:
        return conn.execute("""
            SELECT player_name, user_id, current_name
            FROM player_accounts ORDER BY player_name
        """).fetchall()
    except sqlite3.OperationalError as error:
        # Databases built before the rename resolver have no account table yet.
        if "no such table" in str(error):
            return []
        raise


def _player_rows(conn):
    query = """
        SELECT p.player_name, p.battle_id FROM battle_participants p
        JOIN (SELECT battle_id FROM battle_videos
              UNION SELECT battle_id FROM twitch_battle_videos) linked
          ON linked.battle_id = p.battle_id
        WHERE p.player_name IS NOT NULL AND p.player_name != ''
        ORDER BY p.player_name, p.battle_id
    """
    try:
        return conn.execute(query)
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        return conn.execute("""
            SELECT p.player_name, p.battle_id FROM battle_participants p
            JOIN (SELECT battle_id FROM battle_videos) linked
              ON linked.battle_id = p.battle_id
            WHERE p.player_name IS NOT NULL AND p.player_name != ''
            ORDER BY p.player_name, p.battle_id
        """)


def _video_rows(conn):
    """Return normalized YouTube and Twitch links in one stable ordering."""
    rows = list(conn.execute("""
        SELECT bv.battle_id, b.map_name, b.timestamp, bv.video_id,
               MIN(bv.video_timestamp_sec), v.title, v.upload_date, v.uploader,
               'youtube', NULL
        FROM battle_videos bv
        JOIN videos v ON v.video_id = bv.video_id
        JOIN battles b ON b.battle_id = bv.battle_id
        GROUP BY bv.battle_id, bv.video_id
    """))
    try:
        rows.extend(conn.execute("""
            SELECT tv.battle_id, b.map_name, b.timestamp, tv.video_id,
                   MIN(tv.video_timestamp_sec), v.title, v.upload_date, v.uploader,
                   'twitch', v.thumbnail
            FROM twitch_battle_videos tv
            JOIN twitch_videos v ON v.video_id = tv.video_id
            JOIN battles b ON b.battle_id = tv.battle_id
            GROUP BY tv.battle_id, tv.video_id
        """))
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
    rows.sort(key=lambda row: (row[0], row[3]))
    return rows


def build_catalog(conn):
    rows = _video_rows(conn)
    maps = sorted({row[1] for row in rows if row[1]})
    uploaders = sorted({row[7] or "Unknown channel" for row in rows})
    map_ids = {name: i for i, name in enumerate(maps)}
    uploader_ids = {name: i for i, name in enumerate(uploaders)}
    videos, video_ids, battles, battle_ids = [], {}, [], {}
    for (bid, map_name, date, vid, timestamp, title, uploaded, uploader,
         platform, thumbnail) in rows:
        if vid not in video_ids:
            video_ids[vid] = len(videos)
            public_id = (vid[len(TWITCH_PREFIX):]
                         if platform == "twitch" and vid.startswith(TWITCH_PREFIX)
                         else vid)
            videos.append([public_id, title or "Untitled video",
                           uploader_ids[uploader or "Unknown channel"],
                           uploaded or "", platform, thumbnail or ""])
        if bid not in battle_ids:
            battle_ids[bid] = len(battles)
            battles.append([bid, map_ids.get(map_name, -1), (date or "")[:10], []])
        battles[battle_ids[bid]][3].append([video_ids[vid], timestamp])
    players = defaultdict(list)
    for name, bid in _player_rows(conn):
        if bid in battle_ids:
            players[name].append(battle_ids[bid])
    aliases, accounts = {}, {}
    account_rows = defaultdict(list)
    for name, user_id, current_name in _account_rows(conn):
        if name in players:
            account_rows[user_id].append((name, current_name))
    for user_id, entries in account_rows.items():
        names = [name for name, _ in entries]
        current_name = next((current for _, current in entries if current), None)
        # A single unchanged spelling needs no alias indirection.
        if len(names) == 1 and names[0] == current_name:
            continue
        key = str(user_id)
        aliases.update({name: key for name in names})
        accounts[key] = current_name or names[0]
    return {"version": CATALOG_VERSION, "players": dict(players),
            "aliases": aliases, "accounts": accounts, "battles": battles,
            "videos": videos, "maps": maps, "uploaders": uploaders}


def export_data(*, db_name=DB_NAME, output_file=FRONTEND_DATA_OUTPUT):
    output = Path(output_file)
    with closing(sqlite3.connect(Path(db_name).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")  # All exported indexes describe the same snapshot.
        catalog = build_catalog(conn)
    payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode()
    filename = f"catalog.{sha256(payload).hexdigest()[:16]}.json"
    target = output.parent / filename
    if not target.exists():
        write_json(target, catalog)
    stats = {"players": len(catalog["players"]), "battles": len(catalog["battles"]),
             "videos": len(catalog["videos"]), "maps": len(catalog["maps"])}
    previous_catalog = None
    if output.exists():
        try:
            previous_catalog = json.loads(output.read_text(encoding="utf-8")).get("catalog")
        except (ValueError, AttributeError):
            pass
    manifest = {"version": CATALOG_VERSION, "catalog": filename, "bytes": len(payload),
                "stats": stats,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # Publish the manifest last. Keep the previous catalog for already-open tabs.
    write_json(output, manifest)
    for old in output.parent.glob("catalog.*.json"):
        if old.name not in {filename, previous_catalog}:
            old.unlink()
    print(f"Frontend export: {stats}; {len(payload):,} bytes in {filename}")
    return True


if __name__ == "__main__":
    export_data()
