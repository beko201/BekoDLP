"""Chapter metadata and rollback-safe publication of completed chapter files."""
import os
from pathlib import Path
import tempfile

from chapter_options import EMBED_CHAPTER_EXTENSIONS, MAX_CHAPTERS, chapter_ranges
from errors import MediaOperationError
from media_probe import probe_duration
from errors import DownloadCancelled, InsufficientDiskSpace
from app_logging import log_warning


def _streams(probe):
    return [stream for stream in probe.get('streams', []) if not (
        probe.get('chapters') and stream.get('codec_type') == 'data' and stream.get('codec_name') == 'bin_data')]


def _signature(probe):
    return [(stream.get('codec_type'), stream.get('codec_name')) for stream in _streams(probe)]


def _escape(value):
    return value.replace('\\', '\\\\').replace('=', '\\=').replace(';', '\\;').replace('#', '\\#').replace('\r', '').replace('\n', '\\\n')


class ChapterProcessor:
    def __init__(self, engine):
        self.engine, self.manager = engine, engine.manager

    def check(self, tid):
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()

    def embed(self, tid, source, metadata, start=0):
        if Path(source).suffix.lower() not in EMBED_CHAPTER_EXTENSIONS:
            self.manager.add_warning(tid, 'Chapter markers are not supported by this output format; the media was preserved.')
            return
        try:
            self.check(tid)
            before = self.engine._ffprobe_media(source, fresh=True)
            info, _ = metadata.snapshot() if metadata else (None, None)
            chapters = chapter_ranges(info or {}, start, start + probe_duration(before))
            if not chapters:
                self.manager.add_warning(tid, 'No source chapters were available in this range; the normal output was kept.')
                return
            if len((info or {}).get('chapters') or []) > MAX_CHAPTERS:
                self.manager.add_warning(tid, 'Only the first 1000 source chapters were processed.')
            worker = self.manager.task_control(tid)[1]
            if worker is None:
                raise MediaOperationError('No worker was assigned to chapter processing.')
            with tempfile.TemporaryDirectory(prefix='.chapters-', dir=os.path.dirname(source)) as temporary:
                folder = Path(temporary)
                blocks = [';FFMETADATA1']
                for chapter in chapters:
                    blocks.append('[CHAPTER]\nTIMEBASE=1/1000\nSTART=' + str(round((chapter.start - start) * 1000))
                        + '\nEND=' + str(round((chapter.end - start) * 1000)) + '\ntitle=' + _escape(chapter.title))
                chapter_file = folder / 'chapters.ffmeta'
                chapter_file.write_text('\n'.join(blocks) + '\n', encoding='utf-8')
                output = folder / ('marked' + Path(source).suffix)
                command = [self.engine.ffmpeg, '-y', '-i', source, '-i', str(chapter_file)]
                for stream in _streams(before):
                    command += ['-map', f'0:{stream["index"]}']
                command += ['-map_metadata', '0', '-map_chapters', '1', '-c', 'copy']
                if Path(source).suffix.lower() in ('.mp4', '.m4a'):
                    command += ['-movflags', '+faststart']
                command += [str(output)]
                self.engine._check_disk_space(str(folder), os.path.getsize(source) * 1.1)
                self.engine._run_ffmpeg(tid, worker, command, duration=probe_duration(before),
                    status_label='Embedding chapter markers', progress_stage='verification')
                self.check(tid)
                after = self.engine._ffprobe_media(str(output), fresh=True)
                actual = after.get('chapters') or []
                if (len(actual) != len(chapters) or _signature(before) != _signature(after)
                        or abs(probe_duration(after) - probe_duration(before)) > .25):
                    raise MediaOperationError('Embedded chapter markers could not be verified.')
                for expected, result in zip(chapters, actual):
                    if (abs(float(result['start_time']) - (expected.start - start)) > .1 or
                            abs(float(result['end_time']) - (expected.end - start)) > .15 or
                            result.get('tags', {}).get('title') != expected.title):
                        raise MediaOperationError('Embedded chapter timing or titles could not be verified.')
                self.engine._replace_media(str(output), source)
        except (DownloadCancelled, InsufficientDiskSpace):
            raise
        except Exception as error:
            self.check(tid)
            self.manager.add_warning(tid, 'Chapter markers could not be embedded; the media was preserved.')
            log_warning('Chapter embedding failed', error, [('task', tid)])


def publish_chapter_folder(engine, tid, stage, parent, name):
    """Commit a chapter group, removing only files created by this attempt on error."""
    files = list(Path(stage).iterdir())
    if not files or any(not path.is_file() or path.is_symlink() for path in files):
        raise MediaOperationError('The chapter output folder is incomplete.')
    while True:
        if engine.manager.is_cancelled(tid):
            raise DownloadCancelled()
        destination = Path(engine._dedupe_path(str(Path(parent) / name), tid))
        try:
            destination.mkdir()
            break
        except FileExistsError:
            engine._release_output_reservation(str(destination), tid)
    created = []
    try:
        engine._check_disk_space(parent, sum(path.stat().st_size for path in files))
        for index, source in enumerate(files):
            if engine.manager.is_cancelled(tid):
                raise DownloadCancelled()
            target = destination / source.name
            try:
                os.link(source, target)
                created.append(target)
            except FileExistsError:
                raise
            except OSError:
                with target.open('xb') as output:
                    created.append(target)
                    with source.open('rb') as input_file:
                        while True:
                            if engine.manager.is_cancelled(tid):
                                raise DownloadCancelled()
                            block = input_file.read(1024 * 1024)
                            if not block:
                                break
                            output.write(block)
            engine.manager.report_stage_progress(tid, 'publishing', (index + 1) / len(files))
        if engine.manager.is_cancelled(tid):
            raise DownloadCancelled()
        return str(destination)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass  # Foreign files must never be removed during rollback.
        engine._release_output_reservation(str(destination), tid)
        raise
