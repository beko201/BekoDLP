"""Bind old crates without VCS metadata to upstream revisions by source comparison."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import tarfile
import tomllib

from verify_runtime_notices import BASE, ROOT, NOTICES, FILENAMES, fetch, license_text

# Candidate revisions, not assertions. Manifest identity and every published Rust
# source must match before any candidate can supply a package notice.
CANDIDATES = {'valuable': ('tokio-rs/valuable', 'v0.1.0', 'valuable'),
              'dlopen2': ('OpenByteDev/dlopen2', '122b5b7d9f15718de9525b5968ea8888ddc3a89d', 'dlopen2'),
              'winapi-i686-pc-windows-gnu': ('retep998/winapi-rs', '0.3.9', 'i686'),
              'winapi-x86_64-pc-windows-gnu': ('retep998/winapi-rs', '0.3.9', 'x86_64')}


def main():
    records_path = BASE / 'deno-cargo-sources.json'
    records = json.loads(records_path.read_text(encoding='utf-8'))
    report_path = BASE / 'runtime-notice-recheck.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    for record in records:
        if record['name'] not in CANDIDATES:
            continue
        repo, ref, directory = CANDIDATES[record['name']]
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
            matches = all(upstream.get(key) == package.get(key) for key in ('name', 'version', 'license'))
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
    records_path.write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
