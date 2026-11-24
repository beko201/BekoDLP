# Libraries and sources

## Python libraries

| Library | Purpose | Source |
| --- | --- | --- |
| PySide6 | Desktop interface | [Qt for Python](https://doc.qt.io/qtforpython-6/) |
| psutil | Process control and resource monitoring | [GitHub](https://github.com/giampaolo/psutil) |
| Unidecode | Filename transliteration | [GitHub](https://github.com/avian2/unidecode) |

## External tools

Credits below do not replace distribution notices or corresponding sources.
See the [Third-party license information](third-party-licenses.md) and the notice collector in
the [build guide](building.md) before publishing an executable.

| Tool | Purpose | Source |
| --- | --- | --- |
| yt-dlp standalone | Video, audio, and playlist downloads | [GitHub](https://github.com/yt-dlp/yt-dlp) |
| FFmpeg / FFprobe | Media conversion, merging, trimming, and inspection | [FFmpeg](https://ffmpeg.org/), [Windows builds](https://www.gyan.dev/ffmpeg/builds/) |
| Deno | JavaScript runtime for yt-dlp | [GitHub](https://github.com/denoland/deno) |
| PyInstaller | Windows executable packaging (build only) | [Documentation](https://pyinstaller.org/) |

Exact Python versions are pinned in `requirements.txt`,
`requirements-windows.lock.txt` and `requirements-build.txt`. External tool
versions and archive hashes are pinned in `tools/install_ytdlp.py`,
`tools/install_deno.py` and `tools/install_ffmpeg.py`; collected notices describe the binaries actually shipped.

The official yt-dlp executable includes its own dependencies, which retain their
respective licenses and notices.
