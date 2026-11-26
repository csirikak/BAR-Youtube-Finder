from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import exportForFrontend
import updateBattleDB as battles
import update_pipeline as update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.metadata = self.root / "metadata.json"
        self.database = self.root / "battles.db"
        self.addCleanup(patch.stopall)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def ingest(self, **kwargs):
        return update.run_ingestion(
            channel_urls=["channel"], screenshots_dir=self.images,
            metadata_file=self.metadata, player_database=self.database,
            ocr_batch_size=1, **kwargs)

    def test_download_branches_overlap_and_ocr_starts_before_capture_ends(self):
        source_started = threading.Event()
        database_ready = threading.Event()
        first_recognized = threading.Event()
        capture_complete = threading.Event()
        def sync(**kwargs):
            self.assertTrue(source_started.wait(3), "Channel did not overlap replay sync")
            database_ready.set()
        def scrape(channel, directory, **kwargs):
            source_started.set()
            kwargs["on_screenshot"](Path(directory) / "video_90s.png")
            self.assertTrue(first_recognized.wait(3), "OCR waited for the entire channel")
            kwargs["on_screenshot"](Path(directory) / "video_810s.png")
            capture_complete.set()
            return True
        def recognize(**kwargs):
            self.assertTrue(database_ready.is_set(), "OCR read the catalog before commit")
            batches = iter(kwargs["frame_batches"])
            first = next(batches)
            self.assertFalse(capture_complete.is_set())
            self.assertEqual(first[0][2], "90")
            first_recognized.set()
            self.assertEqual([frame[2] for batch in batches for frame in batch], ["810"])
        with patch.object(battles, "main", side_effect=sync), \
             patch.object(update.scrape, "get_channel_screenshots", side_effect=scrape), \
             patch.object(update.ocr, "main", side_effect=recognize):
            self.ingest()

    def test_capture_limit_is_shared_across_channels(self):
        sources_started = threading.Barrier(2)
        all_workers_busy = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = peak = 0
        executor_ids = set()
        def capture():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
                if active == 4:
                    all_workers_busy.set()
            try:
                self.assertTrue(release.wait(3))
            finally:
                with lock:
                    active -= 1
        def source(channel, directory, **kwargs):
            executor = kwargs["capture_executor"]
            with lock:
                executor_ids.add(id(executor))
            sources_started.wait(timeout=3)
            futures = [executor.submit(capture) for _ in range(4)]
            for future in futures:
                future.result()
            return True
        with patch.object(update.scrape, "get_channel_screenshots", side_effect=source), \
             ThreadPoolExecutor(max_workers=1) as runner:
            future = runner.submit(update.scrape_channels, ["one", "two"], self.images,
                                   self.metadata, update.FrameStream(), 2, 4)
            try:
                self.assertTrue(all_workers_busy.wait(3))
                self.assertEqual(peak, 4)
            finally:
                release.set()
            future.result(timeout=3)
        self.assertEqual(len(executor_ids), 1)
        self.assertEqual(peak, 4)

    def test_replay_failure_cancels_capture_and_does_not_start_ocr(self):
        source_started = threading.Event()
        def sync(**kwargs):
            self.assertTrue(source_started.wait(3))
            raise RuntimeError("Replay API failed")
        def scrape(channel, directory, **kwargs):
            source_started.set()
            self.assertTrue(kwargs["stop_event"].wait(3), "Failure did not cancel downloads")
            return False
        with patch.object(battles, "main", side_effect=sync), \
             patch.object(update.scrape, "get_channel_screenshots", side_effect=scrape), \
             patch.object(update.ocr, "main") as recognize:
            with self.assertRaisesRegex(RuntimeError, "Replay API failed"):
                self.ingest()
        recognize.assert_not_called()

    def test_existing_pending_images_are_queued_once(self):
        for filename in ["video_90s.png", "video_810s.png", "video_1530s.part.png"]:
            (self.images / filename).touch()
        self.metadata.write_text(json.dumps({"video": {"screenshots": {"90": []}}}))
        observed = []
        def source(channel, directory, **kwargs):
            kwargs["on_screenshot"](self.images / "video_810s.png")
            return True
        def recognize(**kwargs):
            observed.extend(frame[2] for batch in kwargs["frame_batches"] for frame in batch)
        with patch.object(battles, "main"), \
             patch.object(update.scrape, "get_channel_screenshots", side_effect=source), \
             patch.object(update.ocr, "main", side_effect=recognize):
            self.ingest()
        self.assertEqual(observed, ["810"])

    def test_stream_flushes_final_partial_batch_and_closes_idempotently(self):
        stream = update.FrameStream(8)
        stream.put("video_90s.png")
        stream.put("video_90s.png")
        stream.close()
        stream.close()
        self.assertEqual([[f[2] for f in batch] for batch in stream.batches()], [["90"]])
        with self.assertRaises(RuntimeError):
            stream.put("video_810s.png")

    def test_stream_flushes_without_waiting_for_end_of_downloads(self):
        stream = update.FrameStream(8, flush_seconds=0)
        stream.put("video_90s.png")
        batches = stream.batches()
        self.assertEqual(next(batches)[0][2], "90")
        stream.close()
        self.assertEqual(list(batches), [])

    def test_stream_signals_idle_so_ocr_can_collect_pending_results(self):
        stream = update.FrameStream(1)
        stream.put("video_90s.png")
        batches = stream.batches(flush_idle=True)
        self.assertEqual(next(batches)[0][2], "90")
        self.assertEqual(next(batches), [])
        stream.put("video_810s.png")
        stream.close()
        self.assertEqual([[f[2] for f in batch] for batch in batches], [["810"]])

    def patch_pipeline(self):
        patch.object(update, "network_services", return_value=nullcontext()).start()
        self.ingestion = patch.object(update, "run_ingestion").start()
        self.twitch_ingestion = patch.object(update, "run_twitch_ingestion").start()
        self.matching = patch.object(update.subprocess, "run").start()
        self.renames = patch.object(update.rename_resolver, "resolve").start()
        self.export = patch.object(exportForFrontend, "export_data", return_value=True).start()
        self.publish = patch.object(update, "publish_changes").start()
        self.cleanup = patch.object(update.shutil, "rmtree").start()
        patch.object(update.scrape, "SCREENSHOT_DIR", str(self.images)).start()

    def test_success_preserves_matching_export_publish_cleanup_order(self):
        self.patch_pipeline()
        calls = Mock()
        for name, function in [("ingest", self.ingestion), ("twitch", self.twitch_ingestion),
                               ("match", self.matching), ("renames", self.renames),
                               ("export", self.export), ("publish", self.publish),
                               ("cleanup", self.cleanup)]:
            calls.attach_mock(function, name)
        update.main()
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["ingest", "twitch", "match", "match", "renames",
                          "export", "publish", "cleanup", "cleanup"])
        self.assertTrue(self.matching.call_args.kwargs["check"])

    def test_failed_prerequisite_prevents_all_downstream_work(self):
        self.patch_pipeline()
        self.ingestion.side_effect = RuntimeError("failed prerequisite")
        with self.assertRaisesRegex(RuntimeError, "failed prerequisite"):
            update.main()
        for task in [self.twitch_ingestion, self.matching, self.renames,
                     self.export, self.publish, self.cleanup]:
            task.assert_not_called()

    def test_matching_failure_prevents_export_publish_and_cleanup(self):
        self.patch_pipeline()
        self.matching.side_effect = subprocess.CalledProcessError(1, ["matcher"])
        with self.assertRaises(subprocess.CalledProcessError):
            update.main()
        self.twitch_ingestion.assert_called_once()
        self.assertEqual(self.matching.call_count, 1)
        for task in [self.renames, self.export, self.publish, self.cleanup]:
            task.assert_not_called()

    def test_false_export_prevents_publish_and_cleanup(self):
        self.patch_pipeline()
        self.export.return_value = False
        with self.assertRaisesRegex(RuntimeError, "frontend export"):
            update.main()
        self.publish.assert_not_called()
        self.cleanup.assert_not_called()

    def test_failed_publish_retains_screenshots(self):
        self.patch_pipeline()
        self.publish.side_effect = RuntimeError("push rejected")
        with self.assertRaisesRegex(RuntimeError, "push rejected"):
            update.main()
        self.cleanup.assert_not_called()

    def test_can_disable_publish_and_cleanup(self):
        self.patch_pipeline()
        update.main(publish=False, cleanup=False)
        self.export.assert_called_once()
        self.publish.assert_not_called()
        self.cleanup.assert_not_called()

    def test_twitch_limit_zero_skips_twitch_and_keeps_twitch_screenshots(self):
        self.patch_pipeline()
        update.main(twitch_limit=0)
        self.twitch_ingestion.assert_not_called()
        self.assertEqual(self.matching.call_count, 1)
        self.cleanup.assert_called_once_with(update.scrape.SCREENSHOT_DIR)

    def test_twitch_full_population_uses_entire_retention_window(self):
        self.patch_pipeline()
        update.main(twitch_full=True)
        self.twitch_ingestion.assert_called_once()
        self.assertEqual(self.twitch_ingestion.call_args.kwargs["limit"],
                         update.scrape_twitch.MAX_VIDEO_PAGE)

    def test_twitch_default_populates_full_retention_window(self):
        self.patch_pipeline()
        update.main()
        self.assertEqual(self.twitch_ingestion.call_args.kwargs["limit"],
                         update.scrape_twitch.MAX_VIDEO_PAGE)

    def test_twitch_full_overrides_a_zero_limit(self):
        self.patch_pipeline()
        update.main(twitch_limit=0, twitch_full=True)
        self.twitch_ingestion.assert_called_once()

    def test_invalid_limits_fail_before_starting_services(self):
        with patch.object(update, "network_services") as services:
            with self.assertRaises(ValueError):
                update.main(channel_workers=0)
        services.assert_not_called()

    def test_negative_rename_budget_fails_before_starting_services(self):
        with patch.object(update, "network_services") as services:
            with self.assertRaises(ValueError):
                update.main(rename_requests=-1)
        services.assert_not_called()

    def test_negative_twitch_limit_fails_before_starting_services(self):
        with patch.object(update, "network_services") as services:
            with self.assertRaises(ValueError):
                update.main(twitch_limit=-1)
        services.assert_not_called()

    def test_twitch_ingestion_captures_before_ocr(self):
        with patch.object(update.scrape_twitch, "capture_category") as capture, \
             patch.object(update.ocr, "main") as recognize:
            update.run_twitch_ingestion(limit=5, capture_workers=2,
                                        ocr_batch_size=4)
        self.assertEqual(capture.call_args.kwargs["limit"], 5)
        self.assertEqual(capture.call_args.kwargs["workers"], 2)
        self.assertEqual(recognize.call_args.kwargs["screenshots_dir"],
                         update.scrape_twitch.SCREENSHOT_DIR)
        self.assertEqual(recognize.call_args.kwargs["output_file"],
                         update.scrape_twitch.METADATA_FILE)

    def test_owned_network_services_close_on_exception(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(update, "provider_ready", side_effect=[False, True]), \
             patch.object(update.subprocess, "Popen", return_value=process), \
             patch.object(update.subprocess, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "task failed"):
                with update.network_services():
                    raise RuntimeError("task failed")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertEqual([c.args[0] for c in command.call_args_list],
                         [["warp-cli", "disconnect"], ["warp-cli", "connect"],
                          ["warp-cli", "disconnect"]])

    def test_existing_provider_is_reused_and_left_running(self):
        with patch.object(update, "provider_ready", return_value=True), \
             patch.object(update.subprocess, "Popen") as start:
            with update.network_services(use_warp=False):
                pass
        start.assert_not_called()

    def screenshot(self, name, width, height):
        path = self.root / name
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", width, height))
        return path

    def test_screenshot_resolutions_are_bucketed(self):
        resolutions = update.ScreenshotResolutions()
        for name, width, height in [
            ("4k.png", 3840, 2160),
            ("2k.png", 2560, 1440),
            ("1080.png", 1920, 1080),
            ("720.png", 1280, 720),
            ("480.png", 854, 480),
            ("other.png", 640, 360),
        ]:
            resolutions.add(self.screenshot(name, width, height))
        resolutions.add(self.root / "missing.png")
        self.assertEqual(resolutions.snapshot(), {
            "4K": 1, "2K": 1, "1080p": 1, "720p": 1,
            "480p": 1, "Other": 1,
        })

    def test_youtube_and_twitch_capture_resolutions_are_counted(self):
        youtube = update.ScreenshotResolutions()
        twitch = update.ScreenshotResolutions()
        youtube_frame = self.screenshot("video_90s.png", 1920, 1080)
        twitch_frame = self.screenshot("twitch_123_90s.png", 1280, 720)

        def youtube_source(channel, directory, **kwargs):
            kwargs["on_screenshot"](youtube_frame)
            return True

        def twitch_source(**kwargs):
            kwargs["on_screenshot"](twitch_frame)

        with patch.object(battles, "main"), \
             patch.object(update.scrape, "get_channel_screenshots",
                          side_effect=youtube_source), \
             patch.object(update.ocr, "main"):
            self.ingest(resolutions=youtube)
        with patch.object(update.scrape_twitch, "capture_category",
                          side_effect=twitch_source), \
             patch.object(update.ocr, "main"):
            update.run_twitch_ingestion(limit=5, resolutions=twitch)
        self.assertEqual(youtube.snapshot()["1080p"], 1)
        self.assertEqual(twitch.snapshot()["720p"], 1)

    def test_report_summary_prints_screenshot_resolutions(self):
        with patch.object(update.ui, "stats_table") as table:
            update.report_summary(
                update.scrape.IngestionStats(), update.StageTimes(),
                screenshot_resolutions={
                    "YouTube": {"4K": 0, "2K": 2, "1080p": 10,
                                "720p": 1, "480p": 0, "Other": 0},
                    "Twitch": {"4K": 1, "2K": 0, "1080p": 5,
                               "720p": 0, "480p": 0, "Other": 0},
                })
        table.assert_called_once()
        title, rows = table.call_args.args
        self.assertEqual(title, "Screenshot resolutions")
        self.assertEqual(rows, [
            ("YouTube", "2K 2 · 1080p 10 · 720p 1"),
            ("Twitch", "4K 1 · 1080p 5"),
        ])

    def test_main_counts_resolutions_for_both_platforms(self):
        self.patch_pipeline()
        youtube_frame = self.screenshot("youtube.png", 1920, 1080)
        twitch_frame = self.screenshot("twitch.png", 2560, 1440)
        self.ingestion.side_effect = lambda **kwargs: kwargs["resolutions"].add(youtube_frame)
        self.twitch_ingestion.side_effect = lambda **kwargs: kwargs["resolutions"].add(twitch_frame)
        with patch.object(update, "report_summary") as report:
            update.main()
        counts = report.call_args.kwargs["screenshot_resolutions"]
        self.assertEqual(counts["YouTube"]["1080p"], 1)
        self.assertEqual(counts["Twitch"]["2K"], 1)


class ReplayFailureTests(unittest.TestCase):
    def test_api_failure_propagates_instead_of_ending_successfully(self):
        error = battles.requests.HTTPError("API failed")
        with patch.object(battles.requests, "get", side_effect=error) as get, \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(battles.requests.HTTPError):
                list(battles.fetch_battles_from_api())
        self.assertEqual(get.call_args.kwargs["timeout"], (10, 60))

    def test_failed_pagination_rolls_back_inserted_battles(self):
        conn = battles.setup_database(":memory:")
        self.addCleanup(conn.close)
        def feed():
            yield {"id": "new", "startTime": "2026-09-28T00:00:00Z",
                   "AllyTeams": [{"Players": [{"name": "KnownName"}]}]}
            raise RuntimeError("next page failed")
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "next page failed"):
                battles.process_and_insert_data(conn, feed())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM battles").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM players").fetchone()[0], 0)

    def test_replay_connection_closes_on_error(self):
        conn = Mock()
        with patch.object(battles, "setup_database", return_value=conn), \
             patch.object(battles, "get_last_sync_timestamp", side_effect=RuntimeError("bad DB")), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "bad DB"):
                battles.main()
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
