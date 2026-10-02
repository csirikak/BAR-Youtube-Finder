from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import findScreenshotBattles as matching
from updateBattleDB import setup_database
from updateSchema import add_new_tables


class MatchingCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "battles.db"
        self.source = self.root / "ocr.json"
        self.output = self.root / "matches.json"
        redirect = redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        setup_database(self.db).close()
        add_new_tables(self.db)
        self.players = [f"Player{i}" for i in range(6)]
        self.add_battle("battle-a", "2026-08-01T12:00:00Z")
        self.data = {"video": {"upload_date": "20260802", "title": "Original",
                               "screenshots": {"90": self.players}}}
        self.save_input()

    def save_input(self):
        self.source.write_text(json.dumps(self.data))

    def add_battle(self, bid, date):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO battles VALUES (?, ?, 'Map')", (bid, date))
            conn.executemany("INSERT INTO battle_participants VALUES (?, ?)",
                             [(bid, name) for name in self.players])
            conn.commit()

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.db)) as conn:
            result = conn.execute(statement, args).fetchall()
            conn.commit()
            return result

    def run_match(self, **kwargs):
        return matching.main(db_name=self.db, screenshot_file=self.source,
                             output_file=self.output, max_workers=1, **kwargs)

    def test_unchanged_run_skips_index_load_and_produces_same_output(self):
        self.assertEqual(self.run_match()["computed"], 1)
        original = self.output.read_bytes()
        with patch.object(matching, "load_data_from_db", side_effect=AssertionError("index loaded")):
            summary = self.run_match()
        self.assertEqual(summary["cached"], 1)
        self.assertEqual(summary["computed"], 0)
        self.assertEqual(self.output.read_bytes(), original)

    def test_new_replay_outside_upload_window_keeps_cache(self):
        self.run_match()
        self.add_battle("future", "2026-08-03T00:00:00Z")
        self.add_battle("too-old", "2024-01-01T00:00:00Z")
        self.assertEqual(self.run_match()["cached"], 1)

    def test_same_upload_day_invalidates_cache(self):
        self.run_match()
        self.add_battle("same-day", "2026-08-02T23:59:59Z")
        self.assertEqual(self.run_match()["computed"], 1)

    def test_late_historical_replay_invalidates_only_relevant_video(self):
        self.data["old-video"] = dict(self.data["video"], upload_date="20250102")
        self.save_input()
        self.run_match()
        self.add_battle("historical", "2025-01-01T12:00:00Z")
        summary = self.run_match()
        self.assertEqual((summary["computed"], summary["cached"]), (1, 1))
        result = json.loads(self.output.read_text())
        self.assertEqual(result["old-video"]["screenshots"]["90"]["matched_battle_id"], "historical")

    def test_roster_correction_invalidates_cache(self):
        self.run_match()
        self.sql("DELETE FROM battle_participants WHERE battle_id='battle-a'")
        self.assertEqual(self.run_match()["computed"], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM battle_videos")[0][0], 0)

    def test_deleted_battle_invalidates_cache(self):
        self.run_match()
        self.sql("DELETE FROM battles WHERE battle_id='battle-a'")
        self.assertEqual(self.run_match()["computed"], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM battle_videos")[0][0], 0)

    def test_timestamp_correction_invalidates_old_and_new_windows(self):
        self.run_match()
        self.sql("UPDATE battles SET timestamp='2027-01-01T00:00:00Z' WHERE battle_id='battle-a'")
        self.assertEqual(self.run_match()["computed"], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM battle_videos")[0][0], 0)

    def test_replace_with_new_timestamp_invalidates_original_window(self):
        self.run_match()
        self.sql("INSERT OR REPLACE INTO battles VALUES ('battle-a', '2027-01-01T00:00:00Z', 'Map')")
        self.assertEqual(self.run_match()["computed"], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM battle_videos")[0][0], 0)

    def test_unknown_battle_date_invalidates_all_windows(self):
        self.run_match()
        self.add_battle("bad-date", "not-a-date")
        self.assertEqual(self.run_match()["computed"], 1)

    def test_bad_time_outside_window_still_invalidates_cache(self):
        self.run_match()
        self.add_battle("bad-time", "2024-01-01Tnot-a-time")
        self.assertEqual(self.run_match()["computed"], 1)

    def test_unknown_upload_date_considers_all_replay_changes(self):
        self.data["video"]["upload_date"] = None
        self.save_input()
        self.run_match()
        self.add_battle("future", "2027-01-01T00:00:00Z")
        self.assertEqual(self.run_match()["computed"], 1)

    def test_metadata_change_reuses_matching_but_updates_exports(self):
        self.run_match()
        self.data["video"]["title"] = "New title"
        self.save_input()
        self.assertEqual(self.run_match()["cached"], 1)
        self.assertEqual(self.sql("SELECT title FROM videos")[0][0], "New title")
        self.assertEqual(json.loads(self.output.read_text())["video"]["title"], "New title")

    def test_ocr_change_and_removed_frames_replace_old_links(self):
        self.run_match()
        self.data["video"]["screenshots"] = {"810": self.players}
        self.save_input()
        self.assertEqual(self.run_match()["computed"], 1)
        self.assertEqual(self.sql("SELECT video_timestamp_sec FROM battle_videos"), [(810,)])

    def test_removed_video_removes_links_and_cache(self):
        self.run_match()
        self.data = {}
        self.save_input()
        self.run_match()
        self.assertEqual(self.sql("SELECT * FROM video_match_cache"), [])
        self.assertEqual(self.sql("SELECT * FROM battle_videos"), [])

    def test_matching_setting_changes_invalidate_cache(self):
        self.run_match()
        with patch.object(matching, "MIN_LEN", 7):
            self.assertEqual(self.run_match()["computed"], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM battle_videos")[0][0], 0)

    def test_force_recomputes_all_cached_videos(self):
        self.run_match()
        self.assertEqual(self.run_match(force=True)["computed"], 1)

    def test_corrupt_cache_is_recomputed(self):
        self.run_match()
        self.sql("UPDATE video_match_cache SET result_json='broken'")
        self.assertEqual(self.run_match()["computed"], 1)

    def test_cache_with_wrong_json_shape_is_recomputed(self):
        self.run_match()
        self.sql("UPDATE video_match_cache SET result_json=?", ('{"a":1,"b":2,"c":3,"d":4}',))
        self.assertEqual(self.run_match()["computed"], 1)

    def test_candidate_loading_agrees_with_full_history(self):
        self.add_battle("old", "2024-01-01T00:00:00Z")
        self.add_battle("future", "2027-01-01T00:00:00Z")
        self.add_battle("bad-date", "unknown")
        self.add_battle("unrelated", "2026-08-01T00:00:00Z")
        self.sql("UPDATE battle_participants SET player_name='Other' || player_name WHERE battle_id='unrelated'")
        with closing(sqlite3.connect(self.db)) as conn:
            complete = matching.load_data_from_db(conn)
            reduced = matching.load_data_from_db(conn, list(self.data.items()))
        self.assertEqual(set(reduced[1]), {"battle-a", "bad-date"})
        for names in [self.players, self.players + ['Another'], self.players[:5]]:
            self.assertEqual(matching.find_best_match(names, '20260802', *complete),
                             matching.find_best_match(names, '20260802', *reduced))

    def test_candidate_loading_with_unknown_upload_date_keeps_all_dates(self):
        self.add_battle("future", "2027-01-01T00:00:00Z")
        self.data["video"]["upload_date"] = None
        with closing(sqlite3.connect(self.db)) as conn:
            reduced = matching.load_data_from_db(conn, list(self.data.items()))
        self.assertEqual(set(reduced[1]), {"battle-a", "future"})

    def test_short_or_ai_rosters_skip_database_indexing(self):
        self.data["video"]["screenshots"] = {'90': self.players[:5], '810': self.players + ['Bot (AI)']}
        with closing(sqlite3.connect(self.db)) as conn:
            index, battles = matching.load_data_from_db(conn, list(self.data.items()))
        self.assertFalse(index)
        self.assertFalse(battles)

    def test_failure_keeps_previous_links_and_output(self):
        self.run_match()
        before = self.output.read_bytes()
        links = self.sql("SELECT * FROM battle_videos")
        with patch.object(matching, "process_video_task", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                self.run_match(force=True)
        self.assertEqual(self.sql("SELECT * FROM battle_videos"), links)
        self.assertEqual(self.output.read_bytes(), before)

    def test_ignored_duplicate_insert_does_not_invalidate_cache(self):
        self.run_match()
        self.sql("INSERT OR IGNORE INTO battles VALUES ('battle-a','2026-08-01T12:00:00Z','Map')")
        self.assertEqual(self.run_match()["cached"], 1)


if __name__ == "__main__":
    unittest.main()
