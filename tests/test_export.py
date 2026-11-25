from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from exportForFrontend import export_data
from rename_resolver import ensure_schema as ensure_rename_schema
from updateBattleDB import setup_database
from updateSchema import add_new_tables


class CompactExportTests(unittest.TestCase):
    def test_compact_catalog_preserves_rosters_links_and_shared_video_metadata(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            db = root / "battles.db"
            setup_database(db).close()
            add_new_tables(db)
            with closing(sqlite3.connect(db)) as conn:
                ensure_rename_schema(conn)
                conn.executemany("INSERT INTO battles VALUES (?, '2026-09-01T12:00:00Z', ?)",
                                 [("b1", "Map one"), ("b2", None)])
                conn.executemany("INSERT INTO battle_participants VALUES (?, ?)",
                                 [("b1", "__proto__"), ("b1", "玩家"), ("b2", "玩家")])
                conn.execute("INSERT INTO player_accounts VALUES "
                             "('玩家', 42, 'NewName', 'gex', '2026-09-01T00:00:00Z')")
                conn.execute("INSERT INTO videos VALUES ('v1','20260902','A <title>','Channel')")
                conn.executemany("""INSERT INTO battle_videos
                    (battle_id,video_id,video_timestamp_sec) VALUES (?, 'v1', ?)""",
                                 [("b1", 810), ("b1", 90), ("b2", 1530)])
                conn.execute("INSERT INTO twitch_videos VALUES "
                             "('twitch_t1','20260903','Twitch <title>','TwitchChannel','https://thumb')")
                conn.execute("INSERT INTO twitch_battle_videos "
                             "(battle_id,video_id,video_timestamp_sec) "
                             "VALUES ('b2','twitch_t1',300)")
                conn.commit()
            output = root / "frontend_data.json"
            self.assertTrue(export_data(db_name=db, output_file=output))
            manifest = json.loads(output.read_text())
            self.assertEqual(manifest["version"], 4)
            catalog = json.loads((root / manifest["catalog"]).read_text())
            self.assertEqual(manifest["stats"], {"players": 2, "battles": 2, "videos": 2, "maps": 1})
            self.assertEqual(catalog["version"], 4)
            self.assertEqual(catalog["players"]["玩家"], [0, 1])
            self.assertEqual(catalog["players"]["__proto__"], [0])
            self.assertEqual(catalog["aliases"], {"玩家": "42"})
            self.assertEqual(catalog["accounts"], {"42": "NewName"})
            self.assertEqual(catalog["videos"], [
                ["v1", "A <title>", 0, "20260902", "youtube", ""],
                ["t1", "Twitch <title>", 1, "20260903", "twitch", "https://thumb"],
            ])
            self.assertEqual(catalog["battles"][0][3], [[0, 90]])
            self.assertEqual(catalog["battles"][1][1], -1)
            self.assertEqual(catalog["battles"][1][3], [[1, 300], [0, 1530]])
            self.assertEqual(manifest["bytes"], (root / manifest["catalog"]).stat().st_size)
            original_name = manifest["catalog"]
            export_data(db_name=db, output_file=output)
            self.assertEqual(json.loads(output.read_text())["catalog"], original_name)
            self.assertEqual(len(list(root.glob("catalog.*.json"))), 1)
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("UPDATE videos SET title='Changed'")
                conn.commit()
            export_data(db_name=db, output_file=output)
            self.assertNotEqual(json.loads(output.read_text())["catalog"], original_name)
            self.assertTrue((root / original_name).exists())


if __name__ == "__main__":
    unittest.main()
