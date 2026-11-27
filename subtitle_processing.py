"""Download text tracks, synchronize clips, embed, and publish sidecars safely."""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import re
import tempfile

from errors import MediaOperationError
from media_probe import probe_duration
from subtitle_options import select_languages, language_patterns, validate_subtitle_options
from errors import DownloadCancelled, InsufficientDiskSpace
from app_logging import log_warning

MAX_SUBTITLE_BYTES = 16 * 1024 ** 2
_TIMES = re.compile(r'^(\d+:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d+:\d{2}:\d{2}[,.]\d{3})(?:\s.*)?$')


def _seconds(text):
    hours, minutes, seconds = text.replace(',', '.').split(':')
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def read_cues(path):
    with open(path, 'rb') as source:
        raw = source.read(MAX_SUBTITLE_BYTES + 1)
    if len(raw) > MAX_SUBTITLE_BYTES:
        raise MediaOperationError('Subtitle file exceeds the 16 MiB limit.')
    text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    cues = []
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = block.splitlines()
        for index, line in enumerate(lines[:2]):
            match = _TIMES.fullmatch(line.strip())
            if match:
                start, end = map(_seconds, match.groups())
                if end > start and lines[index + 1:]:
                    cues.append((start, end, '\n'.join(lines[index + 1:])))
                break
    if not cues:
        raise MediaOperationError('The subtitle file has no readable timed captions.')
    return cues


def clip_cues(cues, start, duration):
    stop = start + duration if duration and math.isfinite(duration) else math.inf
    return [(max(begin, start) - start, min(end, stop) - start, text)
            for begin, end, text in cues if end > start and begin < stop]


def write_cues(path, cues, format):
    def stamp(seconds):
        milliseconds = max(0, round(seconds * 1000))
        hours, remainder = divmod(milliseconds, 3600000)
        minutes, remainder = divmod(remainder, 60000)
        seconds, fraction = divmod(remainder, 1000)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}{"," if format == "srt" else "."}{fraction:03d}'
    blocks = [f'{i}\n{stamp(begin)} --> {stamp(end)}\n{text}\n'
              for i, (begin, end, text) in enumerate(cues, 1)]
    content = ('WEBVTT\n\n' if format == 'vtt' else '') + '\n'.join(blocks)
    Path(path).write_text(content, encoding='utf-8')


def _language_tag(language):
    base = language.split('-')[0].lower()
    return {'tr': 'tur', 'en': 'eng', 'de': 'deu', 'fr': 'fra', 'es': 'spa', 'it': 'ita',
            'pt': 'por', 'ru': 'rus', 'ar': 'ara', 'ja': 'jpn', 'ko': 'kor', 'zh': 'zho',
            'nl': 'nld', 'pl': 'pol', 'uk': 'ukr', 'hi': 'hin'}.get(base, base if len(base) == 3 else 'und')


class SubtitleProcessor:
    def __init__(self, engine):
        self.engine, self.manager = engine, engine.manager

    def check(self, tid):
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()

    @contextmanager
    def prepare(self, tid, source, metadata=None, start=0):
        task = self.manager.get_task(tid) or {}
        selected = validate_subtitle_options(task)
        worker = self.manager.task_control(tid)[1]
        if worker is None:
            raise MediaOperationError('No worker was assigned to subtitle processing.')
        with tempfile.TemporaryDirectory(prefix='.subs-', dir=os.path.dirname(source)) as temporary:
            folder = Path(temporary)
            sidecars = []
            try:
                self.check(tid)
                if metadata is None:
                    from download_engine import fetch_ytinfo_with_retry
                    from cookie_options import cookie_kwargs
                    from network_options import network_kwargs
                    metadata = fetch_ytinfo_with_retry(task['url'], cancel_event=self.manager.task_control(tid)[0],
                                                       **cookie_kwargs(task), **network_kwargs(task))._media_info
                info, _ = metadata.snapshot()
                if info is None:
                    raise MediaOperationError('Subtitle metadata is unavailable.')
                languages = select_languages(info, selected)
                if not languages:
                    self.manager.add_warning(tid, 'No subtitles were available for the selected languages.')
                else:
                    unavailable = [p for p in language_patterns(selected['subtitle_languages']) if p != 'all'
                                   and not any(lang.startswith(p[:-2]) if p.endswith('.*') else lang == p for lang in languages)]
                    if unavailable:
                        self.manager.add_warning(tid, 'Some requested subtitle languages were unavailable: ' + ', '.join(unavailable))
                    self.manager.report_stage_progress(tid, 'verification', status='Downloading subtitles')
                    with self.engine.media_download_session(tid, fragmented=False):
                        with self.engine.ytdlp_process({}, tid, folder=str(folder)) as ydl:
                            downloaded = ydl.download_subtitles(info, str(folder / 'captions.%(ext)s'), languages,
                                                                automatic=selected['subtitle_auto'] == 'on')
                    probe = self.engine._ffprobe_media(source, fresh=True)
                    duration = probe_duration(probe)
                    tracks = []
                    for language in languages:
                        record = downloaded.get(language) or {}
                        path = Path(record.get('filepath') or folder / f'captions.{language}.srt')
                        if (path.parent.resolve() != folder.resolve() or path.is_symlink()
                                or path.suffix.lower() != '.srt' or not path.is_file()):
                            self.manager.add_warning(tid, f'Subtitle track could not be saved: {language}')
                            continue
                        try:
                            cues = clip_cues(read_cues(path), start, duration)
                            if not cues:
                                self.manager.add_warning(tid, f'No subtitle captions fall inside this clip: {language}')
                                continue
                            normalized = folder / f'normalized.{language}.srt'
                            write_cues(normalized, cues, 'srt')
                            tracks.append((language, normalized))
                            if selected['subtitle_mode'] in ('files', 'both'):
                                sidecar = folder / f'sidecar.{language}.{selected["subtitle_format"]}'
                                write_cues(sidecar, cues, selected['subtitle_format'])
                                sidecars.append((language, sidecar))
                        except (OSError, ValueError, UnicodeError, MediaOperationError) as error:
                            self.manager.add_warning(tid, f'Subtitle track could not be read: {language}')
                            log_warning('Subtitle conversion failed', error, [('task', tid), ('language', language)])
                    if tracks and selected['subtitle_mode'] in ('embed', 'both'):
                        self.embed(tid, worker, source, probe, tracks, folder)
                    missing = [language for language in languages if language not in downloaded]
                    if missing:
                        self.manager.add_warning(tid, 'Some selected subtitle tracks were unavailable: ' + ', '.join(missing))
            except (DownloadCancelled, InsufficientDiskSpace):
                raise
            except Exception as error:
                self.check(tid)
                self.manager.add_warning(tid, 'Subtitles could not be fully processed; the media file was preserved.')
                log_warning('Subtitle processing failed', error, [('task', tid)])
            self.check(tid)
            yield sidecars

    def embed(self, tid, worker, source, before, tracks, folder):
        extension = Path(source).suffix.lower()
        codec = {'.mp4': 'mov_text', '.mkv': 'srt', '.webm': 'webvtt'}.get(extension)
        if codec is None or not any(s.get('codec_type') == 'video' for s in before['streams']):
            raise MediaOperationError('Subtitle embedding requires an MP4, MKV or WebM video.')
        output = folder / ('embedded' + extension)
        cmd = [self.engine.ffmpeg, '-y', '-i', str(source)]
        for _, path in tracks:
            cmd += ['-i', str(path)]
        cmd += ['-map', '0:v?', '-map', '0:a?', '-map_chapters', '0', '-c:v', 'copy', '-c:a', 'copy', '-c:s', codec]
        for index, (language, _) in enumerate(tracks):
            cmd += ['-map', f'{index + 1}:0', f'-metadata:s:s:{index}', 'language=' + _language_tag(language),
                    f'-metadata:s:s:{index}', 'title=' + language]
        if extension == '.mp4':
            cmd += ['-movflags', '+faststart']
        cmd.append(str(output))
        self.engine._check_disk_space(str(folder), os.path.getsize(source) * 1.1)
        self.engine._run_ffmpeg(tid, worker, cmd, duration=probe_duration(before),
                                status_label='Embedding subtitles', progress_stage='verification')
        self.check(tid)
        after = self.engine._ffprobe_media(str(output), fresh=True)
        av = lambda probe: sorted((s.get('codec_type'), s.get('codec_name')) for s in probe['streams']
                                  if s.get('codec_type') in ('video', 'audio'))
        subtitles = [s for s in after['streams'] if s.get('codec_type') == 'subtitle']
        expected_codec = 'subrip' if codec == 'srt' else codec
        if av(before) != av(after) or len(subtitles) != len(tracks) or any(s.get('codec_name') != expected_codec for s in subtitles):
            raise MediaOperationError('The embedded subtitle streams could not be verified.')
        self.engine._replace_media(str(output), source)


def publish_bundle(engine, tid, source, output_folder, sidecars, *, stem=None):
    """Publish media and matching sidecars as a group, rolling back owned files on failure."""
    original_stem, extension = os.path.splitext(os.path.basename(source))
    stem = stem if stem is not None else original_stem
    attempt = 0
    while True:
        if engine.manager.is_cancelled(tid):
            raise DownloadCancelled()
        name = stem + (f'_{tid}_{attempt}' if attempt else '') + extension
        destination = engine._dedupe_path(os.path.join(output_folder, name), tid)
        base = os.path.splitext(destination)[0]
        outputs = [(str(source), destination), *[(str(path), base + '.' + language + path.suffix)
                                                for language, path in sidecars]]
        reserved = [destination]
        created = []
        collision = False
        committed = False
        try:
            with engine.resource_lock:
                for _, target in outputs[1:]:
                    key = os.path.normcase(os.path.realpath(os.path.abspath(target)))
                    if os.path.lexists(target) or key in engine.reserved_output_paths:
                        collision = True
                        break
                    engine.reserved_output_paths[key] = tid
                    reserved.append(target)
            if collision:
                attempt += 1
                continue
            engine.manager.report_stage_progress(tid, 'publishing', status='Saving')
            for input_path, target in outputs:
                if engine.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                try:
                    os.link(input_path, target)
                    created.append(target)
                except FileExistsError:
                    raise
                except OSError:
                    engine._check_disk_space(output_folder, os.path.getsize(input_path))
                    with open(target, 'xb') as output:
                        created.append(target)
                        with open(input_path, 'rb') as incoming:
                            while block := incoming.read(1024 * 1024):
                                if engine.manager.is_cancelled(tid):
                                    raise DownloadCancelled()
                                output.write(block)
            if engine.manager.is_cancelled(tid):
                raise DownloadCancelled()
            engine.manager.report_stage_progress(tid, 'publishing', 1)
            committed = True
            return destination
        except FileExistsError:
            attempt += 1
        finally:
            # Only a returned, complete bundle is retained. Existing foreign files
            # never enter created and therefore can never be removed here.
            if not committed:
                for target in created:
                    Path(target).unlink(missing_ok=True)
            for target in reserved:
                engine._release_output_reservation(target, tid)
