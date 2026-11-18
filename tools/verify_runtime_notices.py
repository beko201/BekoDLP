"""Recheck missing Cargo notices against checksum-bound source metadata and exact VCS.

Never substitute a repository's current branch for an unavailable release commit.
This collects evidence; it does not assert which crates link into Deno on Windows.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import hashlib
import json
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
        request = Request(url, headers={'User-Agent': 'BekoDLP-license-evidence/1.0'})
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
            if content is None or not license_text(content):
                continue
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
              'records': sorted(results, key=lambda r: (r['name'], r['version']))}
    evidence_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('Rechecked', len(results), 'records;', sum(bool(r['notices']) for r in results), 'with exact-commit notices.', flush=True)


if __name__ == '__main__':
    main()
