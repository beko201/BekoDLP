"""Recheck missing Cargo notices against checksum-bound source metadata and exact VCS.

Never substitute a repository's current branch for an unavailable release commit.
This collects evidence; it does not assert which crates link into Deno on Windows.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
from datetime import datetime, timezone
import hashlib
import json
import posixpath
from pathlib import Path, PurePosixPath
import re
import ssl
import sys
import tarfile
import threading
import tomllib
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import certifi

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_VERSION
sys.path.insert(0, str(ROOT))
BASE = ROOT / 'build/license-audit'
NOTICES = ROOT / 'build/release-notices/deno/cargo-notices'
FILENAMES = ('LICENSE', 'LICENSE.md', 'LICENSE-MIT', 'LICENSE-APACHE',
             'LICENSE.txt', 'COPYING', 'UNLICENSE', 'NOTICE', 'NOTICE.txt',
             'LICENSE-APACHE.txt', 'LICENSE-MIT.txt')
CONTEXT = ssl.create_default_context(cafile=certifi.where())
LOCK = threading.Lock()
CACHE = {}
# These are candidates only: package metadata at the exact commit must match
# the checksum-bound Cargo.toml.orig before a hint can supply a missing URL.
REPOSITORY_HINTS = {'dlopen2_derive': 'OpenByteDev/dlopen2',
                    'lazy-regex-proc_macros': 'Canop/lazy-regex'}


def repository_name(value):
    match = re.match(r'https://github\.com/([^/]+/[^/#]+)', value or '')
    return match.group(1).removesuffix('.git') if match else None


def license_text(content):
    text = content.decode('utf-8', errors='replace').lower()
    return ('apache license' in text and 'version 2.0' in text
            or 'permission is hereby granted' in text and 'copyright' in text
            or 'this is free and unencumbered software released into the public domain' in text
            or 'redistribution and use' in text and 'copyright' in text
            or 'boost software license' in text)


def fetch(url):
    with LOCK:
        if url in CACHE:
            return CACHE[url]
    try:
        request = Request(url, headers={'User-Agent': f'BekoDLP-license-evidence/{APP_VERSION}'})
        with urlopen(request, context=CONTEXT, timeout=15) as response:
            content = response.read(4 * 1024 ** 2 + 1)
            if len(content) > 4 * 1024 ** 2:
                raise ValueError('Oversized notice')
            result = (content, {'url': url, 'status': response.status,
                                'sha256': hashlib.sha256(content).hexdigest()})
    except HTTPError as error:
        result = (None, {'url': url, 'status': error.code})
    except Exception as error:
        result = (None, {'url': url, 'error': str(error)})
    with LOCK:
        CACHE[url] = result
    return result


def linked_archive_license(content, relative, repo, commit, archive_path, prefix,
                           crate_directory='', attempts=None):
    """Follow a proven upstream symlink only to a regular file in this crate.

    A nested dependency notice by itself is insufficient. The exact upstream
    LICENSE entry must explicitly select it, with its Git blob identity checked.
    The caller has already verified the complete crate archive's Cargo checksum.
    """
    if len(content) > 1024:
        return None
    try:
        target = content.decode('utf-8').strip()
    except UnicodeDecodeError:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', target):
        return None
    path = PurePosixPath(target)
    if path.is_absolute():
        return None
    resolved = PurePosixPath(posixpath.normpath(str(PurePosixPath(relative).parent / path)))
    if resolved.is_absolute() or '..' in resolved.parts:
        return None
    try:
        local = resolved.relative_to(PurePosixPath(crate_directory))
    except ValueError:
        return None
    url = f'https://api.github.com/repos/{repo}/contents/{quote(relative)}?ref={commit}'
    metadata, receipt = fetch(url)
    if attempts is not None:
        attempts.append(receipt)
    if metadata is None:
        return None
    info = json.loads(metadata)
    blob = hashlib.sha1(b'blob ' + str(len(content)).encode('ascii') + b'\0' + content).hexdigest()
    if (not isinstance(info, dict) or info.get('type') != 'symlink'
            or info.get('target') != target or info.get('sha') != blob):
        return None
    with tarfile.open(archive_path) as archive:
        try:
            entry = archive.getmember(prefix + '/' + local.as_posix())
        except KeyError:
            return None
        if not entry.isfile() or entry.size > 4 * 1024 ** 2:
            return None
        notice = archive.extractfile(entry).read()
    if not license_text(notice):
        return None
    return notice, {'upstream_symlink': url, 'git_blob_sha1': blob,
                    'link_sha256': hashlib.sha256(content).hexdigest(),
                    'link_target': target, 'crate_notice_path': local.as_posix()}


def dependency_declarations(document, names, workspace=None):
    """Expose target/kind conditions without claiming a resolved binary graph."""
    result = []
    sections = [(None, document), *document.get('target', {}).items()]
    for target, section in sections:
        for kind in ('dependencies', 'build-dependencies', 'dev-dependencies'):
            for alias, value in section.get(kind, {}).items():
                value = {'version': value} if isinstance(value, str) else dict(value)
                if value.get('workspace') is True:
                    inherited = (workspace or {}).get('workspace', {}).get('dependencies', {}).get(alias, {})
                    inherited = {'version': inherited} if isinstance(inherited, str) else inherited
                    value = {**inherited, **value}
                name = value.get('package', alias)
                if name in names:
                    result.append({'dependency': name, 'alias': alias, 'kind': kind,
                                   'target': target, 'optional': value.get('optional', False),
                                   'version_requirement': value.get('version')})
    return result


def dependency_scope(records, names):
    """Checksum-bound incoming declarations, deliberately not feature resolution."""
    declarations, archives_checked = [], 0
    for record in records:
        source = BASE / 'deno-crates' / record['filename']
        with source.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                raise ValueError('Cargo archive checksum changed: ' + source.name)
        archives_checked += 1
        prefix = f"{record['name']}-{record['version']}"
        with tarfile.open(source, 'r|gz') as archive:
            for member in archive:
                if member.name != prefix + '/Cargo.toml':
                    continue
                if not member.isfile() or member.size > 4 * 1024 ** 2:
                    raise ValueError('Invalid Cargo manifest')
                document = tomllib.loads(archive.extractfile(member).read().decode('utf-8'))
                for item in dependency_declarations(document, names):
                    declarations.append({**item, 'dependent': prefix,
                        'source_archive_sha256': record['sha256'], 'manifest': member.name})
                break
            else:
                raise ValueError('Published Cargo manifest is missing: ' + prefix)
    review = json.loads((BASE / 'review.json').read_text(encoding='utf-8'))
    deno = next(item for item in review['source_archives'] if item['filename'] == 'deno-2.9.8.tar.gz')
    source = BASE / 'sources' / deno['filename']
    with source.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != deno['sha256']:
            raise ValueError('Retained Deno source checksum changed')
    with tarfile.open(source) as archive:
        prefix = 'deno-2.9.8/'
        workspace = tomllib.loads(archive.extractfile(prefix + 'Cargo.toml').read().decode('utf-8'))
        for member in archive:
            if (not member.isfile() or not member.name.endswith('/Cargo.toml')
                    or member.size > 4 * 1024 ** 2 or member.name.startswith(prefix + 'tests/')):
                continue
            document = tomllib.loads(archive.extractfile(member).read().decode('utf-8'))
            for item in dependency_declarations(document, names, workspace):
                declarations.append({**item, 'dependent': document.get('package', {}).get('name', 'workspace'),
                    'source_archive_sha256': deno['sha256'], 'manifest': member.name})
    return {'checked_at': datetime.now(timezone.utc).isoformat(),
            'target_under_review': 'x86_64-pc-windows-msvc', 'cargo_archives_checked': archives_checked,
            'scope': 'Incoming manifest declarations, including optional/build/dev dependencies. '
                     'Not a feature-resolved dependency graph or Windows binary link map; '
                     'these annotations do not clear any notice gap.',
            'declarations': declarations}


def recheck(record, repo_by_commit):
    path = BASE / 'deno-crates' / record['filename']
    if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
        raise ValueError('Cargo archive checksum changed: ' + path.name)
    prefix = f"{record['name']}-{record['version']}"
    with tarfile.open(path) as archive:
        package = tomllib.loads(archive.extractfile(prefix + '/Cargo.toml').read().decode('utf-8'))['package']
        try:
            vcs = json.loads(archive.extractfile(prefix + '/.cargo_vcs_info.json').read())
        except KeyError:
            vcs = {}
        try:
            readme = archive.extractfile(prefix + '/README.md').read().decode('utf-8')
        except KeyError:
            readme = ''
        original = tomllib.loads(archive.extractfile(prefix + '/Cargo.toml.orig').read().decode('utf-8'))
    record.update(license=package.get('license'), repository=package.get('repository'), vcs=vcs)
    commit = vcs.get('git', {}).get('sha1')
    repo = repository_name(package.get('repository')) or repository_name(package.get('homepage'))
    if not repo and commit:
        repo = repo_by_commit.get(commit)
    if not repo:
        matches = re.findall(r'https://github\.com/([^/\s)]+/[^/\s)]+)', readme)
        if len(set(matches)) == 1:
            repo = matches[0].removesuffix('.git')
    evidence = {'name': record['name'], 'version': record['version'],
                'crate_sha256': record['sha256'], 'repository': repo, 'vcs': vcs,
                'license_declaration': package.get('license'), 'attempts': [], 'notices': []}
    if not repo and record['name'] in REPOSITORY_HINTS and commit:
        candidate = REPOSITORY_HINTS[record['name']]
        relative = str(PurePosixPath(vcs.get('path_in_vcs', '')) / 'Cargo.toml')
        content, attempt = fetch(f'https://raw.githubusercontent.com/{candidate}/{commit}/{relative}')
        evidence['attempts'].append(attempt)
        if content is not None and tomllib.loads(content.decode('utf-8')) == original:
            repo = candidate
            evidence.update(repository=repo, repository_origin='Exact-commit Cargo.toml matches published Cargo.toml.orig.')
    if not repo or not commit or not re.fullmatch('[a-f0-9]{40}', commit):
        evidence['status'] = 'missing_exact_vcs_locator'
        return record, evidence
    # Root and the crate's own directory/ancestors can contain workspace notices.
    location = PurePosixPath(vcs.get('path_in_vcs', ''))
    if location.is_absolute() or '..' in location.parts:
        raise ValueError('Invalid path_in_vcs')
    folders = ['', *[str(p) for p in [location, *location.parents] if str(p) != '.']]
    # Root is sufficient for most crates; also collect package-local notices.
    seen, license_found = set(), False
    for folder in dict.fromkeys(folders):
        for name in FILENAMES:
            relative = f'{folder}/{name}' if folder else name
            url = f'https://raw.githubusercontent.com/{repo}/{commit}/{quote(relative)}'
            content, attempt = fetch(url)
            evidence['attempts'].append(attempt)
            if content is None:
                continue
            linked = None
            if not license_text(content):
                resolved = linked_archive_license(content, relative, repo, commit, path, prefix,
                    vcs.get('path_in_vcs', ''), evidence['attempts'])
                if resolved is None:
                    continue
                content, linked = resolved
            digest = hashlib.sha256(content).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            target_name = 'upstream-' + relative.replace('/', '__')
            target = NOTICES / prefix / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            notice = {'url': url, 'sha256': digest, 'file': str(target.relative_to(ROOT)),
                      'source_path': relative, 'exact_commit': commit}
            if linked is not None:
                notice.update(linked)
                notice['url'] = record['url'] + '#' + quote(prefix + '/' + linked['crate_notice_path'])
            evidence['notices'].append(notice)
            record.setdefault('upstream_notice_sources', []).append({k: notice[k] for k in ('url', 'sha256')})
            record.setdefault('notices', []).append(prefix + '/' + target_name)
            license_found = True
        if license_found:
            break
    dirty = vcs.get('git', {}).get('dirty', False)
    evidence['status'] = ('exact_commit_notices_collected_dirty_tree' if dirty else 'exact_commit_notices_collected') if license_found else 'exact_commit_notice_unavailable'
    if license_found:
        record['status'] = evidence['status']
    return record, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--scope-only', action='store_true', help='Record incoming dependency conditions offline.')
    args = parser.parse_args()
    records_path = BASE / 'deno-cargo-sources.json'
    records = json.loads(records_path.read_text(encoding='utf-8'))
    previous = json.loads((BASE / 'review.json').read_text(encoding='utf-8'))
    missing = {(r['name'], r['version']) for r in previous['deno_package_notices_unverified']}
    repo_by_commit = {}
    for record in records:
        repo = repository_name(record.get('repository'))
        commit = (record.get('vcs') or {}).get('git', {}).get('sha1')
        if repo and commit:
            repo_by_commit[commit] = repo
    evidence_path = BASE / 'runtime-notice-recheck.json'
    previous_evidence = json.loads(evidence_path.read_text(encoding='utf-8')) if evidence_path.exists() else {}
    results = previous_evidence.get('records', [])
    if args.scope_only:
        from tools.audit_release_licenses import cargo_notice_gaps
        gaps = cargo_notice_gaps(records, NOTICES)
        previous_evidence['dependency_scope'] = dependency_scope(records, {r['name'] for r in gaps})
        evidence_path.write_text(json.dumps(previous_evidence, indent=2) + '\n', encoding='utf-8')
        print('Recorded dependency declarations for', len(gaps), 'remaining notice gaps.', flush=True)
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = {pool.submit(recheck, record, repo_by_commit): index for index, record in enumerate(records)
                   if (record['name'], record['version']) in missing
                   and (args.only is None or record['name'] in args.only)}
        for future in as_completed(pending):
            record, evidence = future.result()
            records[pending[future]] = record
            previous_record = next((r for r in results if (r['name'], r['version']) == (record['name'], record['version'])), {})
            evidence = {**previous_record, **evidence}
            results = [r for r in results if (r['name'], r['version']) != (record['name'], record['version'])]
            results.append(evidence)
            print(record['name'], record['version'], evidence['status'], len(evidence['notices']), flush=True)
    for record in records:
        record['notices'] = list(dict.fromkeys(record.get('notices', [])))
        if record.get('upstream_notice_sources'):
            record['upstream_notice_sources'] = list({(r['url'], r['sha256']): r for r in record['upstream_notice_sources']}.values())
    records_path.write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')
    report = {**previous_evidence, 'scope': 'Checksum-verified Cargo source superset; not a Windows binary link map.',
              'checked_at': datetime.now(timezone.utc).isoformat(),
              'records': sorted(results, key=lambda r: (r['name'], r['version']))}
    evidence_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Rechecked', len(results), 'records;', sum(bool(r['notices']) for r in results), 'with exact-commit notices.', flush=True)


if __name__ == '__main__':
    main()
