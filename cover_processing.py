"""Fetch source artwork and atomically embed it after all media/subtitle processing."""
import os
from pathlib import Path
import tempfile

from cover_options import SUPPORTED_COVER_EXTENSIONS, validate_cover_options
from errors import MediaOperationError
from media_probe import probe_duration
from errors import DownloadCancelled, InsufficientDiskSpace
from app_logging import log_warning


def artwork(stream):
    return bool(stream.get('disposition', {}).get('attached_pic')) or (
        stream.get('tags', {}).get('filename', '').lower() in ('cover.jpg', 'cover.jpeg', 'cover.png') and
        stream.get('tags', {}).get('mimetype', '').startswith('image/'))


def retained_streams(probe):
    return [stream for stream in probe.get('streams', []) if not artwork(stream) and not (
        probe.get('chapters') and stream.get('codec_type') == 'data' and stream.get('codec_name') == 'bin_data')]


def stream_signature(probe):
    return [(stream.get('codec_type'), stream.get('codec_name'), stream.get('sample_rate'), stream.get('channels'))
            for stream in retained_streams(probe)]


class CoverProcessor:
    def __init__(self, engine):
        self.engine, self.manager = engine, engine.manager

    def check(self, tid):
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()

    def prepare(self, tid, source, metadata=None):
        task = self.manager.get_task(tid) or {}
        if validate_cover_options(task) == 'off':
            return
        if Path(source).suffix.lower() not in SUPPORTED_COVER_EXTENSIONS:
            self.manager.add_warning(tid, 'Cover artwork is not supported by this output format; the media was preserved.')
            return
        try:
            self.check(tid)
            worker = self.manager.task_control(tid)[1]
            if worker is None:
                raise MediaOperationError('No worker was assigned to cover processing.')
            if metadata is None:
                from download_engine import fetch_ytinfo_with_retry
                from cookie_options import cookie_kwargs
                from network_options import network_kwargs
                metadata = fetch_ytinfo_with_retry(task['url'], cancel_event=self.manager.task_control(tid)[0],
                    **cookie_kwargs(task), **network_kwargs(task))._media_info
            info, _ = metadata.snapshot()
            if not info or not (info.get('thumbnail') or info.get('thumbnails')):
                self.manager.add_warning(tid, 'No cover image was available; the media was preserved.')
                return
            with tempfile.TemporaryDirectory(prefix='.cover-', dir=os.path.dirname(source)) as temporary:
                folder = Path(temporary)
                self.manager.report_stage_progress(tid, 'verification', status='Downloading cover artwork')
                with self.engine.media_download_session(tid, fragmented=False):
                    with self.engine.ytdlp_process({}, tid, folder=str(folder)) as process:
                        image = process.download_thumbnail(info, folder)
                if image is None:
                    self.manager.add_warning(tid, 'No cover image was available; the media was preserved.')
                    return
                self.check(tid)
                image_probe = self.engine._ffprobe_media(str(image))
                pictures = [stream for stream in image_probe.get('streams', []) if stream.get('codec_type') == 'video']
                if not pictures or not 0 < pictures[0].get('width', 0) * pictures[0].get('height', 0) <= 24000000:
                    raise MediaOperationError('The cover image dimensions are invalid or too large.')
                jpeg = folder / 'normalized.jpg'
                command = [self.engine.ffmpeg, '-y', '-i', str(image), '-map', '0:v:0', '-frames:v', '1',
                    '-vf', "scale='min(1024,iw)':'min(1024,ih)':force_original_aspect_ratio=decrease",
                    '-q:v', '2', str(jpeg)]
                self.engine._run_ffmpeg(tid, worker, command, status_label='Preparing cover artwork', progress_stage='verification')
                self.embed(tid, worker, source, jpeg, folder)
        except (DownloadCancelled, InsufficientDiskSpace):
            raise
        except Exception as error:
            self.check(tid)
            self.manager.add_warning(tid, 'Cover artwork could not be embedded; the media was preserved.')
            log_warning('Cover artwork processing failed', error, [('task', tid)])

    def embed(self, tid, worker, source, jpeg, folder):
        before = self.engine._ffprobe_media(source, fresh=True)
        streams = retained_streams(before)
        output = folder / ('covered' + Path(source).suffix)
        command = [self.engine.ffmpeg, '-y', '-i', source]
        matroska = Path(source).suffix.lower() in ('.mkv', '.mka')
        if not matroska:
            command += ['-i', str(jpeg)]
        for stream in streams:
            command += ['-map', f'0:{stream["index"]}']
        command += ['-map_metadata', '0', '-map_chapters', '0', '-c', 'copy']
        if matroska:
            attachments = sum(stream.get('codec_type') == 'attachment' for stream in streams)
            command += ['-attach', str(jpeg), f'-metadata:s:t:{attachments}', 'mimetype=image/jpeg',
                        f'-metadata:s:t:{attachments}', 'filename=cover.jpg']
        else:
            videos = sum(stream.get('codec_type') == 'video' for stream in streams)
            command += ['-map', '1:v:0', f'-disposition:v:{videos}', 'attached_pic',
                        f'-metadata:s:v:{videos}', 'title=Album cover', f'-metadata:s:v:{videos}', 'comment=Cover (front)']
        if Path(source).suffix.lower() == '.mp3':
            command += ['-id3v2_version', '3']
        if Path(source).suffix.lower() in ('.mp4', '.m4a'):
            command += ['-movflags', '+faststart']
        command += [str(output)]
        self.engine._check_disk_space(str(folder), os.path.getsize(source) * 1.1 + jpeg.stat().st_size)
        self.engine._run_ffmpeg(tid, worker, command, duration=probe_duration(before),
                               status_label='Embedding cover artwork', progress_stage='verification')
        self.check(tid)
        after = self.engine._ffprobe_media(str(output), fresh=True)
        covers = [stream for stream in after.get('streams', []) if artwork(stream)]
        if stream_signature(before) != stream_signature(after) or len(covers) != 1:
            raise MediaOperationError('The embedded cover or preserved media streams could not be verified.')
        if abs(probe_duration(before) - probe_duration(after)) > .15:
            raise MediaOperationError('The covered media duration could not be verified.')
        self.engine._replace_media(str(output), source)
