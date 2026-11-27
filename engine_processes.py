"""FFmpeg process ownership, shutdown and progress-pipe handling."""
import re
import subprocess
import time
import threading
from contextvars import copy_context
from queue import Queue, Empty
from engine_runtime import EngineRuntime
from component_ports import EngineOperations, TaskOperations
from task_state import TaskState
from ytdlp_process import kill_process_tree
from processing_limits import video_encoder, budget_command
from errors import MediaOperationError
from errors import DownloadCancelled
from process_utils import NO_WINDOW, psutil
from download_support import _check_cancelled
from app_logging import log_error, log_warning


class MediaProcesses:
    def __init__(self, state: EngineRuntime, manager: TaskOperations, operations: EngineOperations):
        self.state = state
        self.manager = manager
        self.operations = operations

    def _run_ffmpeg(self, tid, worker_id, cmd, duration=0, status_label="Processing", *, check=True,
                    progress_stage="processing", stop_event=None):
        def check_cancelled():
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

        def run(budget):
            self.operations._invalidate_media_probes(cmd[-1])
            try:
                return self.operations._run_ffmpeg_process(
                    tid, worker_id, budget_command(cmd, budget), duration, status_label,
                    check=check, progress_stage=progress_stage, stop_event=stop_event)
            finally:
                self.operations._invalidate_media_probes(cmd[-1])

        if video_encoder(cmd):
            with self.state.processing_limits.video_slot(
                    check_cancelled, lambda: self.manager.paused,
                    lambda: self.manager.update_task(tid, state=TaskState.WAITING, status="Waiting for video conversion slot")) as budget:
                return run(budget)
        return run(self.state.processing_limits.thread_budget())

    def _run_ffmpeg_process(self, tid, worker_id, cmd, duration=0, status_label="Processing", *, check=True,
                            progress_stage="processing", stop_event=None):
        """Register and control each FFmpeg process, always closing it on exit."""
        while self.manager.paused and not self.manager.is_cancelled(tid):
            _check_cancelled(stop_event)
            time.sleep(0.1)
        _check_cancelled(stop_event)
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()
        self.manager.report_stage_progress(tid, progress_stage, status=status_label)
        disk_folder = self.operations._check_ffmpeg_disk_space(tid, cmd, duration)
        proc = subprocess.Popen(
            [cmd[0], "-nostdin", *cmd[1:]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **NO_WINDOW)
        try:
            self.operations._register_process(proc)
            with self.state.resource_lock:
                self.state.current_process_pids[worker_id] = proc.pid
            tail = self.operations._pump_ffmpeg_progress(proc, tid, duration, status_label,
                                              disk_folder=disk_folder, progress_stage=progress_stage,
                                              stop_event=stop_event)
            returncode = proc.wait()
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            if returncode != 0:
                log_error("ERROR FFmpeg process", details=[("task", tid), ("worker", worker_id),
                          ("operation", status_label), ("exit code", returncode), ("command", repr(cmd)),
                          ("ffmpeg", "\n".join(tail))], event='ffmpeg_process_failed', component='download_engine')
                if check:
                    raise MediaOperationError(f"FFmpeg process failed ({returncode}): " + " ".join(tail[-5:]))
            if returncode == 0:
                self.manager.report_stage_progress(tid, progress_stage, 1)
            return returncode
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()  # Killing also terminates a suspended process.
                proc.wait()
            finally:
                try:
                    proc.stdout.close()
                finally:
                    with self.state.resource_lock:
                        self.state.managed_processes.discard(proc)
                        if self.state.current_process_pids.get(worker_id) == proc.pid:
                            self.state.current_process_pids.pop(worker_id, None)

    def _register_process(self, proc):
        with self.state.resource_lock:
            self.state.managed_processes.add(proc)
            stopping = self.state._force_stopping
        if stopping:
            # Covers a process created just as the shutdown snapshot was taken.
            try:
                if proc.poll() is None:
                    proc.kill()
            finally:
                raise DownloadCancelled()

    def _unregister_process(self, proc):
        with self.state.resource_lock:
            self.state.managed_processes.discard(proc)

    def terminate_managed_processes(self):
        """Kill only owned process handles; never look up a possibly reused PID."""
        with self.state.resource_lock:
            self.state._force_stopping = True
            processes = list(self.state.managed_processes)
        for proc in processes:
            try:
                if proc.poll() is None or getattr(proc, '_owned_process_tree', None) is not None:
                    kill_process_tree(proc)
            except OSError as error:
                log_warning("Could not terminate the managed subprocess", error, [("pid", proc.pid)])

    def _pump_ffmpeg_progress(self, proc, tid, duration, status_label, *, disk_folder=None,
                              progress_stage="processing", stop_event=None):
        """Check cancellation and pause regularly, even when FFmpeg produces no output."""
        output = Queue()
        tail = []
        read_errors = []
        time_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

        def read_output():
            try:
                for line in proc.stdout:
                    output.put(line)
            except Exception as error:
                read_errors.append(error)
            finally:
                output.put(None)

        reader = threading.Thread(target=copy_context().run, args=(read_output,), daemon=True)
        reader.start()
        suspended = False
        output_closed = False
        last_update = 0
        last_disk_check = 0
        try:
            while True:
                _check_cancelled(stop_event)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                now = time.monotonic()
                if disk_folder and now - last_disk_check >= 1:
                    self.operations._check_disk_space(disk_folder, 0)
                    last_disk_check = now
                if proc.poll() is None and self.manager.paused != suspended:
                    try:
                        process = psutil.Process(proc.pid)
                        if self.manager.paused:
                            process.suspend()
                            suspended = True
                            self.manager.update_task(tid, state=TaskState.PAUSED, status="Paused")
                        else:
                            process.resume()
                            suspended = False
                            self.manager.update_task(tid, state=TaskState.RUNNING, status=status_label)
                    except psutil.NoSuchProcess:
                        pass
                if output_closed:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                try:
                    line = output.get(timeout=0.1)
                except Empty:
                    continue
                if line is None:
                    if read_errors:
                        raise RuntimeError(f"Could not read FFmpeg output: {read_errors[0]}") from read_errors[0]
                    output_closed = True
                    continue
                tail.append(line.strip())
                del tail[:-40]
                m = time_re.search(line)
                now = time.monotonic()
                if m and duration > 0 and not self.manager.paused and now - last_update >= 0.2:
                    h, mm, ss = m.groups()
                    seconds = int(h) * 3600 + int(mm) * 60 + float(ss)
                    fraction = max(0, min(1, seconds / duration))
                    self.manager.report_stage_progress(
                        tid, progress_stage, fraction, status=f"{status_label}...")
                    last_update = now
            return tail
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.join(timeout=1)
