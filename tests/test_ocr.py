import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

import processScreenshotsRapidOCR as ocr
from ocr_names import PlayerNames


def text_result(text="KnownName", box=None):
    return SimpleNamespace(txts=[text], scores=[0.99],
                           boxes=[box or [[30, 5], [80, 5], [80, 20], [30, 20]]])


def detection(image):
    boxes = [] if not image.any() else [
        SimpleNamespace(conf=[0.99], xyxy=np.array([[110, 10, 200, 80]]))
    ]
    return SimpleNamespace(boxes=boxes)


class NamesTests(unittest.TestCase):
    def test_known_numeric_names_and_clans_survive(self):
        catalog = PlayerNames(["30_ztx", "[Crd]Protato", "solo560", "玩家名字", "TeamUnits"])
        for raw, expected in [
            ("41 30_ztx", "30_ztx"), ("30_ztx", "30_ztx"),
            ("*[Crd]Protato", "[Crd]Protato"), ("16solo560", "solo560"),
            ("玩家名字", "玩家名字"), ("TeamUnits", "TeamUnits"),
        ]:
            self.assertEqual(catalog.resolve(raw), expected)

    def test_exact_original_wins_over_stripped_variants(self):
        catalog = PlayerNames(["123Player", "Player", "Player99"])
        self.assertEqual(catalog.resolve("123Player"), "123Player")
        self.assertEqual(catalog.resolve("Player99"), "Player99")

    def test_unknown_digits_and_unicode_are_preserved(self):
        catalog = PlayerNames()
        for name in ["30_new_player", "Player123", "玩家名字", "[abc]NewName"]:
            self.assertEqual(catalog.resolve(name), name)

    def test_ui_labels_and_resources_are_not_names(self):
        catalog = PlayerNames()
        for text in ["Enemies", "Spectators 9", "tors 8", "1.04k", "59 t", "Raptors (AI)", "99 1", "123", '\" ā']:
            self.assertIsNone(catalog.resolve(text), text)
        self.assertEqual(catalog.resolve("EnemiesWithin"), "EnemiesWithin")

    def test_corrects_unambiguous_typos_and_stats(self):
        catalog = PlayerNames(["ResurrectedAshes", "CursedDragoon", "BlackBatA8T"])
        self.assertEqual(catalog.resolve("26ResurrectedAshes55 1,"), "ResurrectedAshes")
        self.assertEqual(catalog.resolve("CursedDragoor99.1"), "CursedDragoon")
        self.assertEqual(catalog.resolve("BlackBatABT"), "BlackBatA8T")

    def test_ambiguous_spelling_is_not_guessed(self):
        catalog = PlayerNames(["LongPlayerAlpha", "LongPlayerAlphb"])
        self.assertEqual(catalog.resolve("LongPlayerAlphc"), "LongPlayerAlphc")
        catalog = PlayerNames(["PlayerName", "playername"])
        self.assertEqual(catalog.resolve("PLAYERNAME"), "PLAYERNAME")

    def test_rank_glyph_filter_uses_position_and_keeps_catalog_names(self):
        catalog = PlayerNames(["参参参"])
        rank = [[10, 5], [70, 5], [70, 20], [10, 20]]
        self.assertEqual(ocr.names_from_result(text_result("参13", rank), catalog, 271), [])
        self.assertEqual(ocr.names_from_result(text_result("参参参", rank), catalog, 271), ["参参参"])
        self.assertEqual(ocr.names_from_result(text_result("玩家名字"), catalog, 200), ["玩家名字"])

    def test_loads_read_only_catalog_and_missing_database_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "players.db"
            self.assertEqual(PlayerNames.from_database(path).names, frozenset())
            self.assertFalse(path.exists())
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE players(player_name TEXT PRIMARY KEY)")
                db.execute("INSERT INTO players VALUES ('KnownName')")
                db.commit()
            self.assertEqual(PlayerNames.from_database(path).resolve("knownname"), "KnownName")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.output = self.root / "metadata.json"
        self.reader = Mock(return_value=text_result())
        self.detector = Mock(side_effect=lambda images, **kwargs: (
            [detection(image) for image in images]
            if isinstance(images, list) else [detection(images)]
        ))
        self.factory = patch.object(ocr, "create_models", return_value=(self.detector, self.reader)).start()
        self.addCleanup(patch.stopall)
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def frame(self, timestamp, value=1):
        path = self.images / f"video_id_{timestamp}s.png"
        cv2.imwrite(str(path), np.full((80, 200, 3), value, dtype=np.uint8))
        return path

    def run_ocr(self, **kwargs):
        return ocr.main(screenshots_dir=self.images, output_file=self.output,
                        player_database=self.root / "absent.db", **kwargs)

    def saved(self):
        return json.loads(self.output.read_text())

    def test_loads_models_once_and_bounds_batches(self):
        for timestamp in [90, 810, 1530]:
            self.frame(timestamp)
        self.assertEqual(self.run_ocr(batch_size=2), {"processed": 3, "failed": 0})
        self.factory.assert_called_once()
        self.assertEqual([len(call.args[0]) for call in self.detector.call_args_list], [2, 1])
        self.assertEqual(set(self.saved()["video_id"]["screenshots"]), {"90", "810", "1530"})

    def test_completed_empty_results_skip_models(self):
        self.frame(90)
        self.output.write_text(json.dumps({"video_id": {"screenshots": {"90": []}}}))
        self.assertEqual(self.run_ocr()["processed"], 0)
        self.factory.assert_not_called()

    def test_no_panel_is_a_successful_empty_result(self):
        self.frame(90, value=0)
        self.run_ocr()
        self.reader.assert_not_called()
        self.assertEqual(self.saved()["video_id"]["screenshots"]["90"], [])

    def test_inference_failure_is_retryable_and_success_is_saved(self):
        self.frame(90, value=1)
        self.frame(810, value=2)
        self.output.write_text(json.dumps({"video_id": {"title": "Preserve", "custom": True}}))
        def recognize(panel):
            if panel[0, 0, 0] == 2:
                raise RuntimeError("GPU error")
            return text_result()
        self.reader.side_effect = recognize
        with self.assertRaisesRegex(RuntimeError, "1 frame"):
            self.run_ocr(checkpoint_every=100)
        info = self.saved()["video_id"]
        self.assertEqual(info["screenshots"], {"90": ["KnownName"]})
        self.assertEqual(info["title"], "Preserve")
        self.assertTrue(info["custom"])
        self.reader.side_effect = None
        self.assertEqual(self.run_ocr()["processed"], 1)
        self.assertEqual(set(self.saved()["video_id"]["screenshots"]), {"90", "810"})

    def test_unreadable_image_does_not_prevent_other_frames(self):
        self.frame(90)
        (self.images / "video_id_810s.png").write_bytes(b"bad png")
        with self.assertRaisesRegex(RuntimeError, "1 frame"):
            self.run_ocr()
        self.assertEqual(self.saved()["video_id"]["screenshots"], {"90": ["KnownName"]})

    def test_batch_failure_retries_and_isolates_bad_frame(self):
        self.frame(90, value=1)
        self.frame(810, value=2)
        def detect(images, **kwargs):
            if isinstance(images, list) or images[0, 0, 0] == 2:
                raise RuntimeError("Inference error")
            return [detection(images)]
        self.detector.side_effect = detect
        with self.assertRaisesRegex(RuntimeError, "1 frame"):
            self.run_ocr()
        self.assertEqual(self.saved()["video_id"]["screenshots"], {"90": ["KnownName"]})
        self.assertEqual(self.detector.call_count, 3)

    def test_interrupt_saves_completed_work(self):
        self.frame(90, value=1)
        self.frame(810, value=2)
        # Files sort lexically, so 810 is processed before 90.
        self.reader.side_effect = [text_result(), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_ocr()
        self.assertEqual(self.saved()["video_id"]["screenshots"], {"810": ["KnownName"]})

    def test_corrupt_metadata_is_not_overwritten(self):
        self.output.write_text('{"broken":')
        with self.assertRaises(json.JSONDecodeError):
            self.run_ocr()
        self.assertEqual(self.output.read_text(), '{"broken":')
        self.factory.assert_not_called()

    def test_bad_metadata_structure_is_not_overwritten(self):
        self.output.write_text('{"video_id": {"screenshots": []}}')
        with self.assertRaises(ValueError):
            self.run_ocr()
        self.assertEqual(self.output.read_text(), '{"video_id": {"screenshots": []}}')

    def test_failed_atomic_replace_preserves_previous_database(self):
        self.output.write_text('{"previous": {}}')
        with patch.object(ocr.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                ocr.save_metadata(self.output, {"new": {}})
        self.assertEqual(self.output.read_text(), '{"previous": {}}')
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_partial_captures_and_non_frames_are_ignored(self):
        for name in ["video_id_90s.part.png", "other.png", "_90s.png"]:
            (self.images / name).touch()
        self.assertEqual(self.run_ocr()["processed"], 0)
        self.factory.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_reprocess_updates_only_available_frames(self):
        self.frame(90)
        self.output.write_text(json.dumps({"video_id": {"screenshots": {"90": [], "810": ["Keep"]}}}))
        self.run_ocr(reprocess=True)
        self.assertEqual(self.saved()["video_id"]["screenshots"], {"90": ["KnownName"], "810": ["Keep"]})

    def test_streaming_checkpoint_merges_concurrent_scraper_metadata(self):
        import scrape
        first, second = self.frame(90), self.frame(810)
        self.output.write_text(json.dumps({"video_id": {"title": "Old title", "custom": True}}))
        def batches():
            yield [(first, "video_id", "90")]
            scrape.update_video_database([
                {"id": "video_id", "title": "Updated title"},
                {"id": "another_video", "title": "Newly discovered"},
            ], self.output)
            yield [(second, "video_id", "810")]
        self.run_ocr(frame_batches=batches(), metadata_lock=scrape.DB_LOCK)
        result = self.saved()
        self.assertEqual(result["video_id"]["title"], "Updated title")
        self.assertTrue(result["video_id"]["custom"])
        self.assertEqual(result["another_video"]["title"], "Newly discovered")
        self.assertEqual(set(result["video_id"]["screenshots"]), {"90", "810"})
        self.factory.assert_called_once()

    def test_streaming_deduplicates_already_completed_frames(self):
        path = self.frame(90)
        frame = (path, "video_id", "90")
        result = self.run_ocr(frame_batches=iter([[frame], [frame]]),
                              metadata_lock=threading.Lock())
        self.assertEqual(result["processed"], 1)
        self.reader.assert_called_once()

    def test_stream_failure_checkpoints_completed_ocr(self):
        path = self.frame(90)
        def batches():
            yield [(path, "video_id", "90")]
            raise RuntimeError("producer failed")
        with self.assertRaisesRegex(RuntimeError, "producer failed"):
            self.run_ocr(frame_batches=batches(), metadata_lock=threading.Lock())
        self.assertEqual(self.saved()["video_id"]["screenshots"], {"90": ["KnownName"]})


if __name__ == "__main__":
    unittest.main()
