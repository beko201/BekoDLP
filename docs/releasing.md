# Publication checklist

This project was developed with AI using Claude and ChatGPT.

## Source repository

- Validate in a clean Python 3.13 Windows x64 development environment.
- Run the full test suite and both English and Turkish Qt smoke flows described in [technical details](technical-details.md#maintenance-and-validation).
- Keep the project's `LICENSE` file (GPL-3.0-only) and the README license declaration in source releases. Preserve applicable third-party license notices separately.
- Review the files staged for publication. Local logs, settings, virtual environments, portable runtimes, benchmark JSON files, and build outputs are excluded by `.gitignore`; files already tracked by Git are not removed by ignore rules.

## Windows binary release

- Keep `APP_VERSION` in `app_info.py` aligned with the release tag.
- Follow the [Windows build guide](building.md) and record the exact dependencies and external binaries used.
- Include Python dependencies, FFmpeg, FFprobe, compatible Deno/EJS, application assets, Qt platform plugins, and Qt's Turkish translation catalog (`qtbase_tr.qm`). Verify executable discovery in the packaged application.
- Include applicable dependency and bundled-tool license notices for the exact distributions shipped.
- Generate `build/release-notices/` with `tools/collect_licenses.py --fetch-upstream` and complete the [binary license review](license-review.md), including corresponding sources and embedded-library notices. The generated inventory alone is not release clearance.
- Test the extracted build on a Windows machine without Python, FFmpeg, or Deno installed. Check English/Turkish switching, video and audio downloads, trimming, pause/cancel, and diagnostic export.
- Publish binaries as release assets; keep `build/`, `dist/`, and downloaded runtime executables out of source commits.

## GitHub update feed

- The configured repository is `beko201/BekoDLP`; change `UPDATE_REPOSITORY` in `app_info.py` if the final repository name differs. The repository must be public for the unauthenticated updater.
- The first public build must already contain the updater; older builds without it cannot acquire it automatically.
- Build a **single-file Windows x64 application**, not an installer or an executable that needs a separate `_internal` folder. Bundle the helper at `tools/apply_update.ps1` along with the runtime dependencies described above.
- Publish a stable GitHub Release with a numeric tag such as `v1.1` or `v1.1.0`. Keep the built application's `APP_VERSION` equal to that version. Drafts and prereleases are not installed.
- Upload the executable with the exact, stable asset name **`BekoDLP-Windows-x64.exe`** (`UPDATE_ASSET_NAME`). GitHub must report its completed upload, size, and `sha256:` digest through the release API. A missing or invalid digest prevents downloading/installing; there is no unchecked fallback.
- The updater compares numeric versions, checks the repository/asset URL, streams into a temporary folder next to the executable, verifies SHA-256, byte count, and Windows x64 PE header, and asks again before installation. The executable's folder must be writable; no automatic elevation is requested.
- Installation checks unfinished tasks, assigned workers (including terminal-row cleanup), and pending link resolution even while paused. The final idle check and shutdown admission share the manager lock, preventing new work from being cancelled between checking and closing. A refused installation keeps the verified update and preferences usable for the session.
- After all tasks stop and preferences flush, a hidden PowerShell helper waits for the application to exit, rechecks both files, atomically replaces the executable with a backup, and restarts. If replacement or immediate process launch fails, it preserves or restores the previous executable. This does not detect crashes after a successful launch.
- Settings and logs are kept next to the installed executable, outside one-file extraction folders. They are not overwritten by the updater. Declined/cancelled downloads are removed; deferred verified downloads are retained only for the current session. A failed helper leaves `update-error.txt` and any recovery files in its `.bekodlp-update-*` folder.
- Provide corresponding source code for the GPL release, and validate an actual upgrade between two packaged versions on a clean Windows machine before publishing. Source-mode tests cannot certify the bundle or GitHub asset configuration.
