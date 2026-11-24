"""Package collected evidence without presenting it as completed legal clearance."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_NAME, APP_VERSION


def source_files():
    files = [*ROOT.glob('*.py'), *ROOT.glob('requirements*.txt'),
             ROOT / 'README.md', ROOT / 'LICENSE', ROOT / '.gitignore', ROOT / 'THIRD_PARTY_NOTICES.md']
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
    return sorted(set(files))


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_source_archive(path, files):
    temporary = path.with_suffix('.zip.tmp')
    if temporary.exists():
        raise ValueError('A temporary source archive already exists.')
    with zipfile.ZipFile(temporary, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source in files:
            if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
                raise ValueError('Source archive input escapes the project.')
            archive.write(source, source.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:
            raise ValueError('Source archive integrity check failed.')
    temporary.replace(path)


def evidence_files(report):
    base = ROOT / 'build/license-audit'
    yield base / 'review.json', 'review.json'
    index = ROOT / 'dist' / APP_NAME / 'licenses/NOTICE_INDEX.json'
    if index.is_file():
        yield index, 'distribution-notice-index.json'
    for name in ('deno-cargo-sources.json', 'curl-native-sources.json', 'qt-source-downloads.json',
                 'additional-downloads.json', 'runtime-notice-recheck.json', 'ffmpeg-source-recheck.json'):
        yield base / name, name
    for name in ('deno-cargo.lock', 'curlimp-CMakeLists.txt', 'curlimp-CollectLicenses.cmake.in',
                 'curl-cffi-Makefile', 'pyside-source-sha256.txt', 'gyan-release.json', 'gyan-repository.json'):
        yield base / 'upstream' / name, 'upstream/' + name
    for record in report['source_archives']:
        path = base / 'sources' / record['filename']
        if file_hash(path) != record['sha256']:
            raise ValueError('Source evidence checksum changed: ' + path.name)
        yield path, 'sources/' + path.name
    for record in json.loads((base / 'deno-cargo-sources.json').read_text(encoding='utf-8')):
        path = base / 'deno-crates' / record['filename']
        if file_hash(path) != record['sha256']:
            raise ValueError('Cargo source evidence checksum changed: ' + path.name)
        yield path, 'deno-cargo-sources/' + path.name
    notices = ROOT / 'build/release-notices'
    for path in sorted(notices.rglob('*')):
        if path.is_file():
            yield path, 'licenses/' + path.relative_to(notices).as_posix()
    yield ROOT / 'docs/third-party-licenses.md', 'documentation/third-party-licenses.md'


def prepare_release_files():
    """Create source and explicitly partial evidence; never manufacture a binary."""
    from tools.audit_release_licenses import preflight
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    planned = preflight(APP_VERSION)
    baseline = json.loads((ROOT / 'build/license-audit/review.json').read_text(encoding='utf-8'))
    if baseline.get('phase') != 'existing-binary':
        raise ValueError('Preparation requires a clearly identified existing-binary baseline.')
    executable = output / APP_NAME / 'BekoDLP.exe'
    if file_hash(executable) != baseline['scope']['exe_sha256']:
        raise ValueError('The baseline executable changed after its review.')
    source = output / f'{APP_NAME}-{APP_VERSION}-source.zip'
    files = source_files()
    expected = {row['path']: row['sha256'] for row in planned['source_inputs']}
    if {p.relative_to(ROOT).as_posix(): file_hash(p) for p in files} != expected:
        raise ValueError('Source archive inputs differ from the prebuild fingerprints.')
    write_source_archive(source, files)
    evidence = output / f'{APP_NAME}-{APP_VERSION}-partial-license-evidence.zip'
    temporary = evidence.with_suffix('.zip.tmp')
    if temporary.exists():
        raise ValueError('A temporary evidence archive already exists.')
    with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('README.md', f'# {APP_NAME} {APP_VERSION}: partial prebuild evidence\n\n'
            f'The target version is {APP_VERSION}. No target Windows package was built or reviewed.\n'
            f'baseline/ contains evidence from the existing {baseline["version"]} binary review.\n'
            'preflight.json fingerprints the current source and lists outstanding items.\n'
            'The retained sources and notices are incomplete and are not a compliance approval.\n'
            'FFmpeg dependency sources, Deno notices and its Windows link map, standalone\n'
            'yt-dlp dependency coverage, final package notices and publication remain open.\n'
            'Nothing in this archive establishes that these materials are publicly hosted.\n')
        archive.writestr('preflight.json', json.dumps(planned, indent=2) + '\n')
        for path, name in evidence_files(baseline):
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise ValueError('Evidence input escapes the project.')
            archive.write(path, 'baseline/' + name,
                          compress_type=zipfile.ZIP_DEFLATED if name.startswith('licenses/') else zipfile.ZIP_STORED)
    with zipfile.ZipFile(temporary) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())) or archive.testzip() is not None:
            raise ValueError('Evidence archive integrity check failed.')
    temporary.replace(evidence)
    assets = [{'name': path.name, 'size': path.stat().st_size, 'sha256': file_hash(path)}
              for path in (source, evidence)]
    status = {'application': APP_NAME, 'version': APP_VERSION, 'phase': 'prebuild',
              'release_ready': False, 'review_status': planned['status'], 'published': False,
              'baseline_version': baseline['version'], 'assets': assets,
              'windows_package': {'name': f'{APP_NAME}-{APP_VERSION}-Windows-x64.zip',
                                  'ready': False, 'reason': 'Build and actual target-binary review are pending.'},
              'open_items': planned['open_items']}
    status_path = output / f'{APP_NAME}-{APP_VERSION}-release-status.json'
    status_path.write_text(json.dumps(status, indent=2) + '\n', encoding='utf-8')
    note = output / f'{APP_NAME}-{APP_VERSION}-release-status.md'
    note.write_text('\n'.join([f'# {APP_NAME} {APP_VERSION}: local release preparation', '',
        '**Not ready for binary publication. Nothing was uploaded.**', '',
        f'- `{source.name}`: current source with the updater launch fix.',
        f'- `{evidence.name}`: partial evidence with the older binary baseline explicitly labelled.',
        f'- `{APP_NAME}-{APP_VERSION}-Windows-x64.zip`: not prepared; no target build was performed.', '',
        '## Outstanding items', '', *['- ' + str(item['detail']) for item in planned['open_items']], '',
        'When those items are resolved and the actual target binary is reviewed, publish matching',
        'source/material assets with durable release links. Local files do not establish publication.', '']), encoding='utf-8')
    checksums = output / f'{APP_NAME}-{APP_VERSION}-SHA256SUMS.txt'
    checksums.write_text(''.join(file_hash(path) + '  ' + path.name + '\n'
                                 for path in (source, evidence, status_path, note)), encoding='ascii')
    for path in (source, evidence, status_path, note, checksums):
        print('Prepared:', path.name, flush=True)
    return status


def main():
    base = ROOT / 'build/license-audit'
    output = ROOT / 'dist'
    report = json.loads((base / 'review.json').read_text(encoding='utf-8'))
    if (report.get('application'), report.get('version')) != (APP_NAME, APP_VERSION):
        raise ValueError('The binary review does not match this source version. '
                         'Build and audit the actual release before packaging its evidence.')
    if report.get('phase') == 'prebuild':
        raise ValueError('A prebuild review cannot be packaged as a completed binary review.')
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
        for name in ('third-party-licenses.md',):
            target.write(ROOT / 'docs' / name, 'documentation/' + name)

    source_zip = output / f'{APP_NAME}-{APP_VERSION}-source.zip'
    write_source_archive(source_zip, source_files())

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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true',
                        help='Prepare source, partial prebuild evidence and checksums without building or publishing.')
    args = parser.parse_args()
    prepare_release_files() if args.prepare else main()
