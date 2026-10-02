from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from exportForFrontend import export_data
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
                conn.executemany("INSERT INTO battles VALUES (?, '2026-09-01T12:00:00Z', ?)",
                                 [("b1", "Map one"), ("b2", None)])
                conn.executemany("INSERT INTO battle_participants VALUES (?, ?)",
                                 [("b1", "__proto__"), ("b1", "玩家"), ("b2", "玩家")])
                conn.execute("INSERT INTO videos VALUES ('v1','20260902','A <title>','Channel')")
                conn.executemany("""INSERT INTO battle_videos
                    (battle_id,video_id,video_timestamp_sec) VALUES (?, 'v1', ?)""",
                                 [("b1", 810), ("b1", 90), ("b2", 1530)])
                conn.commit()
            output = root / "frontend_data.json"
            self.assertTrue(export_data(db_name=db, output_file=output))
            manifest = json.loads(output.read_text())
            catalog = json.loads((root / manifest["catalog"]).read_text())
            self.assertEqual(manifest["stats"], {"players": 2, "battles": 2, "videos": 1, "maps": 1})
            self.assertEqual(catalog["players"]["玩家"], [0, 1])
            self.assertEqual(catalog["players"]["__proto__"], [0])
            self.assertEqual(catalog["videos"], [["v1", "A <title>", 0, "20260902"]])
            self.assertEqual(catalog["battles"][0][3], [[0, 90]])
            self.assertEqual(catalog["battles"][1][1], -1)
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
