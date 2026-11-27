"""Download task orchestration; the engine supplies shared services and state."""
import os
import tempfile
from engine_runtime import EngineRuntime
from component_ports import EngineOperations, TaskOperations
from task_state import TaskState
from output_compat import normalize_output
from errors import MediaOperationError
from cookie_options import cookie_selection, cookie_kwargs
from network_options import network_selection, network_kwargs
from naming_templates import NAMING_DEFAULTS, render_naming, ensure_naming_folder
from errors import DownloadCancelled
from file_utils import safe_filename, remove_leftovers, part_matcher
from time_utils import validate_trim_range
from app_logging import log_context
from engine_metadata import YTInfo
import engine_metadata


class DownloadTasks:
    def __init__(self, state: EngineRuntime, manager: TaskOperations, operations: EngineOperations):
        self.state = state
        self.manager = manager
        self.operations = operations

    def _process_task_attempt(self, task_tuple, worker_id: int):
        tid, url = task_tuple
        output_paths = []  # Track only output paths reserved for this task.
        output_complete = False
        work_dir = None
        task = self.manager.get_task(tid)
        if task is None:
            return
        task = normalize_output(task)
        output_folder = task["folder"]
        folder = output_folder
        trim_start = task.get("trim_start", "")
        trim_end = task.get("trim_end", "")
        try:
            if self.manager.is_cancelled(tid):
                self.manager.update_task(tid, state=TaskState.CANCELLED, status="Cancelled")
                return

            validate_trim_range(trim_start, trim_end)
            from media_options import validate_output_options
            validate_output_options(task)
            self.operations._wait_for_download_resume(tid)
            self.manager.update_task(tid, state=TaskState.RUNNING, status=f"Worker{worker_id}: Starting", progress=0)
            if not folder:
                raise MediaOperationError("No output folder was selected")

            # Isolate each attempt so reused task IDs cannot affect older or unrelated files.
            work_dir = tempfile.mkdtemp(prefix=f".yd-{tid}-", dir=output_folder)
            folder = work_dir

            resolved = self.manager.take_resolved_info(tid)
            yt = (YTInfo(url, resolved, cookie_selection(task), network_selection(task)) if resolved is not None else
                  engine_metadata.fetch_ytinfo_with_retry(url, cancel_event=self.manager.task_control(tid)[0], **cookie_kwargs(task), **network_kwargs(task)))
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

            self.manager.update_metadata(tid, duration=getattr(yt, 'duration', 0) or 0)
            raw_title = yt.title or "Unnamed"
            if (task.get('subtitle_mode', 'none') != 'none' or task.get('embed_cover', 'off') == 'on'
                    or task.get('chapter_mode', 'none') != 'none'):
                with self.state.resource_lock:
                    self.state._subtitle_context[tid] = {'metadata': yt._media_info, 'start': 0}
            title_safe = safe_filename(raw_title)
            if any(task.get(key, default) != default for key, default in NAMING_DEFAULTS.items()):
                folders, stem = render_naming(task, yt._media_info.info)
                with self.state.resource_lock:
                    self.state._output_naming[tid] = (folders, stem)
            self.manager.update_task(tid, title=raw_title)
            # Let the UI load the thumbnail asynchronously without blocking the download.
            self.manager.emit("thumbnail", tid, url=getattr(yt, 'thumbnail_url', None))

            from media_processing import ConfiguredOutput
            final_path = ConfiguredOutput(self.operations).process(tid, worker_id, url, yt, task, folder, title_safe)
            from chapter_options import ChapterBatch
            if isinstance(final_path, ChapterBatch):
                from chapter_processing import publish_chapter_folder
                batch = final_path
                stage = tempfile.mkdtemp(prefix='.chapter-output-', dir=folder)
                for part in batch.parts:
                    self.operations._record_subtitle_start(tid, part.start)
                    self.operations._publish_output(tid, part.path, stage, apply_naming=False)
                with self.state.resource_lock:
                    naming = self.state._output_naming.get(tid)
                parent = ensure_naming_folder(output_folder, naming[0]) if naming else output_folder
                final_path = publish_chapter_folder(self.operations, tid, stage, parent, batch.directory_name)
                output_paths.append(final_path)
                output_complete = True
                label = f'{len(batch.parts)} {batch.group_label}'
                self.manager.update_task(tid, state=TaskState.COMPLETED, status=f'Completed ({label})', progress=100, output_path=final_path)
                self.manager.notify(f'{raw_title} downloaded ({label}).')
                return
            final_path = self.operations._publish_output(tid, final_path, output_folder)
            output_paths.append(final_path)
            output_complete = True
            label = os.path.splitext(final_path)[1][1:].upper()
            self.manager.update_task(tid, state=TaskState.COMPLETED, status=f"Completed ({label})", progress=100, output_path=final_path)
            self.manager.notify(f"{raw_title} downloaded ({label}).")
            return

        except DownloadCancelled:
            self.manager.update_task(tid, state=TaskState.CANCELLED, status="Cancelled", progress=0)
            self.manager.notify(f"{tid} cancelled.")
        except Exception as e:
            if self.manager.is_cancelled(tid):
                self.manager.update_task(tid, state=TaskState.CANCELLED, status="Cancelled", progress=0)
            else:
                self.manager.fail_task(tid, str(e))
        finally:
            # Clean incomplete outputs after FFmpeg exits; preserve successful and unreserved files.
            with log_context(stage='cleanup'):
                if not output_complete:
                    for path in output_paths:
                        remove_leftovers(os.path.dirname(path) or folder,
                                         part_matcher(os.path.basename(path)))
                if work_dir is not None:
                    self.operations._cleanup_workspace(work_dir, output_folder)
            with self.state.resource_lock:
                self.state._subtitle_context.pop(tid, None)
                self.state._output_naming.pop(tid, None)
                for key, owner in list(self.state.reserved_output_paths.items()):
                    if owner == tid:
                        del self.state.reserved_output_paths[key]
