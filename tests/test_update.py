from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
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

    def patch_pipeline(self):
        patch.object(update, "network_services", return_value=nullcontext()).start()
        self.ingestion = patch.object(update, "run_ingestion").start()
        self.matching = patch.object(update.subprocess, "run").start()
        self.export = patch.object(exportForFrontend, "export_data", return_value=True).start()
        self.publish = patch.object(update, "publish_changes").start()
        self.cleanup = patch.object(update.shutil, "rmtree").start()
        patch.object(update.scrape, "SCREENSHOT_DIR", str(self.images)).start()

    def test_success_preserves_matching_export_publish_cleanup_order(self):
        self.patch_pipeline()
        calls = Mock()
        for name, function in [("ingest", self.ingestion), ("match", self.matching),
                               ("export", self.export), ("publish", self.publish),
                               ("cleanup", self.cleanup)]:
            calls.attach_mock(function, name)
        update.main()
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["ingest", "match", "export", "publish", "cleanup"])
        self.assertTrue(self.matching.call_args.kwargs["check"])

    def test_failed_prerequisite_prevents_all_downstream_work(self):
        self.patch_pipeline()
        self.ingestion.side_effect = RuntimeError("failed prerequisite")
        with self.assertRaisesRegex(RuntimeError, "failed prerequisite"):
            update.main()
        for task in [self.matching, self.export, self.publish, self.cleanup]:
            task.assert_not_called()

    def test_matching_failure_prevents_export_publish_and_cleanup(self):
        self.patch_pipeline()
        self.matching.side_effect = subprocess.CalledProcessError(1, ["matcher"])
        with self.assertRaises(subprocess.CalledProcessError):
            update.main()
        for task in [self.export, self.publish, self.cleanup]:
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

    def test_invalid_limits_fail_before_starting_services(self):
        with patch.object(update, "network_services") as services:
            with self.assertRaises(ValueError):
                update.main(channel_workers=0)
        services.assert_not_called()

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
