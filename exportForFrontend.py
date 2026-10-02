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


def build_catalog(conn):
    rows = list(conn.execute("""
        SELECT bv.battle_id, b.map_name, b.timestamp, bv.video_id,
               MIN(bv.video_timestamp_sec), v.title, v.upload_date, v.uploader
        FROM battle_videos bv
        JOIN videos v ON v.video_id = bv.video_id
        JOIN battles b ON b.battle_id = bv.battle_id
        GROUP BY bv.battle_id, bv.video_id
        ORDER BY bv.battle_id, bv.video_id
    """))
    maps = sorted({row[1] for row in rows if row[1]})
    uploaders = sorted({row[7] or "Unknown channel" for row in rows})
    map_ids = {name: i for i, name in enumerate(maps)}
    uploader_ids = {name: i for i, name in enumerate(uploaders)}
    videos, video_ids, battles, battle_ids = [], {}, [], {}
    for bid, map_name, date, vid, timestamp, title, uploaded, uploader in rows:
        if vid not in video_ids:
            video_ids[vid] = len(videos)
            videos.append([vid, title or "Untitled video",
                           uploader_ids[uploader or "Unknown channel"], uploaded or ""])
        if bid not in battle_ids:
            battle_ids[bid] = len(battles)
            battles.append([bid, map_ids.get(map_name, -1), (date or "")[:10], []])
        battles[battle_ids[bid]][3].append([video_ids[vid], timestamp])
    players = defaultdict(list)
    for name, bid in conn.execute("""
        SELECT p.player_name, p.battle_id FROM battle_participants p
        JOIN (SELECT DISTINCT battle_id FROM battle_videos) bv ON bv.battle_id = p.battle_id
        WHERE p.player_name IS NOT NULL AND p.player_name != ''
        ORDER BY p.player_name, p.battle_id
    """):
        if bid in battle_ids:
            players[name].append(battle_ids[bid])
    return {"version": 2, "players": dict(players), "battles": battles,
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
    manifest = {"version": 2, "catalog": filename, "bytes": len(payload), "stats": stats,
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
