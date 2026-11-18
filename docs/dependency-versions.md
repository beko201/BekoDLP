# Dependency versions

Verified against the public PyPI metadata, official Deno release, and Gyan's
stable FFmpeg build on 2026-11-18, for Windows x64 and Python 3.13.

| Component | Version | Result |
| --- | --- | --- |
| yt-dlp | 2026.10.26 | Already current; private integration version guard retained |
| yt-dlp-ejs | 0.8.0 | Already current; matches yt-dlp's declared EJS pin |
| curl-cffi | 0.17.0 | Already current |
| psutil | 7.2.2 | Already current; now explicitly pinned |
| Unidecode | 1.4.0 | Already current; now explicitly pinned |
| PySide6 | 6.12.0 | Already current, including its required Qt packages |
| Deno | 2.9.8 | Already current; portable archive digest retained |
| FFmpeg / FFprobe | 9.0.2 full build | Updated from 8.0 to a verified portable build |
| pip | 26.3 | Updated from 26.2.2 |
| PyInstaller | 6.22.3 | Installed the current stable build tool and required dependencies |

The runtime dependency graph (including certifi, cffi, pycparser, and shiboken6)
was also checked against PyPI and already current. Installed packages unrelated
to this application's runtime/build graph were not treated as project dependencies.

## Files to maintain together

- `requirements.txt`: exact pins for direct application dependencies.
- `requirements-windows.lock.txt`: Windows runtime transitive pins plus the direct requirements.
- `requirements-build.txt`: runtime lock plus exact packaging-tool pins.
- `tools/install_deno.py`: portable Deno version and official archive digest.
- `tools/install_ffmpeg.py`: portable FFmpeg/FFprobe version and official archive digest.
- `download_engine.SUPPORTED_YTDLP_VERSION`: verified private yt-dlp interface version.

FFmpeg/FFprobe are installed inside `tools/runtime/ffmpeg/` and selected before
system tools. The previous system-wide FFmpeg installation is unchanged. Deno
and media binaries are not Python packages and do not belong in requirements.txt.
The build guide and notice collector select the same FFmpeg executables as the
application. Recollect release notices after future updates.

## Primary sources

- [Python package metadata](https://pypi.org/)
- [yt-dlp stable releases](https://github.com/yt-dlp/yt-dlp/releases)
- [Deno stable releases](https://github.com/denoland/deno/releases)
- [Gyan FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/)
