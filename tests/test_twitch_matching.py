from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import findScreenshotBattles as matching
import findTwitchBattles
from updateBattleDB import setup_database
from updateSchema import add_new_tables


PLAYERS = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank"]


class TwitchMatchingTests(unittest.TestCase):
    def test_matches_are_written_to_separate_twitch_tables(self):
        with tempfile.TemporaryDirectory() as directory, \
                redirect_stdout(io.StringIO()):
            root = Path(directory)
            db = root / "battles.db"
            setup_database(db).close()
            add_new_tables(db)
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("INSERT INTO battles VALUES (?, ?, ?)",
                             ("b1", "2026-09-01T12:00:00Z", "Map"))
                conn.executemany("INSERT INTO battle_participants VALUES (?, ?)",
                                 [("b1", player) for player in PLAYERS])
                conn.execute("INSERT INTO twitch_videos VALUES "
                             "('twitch_old','20260601','Old VOD','OldStreamer',NULL)")
                conn.execute("INSERT INTO twitch_battle_videos "
                             "(battle_id,video_id,video_timestamp_sec) "
                             "VALUES ('b1','twitch_old',90)")
                conn.commit()

            metadata = root / "twitch_data.json"
            metadata.write_text(json.dumps({
                "twitch_123": {
                    "title": "Twitch VOD",
                    "upload_date": "20260902",
                    "started_at": "2026-09-01T18:00:00Z",
                    "uploader": "Streamer",
                    "thumbnail": "https://thumb",
                    "screenshots": {"90": PLAYERS},
                },
                "twitch_live": {
                    "title": "Still live",
                    "upload_date": "20260902",
                    "uploader": "LiveStreamer",
                    "live_status": "is_live",
                    "screenshots": {"90": PLAYERS},
                },
                "twitch_expired": {
                    "title": "Expired VOD",
                    "upload_date": "20260601",
                    "uploader": "OldStreamer",
                    "screenshots": {"90": PLAYERS},
                },
            }))
            output = root / "matches.json"
            summary = findTwitchBattles.main(
                db_name=db, screenshot_file=metadata, output_file=output,
                max_workers=1, now=datetime(2026, 9, 3, tzinfo=timezone.utc))
            self.assertEqual(summary["videos"], 1)
            self.assertEqual(summary["matches"], 1)
            self.assertEqual(summary["purged"], 1)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT video_id, uploader FROM twitch_videos").fetchall(),
                    [("twitch_123", "Streamer")])
                self.assertEqual(conn.execute(
                    "SELECT battle_id, video_id, video_timestamp_sec "
                    "FROM twitch_battle_videos").fetchall(),
                    [("b1", "twitch_123", 90)])
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM battle_videos").fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM videos").fetchone()[0], 0)
            saved = json.loads(output.read_text())
            self.assertNotIn("twitch_live", saved)
            self.assertNotIn("twitch_expired", saved)
            self.assertEqual(saved["twitch_123"]["screenshots"]["90"]
                             ["matched_battle_id"], "b1")

    def test_screenshot_moment_limits_candidate_battles(self):
        players = set(PLAYERS)
        index = {name: ["far", "near"] for name in players}
        battle_data = {
            "far": {"timestamp": "2026-09-02T07:00:00Z", "players": players},
            "near": {"timestamp": "2026-09-01T12:00:00Z", "players": players},
        }
        with redirect_stdout(io.StringIO()):
            matching.init_worker(index, battle_data, match_window_hours=12)
            try:
                _, _, matches, screenshots = matching.process_video_task((
                    "twitch_123",
                    {"title": "VOD", "upload_date": "20260902",
                     "started_at": "2026-09-01T18:00:00Z",
                     "screenshots": {"90": PLAYERS}},
                ))
            finally:
                matching.init_worker({}, {}, None)
        self.assertEqual(matches[0][0], "near")
        self.assertEqual(screenshots["90"]["matched_battle_id"], "near")


if __name__ == "__main__":
    unittest.main()
