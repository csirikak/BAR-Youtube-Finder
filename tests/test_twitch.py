import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import scrape
import scrape_twitch


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8'
    '/x8AAwMCAO+aRZkAAAAASUVORK5CYII=')

STREAM = {
    "stream_id": "317952801783",
    "login": "siven12",
    "display_name": "siven12",
    "title": "good morning!",
    "viewers": 82,
    "started_at": "2026-11-23T18:22:21Z",
}

VOD = {
    "video_id": "1234567890",
    "title": "A full BAR stream",
    "duration": 3000,
    "created_at": "2026-11-23T18:22:21Z",
    "views": 12,
    "login": "streamer",
    "display_name": "Streamer",
}

VOD_INFO = {
    "id": "v1234567890",
    "title": "yt-dlp title",
    "uploader": "streamer",
    "upload_date": "20261124",
    "duration": 3000,
    "is_live": True,
    "live_status": "is_live",
    "webpage_url": "https://www.twitch.tv/videos/1234567890",
    "http_headers": {},
    "formats": [{
        "format_id": "1080p60",
        "url": "https://media.invalid/vod.m3u8",
        "protocol": "m3u8_native",
        "vcodec": "avc1.640028",
        "http_headers": {},
    }],
}

INFO = {
    "id": "317952801783",
    "title": "siven12 (live)",
    "uploader": "siven12",
    "upload_date": "20261123",
    "live_status": "is_live",
    "webpage_url": "https://www.twitch.tv/siven12",
    "thumbnail": "https://static.invalid/thumb.jpg",
    "http_headers": {},
    "formats": [{
        "format_id": "1080p",
        "url": "https://media.invalid/live.m3u8",
        "protocol": "m3u8_native",
        "vcodec": "avc1.640028",
        "http_headers": {},
    }],
}


class TwitchDiscoveryTests(unittest.TestCase):
    def test_category_page_slug_is_discovered_through_gql(self):
        response = Mock()
        response.json.return_value = {"data": {"game": {
            "name": "Beyond All Reason",
            "streams": {"edges": [
                {"node": {
                    "id": "1", "title": "BAR", "viewersCount": 10,
                    "createdAt": "2026-11-23T18:00:00Z",
                    "broadcaster": {"login": "alpha", "displayName": "Alpha"},
                }},
                {"node": {
                    "id": "2", "title": "Second", "viewersCount": 20,
                    "createdAt": "2026-11-23T19:00:00Z",
                    "broadcaster": {"login": "beta", "displayName": "Beta"},
                }},
            ]},
        }}}
        session = Mock()
        session.post.return_value = response
        streams = scrape_twitch.discover_streams(session=session)
        self.assertEqual([stream["login"] for stream in streams],
                         ["beta", "alpha"])
        response.raise_for_status.assert_called_once()
        call = session.post.call_args
        self.assertEqual(call.kwargs["json"]["variables"],
                         {"slug": "beyond-all-reason", "first": 100})
        self.assertEqual(call.kwargs["headers"]["Client-ID"],
                         scrape_twitch.TWITCH_CLIENT_ID)

    def test_non_category_urls_are_rejected(self):
        with self.assertRaises(ValueError):
            scrape_twitch.discover_streams("https://www.twitch.tv/siven12")

    def test_category_vods_are_discovered_and_sorted(self):
        response = Mock()
        response.json.return_value = {"data": {"game": {
            "name": "Beyond All Reason",
            "videos": {"edges": [
                {"node": {"id": "1", "title": "Older", "lengthSeconds": 300,
                          "createdAt": "2026-11-22T18:00:00Z", "viewCount": 5,
                          "owner": {"login": "alpha", "displayName": "Alpha"}}},
                {"node": {"id": "2", "title": "Newer", "lengthSeconds": 600,
                          "createdAt": "2026-11-23T19:00:00Z", "viewCount": 7,
                          "owner": {"login": "beta", "displayName": "Beta"}}},
            ]},
        }}}
        session = Mock()
        session.post.return_value = response
        videos = scrape_twitch.discover_videos(session=session)
        self.assertEqual([video["video_id"] for video in videos], ["2", "1"])
        self.assertEqual(videos[0]["duration"], 600)
        call = session.post.call_args
        self.assertEqual(call.kwargs["json"]["variables"],
                         {"slug": "beyond-all-reason", "first": 100})
        self.assertIn("videos(first", call.kwargs["json"]["query"])

    def test_vod_discovery_pages_beyond_one_gql_page(self):
        def make_response(video_id, date, page_info):
            response = Mock()
            response.json.return_value = {"data": {"game": {
                "videos": {
                    "pageInfo": page_info,
                    "edges": [{"node": {
                        "id": video_id, "title": video_id,
                        "lengthSeconds": 60, "createdAt": date, "viewCount": 1,
                        "owner": {"login": "alpha", "displayName": "Alpha"},
                    }}],
                },
            }}}
            return response
        session = Mock()
        session.post.side_effect = [
            make_response("1", "2026-11-23T00:00:00Z",
                          {"hasNextPage": True, "endCursor": "next"}),
            make_response("2", "2026-11-22T00:00:00Z",
                          {"hasNextPage": False, "endCursor": None}),
        ]
        videos = scrape_twitch.discover_videos(limit=101, session=session)
        self.assertEqual([video["video_id"] for video in videos], ["1", "2"])
        calls = session.post.call_args_list
        self.assertEqual(calls[0].kwargs["json"]["variables"],
                         {"slug": "beyond-all-reason", "first": 101})
        self.assertEqual(calls[1].kwargs["json"]["variables"],
                         {"slug": "beyond-all-reason", "first": 100, "after": "next"})

    def test_vod_discovery_applies_retention_cutoff(self):
        response = Mock()
        response.json.return_value = {"data": {"game": {"videos": {
            "edges": [
                {"node": {"id": "old", "lengthSeconds": 60,
                          "createdAt": "2026-09-23T00:00:00Z",
                          "owner": {"login": "alpha"}}},
                {"node": {"id": "new", "lengthSeconds": 60,
                          "createdAt": "2026-09-24T00:00:00Z",
                          "owner": {"login": "alpha"}}},
            ],
        }}}}
        session = Mock()
        session.post.return_value = response
        videos = scrape_twitch.discover_videos(
            since="20260924", session=session)
        self.assertEqual([video["video_id"] for video in videos], ["new"])

    def test_limit_is_forwarded_to_gql(self):
        response = Mock()
        response.json.return_value = {"data": {"game": {
            "streams": {"edges": []},
        }}}
        session = Mock()
        session.post.return_value = response
        scrape_twitch.discover_streams(limit=3, session=session)
        self.assertEqual(session.post.call_args.kwargs["json"]["variables"]["first"], 3)


class TwitchVodCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.frames = self.root / "frames"
        self.metadata = self.root / "metadata.json"

    def test_vod_info_is_made_seekable_and_uses_category_title(self):
        with patch.object(scrape, "update_video_database", return_value=(1, 0)), \
             patch.object(scrape, "process_video_screenshots",
                          return_value=("twitch_1234567890", "Processed 'A full BAR stream'", 4)) as process:
            result = scrape_twitch.capture_vod(
                VOD, output_dir=self.frames, metadata_file=self.metadata,
                extractor=lambda video_id: VOD_INFO)
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["frames"], 4)
        info = process.call_args.args[0]
        self.assertEqual(info["id"], "twitch_1234567890")
        self.assertEqual(info["title"], "A full BAR stream")
        self.assertEqual(info["upload_date"], "20261123")
        self.assertFalse(info["is_live"])
        self.assertEqual(info["live_status"], "was_live")
        self.assertEqual(info["webpage_url"], "https://www.twitch.tv/videos/1234567890")

    def test_vod_without_seekable_formats_is_deferred(self):
        result = scrape_twitch.capture_vod(
            VOD, output_dir=self.frames, metadata_file=self.metadata,
            extractor=lambda video_id: {**VOD_INFO, "formats": []})
        self.assertEqual(result["status"], "deferred")
        self.assertFalse(self.metadata.exists())


class TwitchCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.frames = self.root / "frames"
        self.metadata = self.root / "metadata.json"

    def screenshot(self, output):
        Path(output).write_bytes(PNG)
        return None

    def test_live_frame_uses_stream_relative_metadata_and_preserves_ocr(self):
        key = "twitch_317952801783"
        self.metadata.write_text(json.dumps({
            key: {"screenshots": {"120": ["Earlier"]}},
        }))
        now = datetime(2026, 11, 23, 20, 4, 21, tzinfo=timezone.utc)
        with patch.object(scrape_twitch.scrape, "capture_screenshot",
                          side_effect=lambda fmt, ts, path: self.screenshot(path)) as capture:
            result = scrape_twitch.capture_stream(
                STREAM, output_dir=self.frames, metadata_file=self.metadata,
                now=now, extractor=lambda login: INFO)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["offset"], 6120)
        self.assertTrue((self.frames / f"{key}_6120s.png").is_file())
        capture.assert_called_once()
        self.assertEqual(capture.call_args.args[1], 0)
        saved = json.loads(self.metadata.read_text())[key]
        self.assertEqual(saved["upload_date"], "20261123")
        self.assertEqual(saved["live_status"], "is_live")
        self.assertEqual(saved["source"], "twitch")
        self.assertEqual(saved["screenshots"], {"120": ["Earlier"]})
        self.assertEqual(saved["uploader"], "siven12")

    def test_existing_frame_is_skipped_without_extracting(self):
        now = datetime(2026, 11, 23, 20, 4, 21, tzinfo=timezone.utc)
        output = self.frames / "twitch_317952801783_6120s.png"
        output.parent.mkdir(parents=True)
        output.write_bytes(PNG)
        extractor = Mock()
        result = scrape_twitch.capture_stream(
            STREAM, output_dir=self.frames, metadata_file=self.metadata,
            now=now, extractor=extractor)
        self.assertEqual(result["status"], "skipped")
        extractor.assert_not_called()
        self.assertFalse(self.metadata.exists())

    def test_all_format_failures_are_reported_without_metadata(self):
        now = datetime(2026, 11, 23, 20, 4, 21, tzinfo=timezone.utc)
        result = scrape_twitch.capture_stream(
            STREAM, output_dir=self.frames, metadata_file=self.metadata,
            now=now, extractor=lambda login: INFO,
            screenshotter=lambda fmt, ts, path: "HTTP 403")
        self.assertEqual(result["status"], "failed")
        self.assertIn("1080p", result["message"])
        self.assertFalse(self.metadata.exists())

    def test_non_live_info_is_deferred(self):
        now = datetime(2026, 11, 23, 20, 4, 21, tzinfo=timezone.utc)
        result = scrape_twitch.capture_stream(
            STREAM, output_dir=self.frames, metadata_file=self.metadata,
            now=now, extractor=lambda login: {**INFO, "live_status": "was_live"})
        self.assertEqual(result["status"], "deferred")
        self.assertFalse(self.metadata.exists())


if __name__ == "__main__":
    unittest.main()
