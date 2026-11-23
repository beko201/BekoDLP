"""Drive the real packaged Windows UI against local media with no system tools."""
from functools import partial
import argparse
import ctypes
from copy import deepcopy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils import NO_WINDOW, which_executable
from queue_store import QueueStore
from subtitle_processing import read_cues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language', choices=('en', 'tr'))
    parser.add_argument('--cookies', action='store_true', help='Require a synthetic cookie for every local media request.')
    parser.add_argument('--subtitles', action='store_true', help='Check embedded and separate subtitle outputs.')
    parser.add_argument('--network', action='store_true', help='Check direct routing and a saved per-task speed limit.')
    parser.add_argument('--sources', action='store_true', help='Inspect and queue explicit sources through the real format dialog.')
    parser.add_argument('--playlist', action='store_true', help='Preview a real three-video collection and download only position 2.')
    parser.add_argument('--section', action='store_true', help='Download a section and validate final media/subtitle timing.')
    args = parser.parse_args()
    if args.playlist and args.sources:
        parser.error('--playlist and --sources exercise separate submission flows; use one at a time.')
    if args.section and (args.cookies or args.network):
        parser.error('--section uses a loopback source without cookies or rate limiting; test those in full mode separately.')
    base = ROOT / 'build/packaged-media-check' / time.strftime('%Y%m%d-%H%M%S')
    base.mkdir(parents=True, exist_ok=True)
    fixture = base / 'sample.mp4'
    subprocess.run([which_executable(['ffmpeg']), '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=25', '-f', 'lavfi',
                    '-i', 'sine=frequency=440:sample_rate=48000', '-t', '3',
                    '-c:v', 'libx264', '-g', '25', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-movflags', '+faststart', str(fixture)], check=True, timeout=25, **NO_WINDOW)
    # A bare MP4 URL has no extractor-reported height. Use a normal HTML5
    # source with an explicit resolution so the app's quality cap can apply.
    track = '<track kind="subtitles" src="captions.vtt" srclang="en" label="English">' if args.subtitles else ''
    (base / 'captions.vtt').write_text('WEBVTT\n\n00:00:00.100 --> 00:00:00.400\nBefore trim\n\n'
        '00:00:01.000 --> 00:00:02.000\nPackaged subtitle\n\n00:00:02.300 --> 00:00:02.900\nLast caption\n', encoding='utf-8')
    (base / 'index.html').write_text('<html><head><title>Packaged check</title></head><body>'
        '<video controls><source src="sample.mp4" type="video/mp4" width="320" height="180">' + track + '</video>'
        '</body></html>', encoding='utf-8')
    (base / 'playlist.html').write_text('<html><head><title>Playlist check</title></head><body>' + ''.join(
        f'<video id="clip{number}" controls><source src="sample.mp4?item={number}" type="video/mp4" width="320" height="180">'
        + track + '</video>' for number in range(1, 4)) + '</body></html>', encoding='utf-8')
    class Handler(SimpleHTTPRequestHandler):
        def authorized(self):
            valid = 'session=packaged-cookie-fixture' in self.headers.get('Cookie', '')
            authenticated_requests.append(valid)
            if args.cookies and not valid:
                self.send_error(403)
                return False
            return True
        def do_GET(self):
            requested_paths.append(self.path)
            if self.authorized():
                super().do_GET()
        def do_HEAD(self):
            if self.authorized():
                super().do_HEAD()
        def log_message(self, *args):
            pass
    authenticated_requests = []
    requested_paths = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(base)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    report = []
    try:
        for language in ((args.language,) if args.language else ('en', 'tr')):
            folder = base / language
            folder.mkdir(exist_ok=True)
            downloads = folder / 'downloads'
            downloads.mkdir(exist_ok=True)
            executable = folder / 'BekoDLP.exe'
            if not executable.exists():
                shutil.copytree(ROOT / 'dist/BekoDLP', folder, dirs_exist_ok=True)
            settings = {'schema_version': 1, 'language': language, 'last_folder': str(downloads)}
            settings.update(trim_download_mode='section' if args.section else 'full')
            if args.section:
                settings.update(proxy_mode='direct')
            if args.network:
                settings.update(proxy_mode='direct', download_rate='1048576')
            if args.cookies:
                cookie_file = folder / 'fixture cookies.txt'
                cookie_bytes = (b'# Netscape HTTP Cookie File\n'
                    b'127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession\tpackaged-cookie-fixture\n')
                cookie_file.write_bytes(cookie_bytes)
                settings.update(cookie_mode='file', cookie_file=str(cookie_file))
            if language == 'tr':
                settings.update(output_mode='audio', container='mp3', audio_codec='mp3', audio_bitrate='auto')
            if args.subtitles:
                settings.update(subtitle_mode='files' if language == 'tr' else 'both', subtitle_languages='en')
            (folder / 'yd_settings.json').write_text(json.dumps(settings), encoding='utf-8')
            config = folder / 'ui-check.json'
            values = {'executable': str(executable), 'language': language, 'dump_only': False,
                      'close_after': False, 'trim': language == 'tr',
                      'url': f'http://127.0.0.1:{server.server_port}/index.html',
                      'report': str(folder / 'controls.json')}
            if args.section:
                values.update(trim=True, trim_start='1', trim_end='2')
            if args.cookies:
                values.update(expect_cookie_file=True, cookie_file=str(cookie_file))
            if args.subtitles:
                values.update(expect_subtitles=True)
            if args.sources:
                values.update(expect_sources=True)
            if args.playlist:
                values.update(expect_playlist=True, url=f'http://127.0.0.1:{server.server_port}/playlist.html')
            config.write_text(json.dumps(values), encoding='utf-8')
            env = dict(os.environ, PATH=os.environ['SystemRoot'] + '/System32;' + os.environ['SystemRoot'],
                       QT_ACCESSIBILITY='1')
            for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_PLATFORM'):
                env.pop(key, None)
            # Launch elsewhere to catch accidental dependencies on the working directory.
            process = subprocess.Popen([str(executable)], cwd=base, env=env, **NO_WINDOW)
            print('Checking real packaged UI:', language, flush=True)
            try:
                command = [powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                           '-File', str(ROOT / 'tools/check_windows_ui.ps1'), '-Config', str(config)]
                result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=85, **NO_WINDOW)
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                print(result.stdout.strip(), flush=True)
                extension = '*.mp3' if language == 'tr' else '*.mp4'
                deadline = time.monotonic() + 45
                output = []
                while time.monotonic() < deadline:
                    output = list(downloads.glob(extension))
                    if output:
                        break
                    if process.poll() is not None:
                        raise RuntimeError('The packaged application exited before download completion.')
                    time.sleep(.15)
                if not output:
                    errors = folder / 'errors.log'
                    raise RuntimeError(errors.read_text(encoding='utf-8')[-6000:] if errors.exists() else 'No output file was published.')
                probe = json.loads(subprocess.check_output([which_executable(['ffprobe']), '-v', 'error',
                    '-show_format', '-show_streams', '-of', 'json', str(output[0])], text=True, **NO_WINDOW))
                duration = float(probe['format']['duration'])
                if language == 'tr':
                    assert (.9 < duration < 1.15) if args.section else (1.8 < duration < 2.2), duration
                    assert any(stream['codec_name'] == 'mp3' for stream in probe['streams'])
                else:
                    assert (.9 < duration < 1.15) if args.section else (2.8 < duration < 3.2), duration
                    assert any(stream['codec_type'] == 'video' for stream in probe['streams'])
                if args.subtitles:
                    sidecar = output[0].with_suffix('.en.srt')
                    assert sidecar.is_file(), 'No subtitle sidecar was published.'
                    cues = read_cues(sidecar)
                    assert max(cue[1] for cue in cues) <= duration + .01
                    if language == 'tr':
                        assert abs(cues[0][0] - (0 if args.section else .5)) < .02, cues
                    else:
                        assert len([stream for stream in probe['streams'] if stream['codec_type'] == 'subtitle']) == 1
                startup = (folder / 'debug.log').read_text(encoding='utf-8')
                assert 'FFmpeg  : 9.0.2' in startup and 'Deno    : 2.9.8' in startup
                assert not (folder / 'errors.log').exists() or '[ERROR]' not in (folder / 'errors.log').read_text(encoding='utf-8')
                report.append({'language': language, 'output': output[0].name, 'duration': duration,
                               'trim': language == 'tr', 'system_tools_removed_from_PATH': True,
                               'working_directory_outside_installation': True})
                # Reuse the exact window found during input. Re-running UI
                # Automation discovery after download can wait on accessibility.
                window = json.loads(Path(str(values['report']) + '.window.json').read_text(encoding='utf-8-sig'))
                assert window['pid'] == process.pid
                hwnd = ctypes.c_void_p(window['handle'])
                owner = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                assert owner.value == process.pid
                if not ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0):
                    raise ctypes.WinError()
                assert process.wait(timeout=20) == 0
                print('PASS: real packaged download, media validation and clean exit:', language, flush=True)
                store = QueueStore(folder / 'yd_queue.json')
                document, warnings = store.load()
                assert not warnings and len(document['tasks']) == 1
                assert document['tasks'][0]['state'] == 'completed'
                assert Path(document['tasks'][0]['output_path']) == output[0]
                if args.section:
                    assert document['tasks'][0]['options']['trim_download_mode'] == 'section'
                    report[-1]['section_download_and_subtitle_timing'] = True
                if args.sources:
                    options = document['tasks'][0]['options']
                    assert options['source_selection'] == 'manual'
                    assert options['source_audio_id'] if language == 'tr' else options['source_video_id']
                    report[-1]['explicit_source_selection_saved'] = True
                if args.playlist:
                    assert document['tasks'][0]['url'].endswith('/sample.mp4?item=2')
                    media_requests = [path for path in requested_paths if path.startswith('/sample.mp4')]
                    assert media_requests and all(path == '/sample.mp4?item=2' for path in media_requests), media_requests
                    report[-1]['only_selected_playlist_position_downloaded'] = True
                if args.network:
                    assert document['tasks'][0]['options']['proxy_mode'] == 'direct'
                    assert document['tasks'][0]['options']['download_rate'] == '1048576'
                    report[-1]['network_options_saved'] = True
                if args.subtitles:
                    assert document['tasks'][0]['options']['subtitle_languages'] == 'en'
                    assert document['tasks'][0]['options']['subtitle_mode'] == ('files' if language == 'tr' else 'both')
                    report[-1]['subtitle_sidecar'] = sidecar.name
                    report[-1]['subtitles_embedded'] = language == 'en'
                if args.cookies:
                    assert document['tasks'][0]['options']['cookie_mode'] == 'file'
                    assert document['tasks'][0]['options']['cookie_file'] == str(cookie_file)
                    assert cookie_file.read_bytes() == cookie_bytes
                    for name in ('yd_settings.json', 'yd_queue.json', 'debug.log', 'errors.log'):
                        path = folder / name
                        if path.exists():
                            assert 'packaged-cookie-fixture' not in path.read_text(encoding='utf-8')
                    report[-1]['cookie_authenticated'] = True
                # Include a pending task to prove that restart does not silently
                # repeat a download or lose work when shutdown cancels workers.
                pending = deepcopy(document['tasks'][0])
                pending.update(id='T2', state='pending', status='', output_path=None, title='Pending restart check')
                document['tasks'].append(pending)
                store.save(document)
                original_files = {path.name: (path.stat().st_size, path.stat().st_mtime_ns)
                                  for path in downloads.iterdir() if path.is_file()}
                values.update(inspect_restored_queue=True, expected_task_id='T2', close_after=True)
                config.write_text(json.dumps(values), encoding='utf-8')
                process = subprocess.Popen([str(executable)], cwd=base, env=env, **NO_WINDOW)
                restored = subprocess.run(command, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=60, **NO_WINDOW)
                if restored.returncode:
                    raise RuntimeError(restored.stdout + restored.stderr)
                assert process.wait(timeout=20) == 0
                final, warnings = store.load()
                assert not warnings and [task['state'] for task in final['tasks']] == ['completed', 'pending']
                assert original_files == {path.name: (path.stat().st_size, path.stat().st_mtime_ns)
                                          for path in downloads.iterdir() if path.is_file()}
                report[-1]['queue_restored_paused'] = True
                print('PASS: real packaged queue persistence and restart:', language, flush=True)
            finally:
                if process.poll() is None:
                    import psutil
                    for child in psutil.Process(process.pid).children(recursive=True):
                        try:
                            child.terminate()
                        except psutil.NoSuchProcess:
                            pass
                    process.terminate()
    finally:
        server.shutdown()
        server.server_close()
    (base / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if args.cookies:
        assert authenticated_requests and all(authenticated_requests)
        print('PASS: authenticated cookie requests, unchanged source jars and no secret values in diagnostics', flush=True)
    print('Packaged checks passed:', len(report), flush=True)


if __name__ == '__main__':
    main()
