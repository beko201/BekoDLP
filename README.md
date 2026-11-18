# BekoDLP

An independent Windows app for downloading videos, audio, and playlists from YouTube and other sites supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp). Choose your output format, trim clips, and manage downloads through an English or Turkish interface.

BekoDLP is not affiliated with the yt-dlp project.

**This project was developed with AI using Claude and ChatGPT.**

## Features

- Videos and playlists from YouTube and other sites supported by yt-dlp.
- Single links and bulk downloads.
- Video downloads up to 4K, depending on the source.
- Audio formats including MP3, AAC, Opus, FLAC, and WAV.
- Optional trimming and advanced codec, quality, and bitrate controls.
- Parallel downloads with pause, resume, cancellation, and retry.
- Progress, speed, remaining time, and thumbnail previews.

## Get started

Download **BekoDLP-1.0-Windows-x64.zip** from [GitHub Releases](https://github.com/beko201/BekoDLP/releases) and extract it into a writable folder. Run **BekoDLP.exe** inside the extracted **BekoDLP/** folder. Keep `_internal/`, `tools/` and `licenses/` alongside it. No installation or additional software is required.

1. Choose a download folder.
2. Select **Video** and a resolution, or **Audio** and a format.
3. For a single video, paste its URL into the main field and click **Add to queue**. For a playlist or multiple videos, open **Add multiple links** and paste the URLs there, one per line.
4. Open **Advanced options** for trimming and conversion settings. Click the **ⓘ** icons for explanations.
5. Manage downloads through **Task actions** or the table's right-click menu. Double-click a completed task to show its file.

Choose **Language → Türkçe** to switch to Turkish. Language and output preferences are saved automatically.

## Updates and output

The Windows build announces available updates after startup. Downloading and installing each require your confirmation. You can also use **Help → Check for updates…**. Updates replace the application files together and preserve your settings and downloads. Users of the older single-file build must extract the new ZIP manually once.

- **Original** preserves the source codec; **Automatic** selects a compatible container.
- Output settings apply to newly added tasks.
- Trimming downloads the full source first. Cuts without video conversion may follow keyframe boundaries.
- Download availability depends on the source website.

## Documentation

- [Build from source](docs/building.md)
- [Libraries and sources](docs/libraries-and-sources.md)
- [Release license review](docs/license-review.md)

## Help and contributing

For download errors, double-click the failed task. The **Help** menu provides logs and diagnostic reports.

Report bugs or suggest features through [GitHub Issues](https://github.com/beko201/BekoDLP/issues). Include the app version, steps to reproduce, and relevant error details. Review diagnostic reports for personal information before sharing.

For code contributions, follow the [build guide](docs/building.md) and run the tests before opening a pull request. The project is maintained in [beko201/BekoDLP](https://github.com/beko201/BekoDLP).

## License

The project's own code is licensed under [GPL-3.0-only](LICENSE). Third-party libraries and bundled tools retain their respective licenses.
