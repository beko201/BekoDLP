"""Package collected evidence without presenting it as completed legal clearance."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_NAME, APP_VERSION


def main():
    base = ROOT / 'build/license-audit'
    output = ROOT / 'dist'
    report = json.loads((base / 'review.json').read_text(encoding='utf-8'))
    evidence = output / f'{APP_NAME}-{APP_VERSION}-license-evidence.zip'
    with zipfile.ZipFile(evidence, 'w', compression=zipfile.ZIP_STORED) as target:
        target.writestr('README.md', '# Partial license evidence collection\n\n'
            'Review status: ' + report['status'] + '\n\n'
            'This is not a cleared release or complete FFmpeg corresponding source.\n'
            'The application uses consolidated notices; full originals are retained here under licenses/.\n'
            'Read review.json and licenses/RELEASE_REVIEW.md for concrete remaining gaps.\n'
            'Deno Cargo sources are an upstream dependency superset, not a Windows link map.\n'
            'Investigated alternative FFmpeg provider scripts are deliberately excluded.\n'
            'New provider page/comment caches remain local; their URLs and hashes are recorded in ffmpeg-source-recheck.json.\n'
            'No provider messages have been sent and no source upload is asserted.\n')
        target.write(base / 'review.json', 'review.json')
        distribution_index = output / APP_NAME / 'licenses/NOTICE_INDEX.json'
        if distribution_index.is_file():
            target.write(distribution_index, 'distribution-notice-index.json')
        for filename in ('deno-cargo-sources.json', 'curl-native-sources.json',
                         'qt-source-downloads.json', 'additional-downloads.json',
                         'runtime-notice-recheck.json', 'ffmpeg-source-recheck.json'):
            target.write(base / filename, filename)
        for filename in ('deno-cargo.lock', 'curlimp-CMakeLists.txt', 'curlimp-CollectLicenses.cmake.in',
                         'curl-cffi-Makefile', 'pyside-source-sha256.txt', 'gyan-release.json', 'gyan-repository.json'):
            target.write(base / 'upstream' / filename, 'upstream/' + filename)
        for record in report['source_archives']:
            source = base / 'sources' / record['filename']
            target.write(source, 'sources/' + source.name)
        for source in sorted((base / 'deno-crates').glob('*.crate')):
            target.write(source, 'deno-cargo-sources/' + source.name)
        notices = ROOT / 'build/release-notices'
        for source in sorted(notices.rglob('*')):
            if source.is_file():
                target.write(source, 'licenses/' + source.relative_to(notices).as_posix(),
                             compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
        for name in ('license-review.md',):
            target.write(ROOT / 'docs' / name, 'review-documents/' + name)

    source_zip = output / f'{APP_NAME}-{APP_VERSION}-source.zip'
    files = [*ROOT.glob('*.py'), *ROOT.glob('requirements*.txt'),
             ROOT / 'README.md', ROOT / 'LICENSE', ROOT / '.gitignore']
    for folder in ('ui', 'tools', 'tests', 'docs', 'assets'):
        for source in (ROOT / folder).rglob('*'):
            if not source.is_file() or any(part in ('__pycache__', 'runtime', 'ui-previews')
                                           for part in source.relative_to(ROOT).parts):
                continue
            if folder == 'docs' and source.suffix != '.md':
                continue
            if folder in ('ui', 'tools', 'tests') and source.suffix not in ('.py', '.ps1'):
                continue
            files.append(source)
    with zipfile.ZipFile(source_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for source in sorted(set(files)):
            target.write(source, source.relative_to(ROOT).as_posix())

    lines = []
    for source in sorted(output.iterdir()):
        if source.suffix not in ('.exe', '.zip'):
            continue
        with source.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        lines.append(f'{sha}  {source.name}')
        print(source.name, f'{source.stat().st_size / 1024 / 1024:.1f} MiB', flush=True)
    (output / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', encoding='ascii')


if __name__ == '__main__':
    main()
