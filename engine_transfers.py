"""Media transfers, shared metadata refresh and parallel-stream coordination."""
import os
import time
import threading
import traceback
import tempfile
from contextvars import copy_context
from ytdlp_process import YtDlpProcess
from errors import MediaOperationError
from cookie_options import cookie_selection
from network_options import network_selection
from utils import (
    DownloadCancelled,
    _check_cancelled,
    _network_options,
    safe_filesize,
    validate_trim_range,
    remove_leftovers,
    part_matcher,
    log_warning,
)
from engine_metadata import _is_expired_media_error


class MediaTransferMixin:
    def ytdlp_process(self, options, tid, *, stop_event=None, folder=None):
        paused_status = None
        def check():
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

        def tick():
            nonlocal paused_status
            task = self.manager.get_task(tid) or {}
            if self.manager.paused and paused_status is None:
                paused_status = task.get('status', 'Downloading')
                self.manager.update_task(tid, status='Paused')
            elif not self.manager.paused and paused_status is not None:
                if task.get('status') == 'Paused':
                    self.manager.update_task(tid, status=paused_status)
                paused_status = None
            if folder:
                self._check_disk_space(folder, 0)

        options = dict(options)
        task = self.manager.get_task(tid)
        options.update(cookie_selection(task))
        options.update(network_selection(task))
        if options['download_rate']:
            options['download_rate'] = str(max(1, int(options['download_rate']) // self._rate_divisor.get()))
            options['concurrent_fragment_downloads'] = 1
        options['ffmpeg_threads'] = self.processing_limits.thread_budget()
        if self.ffmpeg:
            options.setdefault('ffmpeg_location', self.ffmpeg)
        return YtDlpProcess(options, check=check, is_paused=lambda: self.manager.paused,
                            on_start=self._register_process, on_finish=self._unregister_process,
                            on_tick=tick)

    def media_download_session(self, tid, *, streams=1, fragmented=True, stop_event=None):
        if (self.manager.get_task(tid) or {}).get('download_rate'):
            fragmented = False
        return self.network_limits.session(
            lambda: self._wait_for_download_resume(tid, stop_event=stop_event),
            lambda: self.manager.update_task(tid, status="Waiting for download connection slot"),
            streams=streams, fragmented=fragmented)

    def _wait_for_download_resume(self, tid, *, stop_event=None):
        """Wait while paused before network work, continuing to check cancellation signals."""
        notified = False
        while True:
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            if not self.manager.paused:
                return
            if not notified:
                self.manager.update_task(tid, status="Paused")
                notified = True
            time.sleep(0.1)

    def _download_from_info(self, ydl, source_url, media_info, cancel_event, *,
                            format_id=None, stop_event=None, tid=None):
        _check_cancelled(cancel_event)
        _check_cancelled(stop_event)
        if tid is not None:
            self._wait_for_download_resume(tid, stop_event=stop_event)
        info, generation = media_info.snapshot(format_id) if media_info else (None, None)
        if info is None:
            if tid is not None and self.manager.get_task(tid).get('source_selection', 'auto') == 'manual':
                raise MediaOperationError('The selected source format is unavailable. Inspect the source formats again.')
            if format_id is None:
                return ydl.extract_info(source_url, download=True)
            return ydl.download([source_url])
        try:
            return ydl.process_ie_result(info, download=True)
        except Exception as error:
            _check_cancelled(cancel_event)
            _check_cancelled(stop_event)
            if not _is_expired_media_error(error):
                raise
            # Refresh once, shared by both streams. Preserve the original format
            # selection; an expired URL must never lift the resolution limit.
            if tid is not None:
                self._wait_for_download_resume(tid, stop_event=stop_event)
            media_info.refresh(generation, cancel_event, stop_event)
            refreshed, _ = media_info.snapshot(format_id)
            if refreshed is None:
                raise MediaOperationError("The selected format was not found in refreshed video metadata") from error
            _check_cancelled(cancel_event)
            _check_cancelled(stop_event)
            if tid is not None:
                self._wait_for_download_resume(tid, stop_event=stop_event)
            return ydl.process_ie_result(refreshed, download=True)

    def _download_format(self, tid, stream_key, source_url, yt_format, filename, *, folder, stop_event=None):
        """Download the selected format to the requested path.

        Use progress hooks to report download progress and check pause and
        cancellation signals.
        """
        self.manager.set_log_stage(tid, 'download')
        target_path = os.path.join(folder, filename)
        self.manager.register_download_streams(tid, {stream_key: safe_filesize(yt_format)})

        def check_cancelled():
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

        def hook(d):
            check_cancelled()
            if d.get('status') in ('downloading', 'finished'):
                finished = d.get('status') == 'finished'
                downloaded = d.get('downloaded_bytes') or 0
                total = downloaded if finished else d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                self._check_download_disk_space(tid, folder, downloaded, total)
                self.manager.report_progress(tid, stream_key, downloaded, total, force=finished)

        ydl_opts = {
            'quiet': True,
            'format': yt_format.format_id,
            'outtmpl': target_path,
            'progress_hooks': [hook],
            'noplaylist': True,
            'retries': 5,
            'fragment_retries': 5,
            'noprogress': True,
        }
        if self.manager.get_task(tid).get('source_selection', 'auto') == 'manual':
            # "all" keeps formats with unknown codecs too; the exact-ID filter
            # and the one-format metadata snapshot still select a single source.
            ydl_opts['format'] = f'all[format_id="{yt_format.format_id}"]'
        failed = None
        try:
            cancel_event, worker_id = self.manager.task_control(tid)
            if worker_id is None:
                raise RuntimeError("No worker was assigned to the download")
            _check_cancelled(cancel_event)
            self._wait_for_download_resume(tid, stop_event=stop_event)
            ydl_opts.update(_network_options(cancel_event, stop_event=stop_event))
            protocol = getattr(yt_format, 'protocol', '')
            fragmented = not protocol or any(kind in protocol for kind in ('m3u8', 'dash_segments', 'ism'))
            with self.media_download_session(tid, fragmented=fragmented, stop_event=stop_event) as network:
                ydl_opts.update(network)
                with self.ytdlp_process(ydl_opts, tid, stop_event=stop_event, folder=folder) as ydl:
                    self._download_from_info(ydl, source_url, getattr(yt_format, '_media_info', None),
                                             cancel_event, format_id=yt_format.format_id, stop_event=stop_event, tid=tid)
            check_cancelled()
        except BaseException as e:
            # Traceback frames can retain open yt-dlp files and block deletion on Windows.
            # Release those references here, then clean up after leaving the exception handler.
            try:
                traceback.clear_frames(e.__traceback__)
            except Exception as error:
                log_warning("Could not clear download traceback frames", error, [("task", tid)], once_key=("_download_format", tid))
            failed = e
        if failed is not None:
            # Parallel downloads are cleaned together after both threads exit.
            # Do not delay signalling the peer while retrying locked files here.
            if stop_event is None:
                remove_leftovers(folder, part_matcher(filename))
            raise failed
        size = os.path.getsize(target_path)
        self.manager.report_progress(tid, stream_key, size, size, force=True)
        return target_path

    def _download_streams_parallel(self, tid, source_url, video_stream, audio_stream, video_filename, audio_filename,
                                   *, folder):
        """Download video and audio concurrently, stopping the peer if either stream fails."""
        results = {}
        errors = []
        state_lock = threading.Lock()
        stop_event = threading.Event()
        self.manager.register_download_streams(tid, {
            "video": safe_filesize(video_stream), "audio": safe_filesize(audio_stream),
        })
        # Precompute both paths so cleanup includes downloads that fail before returning.
        expected_paths = {
            "video": os.path.join(folder, video_filename),
            "audio": os.path.join(folder, audio_filename),
        }

        def _dl(key, stream, filename):
            token = self._rate_divisor.set(2)
            try:
                path = self._download_format(tid, key, source_url, stream, filename,
                                             folder=folder, stop_event=stop_event)
                with state_lock:
                    results[key] = path
            except Exception as e:
                with state_lock:
                    # Store the original failure before the peer can report
                    # DownloadCancelled in response to this signal.
                    errors.append(e)
                    stop_event.set()

            finally:
                self._rate_divisor.reset(token)

        t_video = threading.Thread(target=copy_context().run,
                                   args=(_dl, "video", video_stream, video_filename), daemon=True)
        t_audio = threading.Thread(target=copy_context().run,
                                   args=(_dl, "audio", audio_stream, audio_filename), daemon=True)
        started = []
        try:
            for thread in (t_video, t_audio):
                thread.start()
                started.append(thread)
        except BaseException as error:
            with state_lock:
                # Record the launch failure before the running peer can
                # respond with DownloadCancelled and hide the original cause.
                errors.append(error)
                stop_event.set()
        finally:
            # Never clean up a workspace while a successfully started stream
            # still owns files in it. An unstarted thread cannot be joined.
            for thread in started:
                thread.join()

        if errors:
            # Include partial files from failed streams, even if no path was returned.
            for p in expected_paths.values():
                remove_leftovers(folder, part_matcher(os.path.basename(p)))
            raise errors[0]

        return results["video"], results["audio"]

    def _complete_via_selector(self, tid, worker_id, url, title_safe, raw_title, fmt,
                               target_height, force_h264, trim_start, trim_end, *, folder,
                               output_folder=None, media_info=None):
        validate_trim_range(trim_start, trim_end)
        output_folder = folder if output_folder is None else output_folder
        work_dir = tempfile.mkdtemp(prefix=f".yd-{tid}-", dir=folder)
        try:
            return self._complete_via_selector_in_workspace(
                tid, worker_id, url, title_safe, raw_title, fmt, target_height, force_h264,
                trim_start, trim_end, folder=work_dir, output_folder=output_folder, media_info=media_info)
        finally:
            self._cleanup_workspace(work_dir, folder)

    def _complete_via_selector_in_workspace(self, tid, worker_id, url, title_safe, raw_title, fmt,
                                            target_height, force_h264, trim_start, trim_end, *, folder,
                                            output_folder, media_info=None):
        """Use yt-dlp selection when native format classification finds no suitable streams.

        Apply the requested MP3 conversion or H.264 compatibility checks and
        optional trimming. Clean incomplete outputs on failure or cancellation.
        """
        self.manager.set_log_stage(tid, 'download')
        validate_trim_range(trim_start, trim_end)
        if not self.ffmpeg or not self.ffprobe:
            raise MediaOperationError("ffmpeg/ffprobe were not found; processing is unavailable")

        self._check_disk_space(folder, 0)
        audio_only = (fmt == "MP3")
        prefix = f"{title_safe}_{tid}"
        outtmpl = os.path.join(folder, prefix + ".%(ext)s")

        if audio_only:
            fmt_sel = "ba/b"
        else:
            h = f"[height<={target_height}]" if target_height else ""
            if force_h264:
                fmt_sel = f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/bv*{h}+ba/b{h}"
            else:
                fmt_sel = f"bv*{h}+ba/b{h}"

        def hook(d):
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            if d.get('status') in ('downloading', 'finished'):
                finished = d.get('status') == 'finished'
                key = (d.get('info_dict') or {}).get('format_id') or d.get('filename') or 'sel'
                downloaded = d.get('downloaded_bytes') or 0
                total = downloaded if finished else d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                self._check_download_disk_space(tid, folder, downloaded, total)
                self.manager.report_progress(tid, key, downloaded, total, force=finished)

        def pp_hook(d):
            if d.get('status') == 'started':
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                self.manager.report_stage_progress(
                    tid, "processing", status=f"Worker{worker_id}: Processing (merging)")

        ydl_opts = {
            'quiet': True,
            'format': fmt_sel,
            'outtmpl': outtmpl,
            'progress_hooks': [hook],
            'postprocessor_hooks': [pp_hook],
            'noplaylist': True,
            'retries': 5,
            'fragment_retries': 5,
            'noprogress': True,
            'merge_output_format': 'mp4/mkv',
        }
        if self.ffmpeg and os.path.isabs(self.ffmpeg):
            ydl_opts['ffmpeg_location'] = self.ffmpeg
        cancel_event = self.manager.task_control(tid)[0]
        _check_cancelled(cancel_event)
        ydl_opts.update(_network_options(cancel_event))

        cleanup = []  # Remove these generated outputs on failure or cancellation.

        def purge():
            remove_leftovers(folder, lambda n: n.startswith(prefix + "."))
            for pth in cleanup:
                if pth:
                    remove_leftovers(os.path.dirname(pth) or folder,
                                     lambda n, b=os.path.basename(pth): n == b)

        failed = None
        try:
            self._wait_for_download_resume(tid)
            with self.media_download_session(tid, streams=1 if audio_only else 2) as network:
                ydl_opts.update(network)
                with self.ytdlp_process(ydl_opts, tid, folder=folder) as ydl:
                    info = self._download_from_info(ydl, url, media_info, cancel_event, tid=tid)
            if (info or {}).get("duration"):
                self.manager.update_metadata(tid, duration=info["duration"])

            src = None
            rd = (info or {}).get('requested_downloads') or []
            if rd:
                src = rd[0].get('filepath') or rd[0].get('_filename')
            if not src or not os.path.exists(src):
                # Find the final output by prefix, excluding intermediate format files.
                for f in os.listdir(folder):
                    if f.startswith(prefix + ".") and "." not in f[len(prefix) + 1:] \
                            and not f.endswith((".part", ".ytdl", ".temp")):
                        src = os.path.join(folder, f)
                        break
            if not src or not os.path.exists(src):
                raise MediaOperationError("The downloaded file was not found")
            if os.path.dirname(os.path.realpath(src)) != os.path.realpath(folder):
                raise RuntimeError("The downloaded file is outside the task workspace")
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

            if audio_only:
                out = self._dedupe_path(os.path.join(folder, f"{title_safe}.mp3"), tid)
                cleanup.append(out)
                self.manager.update_task(tid, status=f"Worker{worker_id}: Converting")
                self._ffmpeg_convert_to_mp3(tid, worker_id, src, out)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                try:
                    os.remove(src)
                except Exception as error:
                    log_warning("Could not remove the temporary source", error, [("task", tid), ("file", src)], once_key=("source_cleanup", tid))
                out = self._maybe_trim(tid, worker_id, out, trim_start, trim_end)
                label = "MP3"
            else:
                ext = os.path.splitext(src)[1].lstrip('.').lower() or 'mp4'
                if force_h264:
                    ext = 'mp4'
                out = self._dedupe_path(os.path.join(folder, f"{title_safe}.{ext}"), tid)
                cleanup.append(out)
                self._replace_media(src, out)
                label = "MP4/H.264" if force_h264 else ext.upper()
                out = self._maybe_trim(tid, worker_id, out, trim_start, trim_end)
                if force_h264:
                    self._ensure_h264_compatible(tid, worker_id, out)
            out = self._publish_output(tid, out, output_folder)
        except BaseException as e:
            try:
                traceback.clear_frames(e.__traceback__)
            except Exception as error:
                log_warning("Could not clear download traceback frames", error, [("task", tid)], once_key=("selector_frames", tid))
            failed = e
        if failed is not None:
            purge()  # Clean up outside the exception handler after releasing traceback-held files.
            raise failed

        self.manager.update_task(tid, status=f"Completed ({label})", progress=100, output_path=out)
        self.manager.notify(f"{raw_title} downloaded ({label}).")
