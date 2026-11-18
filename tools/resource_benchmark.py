"""Offline before/after resource measurements; each scenario uses a fresh process.

python tools/resource_benchmark.py --output benchmark-results.json
CPU seconds measure this process (including the local HTTP server), not FFmpeg.
RSS/USS are sampled process memory, not allocation counts or an OS-wide peak.
"""
import argparse
from functools import partial
import gc
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run_scenario(name, seconds, count):
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    import psutil
    from PySide6.QtWidgets import QApplication
    from task_manager import TaskManager, TaskOptions
    from ui.task_controller import TaskController
    from ui.task_table import TaskTable
    import utils

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    process = psutil.Process()
    result = {'scenario': name}
    server_stop = threading.Event()

    def wait(milliseconds):
        deadline = time.monotonic() + milliseconds / 1000
        while time.monotonic() < deadline:
            app.processEvents()
            # Explicitly yield the GIL to downloader and local server threads.
            time.sleep(.005)

    def memory():
        info = process.memory_full_info()
        return {'rss_mib': round(info.rss / 2**20, 3),
                'uss_mib': round(info.uss / 2**20, 3)}

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def copyfile(self, source, output):
            try:
                while not server_stop.is_set() and (chunk := source.read(64 * 1024)):
                    output.write(chunk)
                    output.flush()
                    time.sleep(.05)
            except (ConnectionError, OSError):
                pass

    with tempfile.TemporaryDirectory(prefix='yd-resources-') as folder:
        utils.ERROR_LOG_FILE = str(Path(folder, 'errors.log'))
        utils.DEBUG_LOG_FILE = str(Path(folder, 'debug.log'))
        utils.FALLBACK_DEBUG_LOG_FILE = str(Path(folder, 'debug-fallback.log'))
        utils.FALLBACK_ERROR_LOG_FILE = str(Path(folder, 'fallback.log'))
        manager = TaskManager()
        controller = TaskController(manager)
        table = TaskTable()
        controller.tasks_changed.connect(table.update_tasks)
        controller.tasks_removed.connect(table.remove_tasks)
        table.resize(1100, 700)
        table.show()
        polls = [0]
        notifications = [0, 0]
        controller.timer.timeout.connect(lambda: polls.__setitem__(0, polls[0] + 1))

        def changed(first, last, roles):
            notifications[0] += 1
            notifications[1] += last.column() - first.column() + 1

        table.model.dataChanged.connect(changed)
        controller.start(1)
        options = TaskOptions(folder=folder)
        server = None
        try:
            wait(300)
            gc.collect()
            result['empty'] = memory()
            if name in ('queue', 'model', 'metadata'):
                manager.toggle_pause()
                if name == 'metadata':
                    for i in range(count):
                        url = f'https://example.com/{i}'
                        # Synthetic variable-sized JSON-like extractor payloads.
                        info = {'title': str(i), 'formats': [
                            {'format_id': str(j), 'url': f'https://example.com/{i}/{j}/' + 'x' * 4096}
                            for j in range(100)]}
                        manager.add_urls([url], options, resolved_info={url: info}, notify=False)
                    del info
                else:
                    manager.add_urls((f'https://example.com/{i}' for i in range(count)), options, notify=False)
                result['pending_events'] = memory()
                while not manager.events.empty():
                    controller.poll()
                wait(200)
                gc.collect()
                result['settled'] = memory()
                result['rows'] = table.model.rowCount()
                if name == 'metadata':
                    result['cached_metadata'] = len(manager._resolved_info)
            if name == 'active':
                source = Path(folder, 'tone.mp3')
                subprocess.run([manager.engine.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                                'sine=frequency=440:duration=300', '-b:a', '320k', str(source)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               **utils.NO_WINDOW)
                server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=folder))
                server.daemon_threads = False
                threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True).start()
                manager.add_urls([f'http://127.0.0.1:{server.server_port}/tone.mp3'],
                                 TaskOptions(folder=folder, format='MP3'))
                deadline = time.monotonic() + 15
                while manager.get_task('T1').get('downloaded_bytes', 0) < 65536:
                    wait(10)
                    if time.monotonic() > deadline:
                        raise RuntimeError(f'Download did not start: {manager.get_task("T1")}')
            polls[0] = 0
            notifications[:] = [0, 0]
            begin_cpu, begin = time.process_time(), time.perf_counter()
            peak = memory()
            if name == 'model':
                # Fresh snapshots with diagnostic status changes; visible labels
                # stay the same. Progress changes on alternate updates.
                for step in range(30):
                    tasks = []
                    for i in range(min(count, 100)):
                        task = manager.get_task(f'T{i + 1}')
                        task.update(status=f'Downloading - sample {step}', progress=step // 2,
                                    speed_bps=125000, eta_seconds=60)
                        tasks.append((task['id'], task))
                    table.update_tasks(tasks)
                    app.processEvents()
            else:
                deadline = begin + seconds
                while time.perf_counter() < deadline:
                    wait(50)
                    current = memory()
                    peak = {key: max(peak[key], current[key]) for key in peak}
            elapsed = time.perf_counter() - begin
            cpu = time.process_time() - begin_cpu
            result.update(elapsed_s=round(elapsed, 4), cpu_s=round(cpu, 4),
                          cpu_percent_one_core=round(100 * cpu / elapsed, 3),
                          timer_polls=polls[0], notifications=notifications[0],
                          notified_cells=notifications[1], sampled_peak=peak, end=memory())
            if name == 'active':
                task = manager.get_task('T1')
                result.update(downloaded_bytes=task.get('downloaded_bytes', 0), status=task['status'])
        finally:
            controller.begin_shutdown()
            deadline = time.monotonic() + 15
            while not manager.finish_shutdown():
                wait(10)
                if time.monotonic() > deadline:
                    raise RuntimeError('Shutdown timed out')
            controller.poll()
            table.close()
            if server:
                server_stop.set()
                server.shutdown()
                server.server_close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--scenario', choices=('idle', 'queue', 'model', 'active', 'metadata'))
    parser.add_argument('--seconds', type=float, default=3)
    parser.add_argument('--queue-size', type=int, default=10000)
    parser.add_argument('--repeat', type=int, default=3)
    args = parser.parse_args()
    if args.scenario:
        print(json.dumps(run_scenario(args.scenario, args.seconds, args.queue_size)))
        return
    results = []
    for scenario in ('idle', 'queue', 'model', 'active', 'metadata'):
        for _ in range(args.repeat):
            command = [sys.executable, str(Path(__file__).resolve()), '--scenario', scenario,
                       '--seconds', str(args.seconds), '--queue-size',
                       str(100 if scenario == 'metadata' else args.queue_size)]
            completed = subprocess.run(command, capture_output=True, text=True)
            if completed.returncode:
                raise RuntimeError(completed.stderr)
            result = json.loads(completed.stdout)
            results.append(result)
            print(json.dumps(result), flush=True)
            if args.output:
                args.output.write_text(json.dumps({'seconds': args.seconds, 'queue_size': args.queue_size,
                                                  'results': results}, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
