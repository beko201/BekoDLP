# Release license review

BekoDLP's own code is GPL-3.0-only. Third-party tools and libraries retain their
licenses. Credits are listed in [libraries and sources](libraries-and-sources.md).

## Current status

Reviewed on 2026-11-18 for BekoDLP 1.0: **REVIEW_BLOCKED**.
This records incomplete evidence, not a finding of infringement or legal clearance.

| Component | Verified | Still needed |
| --- | --- | --- |
| Python, Qt/PySide6 and Curl | Shipped module inventory, available notices, exact-version source archives and pinned Curl native source hashes | Repeat the inventory when dependencies or packaging change |
| FFmpeg / ffprobe 9.0.2 (Gyan full build) | Binary hashes, configuration and core source commit `946fcce07b6dcd0331c8cc609192aeff5e1924f8`; core source retained | Exact revisions, patches, build inputs and notices for statically linked external libraries |
| Deno 2.9.8 | 1,048 Cargo archives checksum-verified; 21 of the original 70 notice gaps resolved | 49 package-level notice records remain unverified; a Windows dependency/link map and complete native notices are not established |

Twenty Deno records were resolved from exact-commit license files. For `valuable`
0.1.0, all 18 published Rust files match the official release tag. The remaining
49 include inaccessible notice locations, failed source comparisons and V8's
dirty release-tree provenance. The Cargo lockfile includes platform, build and
test dependencies; these are not 49 confirmed components of the Windows binary.

The [FFmpeg provider's reply to a similar request](https://github.com/GyanD/codexffmpeg/issues/254)
asks for an official-email request identifying the app and organization. That
reply concerns 8.1.2 and does not establish source coverage for our 9.0.2 build.
No provider messages have been sent on behalf of BekoDLP. Sources for another
FFmpeg provider must not be presented as sources for the shipped Gyan binaries.

## Evidence and maintenance

The generated records live outside source documentation:

- `build/release-notices/`: original license texts, `inventory.json`,
  `RELEASE_REVIEW.json` and `verification/` reports, retained in the separate
  license-evidence archive.
- `build/license-audit/review.json`: actual bundled file inventory and EXE hash,
  source archive hashes, exact remaining crate names and open items.
- `build/license-audit/runtime-notice-recheck.json` and
  `ffmpeg-source-recheck.json`: upstream URLs, HTTP results, hashes and comparisons.

`tools/collect_licenses.py --fetch-upstream` collects installed dependency notices.
`tools/audit_release_licenses.py` audits the built folder against retained source
evidence. `tools/verify_runtime_notices.py`, `tools/resolve_notice_origins.py` and
`tools/verify_ffmpeg_sources.py` refresh the targeted upstream checks when their
local evidence inputs are present. The audit requires those retained inputs;
it is not a fresh-clone, single-command legal review.

`tools/package_license_evidence.py` prepares the application source ZIP, partial
license-evidence ZIP and `SHA256SUMS.txt` from the existing audit. It does not
publish them or turn incomplete evidence into approval.

The application package's `licenses/` folder contains three consolidated files:
`THIRD_PARTY_NOTICES.md` (overview and review status), `NOTICES.txt` (original
notice texts) and `NOTICE_INDEX.json` (component/source paths, hashes and byte
offsets). `tools/prepare_release_notices.py` stores byte-identical notices once
while preserving every component mapping and each distinct copyright text.
Qt attribution records and the conservative Deno notice superset remain included.
Detailed investigation reports stay in the evidence archive; this consolidation
does not establish a Windows dependency map or close the outstanding gaps.

Before binary publication, resolve the remaining source/notice gaps, inspect the
final packaged files, and provide durable links to the matching application
source and required third-party materials. Record actual published locations;
a prepared local archive is not proof of publication.
