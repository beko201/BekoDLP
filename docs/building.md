# Build from source (Windows)

This project was developed with AI using Claude and ChatGPT.

Build a single-file Windows x64 executable with Python 3.13 and PyInstaller. Run the commands below in PowerShell from the project root. Build tools are required only on the developer's machine.

## 1. Prepare the environment

Install Python 3.13 x64. The helpers below install verified portable Deno and the
full [Gyan FFmpeg Windows build](https://www.gyan.dev/ffmpeg/builds/) inside the
project. They do not change system `PATH`; a system FFmpeg installation is used
only when the portable build is absent.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip==26.3
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe tools/install_deno.py
.\.venv\Scripts\python.exe tools/install_ffmpeg.py
.\tools\runtime\ffmpeg\bin\ffmpeg.exe -version
.\tools\runtime\ffmpeg\bin\ffprobe.exe -version
```

Keep the pinned yt-dlp and EJS versions together. Set `APP_VERSION` in `app_info.py` to match the intended release tag, and confirm `UPDATE_REPOSITORY` points to your public GitHub repository.

`requirements.txt` pins the six direct runtime dependencies;
`requirements-windows.lock.txt` also pins their Windows transitive dependencies.
`requirements-build.txt` includes that lock plus the pinned packaging tools.
External helpers currently pin Deno 2.9.8 and FFmpeg/FFprobe 9.0.2, verifying
their official archive digests before installation.

## 2. Check the source

```powershell
.\.venv\Scripts\python.exe tools/run_tests.py
.\.venv\Scripts\python.exe tools/qt_smoke.py --platform offscreen
.\.venv\Scripts\python.exe tools/qt_smoke.py --platform offscreen --language tr
```

Resolve test failures before packaging.

## 3. Build the executable

Collect the installed dependency notices first. This records exact versions,
copies available license texts, downloads the pinned Deno MIT license, and
records FFmpeg/FFprobe hashes and build configuration:

```powershell
.\.venv\Scripts\python.exe tools/collect_licenses.py --fetch-upstream
```

Review `build/release-notices/THIRD_PARTY_NOTICES.md` and `inventory.json` using
the [license review instructions](license-review.md). Collection is not a
completed license review: corresponding sources and embedded library notices
still need checking against the final bundle. Recollect in the clean environment
used for every release; the output describes installed dependencies, some of
which may not be bundled.

Build the EXE and ZIP with the reusable helper:

```powershell
.\.venv\Scripts\python.exe tools/build_windows.py
```

It derives the Windows icon from `assets/app.svg`, uses the inputs listed below,
and writes `dist/BekoDLP-Windows-x64.exe` and
`dist/BekoDLP-1.0-Windows-x64.zip` (using the current `APP_VERSION`). Build caches
and the generated PyInstaller spec stay under `build/`.

The equivalent manual invocation is:

```powershell
$ffmpegPath = & .\.venv\Scripts\python.exe -c 'from utils import which_executable; print(which_executable(["ffmpeg"]) or "")'
$ffprobePath = & .\.venv\Scripts\python.exe -c 'from utils import which_executable; print(which_executable(["ffprobe"]) or "")'
if (-not $ffmpegPath -or -not $ffprobePath) { throw 'Install FFmpeg/FFprobe before building.' }
$translationsPath = & .\.venv\Scripts\python.exe -c 'from PySide6.QtCore import QLibraryInfo; print(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))'
$turkishCatalog = Join-Path $translationsPath 'qtbase_tr.qm'

foreach ($requiredFile in @($turkishCatalog, 'tools/runtime/deno.exe', 'tools/apply_update.ps1', 'build/release-notices/THIRD_PARTY_NOTICES.md')) {
    if (-not (Test-Path -LiteralPath $requiredFile)) {
        throw "Missing build input: $requiredFile"
    }
}

$buildArgs = @(
    '--noconfirm', '--clean', '--onefile', '--windowed', '--noupx',
    '--name', 'BekoDLP-Windows-x64',
    '--specpath', 'build',
    '--runtime-hook', 'tools/pyinstaller_runtime.py',
    '--add-binary', "${ffmpegPath}:.",
    '--add-binary', "${ffprobePath}:.",
    '--add-binary', 'tools/runtime/deno.exe:tools/runtime',
    '--add-data', 'assets/app.svg:assets',
    '--add-data', 'tools/apply_update.ps1:tools',
    '--add-data', "${turkishCatalog}:PySide6/translations",
    '--add-data', 'LICENSE:.',
    '--add-data', 'build/release-notices:licenses',
    '--collect-all', 'yt_dlp',
    '--collect-all', 'yt_dlp_ejs',
    '--collect-all', 'curl_cffi',
    '--recursive-copy-metadata', 'yt-dlp',
    '--recursive-copy-metadata', 'curl-cffi',
    '--copy-metadata', 'yt-dlp-ejs',
    '--copy-metadata', 'PySide6',
    '--copy-metadata', 'psutil',
    '--copy-metadata', 'Unidecode',
    'main.py'
)

& .\.venv\Scripts\python.exe -m PyInstaller @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
```

Output: **`dist/BekoDLP-Windows-x64.exe`**. PyInstaller's Qt hooks collect the imported Qt modules and their plugins. Explicit inputs above preserve the paths expected by the app for its icon, Deno, update helper, and Turkish dialog translations. The runtime hook adds the extracted bundle directory to `PATH` so the engine can discover bundled FFmpeg/FFprobe. Settings and logs remain beside the executable.

The collected notices are embedded under `licenses/` and can be opened from
**Help → Third-party licenses**. Also copy `build/release-notices/` into the
downloadable ZIP as `licenses/`, beside the EXE and the project's `LICENSE`, so
users can read the notices without starting the application.

## 4. Verify and publish

`tools/check_packaged_app.py` drives the actual EXE using Windows UI Automation
against local HTML5 media. It checks English video download, Turkish MP3
conversion with trimming, media output and clean shutdown, with system Python,
FFmpeg and Deno absent from the child process's `PATH`. It uses isolated settings
and downloads below `build/packaged-media-check/`. Run it in an interactive
Windows desktop session where UI Automation can access the test application's
own windows:

```powershell
.\.venv\Scripts\python.exe tools/check_packaged_app.py
```

This supplements, rather than replaces, a clean-machine check and a real
upgrade between packaged release versions.

Run the executable from a writable folder on a Windows x64 machine without Python, FFmpeg, or Deno installed. Check startup, English/Turkish dialogs, video/audio downloads, conversion, trimming, and update behavior. Passing source tests does not establish that the executable works; this recipe still requires a real packaged-build check.

Upload the executable to GitHub Releases as **`BekoDLP-Windows-x64.exe`**, keeping that name unchanged for the updater. Include corresponding source and applicable third-party notices with the release. Follow the [publication checklist](releasing.md) for final checks.

Build outputs under `build/` and `dist/` are ignored by Git. Keep the exact dependency versions and FFmpeg distribution used for each release in your release records.

References: [PyInstaller options](https://pyinstaller.org/en/stable/usage.html), [runtime paths](https://pyinstaller.org/en/stable/runtime-information.html), and [hooks](https://pyinstaller.org/en/stable/hooks.html).
