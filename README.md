# BekoDLP

An independent Windows app for downloading videos, audio, and playlists from YouTube and other sites supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp). Choose your output format, trim clips, and manage downloads through an English or Turkish interface.

The current source version is **1.2**. The portable application targets **Windows x64**.
BekoDLP uses yt-dlp for downloading and FFmpeg for media processing, with a
PySide6/Qt interface. It is not affiliated with the yt-dlp project.

## Features

- Videos and playlists from YouTube and other sites supported by yt-dlp.
- Single links and bulk downloads.
- Preview playlist entries and queue only checked videos or a selected position range.
- Video downloads up to 4K, depending on the source.
- Audio formats including MP3, AAC (M4A), Opus, FLAC, and WAV.
- Single clips or up to 100 time ranges from one source, with full-source or section downloading.
- Chapter markers or separate chapter files.
- Advanced codec, container, quality, bitrate and audio bit-depth controls.
- Detailed source format inspection and exact video/audio stream selection.
- File and subfolder naming templates using title, channel, upload date and original playlist position.
- Parallel downloads with pause, resume, cancellation, and retry.
- Background queue saving and restoration, including each task's output and trim settings.
- Built-in and custom settings profiles.
- Adjustable parallel tasks, video conversions, FFmpeg threads and download connection capacity.
- Optional Netscape cookie files or cookies from a selected browser/profile.
- Subtitle downloads as SRT/WebVTT files and selectable embedded tracks in MP4, MKV and WebM.
- Optional source thumbnail embedded as cover art in MP3, M4A, FLAC, MP4 and MKV.
- System, direct or custom HTTP/SOCKS proxy routing and per-task speed limits.
- Optional actions when the queue finishes, including opening the folder or shutting down.
- Progress, speed, remaining time, and thumbnail previews.
- Application updates and separate updates for yt-dlp, FFmpeg/FFprobe and Deno.
- Logs, per-task error details and exportable diagnostic reports.

Download availability, resolutions and source formats depend on the website
and the media it makes available.

## Get started

Download the latest published Windows package from [GitHub Releases](https://github.com/beko201/BekoDLP/releases).
Extract it into a writable folder and run **BekoDLP.exe** inside **BekoDLP/**.
Keep `_internal/`, `tools/`, `licenses/` and `package-manifest.json` alongside the EXE.
The portable binary package includes Python and its media tools; no separate
Python, FFmpeg, yt-dlp or Deno installation is required.

The source ZIP is intended for development. Follow the [build guide](docs/building.md)
to install its dependencies and external tools before running it.

1. Choose a download folder.
2. Select **Video** and a resolution, or **Audio** and a format.
3. For a single video, paste its URL into the main field and click **Add to queue**. For a playlist or multiple videos, open **Add multiple links** and paste the URLs there, one per line.
4. Open **Advanced options** for trimming and conversion settings. Click the **ⓘ** icons for explanations.
5. Manage downloads through **Task actions** or the table's right-click menu. Double-click a completed task to show its file.

Choose **Language → Türkçe** to switch to Turkish. Language and output preferences are saved automatically.

## Output and processing

**Original** preserves matching source codecs; **Automatic** selects a compatible
container. Quality and bitrate settings apply when conversion is needed.
Video output preserves silent sources without adding an audio track. Audio-only
output requires an audio stream.
Output choices are captured when tasks are added; changing defaults does not
change existing tasks.

### Settings profiles

The **Settings profile** selector offers Music, Original archive and Smaller file
presets. Choose a profile to apply it, or use **Save profile…** to save your current
output settings under a name. Saving the same name updates that profile;
**Delete profile** removes the selected custom profile. Up to 30 custom profiles
are retained in preferences across restarts. Names you enter stay unchanged when
switching interface language.

Profiles include output mode, codecs, container, resolution limit, conversion
quality/bitrate, subtitle choices, artwork, chapter mode and naming templates.
URLs, download folder, cookies, proxy, time ranges and parallel-processing limits
remain task/session context. Applying a profile affects newly queued downloads;
existing tasks keep their captured settings. An unavailable encoder or conflicting
range/chapter selection is reported before any settings change. Matching codecs
are still copied, so conversion quality/bitrate apply when conversion is needed.

### Exact source formats

To choose a website's exact source streams, enter a single video URL, select the
output profile and folder, then click **Source formats…**. The dialog lists
format IDs, stream types, codecs, resolution, FPS, bitrate, size, language, HDR,
audio channels, sample rate and protocol. Filter the list or click a row to
select a source. DRM and non-media entries are visible but cannot be queued.
Sizes marked `≈` are estimates; a dash means the information is unavailable.
Some sites report a media container without codec details. These sources remain
selectable as unknown streams; the downloaded media is inspected before output
processing to check the actual video and audio tracks.

Choose a video format and, for a video-only source, a separate audio format.
Combined formats use their included audio. Audio output can also extract audio
from a combined source. Click **Queue selected formats** to add that video with
its choices. The output codec, container and trim settings captured when opening
the dialog still apply; manual sources override the automatic resolution cap.
Selections are saved per task, including retry and queue restoration, and never
become defaults for another URL. If a selected format disappears, the task fails
instead of substituting another stream. Proxy and cookie settings also apply
when inspecting formats. Closing the dialog cancels its metadata request.

### Trimming and multiple clips

Choose a start and/or end time under **Advanced options**. Two download methods
are available:

- **Download full source and trim** downloads the source and cuts it locally.
  This method supports per-task speed limits and SOCKS proxies.
- **Download selected section only** requests the selected interval through
  yt-dlp and FFmpeg, then applies the output profile and final local cut. It
  never retries by downloading the full video. Section mode rejects speed
  limits and SOCKS proxies because FFmpeg cannot honor them.

Without a trim range, normal downloading is used. Section-mode traffic depends
on the source's seeking support and may include headers and surrounding
keyframes. Original timestamps are retained for audio/video synchronization
and subtitle offsets. Cookies and extractor headers are passed through yt-dlp;
if section authentication fails, try full-source mode.

Copied video can start at an earlier keyframe. Select a different video codec
to re-encode for exact cuts. The download method is saved with each task; older
saved tasks retain full-source behavior.

Use **Advanced options → Multiple time ranges** for several clips from the same video.
Enter one `start - end` pair per line, for example `0:30 - 1:10` and `2:00 - 2:45`.
Seconds, `mm:ss` and `hh:mm:ss` are accepted, up to 100 ranges in the entered order.
The single trim fields are inactive while ranges are entered. Section mode requests
each range separately; full mode downloads the source once and cuts locally. Each
clip uses the selected codecs, cover and subtitles, with captions shifted to its
own start. Chapter markers can be embedded; chapter splitting is a separate mode.
Numbered clips are published together in a folder using the naming template.
The queue preserves the ranges, and a failed cut never falls back to a full video.
Copied video can start at the preceding keyframe; choose a different video codec
to re-encode for exact cuts.

### Chapters

Under **Advanced options → Chapters**, choose **Embed chapter markers** to store the
source's chapter titles and times in MP3, M4A, MP4 or MKV/MKA, or **Save each chapter
as a separate file** to produce numbered files inside a folder named by the output
naming template. The source is downloaded once. Codec, subtitle and cover settings
apply to every chapter; trimming keeps only chapters intersecting the selected
range. Copied video starts at a preceding keyframe and may overlap the preceding
chapter; select a different video codec to re-encode for exact cuts. Missing
chapters keep the normal output with a warning. Separate files are published together after all processing
succeeds. The queue retains the chapter choice and completed folder path.

## Playlists and queue

### Select playlist entries

To choose playlist videos before downloading, open **Add multiple links**, enter
one playlist URL, and click **Choose playlist videos…**. Uncheck individual rows,
use **Select all / Select none**, or enter the first and last playlist positions
and click **Select range**. Range selection replaces the current selection and
uses the original playlist positions, including gaps for unavailable videos.
Private, deleted and unavailable entries cannot be selected.

Click **Queue selected videos** to start only those videos. Previewing or closing
the selection window never adds tasks. The output, trim, cookie and network
settings in effect when opening the window are retained for the selected tasks.
Already queued or repeated URLs are skipped. Selected tasks use normal queue
persistence and wait for Resume after reopening the app.

The preview loads in batches and shows at most 20,000 entries. If loading stops
early, a warning identifies the partial preview; only the displayed entries can
be selected. **Reload playlist** starts a fresh preview and resets the selection.

### Queue saving and restart

The task list is saved automatically in `yd_queue.json` beside the application,
with a backup for recovery. Queue saving runs in the background, and normal
shutdown waits for the final save. A write failure is reported before closing. On reopening, unfinished tasks wait until you click
**Resume**. Interrupted downloads restart as new attempts; partial download bytes
are not restored. Completed, failed and cancelled rows keep their state, and
completed files are not downloaded again. Clearing rows also updates the saved
queue. Only tasks already added to the list are saved; links still being resolved
have not yet become queue tasks.

### When the queue finishes

Use **When queue finishes** to open the download folder, close the application,
sleep, hibernate, or shut down the computer. The action runs once after all new
tasks finish successfully and workers finish cleanup. Failed or cancelled tasks
cancel the action. Power actions show a cancellable 30-second countdown; adding
work or pausing during the countdown cancels it. This selection resets every
session, so reopening a saved queue never re-arms a power action automatically.

Power actions also wait for queue saving to finish. New work added while that
save is pending cancels the power action.

## Advanced options

### File and folder naming

Open **Advanced options → File and folder naming** to choose a preset or edit the
filename and subfolder templates separately. The default `{title}` and an empty
subfolder retain the existing naming behavior. The actual output extension is
added automatically; do not include an extension in the filename template.

Available fields are `{title}`, `{channel}`, `{upload_date}`, `{playlist}`,
`{playlist_index}` and `{id}`. Upload dates use `YYYY-MM-DD`. Use
`{playlist_index:03d}` for zero-padded positions such as `003`. For example,
subfolder `{channel}/{playlist}` and filename `{playlist_index:03d} - {title}`
produce `Channel/Playlist/003 - Video.mp4` for MP4 output. The live example is
illustrative; actual values come from the video's metadata.

Playlist positions and titles are retained when adding a playlist or choosing
specific playlist rows, so selecting positions 3 and 7 keeps `003` and `007`.
Missing metadata uses stable fallback names: `Unknown channel`, `Unknown date`,
`Single videos`, and position `000`. A source without a reported upload or release
date does not use today's date. Channel names fall back to the uploader/artist.

Templates apply to new tasks and are saved with preferences and the queue.
Metadata-derived characters are sanitized using the app's existing filename
rules. Subfolders stay beneath the selected download folder; absolute paths,
parent traversal and linked subfolders are rejected. Existing files are never
overwritten, and media and subtitle sidecars share the final chosen base name.

### Subtitles

Open **Advanced options → Subtitles** to save separate subtitle files, embed
selectable subtitle tracks in the video, or do both. Choose language codes such
as `tr,en`, a prefix such as `en.*`, or `all`. Automatic captions are opt-in;
human subtitles take precedence when both exist for a language. Live-chat tracks
are excluded. Audio downloads support separate subtitle files.

The selection is retained per task and in the saved queue. Subtitles are clipped
and shifted to match successful media trimming, including the actual keyframe
start used when copying video. Sidecars share the final media filename, for
example `Video.en.srt`. Existing media and subtitle files are never overwritten.
Unavailable languages or failed subtitle processing produce a task warning while
preserving the media download. Embedded subtitles are selectable tracks, not
burned into the image.

### Cover artwork

Enable **Advanced options → Cover artwork → Embed the source thumbnail as cover
artwork** to include the video's/source's image in the final file. This supports
MP3, M4A, FLAC, MP4 and MKV. MP3/M4A/FLAC use an embedded front cover; MKV stores
`cover.jpg` as an attachment. Display depends on the player. WAV, WebM and
Ogg/Opus output do not currently support this option.

Artwork is fetched through yt-dlp with the task's cookie and proxy settings,
normalized to JPEG (up to 1024 pixels per dimension), and added after conversion,
trimming and subtitle embedding. Audio, video and subtitle streams are copied;
cover embedding does not re-encode the media. Media and cover verification finish
before the original working file is replaced and the result is published.

The choice is off by default, applies to newly added tasks and is saved with
preferences and the queue. Missing/corrupt artwork, unsupported formats or an
embedding failure preserve the media and add a task warning. Cancellation still
cancels the task. Temporary images are removed; no separate cover file is saved.

### Cookies

For sites requiring a signed-in session, open **Advanced options → Cookies**.
Choose **Cookie file** and select a Netscape `cookies.txt` file, or choose
**Browser cookies**, a browser and optionally a profile name/path. Leave the
profile blank for the default profile. If browser cookie access fails, close the
browser and retry, or use a valid exported cookie file.

The selection applies to newly added tasks and is retained with the saved queue.
Only the source path or browser/profile reference is saved in app preferences and
queue files. Cookie file contents are used through disposable per-process copies;
the original file is not overwritten. Browser access/decryption is handled by
the installed yt-dlp executable and depends on the browser and operating system.

### Network and speed limits

Open **Advanced options → Network** to choose system proxy settings, a direct
connection, or a custom HTTP/HTTPS/SOCKS proxy URL with an explicit port. These
settings route yt-dlp requests and thumbnail previews. Application updates use
their own connections. Thumbnail previews support HTTP and SOCKS5 proxies;
SOCKS5h resolves target names through the proxy. HTTPS proxy, SOCKS4 and SOCKS4a
previews are unavailable because Qt does not support those proxy transports.
Custom-proxy failures, unsupported settings and loopback destinations (including
redirects) never fall back to a direct preview connection. Direct mode explicitly
disables proxying for previews. Each preview has isolated proxy connections and
authentication state. On Windows, proxy URLs containing credentials are stored
with current-user [Windows DPAPI protection](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
in preferences, queue records and their valid backups. Existing readable plain-text
records are migrated on load. Secret-free proxy URLs keep their normal format.

Protected records depend on the Windows user profile and usually the original
computer. If they cannot be opened, the original files are retained and writes
are disabled rather than replacing the credentials. This protects files at rest;
the running application and downloader still need the URL in memory. Saved
profiles exclude connection credentials. Older external backups and corrupt
recovery copies are not rewritten automatically.

Set a speed limit in KiB/s or MiB/s; zero means unlimited. The limit applies per
task, with parallel video and audio streams sharing it. Multiple active tasks
each receive their own limit. Fragment concurrency is reduced while a limit is
active. Network choices apply to new tasks and survive queue restoration.

## Updates

### Application updates

Packaged Windows builds check for newer stable releases shortly after startup.
Downloading and installing each require your confirmation. You can also use
**Help → Check for updates…**. Installation requires an idle queue.

Updates replace the managed application files together and preserve preferences,
the saved queue and downloads. Users of versions 1.0 and 1.1 must update manually:
close the application and extract the new complete Windows package.

### Download-tool updates

**Help → Update download tools…** checks and updates yt-dlp, FFmpeg/FFprobe and
Deno separately on Windows x64. Checks and downloads start only when requested.
Packages come from the yt-dlp, denoland/deno and GyanD/codexffmpeg release pages;
the release asset's SHA-256, executable architecture and reported version are
verified before installation. FFmpeg uses the full static build and FFprobe is
updated in the same operation. Installation waits for an idle queue; a verified
download remains ready in the dialog if work starts while downloading. New tasks
use the updated tools without rebuilding or restarting BekoDLP. Use **Cancel
download** to cancel an uninstalled download, or close the dialog. Closing hides
the window immediately and cancels any active update job; cleanup follows when
the job stops. If temporary files cannot be removed, the cleanup error is logged
without preventing the window from closing. Errors preserve existing tools.
Updates live in `tool-updates/`, separately from packaged binaries, together with
the release URL, archive digest and executable hashes.

## Documentation

- [Build from source](docs/building.md)
- [Libraries and sources](docs/libraries-and-sources.md)
- [Third-party license information](docs/third-party-licenses.md)

## Help and contributing

For download errors, double-click the failed task. The **Help** menu provides logs and diagnostic reports.

Report bugs or suggest features through [GitHub Issues](https://github.com/beko201/BekoDLP/issues). Include the app version, steps to reproduce, and relevant error details. Review diagnostic reports for personal information before sharing.

## License

The project's own code is licensed under [GPL-3.0-only](LICENSE). Third-party libraries and bundled tools retain their respective licenses.

Acknowledgement: [ytdlp-interface](https://github.com/ErrorFlynn/ytdlp-interface)
by ErrorFlynn was consulted for feature comparison and inspiration.
