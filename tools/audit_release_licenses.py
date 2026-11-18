"""Record actual bundled modules, collect source notices, and retain concrete gaps.

No command-line switch can turn unresolved source/notice gaps into approval.
Run after collect_licenses.py and after building the actual Windows executable.
"""
from importlib.metadata import distribution, distributions, packages_distributions
import ast
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_NAME, APP_VERSION, APP_EXECUTABLE
from tools.collect_licenses import collect_distribution, digest


def notice_name(path):
    return (path.suffix.lower() not in ('.c', '.cc', '.cpp', '.h', '.hpp', '.rs', '.py', '.js')
            and (bool(re.match(r'(?i)(licen[cs]e|copying|notice|copyright|authors)', path.name))
                 or path.name == 'qt_attribution.json'
                 or ('LICENSES' in path.parts and path.suffix == '.txt')))


def extract_notices(source, target):
    """Copy only bounded regular notice files, never extract an archive wholesale."""
    found = []
    def save(name, content):
        path = PurePosixPath(name.replace('\\', '/'))
        if path.is_absolute() or '..' in path.parts or any(':' in part for part in path.parts):
            raise ValueError('Unsafe source archive path')
        output = target.joinpath(*path.parts)
        if not output.resolve().is_relative_to(target.resolve()):
            raise ValueError('Source notice escapes its output directory')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        found.append(output)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for entry in archive.infolist():
                if not entry.is_dir() and entry.file_size <= 4 * 1024 * 1024 and notice_name(PurePosixPath(entry.filename)):
                    save(entry.filename, archive.read(entry))
    else:
        with tarfile.open(source, 'r:*') as archive:
            for entry in archive:
                if entry.isfile() and entry.size <= 4 * 1024 * 1024 and notice_name(PurePosixPath(entry.name)):
                    save(entry.name, archive.extractfile(entry).read())
    return found


def bundled_scope(executable):
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith('.pyz')))
    top_levels = sorted({name.split('.')[0] for name in pyz.toc})
    owners = packages_distributions()
    analysis_path = ROOT / 'build/pyinstaller-folder/work' / APP_NAME / 'Analysis-00.toc'
    analysis = ast.literal_eval(analysis_path.read_text(encoding='utf-8'))
    source_by_module = {name: source for name, source, kind in analysis[14]}
    source_by_file = {name.replace('\\', '/'): source for name, source, kind in analysis[15] + analysis[18]}
    # Namespace packages can belong to multiple wheels. Attribute the actual
    # files rather than claiming unused Qt Pdf/WebEngine wheels were shipped.
    record_owners = {}
    for package in distributions():
        for entry in package.files or []:
            record_owners[str(Path(package.locate_file(entry)).resolve()).casefold()] = package.metadata['Name']
    app_modules = {path.stem for path in ROOT.glob('*.py')} | {'ui'}
    packages, unresolved = set(), []
    for name in top_levels:
        if name == 'PySide6':
            packages.add('PySide6')  # The application's diagnostics read its metadata.
            continue
        matches = owners.get(name, [])
        if matches:
            packages.update(matches)
        elif name in sys.stdlib_module_names or name in app_modules or name.startswith('_pyi_'):
            continue
        else:
            unresolved.append(name)
    for name in pyz.toc:
        source = source_by_module.get(name)
        if source:
            owner = record_owners.get(str(Path(source).resolve()).casefold())
            if owner:
                packages.add(owner)
    actual_files = {path.relative_to(executable.parent / '_internal').as_posix()
                    for path in (executable.parent / '_internal').rglob('*') if path.is_file()}
    for name, source in source_by_file.items():
        if name in actual_files:
            owner = record_owners.get(str(Path(source).resolve()).casefold())
            if owner:
                packages.add(owner)
    packages.add('PyInstaller')  # The outer executable contains its bootloader.
    packages = {distribution(name).metadata['Name'] for name in packages}
    return {'exe_sha256': digest(executable), 'archive_entries': len(archive.toc),
            'python_top_levels': top_levels, 'packages': sorted(packages, key=str.lower),
            'unattributed_python_top_levels': unresolved,
            'native_files': sorted(name for name in actual_files if name.endswith(('.dll', '.pyd')))}


def cargo_notice_gaps(cargo, notice_folder):
    """A metadata label or an unaudited dirty VCS base must not close a notice gap."""
    missing = []
    root = Path(notice_folder).resolve()
    for record in cargo:
        verified = []
        dirty = (record.get('vcs') or {}).get('git', {}).get('dirty', False)
        for name in record.get('notices', []):
            relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or any(':' in part or '\\' in part for part in relative.parts):
                raise ValueError('Unsafe Cargo notice path')
            if not relative.parts or relative.parts[0] != f"{record['name']}-{record['version']}":
                raise ValueError('Cargo notice belongs to a different package')
            package_level = len(relative.parts) == 2 or relative.parts[1:2] in (('LICENSES',), ('licenses',))
            if not package_level:
                continue
            path = root.joinpath(*relative.parts)
            if not path.resolve().is_relative_to(root) or not path.is_file():
                continue
            if relative.name.startswith('upstream-'):
                if dirty:
                    continue
                known = {item['sha256'] for item in record.get('upstream_notice_sources', [])}
                if digest(path) not in known:
                    continue
            verified.append(name)
        if not verified:
            reason = ('Notice found at the base VCS commit, but Cargo marks the source tree dirty; '
                      'the published release notice provenance remains unverified.' if dirty else
                      'No package-level license/notice text verified; nested notices do not close this gap.')
            missing.append({'name': record['name'], 'version': record['version'],
                            'source_url': record['url'], 'source_sha256': record['sha256'], 'reason': reason})
    return missing


def audit():
    base = ROOT / 'build/license-audit'
    notices = ROOT / 'build/release-notices'
    executable = ROOT / 'dist' / APP_NAME / APP_EXECUTABLE
    scope = bundled_scope(executable)
    package_records = [collect_distribution(distribution(name), notices) for name in scope['packages']]
    sources = []
    for source in sorted((base / 'sources').iterdir()):
        if not source.is_file() or source.name.startswith('ffmpeg-btbn-'):
            continue  # An investigated alternative is not provenance for shipped Gyan binaries.
        copied = extract_notices(source, notices / 'source-notices' / source.name)
        sources.append({'filename': source.name, 'sha256': digest(source), 'size': source.stat().st_size,
                        'notice_files': [path.relative_to(notices).as_posix() for path in copied]})
    cargo = json.loads((base / 'deno-cargo-sources.json').read_text(encoding='utf-8'))
    # A nested dependency's notice is not evidence of the parent crate license.
    root_missing = cargo_notice_gaps(cargo, notices / 'deno/cargo-notices')
    verification = {}
    for name in ('runtime-notice-recheck.json', 'ffmpeg-source-recheck.json'):
        source = base / name
        if source.is_file():
            verification[name] = json.loads(source.read_text(encoding='utf-8'))
            destination = notices / 'verification' / name
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(source, destination)
    # Earlier broad matching could collect these algorithm files as notices.
    for path in (notices / 'deno/cargo-notices').rglob('*'):
        if path.is_file() and path.suffix.lower() in ('.c', '.cc', '.cpp', '.h', '.hpp', '.rs'):
            path.unlink()
    issues = [
        {'id': 'ffmpeg-corresponding-source', 'status': 'blocked',
         'detail': 'Gyan 9.0.2 identifies core FFmpeg commit 946fcce07b6dcd0331c8cc609192aeff5e1924f8; its core source archive is retained. The inspected release/repository does not supply the statically linked libraries exact source/build manifest. The provider requests an official-email source request identifying the application/organization. Alternative BtbN scripts are not evidence for these Gyan executable hashes.',
         'evidence': ['ffmpeg/version-and-configuration.txt', 'ffmpeg/README.txt', 'verification/ffmpeg-source-recheck.json']},
        {'id': 'deno-package-notices', 'status': 'blocked' if root_missing else 'collected',
         'detail': f'{len(cargo)} Cargo source archives verified against the upstream lockfile; {len(root_missing)} package-level notice texts remain unverified. The lockfile includes a platform/build/test dependency superset, not a proven Windows link map.'},
        {'id': 'publication', 'status': 'pending-publication',
         'detail': 'Upload the exact application source and applicable third-party source materials with durable access directions. Prepared local archives do not establish that these uploads are publicly available.'},
    ]
    if scope['unattributed_python_top_levels']:
        issues.append({'id': 'python-attribution', 'status': 'blocked', 'detail': scope['unattributed_python_top_levels']})
    for package in package_records:
        if not package['notices']:
            issues.append({'id': 'missing-python-notice', 'status': 'blocked', 'detail': package['name']})
    unsupported_qt = [name for name in scope['native_files'] if any(
        token in name for token in ('Qt6Pdf', 'Qt6Qml', 'Qt6Quick', 'Qt6VirtualKeyboard', 'opengl32sw'))]
    if unsupported_qt:
        issues.append({'id': 'extra-qt-source-coverage', 'status': 'blocked',
                       'detail': 'These bundled modules need additional source/notice coverage or must be excluded as unused: ' + ', '.join(unsupported_qt)})
    status = 'REVIEW_BLOCKED' if any(item['status'] == 'blocked' for item in issues) else 'PUBLICATION_PENDING'
    report = {'application': APP_NAME, 'version': APP_VERSION, 'status': status,
              'scope': scope, 'actual_python_packages': package_records, 'source_archives': sources,
              'deno_source_count': len(cargo), 'deno_package_notices_unverified': root_missing,
              'checks_completed': ['Actual EXE Python/native file inventory recorded with EXE hash.',
                                   'Notices collected for actual bundled Python package owners, including packaging tools if present.',
                                   'Qt/PySide6 source notices and copyright files collected from exact-version source archives.',
                                   'Curl native dependency source archives verified against curl-impersonate v2.3.1 CMake hashes.',
                                   'Deno Cargo source archives verified against upstream Cargo.lock checksums.'],
              'open_items': issues,
              'runtime_notice_recheck': {'requested_records': len(verification.get('runtime-notice-recheck.json', {}).get('records', [])),
                                         'remaining_records': len(root_missing),
                                         'dirty_release_tree_records': [r['name'] for r in cargo if (r.get('vcs') or {}).get('git', {}).get('dirty')
                                                                        and any(m['name'] == r['name'] and m['version'] == r['version'] for m in root_missing)]}}
    (base / 'review.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    # The final EXE hash stays external: embedding it into the same EXE would
    # change that hash again. The embedded report describes its module plan.
    embedded_scope = {key: value for key, value in scope.items() if key not in ('exe_sha256', 'archive_entries')}
    embedded_report = {**report, 'scope': embedded_scope}
    (notices / 'RELEASE_REVIEW.json').write_text(json.dumps(embedded_report, indent=2), encoding='utf-8')
    manifest_path = notices / 'inventory.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.update(status=report['status'], actual_bundle_scope=embedded_scope,
                    actual_bundled_python_packages=package_records, review_required=[item['detail'] for item in issues])
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    text = '\n'.join([f'# {APP_NAME} {APP_VERSION} release license review', '',
        f'Status: {status}. This report records concrete checks and remaining evidence gaps; it is not legal clearance.', '',
        '## Completed checks', *['- ' + value for value in report['checks_completed']], '',
        '## Open items', *['- ' + str(value['detail']) for value in issues], '',
        'See RELEASE_REVIEW.json for source archive hashes, actual bundled packages, and unresolved crate notices.', ''])
    (notices / 'RELEASE_REVIEW.md').write_text(text, encoding='utf-8')
    index = notices / 'THIRD_PARTY_NOTICES.md'
    original = index.read_text(encoding='utf-8')
    marker = '\n## Actual release review\n'
    original = original.split(marker)[0].split('\n## Review before publication')[0]
    index.write_text(original + marker + '\n' + text, encoding='utf-8')
    print('Status:', report['status'])
    print('Actual bundled Python distributions:', len(package_records))
    print('Verified source archives:', len(sources))
    print('Unverified Deno package notice texts:', len(root_missing))
    return report


if __name__ == '__main__':
    audit()
