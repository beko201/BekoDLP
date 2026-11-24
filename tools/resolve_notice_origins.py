"""Bind old crates without VCS metadata to upstream revisions by source comparison."""
from concurrent.futures import ThreadPoolExecutor
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_runtime_notices import BASE, NOTICES, FILENAMES, fetch, license_text, repository_name

# Candidate revisions, not assertions. Manifest identity and every published Rust
# source must match before any candidate can supply a package notice.
CANDIDATES = {'valuable': ('tokio-rs/valuable', 'v0.1.0', 'valuable'),
              'dlopen2': ('OpenByteDev/dlopen2', '122b5b7d9f15718de9525b5968ea8888ddc3a89d', 'dlopen2'),
              'winapi-i686-pc-windows-gnu': ('retep998/winapi-rs', '0.3.9', 'i686'),
              'winapi-x86_64-pc-windows-gnu': ('retep998/winapi-rs', '0.3.9', 'x86_64')}


def manifest_identity(package, workspace):
    """Resolve only identity fields inherited from a monorepo workspace."""
    result = {}
    for key in ('name', 'version', 'license'):
        value = package.get(key)
        if isinstance(value, dict) and value.get('workspace') is True:
            value = workspace.get('workspace', {}).get('package', {}).get(key)
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='*', help='Recheck only these crate names.')
    parser.add_argument('--swc-ref', default='v1.15.0', help='Candidate SWC tag; source matching is still required.')
    args = parser.parse_args()
    records_path = BASE / 'deno-cargo-sources.json'
    records = json.loads(records_path.read_text(encoding='utf-8'))
    report_path = BASE / 'runtime-notice-recheck.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    # This release is a candidate, never a substitute for comparing the actual
    # checksum-bound crate. Every published Rust source still has to match.
    missing = {(r['name'], r['version']) for r in
               json.loads((BASE / 'review.json').read_text(encoding='utf-8'))['deno_package_notices_unverified']}
    for record in records:
        if args.only is not None and record['name'] not in args.only:
            continue
        candidate = CANDIDATES.get(record['name'])
        if ((record['name'], record['version']) in missing
                and repository_name(record.get('repository')) == 'swc-project/swc'):
            candidate = ('swc-project/swc', args.swc_ref, record['vcs']['path_in_vcs'])
        if candidate is None:
            continue
        repo, ref, directory = candidate
        evidence = {'name': record['name'], 'version': record['version'], 'crate_sha256': record['sha256'],
                    'repository': repo, 'candidate_ref': ref, 'source_directory': directory,
                    'attempts': [], 'notices': [], 'status': 'source_comparison_failed'}
        commit = ref
        if not re.fullmatch('[a-f0-9]{40}', commit):
            content, receipt = fetch(f'https://api.github.com/repos/{repo}/git/ref/tags/{ref}')
            evidence['attempts'].append(receipt)
            if content is None:
                continue
            obj = json.loads(content)['object']
            if obj['type'] == 'tag':
                content, receipt = fetch(obj['url'])
                evidence['attempts'].append(receipt)
                obj = json.loads(content)['object']
            if obj['type'] != 'commit':
                raise ValueError('Unsupported tag target')
            commit = obj['sha']
        archive_path = BASE / 'deno-crates' / record['filename']
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('Cargo archive checksum changed')
        prefix = f"{record['name']}-{record['version']}/"
        with tarfile.open(archive_path) as archive:
            package = tomllib.loads(archive.extractfile(prefix + 'Cargo.toml').read().decode('utf-8'))['package']
            rust = {item.name[len(prefix):]: archive.extractfile(item).read()
                    for item in archive if item.isfile() and item.name.startswith(prefix) and item.name.endswith('.rs')}
        base_url = f'https://raw.githubusercontent.com/{repo}/{commit}/'
        package_path = (directory + '/' if directory else '') + 'Cargo.toml'
        content, receipt = fetch(base_url + package_path)
        evidence['attempts'].append(receipt)
        matches = False
        if content:
            upstream = tomllib.loads(content.decode('utf-8')).get('package', {})
            workspace = {}
            if any(isinstance(upstream.get(key), dict) for key in ('name', 'version', 'license')):
                root_content, root_receipt = fetch(base_url + 'Cargo.toml')
                evidence['attempts'].append(root_receipt)
                if root_content:
                    workspace = tomllib.loads(root_content.decode('utf-8'))
            matches = manifest_identity(upstream, workspace) == manifest_identity(package, {})
        evidence['manifest_identity_matches'] = matches
        if matches and rust:
            def compare(item):
                path, expected = item
                remote_path = (directory + '/' if directory else '') + path
                content, receipt = fetch(base_url + remote_path)
                return path, content == expected, receipt
            with ThreadPoolExecutor(max_workers=8) as pool:
                checks = list(pool.map(compare, rust.items()))
            evidence['rust_sources_compared'] = len(checks)
            evidence['rust_sources_mismatched'] = [path for path, equal, _ in checks if not equal]
            evidence['attempts'].extend(receipt for _, _, receipt in checks)
            if all(equal for _, equal, _ in checks):
                evidence['matched_commit'] = commit
                for name in FILENAMES:
                    content, receipt = fetch(base_url + name)
                    evidence['attempts'].append(receipt)
                    if content is None or not license_text(content):
                        continue
                    target = NOTICES / prefix / ('upstream-' + name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    evidence['notices'].append({'url': receipt['url'], 'sha256': receipt['sha256'],
                                                'file': str(target.relative_to(ROOT))})
                    relative = prefix + 'upstream-' + name
                    if relative not in record.setdefault('notices', []):
                        record['notices'].append(relative)
                    record.setdefault('upstream_notice_sources', []).append({'url': receipt['url'], 'sha256': receipt['sha256']})
                if evidence['notices']:
                    evidence['status'] = record['status'] = 'tag_or_sibling_commit_source_matched'
        report['records'] = [r for r in report['records'] if (r['name'], r['version']) != (record['name'], record['version'])]
        report['records'].append(evidence)
        print(record['name'], evidence['status'], evidence.get('rust_sources_compared', 0), flush=True)
    report['records'].sort(key=lambda r: (r['name'], r['version']))
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    records_path.write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
