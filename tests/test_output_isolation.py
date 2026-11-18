from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import test_task_cleanup as cleanup_fixture


downloader = cleanup_fixture.downloader


class OutputIsolationTests(unittest.TestCase):
    def test_parallel_same_title_tasks_and_instances_never_share_workspaces_or_outputs(self):
        for mode in ('mp3', 'merge', 'progressive'):
            for shared_app in (True, False):
                with self.subTest(mode=mode, shared_app=shared_app), tempfile.TemporaryDirectory() as temp:
                    folder = Path(temp)
                    originals = {name: name.encode() for name in (
                        'Example.mp3', 'Example.mp4', 'Example_T1.mp3', 'Example_T2.mp3',
                        'Example_T1.mp4', 'Example_T2.mp4', 'Example_T1.m4a',
                        'Example_video_T1.mp4', 'Example_audio_T1.m4a', 'Example_T1.mp4.part')}
                    for name, data in originals.items():
                        (folder / name).write_bytes(data)
                    fmt = 'MP3' if mode == 'mp3' else 'MP4'
                    first = cleanup_fixture.TaskCleanupTests.make_app(self, folder, fmt)
                    if shared_app:
                        first.tasks['T2'] = {**first.tasks['T1'], 'url': 'https://example.com/two'}
                        first.cancel_events['T2'] = threading.Event()
                        apps, tids = [first, first], ['T1', 'T2']
                    else:
                        apps = [first, cleanup_fixture.TaskCleanupTests.make_app(self, folder, fmt)]
                        tids = ['T1', 'T1']
                    barrier = threading.Barrier(2)
                    download_folders = set()
                    guard = threading.Lock()
                    for app_index, app in enumerate(dict.fromkeys(apps)):
                        publish = app._publish_output

                        def publish_together(tid, source, target, publish=publish):
                            barrier.wait(5)
                            return publish(tid, source, target)

                        def download(tid, key, url, stream, filename, *, folder, stop_event=None, tag=app_index):
                            private = Path(folder)
                            self.assertNotEqual(private, Path(temp))
                            self.assertEqual(private.parent, Path(temp))
                            with guard:
                                download_folders.add(private)
                            path = private / filename
                            path.write_bytes(f'{tag}-{tid}-{key}'.encode())
                            # Even unlisted yt-dlp artifacts and nested files must be removed.
                            debris = private / f'{key}-debris'
                            debris.mkdir(exist_ok=True)
                            (debris / 'fragment.part').write_bytes(b'fragment')
                            return str(path)

                        def convert(tid, wid, cmd, *args, tag=app_index, **kwargs):
                            self.assertNotEqual(Path(cmd[-1]).parent, Path(temp))
                            Path(cmd[-1]).write_bytes(f'{tag}-{tid}-converted'.encode())
                            return 0

                        app._publish_output = publish_together
                        app._download_format = download
                        app._run_ffmpeg = convert
                    info = cleanup_fixture.TaskCleanupTests.info(self, mode)
                    with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=info):
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            futures = [pool.submit(app._process_task, (tid, app.tasks[tid]['url']), index + 1)
                                       for index, (app, tid) in enumerate(zip(apps, tids))]
                            for future in futures:
                                future.result(timeout=10)
                    self.assertEqual(len(download_folders), 2)
                    for app in apps:
                        app._task_error.assert_not_called()
                    outputs = [Path(app.tasks[tid]['output_path']) for app, tid in zip(apps, tids)]
                    self.assertNotEqual(*outputs)
                    self.assertNotEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
                    for path in outputs:
                        self.assertEqual(path.parent, folder)
                    for name, data in originals.items():
                        self.assertEqual((folder / name).read_bytes(), data)
                    self.assertEqual({p.name for p in folder.iterdir()}, set(originals) | {p.name for p in outputs})

    def test_external_file_created_after_reservation_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            app = cleanup_fixture.TaskCleanupTests.make_app(self, folder)
            private = Path(tempfile.mkdtemp(prefix='.yd-T1-', dir=temp))
            source = private / 'Example.mp3'
            source.write_bytes(b'new output')
            actual_link = downloader.os.link
            attempts = []

            def race(src, dst):
                attempts.append(dst)
                if len(attempts) == 1:
                    Path(dst).write_bytes(b'external output')
                return actual_link(src, dst)

            with patch.object(downloader.os, 'link', side_effect=race):
                result = app._publish_output('T1', str(source), temp)
            self.assertEqual((folder / 'Example.mp3').read_bytes(), b'external output')
            self.assertEqual(Path(result).read_bytes(), b'new output')
            self.assertNotEqual(Path(result), folder / 'Example.mp3')
            self.assertEqual(len(attempts), 2)

    def test_copy_fallback_also_preserves_external_file_created_after_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp))
            private = Path(tempfile.mkdtemp(prefix='.yd-T1-', dir=temp))
            source = private / 'Example.mp3'
            source.write_bytes(b'new output')
            actual_open = open
            attempts = []

            def race(path, mode='r', *args, **kwargs):
                if mode == 'xb':
                    attempts.append(path)
                    if len(attempts) == 1:
                        with actual_open(path, 'wb') as external:
                            external.write(b'external output')
                return actual_open(path, mode, *args, **kwargs)

            with patch.object(downloader.os, 'link', side_effect=OSError('hard links unavailable')):
                with patch('builtins.open', side_effect=race):
                    result = app._publish_output('T1', str(source), temp)
            self.assertEqual(Path(temp, 'Example.mp3').read_bytes(), b'external output')
            self.assertEqual(Path(result).read_bytes(), b'new output')
            self.assertEqual(len(attempts), 2)

    def test_copy_cancellation_removes_only_its_partial_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            app = cleanup_fixture.TaskCleanupTests.make_app(self, folder)
            private = Path(tempfile.mkdtemp(prefix='.yd-T1-', dir=temp))
            source = private / 'Example.mp3'
            source.write_bytes(b'x' * (2 * 1024 * 1024))
            original = folder / 'Example.mp3'
            original.write_bytes(b'original')
            app._is_cancelled = Mock(side_effect=[False, False, True])
            with patch.object(downloader.os, 'link', side_effect=OSError('hard links unavailable')):
                with self.assertRaises(downloader.DownloadCancelled):
                    app._publish_output('T1', str(source), temp)
            self.assertEqual(original.read_bytes(), b'original')
            self.assertEqual(source.stat().st_size, 2 * 1024 * 1024)
            self.assertEqual(set(folder.iterdir()), {private, original})
            self.assertEqual(app.reserved_output_paths, {})

    def test_selector_rejects_source_outside_its_workspace_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp))
            original = Path(temp, 'Example_T1.mp4')
            original.write_bytes(b'original')
            fake = Mock()
            fake.__enter__ = Mock(return_value=fake)
            fake.__exit__ = Mock(return_value=False)
            fake.extract_info.return_value = {'requested_downloads': [{'filepath': str(original)}]}
            with patch.object(downloader, 'ManagedYoutubeDL', return_value=fake):
                with self.assertRaisesRegex(RuntimeError, 'outside the task workspace'):
                    app._complete_via_selector('T1', 1, 'url', 'Example', 'Example', 'MP4',
                                               1080, False, '', '', folder=temp)
            self.assertEqual(original.read_bytes(), b'original')
            self.assertEqual(list(Path(temp).iterdir()), [original])

    def test_failed_attempts_use_new_workspaces_and_remove_unlisted_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp))
            workspaces = []

            def fail(tid, wid, cmd, *args, **kwargs):
                private = Path(cmd[-1]).parent
                workspaces.append(private)
                (private / 'unlisted.part').write_bytes(b'partial')
                raise RuntimeError('conversion failed')

            app._run_ffmpeg = fail
            info = cleanup_fixture.TaskCleanupTests.info(self, 'mp3')
            with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=info):
                for _ in range(2):
                    app._process_task(('T1', app.tasks['T1']['url']), 1)
                    self.assertEqual(list(Path(temp).iterdir()), [])
            self.assertEqual(len(set(workspaces)), 2)


if __name__ == '__main__':
    unittest.main()
