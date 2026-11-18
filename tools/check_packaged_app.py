"""Drive the real packaged Windows UI against local media with no system tools."""
from functools import partial
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils import NO_WINDOW, which_executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language', choices=('en', 'tr'))
    args = parser.parse_args()
    base = ROOT / 'build/packaged-media-check' / time.strftime('%Y%m%d-%H%M%S')
    base.mkdir(parents=True, exist_ok=True)
    fixture = base / 'sample.mp4'
    subprocess.run([which_executable(['ffmpeg']), '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=25', '-f', 'lavfi',
                    '-i', 'sine=frequency=440:sample_rate=48000', '-t', '3',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-movflags', '+faststart', str(fixture)], check=True, timeout=25, **NO_WINDOW)
    # A bare MP4 URL has no extractor-reported height. Use a normal HTML5
    # source with an explicit resolution so the app's quality cap can apply.
    (base / 'index.html').write_text('<html><head><title>Packaged check</title></head><body>'
        '<video controls><source src="sample.mp4" type="video/mp4" width="320" height="180"></video>'
        '</body></html>', encoding='utf-8')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
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
            executable = folder / 'BekoDLP-Windows-x64.exe'
            if not executable.exists():
                os.link(ROOT / 'dist/BekoDLP-Windows-x64.exe', executable)
            settings = {'schema_version': 1, 'language': language, 'last_folder': str(downloads)}
            if language == 'tr':
                settings.update(output_mode='audio', container='mp3', audio_codec='mp3', audio_bitrate='auto')
            (folder / 'yd_settings.json').write_text(json.dumps(settings), encoding='utf-8')
            config = folder / 'ui-check.json'
            values = {'executable': str(executable), 'language': language, 'dump_only': False,
                      'close_after': False, 'trim': language == 'tr',
                      'url': f'http://127.0.0.1:{server.server_port}/index.html',
                      'report': str(folder / 'controls.json')}
            config.write_text(json.dumps(values), encoding='utf-8')
            env = dict(os.environ, PATH=os.environ['SystemRoot'] + '/System32;' + os.environ['SystemRoot'],
                       QT_ACCESSIBILITY='1')
            for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_PLATFORM'):
                env.pop(key, None)
            process = subprocess.Popen([str(executable)], cwd=folder, env=env, **NO_WINDOW)
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
                    assert 1.8 < duration < 2.2, duration
                    assert any(stream['codec_name'] == 'mp3' for stream in probe['streams'])
                else:
                    assert 2.8 < duration < 3.2, duration
                    assert any(stream['codec_type'] == 'video' for stream in probe['streams'])
                startup = (folder / 'debug.log').read_text(encoding='utf-8')
                assert 'FFmpeg  : 9.0.2' in startup and 'Deno    : 2.9.8' in startup
                assert not (folder / 'errors.log').exists() or '[ERROR]' not in (folder / 'errors.log').read_text(encoding='utf-8')
                report.append({'language': language, 'output': output[0].name, 'duration': duration,
                               'trim': language == 'tr', 'system_tools_removed_from_PATH': True})
                values.update(dump_only=True, close_after=True)
                config.write_text(json.dumps(values), encoding='utf-8')
                closed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, **NO_WINDOW)
                if closed.returncode:
                    raise RuntimeError(closed.stderr)
                assert process.wait(timeout=20) == 0
                print('PASS: real packaged download, media validation and clean exit:', language, flush=True)
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
    print('Packaged checks passed:', len(report), flush=True)


if __name__ == '__main__':
    main()
