"""Record actual bundled modules, collect source notices, and retain concrete gaps.

No command-line switch can turn unresolved source/notice gaps into approval.
Run after collect_licenses.py and after building the actual Windows executable.
"""
from importlib.metadata import distribution, distributions, packages_distributions
import argparse
import ast
from datetime import datetime, timezone
import dis
import hashlib
import json
import marshal
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import types
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_NAME, APP_VERSION, APP_EXECUTABLE
from tools.collect_licenses import collect_distribution, digest
from package_layout import MANAGED_ENTRIES, MANIFEST_NAME, parse_manifest, inspect_package


def code_identity(code):
    """Read literal identity constants without executing bundled application code."""
    wanted = {'APP_NAME', 'APP_VERSION', 'APP_EXECUTABLE'}
    identity, previous = {}, None
    for instruction in dis.get_instructions(code):
        if instruction.opname == 'STORE_NAME' and instruction.argval in wanted:
            if previous is None or previous.opname != 'LOAD_CONST' or not isinstance(previous.argval, str):
                raise ValueError('Bundled application identity is not a literal constant.')
            identity[instruction.argval] = previous.argval
        previous = instruction
    if set(identity) != wanted:
        raise ValueError('Bundled application identity is incomplete.')
    return identity


def validate_identity(manifest, identity, expected_version):
    if (manifest['version'] != identity['APP_VERSION'] or manifest['app'] != identity['APP_NAME']
            or identity['APP_NAME'] != APP_NAME or identity['APP_EXECUTABLE'] != APP_EXECUTABLE):
        raise ValueError('Package manifest and compiled application identity do not match.')
    if identity['APP_VERSION'] != expected_version:
        raise ValueError(f"Expected {expected_version}, but the executable is {identity['APP_VERSION']}. "
                         'Build the target version before its binary audit, or use --preflight.')


def verify_package_folder(folder, manifest):
    actual = set()
    for name in MANAGED_ENTRIES:
        if name == MANIFEST_NAME:
            continue
        entry = folder / name
        for path in entry.rglob('*') if entry.is_dir() else (entry,):
            if path.is_file():
                actual.add(path.relative_to(folder).as_posix())
    if actual != {row['path'] for row in manifest['files']}:
        raise ValueError('Managed package files do not match the manifest file list.')
    for row in manifest['files']:
        path = folder.joinpath(*PurePosixPath(row['path']).parts)
        if (not path.resolve().is_relative_to(folder.resolve()) or path.is_symlink()
                or not path.is_file() or path.stat().st_size != row['size'] or digest(path) != row['sha256']):
            raise ValueError('Packaged file does not match its manifest: ' + row['path'])


def normalized_code(code):
    return code.replace(co_filename='<source>', co_consts=tuple(
        normalized_code(value) if isinstance(value, types.CodeType) else value for value in code.co_consts))


def compare_application_source(executable, source_zip):
    """Compile source for comparison only; never execute its code or imports."""
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith('.pyz')))
    own_top_levels = {path.stem for path in ROOT.glob('*.py')} | {'ui'}
    modules = [name for name in pyz.toc if name.split('.')[0] in own_top_levels] + ['main']
    matched, missing, different = [], [], []
    with zipfile.ZipFile(source_zip) as source:
        if len(source.namelist()) != len(set(source.namelist())):
            raise ValueError('Application source ZIP contains duplicate paths.')
        for name in sorted(modules):
            path = name.replace('.', '/') + '.py'
            if path not in source.namelist():
                path = name.replace('.', '/') + '/__init__.py'
            if path not in source.namelist():
                missing.append(name)
                continue
            if source.getinfo(path).file_size > 4 * 1024 ** 2:
                raise ValueError('Oversized application source module.')
            built = marshal.loads(archive.extract('main')) if name == 'main' else pyz.extract(name)
            candidate = compile(source.read(path), path, 'exec', dont_inherit=True, optimize=0)
            (matched if normalized_code(built) == normalized_code(candidate) else different).append(name)
    return {'archive': str(source_zip.relative_to(ROOT)), 'sha256': digest(source_zip),
            'method': 'Recursive code-object equality with embedded modules, ignoring only co_filename; no code executed.',
            'matched_modules': matched, 'missing_modules': missing, 'different_modules': different,
            'all_bundled_application_modules_match': not missing and not different}


def verify_release_archive(path, manifest_content):
    with zipfile.ZipFile(path) as archive:
        manifest, entries, content = inspect_package(archive)
        if content != manifest_content:
            raise ValueError('Release ZIP and assessed folder have different manifests.')
        for row in manifest['files']:
            with archive.open(entries[row['path']]) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                    raise ValueError('Release ZIP file checksum mismatch: ' + row['path'])
    return {'archive': str(path.relative_to(ROOT)), 'sha256': digest(path),
            'files_verified': len(manifest['files']), 'matches_assessed_folder': True}


def preflight(target_version=APP_VERSION):
    """Prepare a future release review without relabelling an existing binary."""
    base = ROOT / 'build/license-audit'
    baseline_path = base / 'review.json'
    baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
    if not re.fullmatch(r'\d+(?:\.\d+){0,3}', target_version):
        raise ValueError('Invalid target release version.')
    pending = [dict(item) for item in baseline.get('open_items', [])]
    pending.append({'id': 'target-binary-review', 'status': 'pending-build',
                    'detail': f'Build and audit the actual {target_version} executable and ZIP. '
                              f"The existing binary review covers {baseline['version']} only."})
    if target_version != APP_VERSION:
        pending.append({'id': 'target-source-version', 'status': 'pending',
                        'detail': f'Source APP_VERSION is {APP_VERSION}; set it to {target_version} '
                                  'when preparing that release, then recollect notices for the same version.'})
    paths = [*ROOT.glob('*.py'), *ROOT.glob('requirements*.txt'),
             ROOT / 'README.md', ROOT / 'LICENSE', ROOT / '.gitignore', ROOT / 'THIRD_PARTY_NOTICES.md']
    for name in ('ui', 'tools', 'tests', 'docs', 'assets'):
        paths.extend(path for path in (ROOT / name).rglob('*') if path.is_file()
            and not any(part in ('__pycache__', 'runtime', 'ui-previews') for part in path.relative_to(ROOT).parts)
            and (name == 'assets' or path.suffix in ('.py', '.ps1', '.md')))
    source_inputs = [{'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path)}
                     for path in sorted(set(paths)) if path.is_file()]
    report = {'application': APP_NAME, 'version': target_version, 'source_version': APP_VERSION, 'phase': 'prebuild',
              'reviewed_at': datetime.now(timezone.utc).isoformat(), 'status': 'REVIEW_BLOCKED',
              'target_binary_review_complete': False,
              'baseline': {'version': baseline['version'], 'reviewed_at': baseline.get('reviewed_at'),
                           'review_sha256': digest(baseline_path), 'exe_sha256': baseline['scope']['exe_sha256']},
              'deno_package_notices_unverified': baseline['deno_package_notices_unverified'],
              'source_inputs': source_inputs, 'open_items': pending}
    output = base / 'releases' / target_version
    output.mkdir(parents=True, exist_ok=True)
    (output / 'preflight.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    (output / 'preflight.md').write_text('\n'.join([
        f'# {APP_NAME} {target_version} prebuild license review', '', '**REVIEW_BLOCKED**.', '',
        f"The existing binary assessment is for {baseline['version']}, not {target_version}.",
        'No target application was built, packaged or published by this check.', '',
        f'{len(source_inputs)} source/build/documentation inputs were fingerprinted.', '',
        '## Open items', '', *['- ' + str(item['detail']) for item in pending], '']), encoding='utf-8')
    print(f'{target_version}: prebuild review prepared; target binary review remains pending.')
    return report


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
    native_matches, native_mismatches = [], []
    for name, source in source_by_file.items():
        if name in actual_files:
            owner = record_owners.get(str(Path(source).resolve()).casefold())
            if owner:
                packages.add(owner)
                if name.endswith(('.dll', '.pyd')):
                    shipped = executable.parent / '_internal' / name
                    if Path(source).is_file() and digest(shipped) == digest(Path(source)):
                        native_matches.append(name)
                    else:
                        native_mismatches.append(name)
    packages.add('PyInstaller')  # The outer executable contains its bootloader.
    packages = {distribution(name).metadata['Name'] for name in packages}
    return {'exe_sha256': digest(executable), 'archive_entries': len(archive.toc),
            'python_top_levels': top_levels, 'packages': sorted(packages, key=str.lower),
            'unattributed_python_top_levels': unresolved,
            'build_analysis_sha256': digest(analysis_path),
            'wheel_owned_native_files_hash_matched': native_matches,
            'wheel_owned_native_files_hash_mismatched': native_mismatches,
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


def audit(*, expected_version=APP_VERSION):
    base = ROOT / 'build/license-audit'
    notices = ROOT / 'build/release-notices'
    executable = ROOT / 'dist' / APP_NAME / APP_EXECUTABLE
    from PyInstaller.archive.readers import CArchiveReader
    bundle = CArchiveReader(str(executable))
    pyz = bundle.open_embedded_archive(next(name for name in bundle.toc if name.endswith('.pyz')))
    identity = code_identity(pyz.extract('app_info'))
    manifest_path = executable.parent / MANIFEST_NAME
    package_manifest = parse_manifest(manifest_path.read_bytes())
    validate_identity(package_manifest, identity, expected_version)
    verify_package_folder(executable.parent, package_manifest)
    version = identity['APP_VERSION']
    release_path = ROOT / 'dist' / f'{APP_NAME}-{version}-Windows-x64.zip'
    release_package = verify_release_archive(release_path, manifest_path.read_bytes()) if release_path.is_file() else None
    previous_path = base / 'review.json'
    if previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding='utf-8'))
        old_version = previous.get('version', '')
        if old_version != version and re.fullmatch(r'\d+(?:\.\d+){0,3}', old_version):
            history = base / 'releases' / old_version
            history.mkdir(parents=True, exist_ok=True)
            if not (history / 'review.json').exists():
                shutil.copy2(previous_path, history / 'review.json')
                for name in ('RELEASE_REVIEW.json', 'RELEASE_REVIEW.md'):
                    if (notices / name).is_file():
                        shutil.copy2(notices / name, history / name)
    scope = bundled_scope(executable)
    scope.update(embedded_identity=identity, package_manifest_sha256=digest(manifest_path),
                 package_manifest_files_verified=len(package_manifest['files']))
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
    if scope['wheel_owned_native_files_hash_mismatched']:
        issues.append({'id': 'native-wheel-attribution', 'status': 'blocked',
                       'detail': 'Packaged native files differ from the wheel files used for source attribution: '
                                 + ', '.join(scope['wheel_owned_native_files_hash_mismatched'])})
    for package in package_records:
        if not package['notices']:
            issues.append({'id': 'missing-python-notice', 'status': 'blocked', 'detail': package['name']})
    unsupported_qt = [name for name in scope['native_files'] if any(
        token in name for token in ('Qt6Pdf', 'Qt6Qml', 'Qt6Quick', 'Qt6VirtualKeyboard', 'opengl32sw'))]
    if unsupported_qt:
        issues.append({'id': 'extra-qt-source-coverage', 'status': 'blocked',
                       'detail': 'These bundled modules need additional source/notice coverage or must be excluded as unused: ' + ', '.join(unsupported_qt)})
    inventory = json.loads((notices / 'inventory.json').read_text(encoding='utf-8'))
    actual_tools = []
    for name, filename in (('ffmpeg', 'ffmpeg.exe'), ('ffprobe', 'ffprobe.exe'), ('Deno', 'deno.exe'), ('yt-dlp', 'yt-dlp.exe')):
        path = executable.parent / 'tools' / filename
        actual_hash = digest(path)
        collected = next((item for item in inventory.get('external_tools', []) if item['name'] == name), {})
        matches = collected.get('sha256') == actual_hash
        actual_tools.append({'name': name, 'sha256': actual_hash, 'matches_collected_notices': matches,
                             'collected_version': collected.get('version')})
        if not matches:
            issues.append({'id': 'external-tool-notice-mismatch', 'status': 'blocked',
                           'detail': f'{filename} does not match the executable hash in the collected notice inventory.'})
    source_zip = ROOT / 'dist' / f'{APP_NAME}-{version}-source.zip'
    issues.append({'id': 'standalone-ytdlp-source-scope', 'status': 'blocked',
                   'detail': 'The standalone yt-dlp executable hash and upstream notices are recorded, '
                             'but its exact embedded component/version/source map is not established. '
                             'The previous GUI-side Python/Curl audit does not prove coverage of this separate executable.'})
    application_source = compare_application_source(executable, source_zip) if source_zip.is_file() else None
    if not application_source or not application_source['all_bundled_application_modules_match']:
        issues.append({'id': 'application-source-match', 'status': 'blocked',
                       'detail': 'The matching local application source archive is missing or differs from the compiled application modules.'})
    index = json.loads((executable.parent / 'licenses/NOTICE_INDEX.json').read_text(encoding='utf-8'))
    distributed = {item['source']: item['sha256'] for item in index['files']}
    missing_notices = []
    for record in cargo:
        for name in record.get('notices', []):
            path = notices / 'deno/cargo-notices' / name
            relative = 'deno/cargo-notices/' + name
            if path.is_file() and distributed.get(relative) != digest(path):
                missing_notices.append(relative)
    missing_notices = sorted(set(missing_notices))
    if missing_notices:
        issues.append({'id': 'distribution-notice-refresh', 'status': 'blocked',
                       'detail': f'{len(missing_notices)} currently collected Deno notice files are missing or different in the assessed package notice index; regenerate distribution notices in the next package.'})
    if release_package is None:
        issues.append({'id': 'release-package-missing', 'status': 'blocked',
                       'detail': 'The versioned Windows release ZIP is missing; only the local folder was assessed.'})
    status = 'REVIEW_BLOCKED' if any(item['status'] == 'blocked' for item in issues) else 'PUBLICATION_PENDING'
    report = {'application': APP_NAME, 'version': version, 'status': status,
              'reviewed_at': datetime.now(timezone.utc).isoformat(), 'phase': 'existing-binary',
              'scope': scope, 'actual_python_packages': package_records, 'source_archives': sources,
              'external_tools': actual_tools, 'application_source': application_source,
              'release_package': release_package,
              'collected_deno_notices_missing_from_package': missing_notices,
              'deno_source_count': len(cargo), 'deno_package_notices_unverified': root_missing,
              'deno_notice_followup': verification.get('runtime-notice-recheck.json', {}).get('notice_followup'),
              'checks_completed': ['Actual EXE Python/native file inventory recorded with EXE hash.',
                                   'Compiled application identity matches the package manifest and requested review version.',
                                   'Every manifest-listed file matches its recorded size and SHA-256.',
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
    version_folder = base / 'releases' / version
    version_folder.mkdir(parents=True, exist_ok=True)
    (version_folder / 'review.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
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
    text = '\n'.join([f'# {APP_NAME} {version} release license review', '',
        f'Status: {status}. This report records concrete checks and remaining evidence gaps; it is not legal clearance.', '',
        '## Completed checks', *['- ' + value for value in report['checks_completed']], '',
        '## Open items', *['- ' + str(value['detail']) for value in issues], '',
        'See RELEASE_REVIEW.json for source archive hashes, actual bundled packages, and unresolved crate notices.', ''])
    (notices / 'RELEASE_REVIEW.md').write_text(text, encoding='utf-8')
    index = notices / 'THIRD_PARTY_NOTICES.md'
    original = index.read_text(encoding='utf-8')
    acknowledgement_marker = '\n## Acknowledgements\n'
    acknowledgement = original.split(acknowledgement_marker, 1)[1] if acknowledgement_marker in original else ''
    marker = '\n## Actual release review\n'
    original = original.split(marker)[0].split('\n## Review before publication')[0]
    original = original.split(acknowledgement_marker)[0]
    index.write_text(original + marker + '\n' + text
                     + (acknowledgement_marker + acknowledgement if acknowledgement else ''), encoding='utf-8')
    print('Status:', report['status'])
    print('Actual bundled Python distributions:', len(package_records))
    print('Verified source archives:', len(sources))
    print('Unverified Deno package notice texts:', len(root_missing))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight', action='store_true', help='Record pending source-version review without building.')
    parser.add_argument('--version', default=APP_VERSION, help='Expected actual executable version for the binary audit.')
    args = parser.parse_args()
    preflight(args.version) if args.preflight else audit(expected_version=args.version)
