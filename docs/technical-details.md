# Technical details

This project was developed with AI using Claude and ChatGPT.

Implementation and maintenance reference for BekoDLP. For everyday use, see the [README](../README.md). The Windows release runs without a separate software installation.

For Python libraries, external tools, and upstream links, see [libraries and sources](libraries-and-sources.md).

## Architecture

| Path | Responsibility |
| --- | --- |
| `main.py` | Application startup, theme, and exception reporting |
| `ui/` | Qt windows, output controls, task model/view, controller, preferences, previews, translations, and file actions |
| `task_manager.py` | Task options, link resolution, worker pool, events, retries, and shutdown |
| `download_engine.py` | yt-dlp integration, managed processes, progress, trimming, disk checks, and output publication |
| `media_options.py` | Codec/container profiles and option validation |
| `media_sources.py` | Source selection, downloading, and inspection of actual media |
| `media_plan.py` | Immutable source/output models and stream copy, encode, or drop decisions |
| `media_processing.py` | FFmpeg command construction, execution, and output verification |
| `media_probe.py` | Duration parsing and per-attempt FFprobe cache |
| `processing_limits.py`, `network_limits.py` | Video-encoding capacity, thread budgets, and media-download concurrency |
| `resolved_info_cache.py` | Bounded cache of resolved metadata waiting for download |
| `settings.py` | Settings validation, migration, atomic writes, and recovery |
| `utils.py` | Filenames, time parsing, logging, process helpers, and platform utilities |
| `errors.py` | Expected media-operation exception type |
| `app_info.py` | Application name and version |
| `updates.py`, `ui/updates.py`, `tools/apply_update.ps1` | GitHub release validation, opt-in update downloads, and Windows executable replacement |
| `diagnostics.py`, `ui/diagnostics.py` | Runtime snapshots, reports, clipboard, and log-folder actions |
| `tests/`, `tools/` | Regression tests, Qt smoke flows, and resource benchmarks |

The GUI submits immutable `TaskOptions` to the toolkit-independent task manager. Workers emit `TaskEvent` objects; the Qt controller consumes them on the GUI thread in bounded batches. Pending progress updates are coalesced. Table insertions and removals are batched without resetting surviving selections.

`TaskManager.summary()` returns incrementally maintained counts, average progress, and an action revision without scanning task records. Progress changes are published separately from action availability. Change task state through manager methods to keep summaries and events consistent.

## Output processing

| Option | Video | Audio |
| --- | --- | --- |
| Container | Automatic, MP4, MKV, WebM | Automatic, M4A, MP3, Ogg Opus, WebM, FLAC, WAV |
| Video codec | Original, H.264, HEVC, VP9, AV1 | — |
| Audio codec | Original, AAC, MP3, Opus, FLAC, LPCM, No audio | Original, AAC, MP3, Opus, FLAC, LPCM |
| Resolution | 144p–2160p | — |
| Conversion quality | High quality, Balanced, Smaller file | — |
| Conversion bitrate | Automatic or 64–320 kbps for AAC, MP3, and Opus | Same |
| Lossless bit depth | Original, 16-bit, 24-bit | Same |

Incompatible codec/container combinations and unavailable encoders are disabled. Actual streams are inspected with FFprobe before final processing decisions; the output is verified against its plan before publication.

**Original** copies streams without codec conversion. An explicitly selected codec also permits copying when the source already matches. Quality and bitrate controls apply when encoding is required. Automatic encoding bitrate is 192 kbps for MP3/AAC and 128 kbps for Opus.

Automatic container selection uses compatible MP4 or WebM for separate video/audio streams and otherwise MKV. Original streams that cannot fit an explicitly selected container cause an explanatory failure rather than an implicit codec change.

An untrimmed, single-stream audio source can be preserved unchanged with Audio + Original + Automatic. A single video file can also be preserved unchanged when all streams and its actual container match the plan. MKV/WebM require matching EBML DocType evidence; MP4 requires verified faststart layout. Extra streams or required processing prevent this shortcut.

FLAC and LPCM avoid further lossy encoding but cannot restore source quality. Original bit depth preserves known precision; conversion with unknown precision uses 24-bit and reports a warning. Changing bit depth may require encoding even when the codec matches.

FFprobe results are cached for a task attempt, with at most 64 entries keyed by executable, canonical path, and file identity/timestamps. Writes invalidate affected entries; callers receive independent snapshots. Final verification always requests fresh evidence. The cache is cleared after success, failure, or cancellation.

## Trimming and file protection

Trim times accept seconds, `mm:ss`, and `hh:mm:ss`, including fractions. Blank fields mean the beginning or end of the source. Trim fields are session-only preferences. The complete source is downloaded before processing.

Configured output applies trimming in the same FFmpeg pass as conversion or merging. Separate inputs seek together. Copied video starts at a preceding keyframe; an adjustment of at least 50 ms generates a warning. The requested end remains the limit, so the clip may be longer than requested. Frame-exact cuts require video encoding.

If cutting or verification fails, the engine attempts to build and verify the full output from retained sources and reports a warning. Cancellation and insufficient disk space stop the task instead. Task workspaces isolate temporary files, publication avoids overwriting existing outputs, and disk checks account for processing space.

## Concurrency and progress

| Setting | Default | Range |
| --- | --- | --- |
| `workers` | 3 | 1–16 |
| `video_workers` | 1 | 1–4 |
| `ffmpeg_threads` | 0 (Automatic) | 0–64 |
| `fragment_downloads` | 1 | 1–4 |
| `network_connections` | 8 | 2–32 |

Each task retains its submitted output options. The overall worker limit covers downloading, waiting, processing, and saving. Reducing concurrency lets admitted work finish. Video encoding uses a separate FIFO gate; audio conversion and stream copying bypass that gate.

Automatic FFmpeg thread budgets use logical CPU count minus one, divided by the configured video-conversion limit, clamped to 1–8 threads per codec/filter pool. This is an internal parallelism budget, not a process-wide thread or CPU-percentage cap. New processes receive updated settings; running processes retain their budget.

Media-download capacity is shared across sessions. Plain HTTP streams reserve one unit; selector downloads reserve for up to two streams. Native fragment sessions reserve additional controller/key-request allowance. Capacity may reduce fragment concurrency. These reservations do not impose an OS-wide socket limit; metadata and previews have separate behavior. Waiting responds to pause, cancellation, peer failure, and shutdown.

Headless callers can use `manager.engine.configure_processing(video_workers=2, ffmpeg_threads=4)` and `manager.engine.configure_network(fragment_downloads=3, network_connections=8)`.

Download callbacks check controls and disk limits on each call. Progress calculations normally publish at most once per 100 ms per task, with forced completion reports. Speed samples use a two-second window and at most 32 entries. The UI sorts numeric speed and ETA fields; stale metrics clear on pause, processing, retries, and terminal states. A task reaches 100% after successful output publication.

## Links, playlists, and previews

The main URL field accepts a single video; bulk entry expands playlists and albums. A YouTube video URL containing playlist information still queues only that video through the main field. Duplicate queued/active videos and pending resolution requests are skipped. Failed resolution releases reservations for resubmission.

Up to three submitted links resolve concurrently. Bulk playlists deliver batches of up to 100 URLs while extraction continues. Consumed yt-dlp lazy-list caches are released for the supported version. A per-resolution set prevents duplicates across batches. Partial failures retain available entries and report a warning; shutdown discards unfinished batches. Task rows and duplicate sets still grow with playlist size.

Thumbnail loading uses bounded requests and caching. A failed or oversized preview does not fail the download. Pending resolved metadata is limited to 32 records, an estimated 16 MiB payload budget, and a five-minute lifetime; cache misses use normal extraction.

## Preferences and diagnostics

Runtime settings and logs are stored beside the application source; saving preferences requires a writable location. Default output is Video, Automatic container, Original codecs, 1080p, Balanced conversion quality, and 192 kbps audio. English is the default interface language; English/Turkish changes are saved and applied live.

| File | Purpose |
| --- | --- |
| `yd_settings.json` | Preferences |
| `yd_settings.json.bak` | Previous valid settings |
| `yd_settings.json.corrupt-<timestamp>` | Preserved malformed settings |
| `errors.log` | WARNING, ERROR, and CRITICAL records |
| `debug.log` | INFO and DEBUG records, including startup and task stages |
| `errors.log.1`, `debug.log.1`, … | Rotated archives |

Settings writes are atomic and debounced, with a final flush on exit. Legacy values migrate, unknown fields survive, and invalid values use defaults. Malformed files are preserved before recovery; unsupported schemas and unreadable files are not overwritten. Reads are limited to 1 MiB and reject duplicate keys, non-finite numbers, invalid UTF-8, and excessive nesting. Oversized files disable saving until corrected and reloaded. Partial updates retain unrelated on-disk values, without live synchronization between instances.

Logging defaults to 5 MiB per file, three archives, and 60 seconds between repeated-warning summaries. Settings keys are `log_max_bytes` (65536–104857600), `log_backup_count` (1–10), and `log_repeat_seconds` (1–3600). Close the app before manual settings edits.

Records include session information and, during task execution, task ID, attempt, and stage. Debug stages cover resolution, download, conversion, trimming, verification, publication, and cleanup. `YD_DEBUG_LOG=0` suppresses DEBUG traces; INFO startup records remain. Keyed warnings log once immediately, summarize repeats, and flush pending counts at task completion or shutdown.

Expected failures use `MediaOperationError` and retain their operational context. Unexpected exceptions retain tracebacks. Logging failures use independent temporary fallbacks; dialogs report the actual destination or state that saving failed.

The Help menu and error dialogs provide log-folder access, detail copying, and report export. Reports contain runtime versions, error details, up to 100 task snapshots, and up to 64 KiB of recent active-log text. Export is atomic and cannot overwrite application logs or settings. Known credentials and sensitive URL parameters are masked; exported reports also mask known home/output folders. Titles, URLs, and other paths can remain, so review reports before sharing.

Shutdown allows five seconds for cancellation and cleanup, then terminates managed FFmpeg/FFprobe processes and waits two more seconds. If work remains, the UI offers more time or immediate closure. Immediate closure may leave temporary files owned by unfinished workers; completed outputs are preserved.

## Maintenance and validation

`requirements.txt` pins all six direct runtime dependencies. `requirements-windows.lock.txt` also pins their Windows transitive dependencies, and `requirements-build.txt` adds pinned packaging tools. The managed FFmpeg bridge and playlist cache handling use private yt-dlp APIs; startup checks the supported version and required method signatures. For an upgrade, update the yt-dlp pin, matching EJS version, and `SUPPORTED_YTDLP_VERSION` together in a separate development environment, adapt the integration, and run the full suite and Qt smoke flows before release.

With a prepared development environment, run:

```powershell
.\.venv\Scripts\python.exe tools/run_tests.py -v
.\.venv\Scripts\python.exe tools/qt_smoke.py --platform offscreen
.\.venv\Scripts\python.exe tools/qt_smoke.py --platform offscreen --language tr
```

The runner isolates settings and logs in temporary directories. Real-media tests need FFmpeg/FFprobe and relevant encoders. Smoke flows use local media and a local HTTP server. Use `--platform windows` for a native Windows smoke flow. `tools/resource_benchmark.py` provides offline CPU and memory measurements.

`tools/install_deno.py` and `tools/install_ffmpeg.py` are verified Windows x64 development installers. The application detects Deno under `tools/runtime/` and prefers FFmpeg/FFprobe under `tools/runtime/ffmpeg/bin/` before system tools. Neither installer changes system `PATH`. Build scripts and the license collector use the same executable resolution as the application. See the [publication checklist](releasing.md) for Windows build contents and release checks.
