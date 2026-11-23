# Build BekoDLP for Windows

This guide explains how to run BekoDLP from source, create the portable Windows
package, test it and prepare a GitHub release. End users only need to extract the
release ZIP and run `BekoDLP.exe`; the steps below are for development and packaging.

## 1. Prepare your computer

Use **Windows x64 and Python 3.13 x64**, the environment used for this project.
You need internet access to install Python packages and download FFmpeg and Deno.
The FFmpeg installer also needs Windows `tar.exe` to extract its `.7z` archive.

Extract or clone the source, then open PowerShell in the directory containing
`main.py`, `app_info.py` and the requirements files. Confirm the prerequisites:

```powershell
py -3.13 --version
Get-Command tar.exe
Test-Path .\main.py
```

The first command should identify Python 3.13, the second should find `tar.exe`,
and the last should return `True`. If `py` cannot find Python 3.13, install the
x64 version before continuing. If `main.py` is missing, change to the project
root before running the remaining commands.

## 2. Install the development environment

Create a virtual environment so the project's packages stay separate from other
Python projects, then install the application's dependencies and build tools:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m pip check
```

Use `.\.venv\Scripts\python.exe` for every Python command in this guide. You do
not need to activate the environment. `pip check` should report no broken
requirements; resolve installation errors before building.

The three requirements files serve different purposes:

| File | Contents |
| --- | --- |
| `requirements.txt` | Direct application dependencies: PySide6, psutil and Unidecode |
| `requirements-windows.lock.txt` | Direct dependencies plus their pinned Windows dependencies |
| `requirements-build.txt` | The Windows lock file plus PyInstaller and packaging tools |

Installing `requirements-build.txt` includes the other required packages; you do
not need to install all three files separately. The application does not import
the yt-dlp Python package. It communicates with
the standalone executable through JSON and progress templates in `ytdlp_process.py`.

## 3. Install the bundled media tools

The standalone yt-dlp executable downloads media. FFmpeg merges, converts and
trims media. FFprobe inspects the resulting files.
Deno runs the JavaScript used by yt-dlp's YouTube extraction support. These are
external executables, so they are installed by the project helpers rather than pip:

```powershell
.\.venv\Scripts\python.exe tools/install_ytdlp.py
.\.venv\Scripts\python.exe tools/install_deno.py
.\.venv\Scripts\python.exe tools/install_ffmpeg.py
```

The helpers download pinned Windows x64 releases, verify the archive SHA-256
values and place the tools inside the project. They leave the system PATH
unchanged. Verify the installed executables directly:

```powershell
.\tools\runtime\yt-dlp.exe --version
.\tools\runtime\deno.exe --version
.\tools\runtime\ffmpeg\bin\ffmpeg.exe -version
.\tools\runtime\ffmpeg\bin\ffprobe.exe -version
```

All four commands must run successfully. The application prefers these portable
tools over system installations. Keep them installed while building: the builder
copies them into the release so end users do not need to install them.

## 4. Run and test the source

Start the application using the project's Python:

```powershell
.\.venv\Scripts\python.exe main.py
```

Check that the window opens, choose a download folder and try the features you
changed. Close the application, then run the automated checks:

```powershell
.\.venv\Scripts\python.exe tools/run_tests.py
.\.venv\Scripts\python.exe tools/qt_smoke.py --platform offscreen
.\.venv\Scripts\python.exe tools/qt_smoke.py --platform offscreen --language tr
```

The first command runs the test suite. The smoke checks exercise the English
and Turkish interfaces with local media, including download, conversion and
trimming. They use an offscreen Qt platform. Use `--platform windows` instead
of `--platform offscreen` when you need to check the native Windows widgets.
Resolve failures before creating the release package.

## 5. Set the version and collect license notices

In `app_info.py`, set `APP_VERSION` to the version you are building. For example,
`APP_VERSION = '1.1'` corresponds to the GitHub release tag `v1.1`.
`UPDATE_REPOSITORY` identifies the public GitHub repository used by the updater;
the configured repository is `beko201/BekoDLP`.

Collect the notices for the build environment:

```powershell
.\.venv\Scripts\python.exe tools/collect_licenses.py --fetch-upstream
```

This creates `build/release-notices/`, including `THIRD_PARTY_NOTICES.md` and
`inventory.json`. The builder requires these files and checks that their
application name and version match `app_info.py`. Repeat collection after changing
the release version or dependencies, and review the generated inventory.

Collection prepares the available notices; it does not resolve missing source
or license evidence. The current findings, evidence locations and remaining
publication work are tracked in [the license review](license-review.md).

## 6. Build the portable package

Run the builder from the prepared environment:

```powershell
.\.venv\Scripts\python.exe tools/build_windows.py
```

It generates the icon from `assets/app.svg`, packages Python, Qt and application
resources with PyInstaller, copies the media tools and notices, writes a file
manifest, and creates the release ZIP. Wait for successful completion; the final
console output shows the EXE path, ZIP path and license-review status.

For version `1.1`, the outputs are:

```text
dist/
  BekoDLP/
    BekoDLP.exe
    _internal/             Python, Qt, application assets and translations
    tools/                 yt-dlp.exe, ffmpeg.exe, ffprobe.exe, deno.exe, apply_update.ps1
    licenses/              Consolidated third-party notices and index
    LICENSE
    package-manifest.json
  BekoDLP-1.1-Windows-x64.zip
```

Run `dist/BekoDLP/BekoDLP.exe` to try the built application. Keep the entire
`BekoDLP/` directory together when moving or distributing it. `_internal/`
contains the runtime dependencies; `tools/` contains the external executables.
Resources are found relative to the installation, independently of the working
directory. This folder build avoids extracting the runtime on every startup.

`package-manifest.json` records the managed files, their sizes and SHA-256 hashes
for update verification. The ZIP includes only those files and the manifest.
Preferences (`yd_settings.json` and backups), the saved queue (`yd_queue.json` and
backups/recovery copies), logs (`debug.log`, `errors.log` and
rotated files) and downloads are excluded from the release archive. In the
portable application, preferences and logs remain beside the EXE.

The `licenses/` directory contains `THIRD_PARTY_NOTICES.md`, `NOTICES.txt` and
`NOTICE_INDEX.json`. Identical notice texts share one copy, with original
component mappings preserved. Detailed inventories, source evidence and full
review reports are retained separately in the license-evidence archive.

Build caches and intermediate files stay under `build/`. Close the packaged
application before rebuilding; the builder replaces its managed files while
preserving unmanaged files in `dist/BekoDLP/`.

## 7. Test the packaged application

Source tests cannot detect every missing packaged DLL or resource. In an
interactive Windows desktop session, run:

```powershell
.\.venv\Scripts\python.exe tools/check_packaged_app.py
.\.venv\Scripts\python.exe tools/check_packaged_update.py
```

The first helper opens isolated copies of the actual EXE and checks English video
download, Turkish trimmed MP3 conversion and clean shutdown. It starts the app
from another working directory and removes system Python, yt-dlp, FFmpeg and Deno from
the test process's PATH, exercising the bundled tools.

The second helper reinstalls the actual ZIP into an isolated application copy.
It checks startup acknowledgement, removal of a stale DLL and preservation of
settings and existing user files. This is a same-version local reinstall; it
does not verify a public GitHub update between different versions.

The helpers use Windows UI Automation and need access to their test windows.
Their settings, media and reports are stored under `build/packaged-media-check/`
and `build/packaged-update-check/`. Also test the extracted ZIP on a separate
Windows x64 machine without Python, yt-dlp, FFmpeg or Deno installed.

## 8. Prepare the GitHub release

Once the package checks and required license/source work are complete:

1. Tag the exact application source with the matching version, such as `v1.1`.
2. Create a release in the repository configured by `UPDATE_REPOSITORY`.
3. Attach the complete `BekoDLP-1.1-Windows-x64.zip`, using the appropriate version
   in its name. This is the download intended for Windows users.
4. Provide the matching application source and applicable third-party source
   materials, with clear download links. Follow the [license review](license-review.md)
   for the remaining evidence and publication requirements.
5. Extract the uploaded ZIP into a new folder and confirm that it runs. For a
   later release, also test updating from the previous packaged version.

Keep `.venv/`, `tools/runtime/`, `build/`, `dist/`, personal settings and logs out
of source commits; these are covered by `.gitignore`. Upload binaries as release
assets rather than placing them in the source repository.

### How ZIP updates work

The updater accepts `BekoDLP-<version>-Windows-x64.zip` or the stable asset name
`BekoDLP-Windows-x64.zip`, preferring the stable name if both are present. Use a
stable numeric release tag such as `v1.1`. GitHub must report a completed asset
upload, size and SHA-256 digest. Drafts and prereleases are ignored.

Download and installation each require confirmation. Installation can start only
when queued tasks, active workers and link resolution have finished or been
cancelled. If installation is refused because work remains, finish that work
and request installation again through Help > Check for updates. The update is
verified and unpacked into a separate staging folder, then whole managed
application directories are replaced so obsolete files disappear. Settings, logs
and downloads stay in place. Both the installation and its parent directory
must be writable.

The helper retains the previous application files until the new Qt window
acknowledges startup. Failed moves, launch failure or missing acknowledgement
trigger restoration from backup. A power interruption can require manual recovery
from the staging backup; crashes after acknowledged startup are outside this check.

For an older single-file build, migrate manually once: close the old app, extract
the new ZIP and copy `yd_settings.json` if you want to keep its preferences. Keep
the old build until the new one works. Subsequent folder releases use ZIP updates.

## Common build problems

| Problem | What to check |
| --- | --- |
| `py -3.13` cannot find Python | Install Python 3.13 x64, then recreate the virtual environment with it. |
| A Python module is missing | Install `requirements-build.txt` using `.venv`'s Python and run `pip check`. |
| The FFmpeg installer cannot find `tar.exe` | Check `Get-Command tar.exe`; the installer requires Windows tar with support for its `.7z` archive. |
| Missing `inventory.json` or a notice-version mismatch | Set `APP_VERSION`, then collect notices for that version before building. |
| `Missing build input` | Check the path named in the error. Install portable tools, collect notices or reinstall the pinned Qt packages as appropriate. |
| The builder says the application is running | Close the EXE launched from `dist/BekoDLP/`, then retry the build. |
| The moved EXE cannot start | Move or extract the whole `BekoDLP/` folder, including `_internal/` and `tools/`. Check available application logs for startup details. |

### Updating the download engine

`tools/install_ytdlp.py` downloads an official Windows x64 executable and verifies
its pinned SHA-256 before replacing the local development copy. Its release and
checksum are build inputs, not an exact-version runtime restriction. Before
changing that pin, run the regression suite against the candidate executable,
including cached metadata, playlist batches, HLS, pause/cancel and selector merges.
Recollect notices for the exact executable before building a new package.

The GUI does not import `yt_dlp`, `yt_dlp_ejs` or `curl_cffi`. Those components can
still be bundled inside upstream's standalone executable and keep their licenses.
The existing application updater continues to replace the complete managed package;
this change does not add a separate in-app download-engine updater.
