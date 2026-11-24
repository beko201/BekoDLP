# Run and build from source

Windows users can download the portable package from
[GitHub Releases](https://github.com/beko201/BekoDLP/releases), extract it and run
`BekoDLP.exe`. The instructions below are for the source package.

## Requirements

- Windows x64 and Python 3.13 x64.
- Internet access for dependencies and media tools.
- Windows `tar.exe` with `.7z` support for the FFmpeg installer.

Open PowerShell in the project folder containing `main.py`.

## Install dependencies

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m pip check
```

`requirements-build.txt` includes the pinned application dependencies and build
tools. The application uses the standalone yt-dlp executable.

## Install media tools

```powershell
.\.venv\Scripts\python.exe tools/install_ytdlp.py
.\.venv\Scripts\python.exe tools/install_deno.py
.\.venv\Scripts\python.exe tools/install_ffmpeg.py
```

The installers verify pinned SHA-256 hashes and place the tools in
`tools/runtime/`. They do not change the system PATH. FFmpeg and FFprobe are
installed together.

## Run and test

```powershell
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe tools/run_tests.py
```

## Create the Windows package

Set `APP_VERSION` in `app_info.py` to the version being packaged. Collect the
third-party notices for the installed tools and dependencies, then build:

```powershell
.\.venv\Scripts\python.exe tools/collect_licenses.py --fetch-upstream
.\.venv\Scripts\python.exe tools/build_windows.py
```

Notice collection does not establish complete source or license coverage. See
[Third-party license information](third-party-licenses.md) before distribution.

The outputs are `dist/BekoDLP/` and
`dist/BekoDLP-<version>-Windows-x64.zip`. Keep the entire application folder
together, including `_internal/`, `tools/`, `licenses/`, `LICENSE` and
`package-manifest.json`.

Test the built package:

```powershell
.\.venv\Scripts\python.exe tools/check_packaged_app.py
.\.venv\Scripts\python.exe tools/check_packaged_update.py
```

These checks require an interactive Windows desktop. Also test the extracted
package on a Windows x64 computer without the development dependencies installed.

## Distribution

The application version, package manifest and release tag must match. Publish
the complete Windows ZIP and its matching source and required third-party
materials. The updater accepts `BekoDLP-<version>-Windows-x64.zip` or
`BekoDLP-Windows-x64.zip` with a stable numeric release tag and a GitHub-provided
SHA-256 digest.

Personal settings, queues, logs, downloaded media, virtual environments and build
caches are excluded from source and application packages. Do not add them to
source commits or release assets.
