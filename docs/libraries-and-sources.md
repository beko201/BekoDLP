# Libraries and sources

## Python libraries

| Library | Purpose | Source |
| --- | --- | --- |
| PySide6 | Desktop interface | [Qt for Python](https://doc.qt.io/qtforpython-6/) |
| yt-dlp | Video, audio, and playlist downloads | [GitHub](https://github.com/yt-dlp/yt-dlp) |
| yt-dlp-ejs | JavaScript support for yt-dlp extraction | [GitHub](https://github.com/yt-dlp/ejs) |
| curl-cffi | HTTP requests and browser impersonation | [GitHub](https://github.com/lexiforest/curl_cffi) |
| psutil | Process control and resource monitoring | [GitHub](https://github.com/giampaolo/psutil) |
| Unidecode | Filename transliteration | [GitHub](https://github.com/avian2/unidecode) |

## External tools

Credits below do not replace distribution notices or corresponding sources.
See the [binary license review](license-review.md) and the notice collector in
the [build guide](building.md) before publishing an executable.

| Tool | Purpose | Source |
| --- | --- | --- |
| FFmpeg / FFprobe | Media conversion, merging, trimming, and inspection | [FFmpeg](https://ffmpeg.org/), [Windows builds](https://www.gyan.dev/ffmpeg/builds/) |
| Deno | JavaScript runtime for yt-dlp | [GitHub](https://github.com/denoland/deno) |
| PyInstaller | Windows executable packaging (build only) | [Documentation](https://pyinstaller.org/) |

Exact Python versions are pinned in `requirements.txt`,
`requirements-windows.lock.txt` and `requirements-build.txt`. External tool
versions and archive hashes are pinned in `tools/install_deno.py` and
`tools/install_ffmpeg.py`; collected notices describe the binaries actually shipped.
