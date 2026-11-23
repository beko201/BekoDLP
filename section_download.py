"""Download a seekable section while retaining the source timeline for final cuts."""
import math
import os
from pathlib import Path
import re

from errors import MediaOperationError
from utils import _network_options, safe_filesize, validate_trim_range


class SectionMetadata:
    def __init__(self, metadata, format_ids):
        self.metadata, self.format_ids = metadata, format_ids

    def snapshot(self, format_id=None):
        info, generation = self.metadata.snapshot()
        if info is None:
            raise MediaOperationError('Section download metadata is unavailable. Refresh the source and retry.')
        formats = info.get('formats') or []
        if self.format_ids:
            for identifier in self.format_ids:
                if sum(fmt.get('format_id') == identifier for fmt in formats) != 1:
                    raise MediaOperationError('The selected source format is unavailable. Inspect the source formats again.')
            formats = [fmt for fmt in formats if fmt.get('format_id') in self.format_ids]
        if not formats:
            raise MediaOperationError('Section download metadata is unavailable. Refresh the source and retry.')
        # MP4 edit lists and copyts retain original timestamps and decoder preroll.
        # This is an intermediate container, not an output codec conversion.
        for fmt in formats:
            fmt['ext'] = 'mp4'
        info['formats'] = formats
        info['ext'] = 'mp4'
        return info, generation

    def refresh(self, generation, cancel_event, stop_event):
        return self.metadata.refresh(generation, cancel_event, stop_event)


def download_section(preparer, tid, worker, url, info, selection, task, folder):
    engine = preparer.engine
    start, end = validate_trim_range(task.get('trim_start', ''), task.get('trim_end', ''))
    start = start or 0
    if task.get('download_rate'):
        raise MediaOperationError('Section downloads cannot enforce the speed limit. Remove the limit or select full download and trim.')
    if task.get('proxy_mode') == 'custom' and task.get('proxy_url', '').lower().startswith('socks'):
        raise MediaOperationError('Section downloads do not support SOCKS proxies. Select full download and trim to use this proxy.')
    if info.duration and start >= info.duration:
        raise MediaOperationError('The trim start must be earlier than the source duration.')
    if not engine.ffmpeg or not engine.ffprobe:
        raise MediaOperationError('Section downloading requires FFmpeg and FFprobe.')
    identifiers = [stream.format_id for stream in selection]
    if any(not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', identifier)
           for identifier in identifiers):
        raise MediaOperationError('This source format cannot be selected for a section. Select full download and trim.')
    if identifiers:
        selector = '+'.join(f'all[format_id="{identifier}"]' for identifier in identifiers)
    else:
        selector = preparer.selector_expression(task)
    if info.duration and end is not None:
        end = min(end, info.duration)
    expected = sum(safe_filesize(stream) for stream in selection)
    if expected and info.duration and end is not None:
        expected = math.ceil(expected * min(1, (end - start + 10) / info.duration))
    engine._check_disk_space(folder, expected)
    engine.manager.update_task(tid, status='Downloading selected section')
    engine.manager.set_log_stage(tid, 'download')
    def hook(data):
        if data.get('status') in ('downloading', 'finished'):
            finished = data['status'] == 'finished'
            downloaded = data.get('downloaded_bytes') or 0
            total = downloaded if finished else data.get('total_bytes') or 0
            engine._check_download_disk_space(tid, folder, downloaded, total)
            engine.manager.report_progress(tid, 'section', downloaded, total, force=finished)
    options = dict(format=selector, outtmpl=os.path.join(folder, 'section.%(ext)s'),
        download_section=(start, end), merge_output_format='mp4', quiet=False, noplaylist=True,
        progress_hooks=[hook], **_network_options(engine.manager.task_control(tid)[0]))
    streams = len(selection) or (2 if task['output_mode'] == 'video' and task['audio_codec'] != 'none' else 1)
    with engine.media_download_session(tid, streams=streams, fragmented=False) as network:
        options.update(network)
        with engine.ytdlp_process(options, tid, folder=folder) as process:
            engine._download_from_info(process, url, SectionMetadata(info._media_info, identifiers),
                                       engine.manager.task_control(tid)[0], tid=tid)
    engine._wait_for_download_resume(tid)
    path = Path(folder, 'section.mp4')
    if not path.is_file() or path.resolve().parent != Path(folder).resolve():
        raise MediaOperationError('The downloaded section could not be found.')
    return (str(path),)
