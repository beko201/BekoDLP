"""Fetch immutable Cargo sources and notices for the pinned Deno dependency set.

This collects an upstream lockfile superset, not a claim that every crate is
compiled into the Windows binary. Each download must match Cargo.lock's hash.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import time
import tomllib
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def collect(archive_path, folder):
    extracted = []
    with tarfile.open(archive_path, 'r:gz') as archive:
        for entry in archive:
            if not entry.isfile():
                continue
            path = PurePosixPath(entry.name)
            if path.is_absolute() or '..' in path.parts or any(':' in part for part in path.parts):
                raise ValueError('Unsafe crate path')
            if not re.match(r'(?i)(license|licence|copying|notice|copyright|authors)', path.name):
                continue
            if path.suffix.lower() in ('.c', '.cc', '.cpp', '.h', '.hpp', '.rs', '.py', '.js'):
                # Algorithm files such as copying-phase.cc are not licenses.
                continue
            if entry.size > 4 * 1024 * 1024:
                raise ValueError('Unexpectedly large notice file')
            output = folder.joinpath(*path.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(entry) as source:
                output.write_bytes(source.read())
            extracted.append(output.relative_to(folder).as_posix())
    return extracted


def main():
    base = ROOT / 'build/license-audit'
    lock = tomllib.loads((base / 'upstream/deno-cargo.lock').read_text(encoding='utf-8'))
    sources = base / 'deno-crates'
    notices = ROOT / 'build/release-notices/deno/cargo-notices'
    sources.mkdir(parents=True, exist_ok=True)
    notices.mkdir(parents=True, exist_ok=True)
    packages = [package for package in lock['package'] if package.get('source', '').startswith('registry+')]

    def fetch(package):
        name, version, expected = package['name'], package['version'], package['checksum']
        filename = f'{name}-{version}.crate'
        path = sources / filename
        url = f'https://static.crates.io/crates/{name}/{filename}'
        record = {'name': name, 'version': version, 'url': url, 'sha256': expected, 'filename': filename}
        try:
            for attempt in range(3):
                try:
                    if not path.exists():
                        staging = path.with_suffix('.part')
                        total = 0
                        with urlopen(Request(url, headers={'User-Agent': 'BekoDLP-license-audit'}), timeout=45) as source, staging.open('wb') as target:
                            while chunk := source.read(1024 * 1024):
                                total += len(chunk)
                                if total > 256 * 1024 * 1024:
                                    raise ValueError('Crate source exceeds the download size limit')
                                target.write(chunk)
                        with staging.open('rb') as stream:
                            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                                raise ValueError('Cargo checksum mismatch')
                        staging.replace(path)
                    with path.open('rb') as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                            raise ValueError('Cached Cargo checksum mismatch')
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
            record['size'] = path.stat().st_size
            record['notices'] = collect(path, notices)
            record['status'] = 'collected' if record['notices'] else 'notice_text_missing'
        except Exception as error:
            record.update(status='download_failed', error=str(error))
        return record

    records = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, package) for package in packages]
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            if len(records) % 50 == 0 or record['status'] == 'download_failed':
                print('Cargo sources:', len(records), '/', len(packages), record['name'], record['status'], flush=True)
            (base / 'deno-cargo-sources.json').write_text(json.dumps(sorted(records, key=lambda item: (item['name'], item['version'])), indent=2), encoding='utf-8')
    missing = [record for record in records if record['status'] != 'collected']
    print('Downloaded sources:', len(records), 'review items:', len(missing), flush=True)
    if missing:
        print('See deno-cargo-sources.json for unresolved notice texts.', flush=True)


if __name__ == '__main__':
    main()
