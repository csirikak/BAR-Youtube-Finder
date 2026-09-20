import base64
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scrape


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8'
    '/x8AAwMCAO+aRZkAAAAASUVORK5CYII=')


def video(video_id='example', duration=900, **fields):
    return dict(id=video_id, title='Beyond All Reason', duration=duration,
                upload_date='20260901', **fields)


def stream(name='primary', protocol='https', **fields):
    return dict(format_id=name, url=f'https://media.invalid/{name}',
                protocol=protocol, vcodec='avc1', **fields)


class ScraperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / 'screenshots'
        self.output.mkdir()
        self.database = self.directory / 'metadata.json'
        self.addCleanup(patch.stopall)
        patch.object(scrape, 'SCREENSHOT_DATA', str(self.database)).start()
        self.log = io.StringIO()
        self.stdout_redirect = contextlib.redirect_stdout(self.log)
        self.stdout_redirect.__enter__()
        self.addCleanup(self.stdout_redirect.__exit__, None, None, None)

    def write_frame(self, video_id='example', timestamp=90, content=PNG):
        path = self.output / f'{video_id}_{timestamp}s.png'
        path.write_bytes(content)
        return path

    def test_partial_files_do_not_archive_whole_video(self):
        self.write_frame()
        db = {'example': video()}
        self.assertEqual(scrape.populateHaveSet(self.output, db), set())
        self.write_frame(timestamp=810)
        self.assertEqual(scrape.populateHaveSet(self.output, db), {'example'})

    def test_ocr_and_files_together_complete_schedule(self):
        db = {'example': video(screenshots={'90': []})}
        self.assertEqual(scrape.populateHaveSet(self.output, db), set())
        self.write_frame(timestamp=810)
        self.assertEqual(scrape.populateHaveSet(self.output, db), {'example'})

    def test_complete_ocr_survives_deleted_image_directory(self):
        self.output.rmdir()
        db = {'example': video(screenshots={'90': [], '810': ['player']})}
        self.assertEqual(scrape.populateHaveSet(self.output, db), {'example'})

    def test_empty_ocr_and_truncated_png_remain_pending(self):
        self.write_frame(content=PNG[:-12])
        self.write_frame(timestamp=810, content=b'')
        info = video(screenshots={})
        self.assertEqual(scrape.missing_timestamps(info, self.output), [90, 810])
        self.assertEqual(scrape.populateHaveSet(self.output, {'example': info}), set())

    def test_no_duration_is_not_proof_of_completion(self):
        self.write_frame()
        info = video(duration=None, screenshots={'90': []})
        self.assertEqual(scrape.populateHaveSet(self.output, {'example': info}), set())

    def test_metadata_update_preserves_ocr_and_other_fields(self):
        self.database.write_text(json.dumps({
            'example': {'title': 'old', 'screenshots': {'90': []}, 'custom': True}}))
        self.assertEqual(scrape.update_video_database([video()], self.database), (0, 1))
        saved = json.loads(self.database.read_text())['example']
        self.assertEqual(saved['screenshots'], {'90': []})
        self.assertEqual(saved['title'], 'Beyond All Reason')
        self.assertTrue(saved['custom'])

    def test_corrupt_metadata_is_not_overwritten(self):
        self.database.write_text('{"example":')
        with self.assertRaises(json.JSONDecodeError):
            scrape.update_video_database([video()], self.database)
        self.assertEqual(self.database.read_text(), '{"example":')

    def test_formats_exclude_audio_and_fragment_urls_and_merge_headers(self):
        audio = dict(stream('audio'), vcodec='none')
        primary = stream(http_headers={'Referer': 'format'})
        hls = stream('hls', 'm3u8_native')
        info = video(requested_formats=[audio, primary],
                     formats=[hls, stream('dash', 'http_dash_segments')],
                     http_headers={'User-Agent': 'browser', 'Referer': 'base'})
        formats = scrape.screenshot_formats(info)
        self.assertEqual([f['format_id'] for f in formats], ['primary', 'hls'])
        self.assertEqual(formats[0]['http_headers'],
                         {'User-Agent': 'browser', 'Referer': 'format'})

    def test_alternate_transport_precedes_other_direct_encodings(self):
        direct = stream()
        info = dict(video(), **direct, formats=[
            stream('hls', 'm3u8_native'), stream('second'), direct])
        self.assertEqual([f['format_id'] for f in scrape.screenshot_formats(info)],
                         ['primary', 'hls', 'second'])

    def test_capture_requires_valid_output_even_with_zero_exit_status(self):
        target = self.output / 'capture.png'
        with patch.object(scrape.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0, stderr=b'')):
            error = scrape.capture_screenshot(stream(), 90, str(target))
        self.assertIn('without producing', error)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_capture_publishes_png_and_passes_headers_before_input(self):
        target = self.output / 'capture.png'

        def run(command, **kwargs):
            self.assertLess(command.index('-headers'), command.index('-i'))
            self.assertIn('User-Agent: browser\r\n', command)
            self.assertNotEqual(command[-1], str(target))
            Path(command[-1]).write_bytes(PNG)
            return subprocess.CompletedProcess(command, 0, stderr=b'')

        with patch.object(scrape.subprocess, 'run', side_effect=run):
            error = scrape.capture_screenshot(
                stream(http_headers={'User-Agent': 'browser'}), 90, str(target))
        self.assertIsNone(error)
        self.assertTrue(scrape.is_complete_png(target))
        self.assertEqual(list(self.output.iterdir()), [target])

    def test_failure_diagnostics_and_partial_file_cleanup(self):
        target = self.output / 'capture.png'

        def run(command, **kwargs):
            Path(command[-1]).write_bytes(PNG[:20])
            return subprocess.CompletedProcess(
                command, 1, stderr=b'HTTP error 403 for https://media.invalid/?token=secret\n')

        with patch.object(scrape.subprocess, 'run', side_effect=run):
            error = scrape.capture_screenshot(stream(), 90, str(target))
        self.assertIn('403', error)
        self.assertNotIn('secret', error)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_timeout_cleans_partial_file(self):
        def run(command, **kwargs):
            Path(command[-1]).write_bytes(PNG[:20])
            raise subprocess.TimeoutExpired(command, 120, stderr=b'Connection stalled')

        with patch.object(scrape.subprocess, 'run', side_effect=run):
            error = scrape.capture_screenshot(stream(), 90, str(self.output / 'frame.png'))
        self.assertIn('timed out', error)
        self.assertIn('Connection stalled', error)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_resume_captures_only_missing_timestamps(self):
        self.write_frame(timestamp=1530)
        info = dict(video(duration=1700), **stream())
        with patch.object(scrape, 'capture_screenshot', return_value=None) as capture:
            _, message, count = scrape.process_video_screenshots(
                info, self.output, processed_timestamps={'90': []})
        self.assertTrue(message.startswith('Processed'))
        self.assertEqual(count, 1)
        self.assertEqual(capture.call_args.args[1], 810)
        capture.assert_called_once()

    def test_fallback_is_reused_after_first_success(self):
        primary, fallback = stream(), stream('fallback', 'm3u8_native')
        info = dict(video(), **primary, formats=[fallback, primary])
        refresh = Mock()
        with patch.object(scrape, 'capture_screenshot',
                          side_effect=['HTTP 403', None, None]) as capture:
            _, message, count = scrape.process_video_screenshots(
                info, self.output, refresh_video=refresh)
        self.assertTrue(message.startswith('Processed'))
        self.assertEqual(count, 2)
        self.assertEqual([call.args[0]['format_id'] for call in capture.call_args_list],
                         ['primary', 'fallback', 'fallback'])
        refresh.assert_not_called()

    def test_failed_urls_are_refreshed_once(self):
        info = dict(video(), **stream('expired'))
        refresh = Mock(return_value=dict(info, **stream('fresh')))
        with patch.object(scrape, 'capture_screenshot',
                          side_effect=['HTTP 403', None, None]) as capture:
            _, message, count = scrape.process_video_screenshots(
                info, self.output, refresh_video=refresh)
        self.assertTrue(message.startswith('Processed'))
        self.assertEqual(count, 2)
        refresh.assert_called_once_with()
        self.assertEqual(capture.call_args.args[0]['format_id'], 'fresh')

    def test_failed_capture_is_reported_and_retry_is_bounded(self):
        info = dict(video(), **stream())
        refresh = Mock(return_value=info)
        with patch.object(scrape, 'capture_screenshot', return_value='HTTP 403') as capture:
            _, message, count = scrape.process_video_screenshots(
                info, self.output, refresh_video=refresh)
        self.assertTrue(message.startswith('Failed'))
        self.assertIn('2 screenshot(s) still missing', message)
        self.assertEqual(count, 0)
        self.assertEqual(capture.call_count, 2)
        refresh.assert_called_once()
        self.assertEqual(scrape.populateHaveSet(self.output, {'example': info}), set())

    def test_partial_success_is_still_reported_as_failed(self):
        info = dict(video(), **stream())
        with patch.object(scrape, 'capture_screenshot', side_effect=[None, 'error']):
            _, message, count = scrape.process_video_screenshots(info, self.output)
        self.assertTrue(message.startswith('Failed'))
        self.assertIn('1 screenshot(s) still missing', message)
        self.assertEqual(count, 1)

    def test_post_live_fragment_only_video_is_deferred(self):
        info = dict(video(live_status='post_live'),
                    **stream('dash', 'http_dash_segments'))
        with patch.object(scrape, 'capture_screenshot') as capture:
            _, message, count = scrape.process_video_screenshots(info, self.output)
        self.assertTrue(message.startswith('Deferred'))
        self.assertEqual(count, 0)
        capture.assert_not_called()
        self.assertEqual(scrape.populateHaveSet(self.output, {'example': info}), set())

    def test_unfinished_livestream_is_deferred(self):
        info = dict(video(live_status='is_live'), **stream())
        with patch.object(scrape, 'capture_screenshot') as capture:
            _, message, _ = scrape.process_video_screenshots(info, self.output)
        self.assertTrue(message.startswith('Deferred'))
        capture.assert_not_called()

    def test_pipeline_starts_captures_before_fetching_whole_channel(self):
        events = []
        first = dict(video('first', duration=91), **stream('first'))
        second = dict(video('second', duration=91), **stream('second'))
        infos = {'first': first, 'second': second}
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)

        def extract(url, download=False):
            if url == 'channel':
                return {'entries': [
                    {'_type': 'playlist', 'entries': [{'id': 'first'}]},
                    {'id': 'first'}, {'id': 'second'}]}
            video_id = url.rsplit('=', 1)[-1]
            events.append(('metadata', video_id))
            return infos[video_id]

        def capture(selected, timestamp, filename):
            events.append(('capture', selected['format_id']))
            Path(filename).write_bytes(PNG)
            return None

        client.extract_info.side_effect = extract
        with patch.object(scrape.yt_dlp, 'YoutubeDL', return_value=client), \
                patch.object(scrape, 'MAX_WORKERS', 1), \
                patch.object(scrape, 'capture_screenshot', side_effect=capture):
            self.assertTrue(scrape.get_channel_screenshots('channel', self.output))
            self.assertEqual(events, [('metadata', 'first'), ('capture', 'first'),
                                      ('metadata', 'second'), ('capture', 'second')])
            events.clear()
            self.assertTrue(scrape.get_channel_screenshots('channel', self.output))
            self.assertEqual(events, [])
        self.assertEqual(set(json.loads(self.database.read_text())), {'first', 'second'})

    def test_date_and_discovery_filters_use_full_metadata(self):
        infos = [
            dict(video('old', duration=91), upload_date='20200101', **stream('old')),
            dict(video('unrelated', duration=91), title='Another game', **stream('unrelated')),
        ]
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.extract_info.side_effect = [
            {'entries': [{'id': info['id']} for info in infos]}, *infos]
        with patch.object(scrape.yt_dlp, 'YoutubeDL', return_value=client), \
                patch.object(scrape, 'capture_screenshot') as capture:
            self.assertTrue(scrape.get_channel_screenshots(
                'channel', self.output, require_bar_relevance=True))
        capture.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_source_extraction_failure_is_not_reported_as_no_new_videos(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.extract_info.return_value = None
        with patch.object(scrape.yt_dlp, 'YoutubeDL', return_value=client):
            self.assertFalse(scrape.get_channel_screenshots('channel', self.output))


if __name__ == '__main__':
    unittest.main()
