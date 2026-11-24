"""Workspace cleanup, output reservations and atomic media publication."""
import os
import time
import gc
import shutil
from naming_templates import ensure_naming_folder
from utils import DownloadCancelled, remove_leftovers, log_error


class MediaPublicationMixin:
    def _cleanup_workspace(self, work_dir, parent_folder):
        """Remove only our direct child workspace, retrying temporary filesystem locks."""
        target = os.path.realpath(os.path.abspath(work_dir))
        parent = os.path.realpath(os.path.abspath(parent_folder))
        if (os.path.normcase(os.path.dirname(target)) != os.path.normcase(parent)
                or not os.path.basename(target).startswith(".yd-")):
            log_error("Unsafe workspace cleanup was blocked", details=[("folder", work_dir)],
                      event='unsafe_cleanup_blocked', component='download_engine')
            return
        last_error = None
        for attempt in range(8):
            try:
                shutil.rmtree(target)
                return
            except FileNotFoundError:
                return
            except OSError as error:
                last_error = error
                if attempt < 7:
                    gc.collect()
                    time.sleep(0.3)
        log_error("WARNING: could not remove the workspace", details=[("folder", target), ("error", last_error)],
                  level='WARNING', event='workspace_cleanup_failed', component='download_engine')

    def _release_output_reservation(self, path, tid):
        key = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        with self.resource_lock:
            if self.reserved_output_paths.get(key) == tid:
                del self.reserved_output_paths[key]

    def _record_subtitle_start(self, tid, start):
        with self.resource_lock:
            self._subtitle_context.setdefault(tid, {})['start'] = max(0, start or 0)

    def _publish_output(self, tid, source, output_folder, *, apply_naming=True):
        task = self.manager.get_task(tid) or {}
        with self.resource_lock:
            naming = self._output_naming.get(tid)
            state = dict(self._subtitle_context.get(tid, {}))
        def finalize_media():
            if task.get('chapter_mode', 'none') == 'embed':
                from chapter_processing import ChapterProcessor
                ChapterProcessor(self).embed(tid, source, state.get('metadata'), state.get('start', 0))
            if task.get('embed_cover', 'off') == 'on':
                from cover_processing import CoverProcessor
                CoverProcessor(self).prepare(tid, source, state.get('metadata'))
        filename_options = {}
        bundle_options = {}
        if naming and apply_naming:
            folders, stem = naming
            output_folder = ensure_naming_folder(output_folder, folders)
            filename_options['filename'] = stem + os.path.splitext(source)[1]
            bundle_options['stem'] = stem
        if task.get('subtitle_mode', 'none') == 'none':
            finalize_media()
            return self._publish_media_file(tid, source, output_folder, **filename_options)
        from subtitle_processing import SubtitleProcessor, publish_bundle
        with self.resource_lock:
            state = dict(self._subtitle_context.get(tid, {}))
        with SubtitleProcessor(self).prepare(tid, source, state.get('metadata'), state.get('start', 0)) as sidecars:
            finalize_media()
            if sidecars:
                return publish_bundle(self, tid, source, output_folder, sidecars, **bundle_options)
            return self._publish_media_file(tid, source, output_folder, **filename_options)

    def _publish_media_file(self, tid, source, output_folder, *, filename=None):
        """Publish a completed output without overwriting any existing destination."""
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()
        self.manager.report_stage_progress(tid, "publishing", status="Kaydediliyor")
        source_size = os.path.getsize(source)
        desired = os.path.join(output_folder, filename or os.path.basename(source))
        while True:
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            destination = self._dedupe_path(desired, tid)
            created = False
            try:
                try:
                    # Create atomically without overwriting files created after the reservation.
                    os.link(source, destination)
                    created = True
                except FileExistsError:
                    raise
                except OSError:
                    # Use exclusive creation when hard links are unavailable to preserve existing files.
                    self._check_disk_space(output_folder, os.path.getsize(source))
                    with open(destination, "xb") as output:
                        created = True
                        with open(source, "rb") as input_file:
                            copied = 0
                            while True:
                                if self.manager.is_cancelled(tid):
                                    raise DownloadCancelled()
                                block = input_file.read(1024 * 1024)
                                if not block:
                                    break
                                output.write(block)
                                copied += len(block)
                                self.manager.report_stage_progress(
                                    tid, "publishing", copied / source_size if source_size else 0)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                self.manager.report_stage_progress(tid, "publishing", 1)
                return destination
            except FileExistsError:
                self._release_output_reservation(destination, tid)
                if created:
                    remove_leftovers(output_folder, lambda n: n == os.path.basename(destination))
                    raise
                # Another process created this destination; reserve a different name and retry.
            except BaseException:
                if created:
                    remove_leftovers(output_folder, lambda n: n == os.path.basename(destination))
                self._release_output_reservation(destination, tid)
                raise

    def _dedupe_path(self, path, tid):
        """Reserve a unique output path among parallel tasks and existing files."""
        base, ext = os.path.splitext(path)
        candidate = path
        suffix = 0

        with self.resource_lock:
            while True:
                key = os.path.normcase(
                    os.path.realpath(os.path.abspath(candidate))
                )
                if (
                    not os.path.lexists(candidate)
                    and key not in self.reserved_output_paths
                ):
                    self.reserved_output_paths[key] = tid
                    return candidate

                suffix += 1
                extra = f"_{tid}" if suffix == 1 else f"_{tid}_{suffix}"
                candidate = f"{base}{extra}{ext}"
