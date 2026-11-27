"""Verified tool staging and atomic selection without installing real runtimes."""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import app_paths
import tool_updates as updates
from task_manager import TaskManager, TaskOptions
from errors import DownloadCancelled
from runtime_tools import which_executable, portable_deno_path


def pe_payload():
    data = bytearray(256)
    data[:2] = b'MZ'
    data[60:64] = (64).to_bytes(4, 'little')
    data[64:70] = b'PE\x00\x00\x64\x86'
    return bytes(data)


def release_fixture(component='deno'):
    version = {'deno': '2.9.9', 'ytdlp': '2026.11.22', 'ffmpeg': '9.1'}[component]
    payload = pe_payload()
    if component != 'ytdlp':
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as bundle:
            for name in updates.SPECS[component][2]:
                member = f'ffmpeg-{version}-full_build/bin/{name}' if component == 'ffmpeg' else name
                bundle.writestr(member, payload)
        payload = buffer.getvalue()
    asset = updates.SPECS[component][1] or f'ffmpeg-{version}-full_build.zip'
    tag = 'v' + version if component == 'deno' else version
    url = f'https://github.com/{updates.SPECS[component][0]}/releases/download/{tag}/{asset}'
    data = {'tag_name': tag, 'assets': [{'name': asset, 'browser_download_url': url,
        'state': 'uploaded', 'size': len(payload), 'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()}]}
    return updates.parse_release(component, data), payload, data


class ToolUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'updates'
        self.root_patch = patch('app_paths.tool_updates_root', return_value=self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def staged(self, component='deno'):
        release, payload, _ = release_fixture(component)
        stage = updates.StagedToolUpdate(release)
        def download(url, *, output, **kwargs):
            output.write(payload)
            return hashlib.sha256(payload).hexdigest()
        with patch.object(updates, 'read_url', side_effect=download), \
                patch.object(updates, 'run_version', return_value=release.version):
            stage.prepare()
        return stage

    def test_all_components_verified_before_activation_and_resolution(self):
        for component in ('deno', 'ffmpeg', 'ytdlp'):
            with self.subTest(component=component):
                stage = self.staged(component)
                self.assertTrue(stage.ready)
                self.assertIsNone(app_paths.managed_tool_path(updates.SPECS[component][2][0][:-4]))
                stage.activate()
                for name in updates.SPECS[component][2]:
                    expected = str(stage.folder / name)
                    self.assertEqual(which_executable([name[:-4]]), expected)
                    if component == 'deno':
                        self.assertEqual(portable_deno_path(), expected)
                proof = json.loads((stage.folder / 'provenance.json').read_text())
                self.assertEqual(proof['sha256'], stage.release.sha256)
                self.assertEqual(set(proof['files']), set(updates.SPECS[component][2]))
                stage.discard()
                self.assertTrue(stage.folder.exists())

    def test_ffmpeg_pair_is_selected_together_and_missing_pair_falls_back(self):
        previous = self.staged('ffmpeg')
        previous.activate()
        next_stage = self.staged('ffmpeg')
        self.assertEqual(app_paths.managed_tool_path('ffprobe'), str(previous.folder / 'ffprobe.exe'))
        next_stage.activate()
        self.assertEqual(app_paths.managed_tool_path('ffmpeg'), str(next_stage.folder / 'ffmpeg.exe'))
        self.assertEqual(app_paths.managed_tool_path('ffprobe'), str(next_stage.folder / 'ffprobe.exe'))
        (next_stage.folder / 'ffprobe.exe').unlink()
        self.assertIsNone(app_paths.managed_tool_path('ffmpeg'))
        self.assertIsNone(app_paths.managed_tool_path('ffprobe'))
        self.assertTrue((previous.folder / 'ffmpeg.exe').is_file())

    def test_corrupt_download_and_cancel_preserve_active_selection(self):
        previous = self.staged()
        previous.activate()
        pointer = (self.root / 'active.json').read_bytes()
        for digest, error in [('0' * 64, ValueError), (None, DownloadCancelled)]:
            stage = updates.StagedToolUpdate(release_fixture()[0])
            def bad_download(url, *, output, **kwargs):
                output.write(b'broken')
                if digest is None:
                    raise DownloadCancelled()
                return digest
            with patch.object(updates, 'read_url', side_effect=bad_download):
                with self.assertRaises(error):
                    stage.prepare()
            self.assertFalse(stage.ready)
            stage.discard()
            self.assertFalse(stage.folder.exists())
            self.assertEqual((self.root / 'active.json').read_bytes(), pointer)

    def test_failed_atomic_commit_and_staged_corruption_preserve_previous(self):
        old = self.staged()
        old.activate()
        pointer = (self.root / 'active.json').read_bytes()
        stage = self.staged()
        original = Path.replace
        def fail_pointer(path, target):
            if Path(target).name == 'active.json':
                raise OSError('pointer locked')
            return original(path, target)
        with patch.object(Path, 'replace', fail_pointer):
            with self.assertRaises(OSError):
                stage.activate()
        self.assertEqual((self.root / 'active.json').read_bytes(), pointer)
        self.assertFalse(list(self.root.glob('.active-*')))
        (stage.folder / 'deno.exe').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            stage.activate()
        self.assertEqual((self.root / 'active.json').read_bytes(), pointer)

    def test_release_rejects_foreign_urls_missing_digest_prerelease_and_oversize(self):
        for component in updates.SPECS:
            _, _, original = release_fixture(component)
            for key, value in [('browser_download_url', 'https://example.com/tool.exe'), ('digest', None),
                               ('size', True), ('size', updates.MAX_DOWNLOAD + 1), ('state', 'new')]:
                data = json.loads(json.dumps(original))
                data['assets'][0][key] = value
                with self.subTest(component=component, key=key), self.assertRaises(ValueError):
                    updates.parse_release(component, data)
            with self.assertRaises(ValueError):
                updates.parse_release(component, {**original, 'prerelease': True})
        self.assertTrue(updates.is_newer('9.1', '9.0.2'))
        self.assertFalse(updates.is_newer('2.9.8', '2.10.0'))

    def test_archive_traversal_links_duplicates_and_bombs_are_rejected(self):
        release, _, _ = release_fixture()
        for name, symlink in [('../outside.exe', False), ('C:/outside.exe', False), ('/outside.exe', False),
                               ('link', True), ('deno.exe', False)]:
            with self.subTest(name=name):
                archive = self.root.parent / 'bad.zip'
                with zipfile.ZipFile(archive, 'w') as bundle:
                    bundle.writestr('deno.exe', pe_payload())
                    info = zipfile.ZipInfo(name)
                    if symlink:
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore', UserWarning)
                        bundle.writestr(info, b'bad')
                target = self.root.parent / 'extract'
                target.mkdir(exist_ok=True)
                with self.assertRaises(ValueError):
                    updates.extract_payload(archive, target, release, threading.Event())
                self.assertFalse(list(target.iterdir()))

    def test_windows_architecture_and_version_must_match(self):
        release, _, _ = release_fixture()
        folder = self.root.parent
        (folder / 'deno.exe').write_bytes(pe_payload())
        with patch.object(updates, 'run_version', return_value='1.0.0'):
            with self.assertRaises(ValueError):
                updates.validate_payload(folder, release, threading.Event())
        (folder / 'deno.exe').write_bytes(b'not PE')
        with self.assertRaises(ValueError):
            updates.validate_payload(folder, release, threading.Event())

    def test_pointer_cannot_escape_update_root(self):
        self.root.mkdir()
        for data in [{'deno': '../outside'}, {'deno': '/absolute'}, [], {'deno': 10}]:
            (self.root / 'active.json').write_text(json.dumps(data))
            self.assertIsNone(app_paths.managed_tool_path('deno'))

    def test_idle_guard_blocks_pending_active_resolvers_and_shutdown(self):
        from unittest.mock import Mock
        manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(manager.close)
        activate = Mock()
        self.assertTrue(manager.activate_tool_update(activate))
        tid = manager.add_urls(['https://example.com/video'], TaskOptions(folder=str(self.root.parent)))[0]
        manager.paused = True
        self.assertFalse(manager.activate_tool_update(activate))
        manager.update_task(tid, status='Completed', progress=100)
        manager.tid_to_workerid[tid] = 1
        self.assertFalse(manager.activate_tool_update(activate))
        manager.tid_to_workerid.clear()
        manager._pending_link_keys.add(('fixture', False))
        self.assertFalse(manager.activate_tool_update(activate))
        manager._pending_link_keys.clear()
        manager._closing = True
        self.assertFalse(manager.activate_tool_update(activate))
        self.assertEqual(activate.call_count, 1)

    def test_download_transport_limits_redirects_and_cancellation(self):
        class Response(io.BytesIO):
            headers = {'Content-Length': '100'}
            def geturl(self):
                return 'https://github.com/fixture'
        from unittest.mock import Mock
        opener = Mock()
        opener.open.return_value = Response(b'x' * 100)
        with patch.object(updates, 'build_opener', return_value=opener):
            with self.assertRaises(ValueError):
                updates.read_url('https://github.com/fixture', cancel=threading.Event(), limit=20)
        for url in ['http://github.com/a', 'https://github.com.evil.test/a', 'https://user:pass@github.com/a']:
            with self.assertRaises(ValueError):
                updates.trusted_url(url)
        cancelled = threading.Event()
        cancelled.set()
        opener.open.return_value = Response(b'x' * 100)
        with patch.object(updates, 'build_opener', return_value=opener), self.assertRaises(DownloadCancelled):
            updates.read_url('https://github.com/fixture', cancel=cancelled, limit=100)

    def test_latest_release_requests_json_instead_of_binary_media(self):
        from unittest.mock import Mock
        from urllib.error import HTTPError
        release, _, data = release_fixture('deno')
        url = 'https://api.github.com/repos/denoland/deno/releases/latest'
        payload = json.dumps(data).encode('utf-8')

        class Response(io.BytesIO):
            headers = {'Content-Length': str(len(payload))}
            def geturl(self):
                return url

        def serve(request, **kwargs):
            if request.get_header('Accept') != 'application/vnd.github+json':
                raise HTTPError(url, 415, 'Unsupported Media Type', {}, None)
            return Response(payload)

        opener = Mock()
        opener.open.side_effect = serve
        with patch.object(updates, 'build_opener', return_value=opener):
            self.assertEqual(updates.latest_release('deno', threading.Event()), release)

    def test_binary_download_keeps_octet_stream_accept_header(self):
        from unittest.mock import Mock
        url = 'https://github.com/fixture/tool.exe'
        payload = b'fixture executable'

        class Response(io.BytesIO):
            headers = {'Content-Length': str(len(payload))}
            def geturl(self):
                return url

        opener = Mock()
        opener.open.return_value = Response(payload)
        output = io.BytesIO()
        with patch.object(updates, 'build_opener', return_value=opener):
            digest = updates.read_url(url, cancel=threading.Event(), limit=100,
                                      expected=len(payload), output=output)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Accept'), 'application/octet-stream')
        self.assertEqual(output.getvalue(), payload)
        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
