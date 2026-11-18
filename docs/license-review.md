# Binary release license review

The application's own code is GPL-3.0-only. Crediting an upstream project is
useful, but does not replace its license texts, copyright notices, or applicable
corresponding-source obligations. This document tracks release preparation; it
does not certify that an unexamined executable is cleared for distribution.

## Collected evidence

Run `tools/collect_licenses.py --fetch-upstream` in the clean build environment.
The ignored `build/release-notices/` directory contains:

- The application's GPL text and Python's runtime license text.
- Available installed-package notices, plus the ISC/MIT notices embedded in the EJS JavaScript bundle header.
- The pinned Deno MIT license and its executable hash.
- FFmpeg and FFprobe license output, executable hashes, version/configuration output, and any original `LICENSE`/`README.txt` adjacent to their installation.
- `inventory.json` with installed versions, metadata license declarations, notice hashes, and unresolved review items.

The generator follows required Python dependencies, not just the six top-level
requirements. The inventory is conservative: for example the PySide6
metapackage installs modules that this application may never bundle. Compare
it with PyInstaller's actual output. Commercial notices found in Qt wheels are
retained as supplied; this project does not claim a commercial Qt license.

## Findings in the inspected development environment

- FFmpeg/FFprobe identify as `9.0.2-full_build-www.gyan.dev`, built with
  `--enable-gpl --enable-version3`. Their original README identifies GPLv3 and
  FFmpeg source commit `946fcce07b`. No `--enable-nonfree` flag was observed.
- This FFmpeg build statically includes many external libraries. Its top-level
  GPL text and FFmpeg source commit do not by themselves document every
  library's source, version, patches, notices, and build scripts. Obtain and
  retain the corresponding build materials for the exact binary from its
  provider, or use a build for which those materials can be supplied.
- The Qt/PySide6 wheels inspected contain license metadata but do not provide a
  complete standalone collection of notices for every bundled Qt component.
  Review the modules/plugins actually shipped against their exact Qt/PySide
  sources, including the third-party notices in those sources.
- The Deno MIT text and curl-cffi package license do not establish the complete
  license inventory of their embedded native dependencies. Preserve those
  dependencies' required notices too.

These findings concern the inspected environment. They must be repeated if
versions, upstream binaries, optional modules, or packaging options change.

## What to publish together

1. Tag the exact application source used for the binary (for example `v1.0`),
   including requirements, build instructions, license, and local changes.
2. Provide the complete corresponding source/build materials required by the
   licenses of shipped GPL/LGPL components. For a network binary download,
   provide clear, durable access to those materials from the same release page.
   A generic upstream homepage or a future promise without the required terms
   is not a substitute. Do not describe the project's automatic GitHub source
   ZIP as containing third-party sources it does not include.
3. Keep the applicable license texts and copyright notices in the EXE and the
   ZIP. Include `licenses/`, the project `LICENSE`, and a source-access document
   identifying the exact published source assets/URLs. Do not list an upload
   that has not actually been published.
4. Inspect the final executable's collected DLLs, plugins and native libraries
   against the inventory. Add notices for extra components, and retain the
   version/hash records used for this release.

Once these steps are verified, record the actual source locations and the review
results with the release. The generated inventory deliberately remains marked
`REVIEW_REQUIRED`; running a collector is not a legal clearance decision.

## Upstream references

- [FFmpeg licensing and source/build information](https://ffmpeg.org/legal.html)
- [Gyan FFmpeg build variants](https://www.gyan.dev/ffmpeg/builds/)
- [Qt GPL/LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [Qt third-party components](https://doc.qt.io/qt-6/licenses-used-in-qt.html)
- [Qt for Python third-party notices](https://doc.qt.io/qtforpython-6/licenses.html)
- [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.html)
