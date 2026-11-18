"""Consolidate collected notices without changing their bytes or losing attribution."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_NAME, APP_VERSION

EVIDENCE_FILES = {'inventory.json', 'RELEASE_REVIEW.json', 'RELEASE_REVIEW.md', 'THIRD_PARTY_NOTICES.md'}


def evidence_only(relative):
    return (relative.as_posix() in EVIDENCE_FILES or relative.parts[0] == 'verification'
            or '__pycache__' in relative.parts or relative.suffix.lower() in ('.pyc', '.pyo')
            or relative.parts[0] == 'source-notices'
            and relative.name in {'licensewizard-flow.png', 'licensewizard-example.png', 'licensewizard.png'})


def consolidate(source, destination):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Notice output must be a new directory.')
    if destination.resolve().is_relative_to(source) or source.is_relative_to(destination.resolve()):
        raise ValueError('Notice output and evidence input must be separate.')
    inventory = json.loads((source / 'inventory.json').read_text(encoding='utf-8'))
    if (inventory.get('application'), inventory.get('version')) != (APP_NAME, APP_VERSION):
        raise ValueError('Notice inventory does not match the application version.')
    review_path = source / 'RELEASE_REVIEW.json'
    review = json.loads(review_path.read_text(encoding='utf-8')) if review_path.exists() else {}
    groups, files = {}, []
    for path in sorted(source.rglob('*')):
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Notice inputs must not contain links.')
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if evidence_only(relative):
            continue
        content = path.read_bytes()
        # Unexpected encodings/binaries require review instead of lossy replacement.
        content.decode('utf-8')
        digest = hashlib.sha256(content).hexdigest()
        groups.setdefault(digest, content)
        files.append({'source': relative.as_posix(), 'sha256': digest})
    if not files:
        raise ValueError('No notice texts were collected.')
    status = review.get('status', inventory.get('status', 'REVIEW_REQUIRED'))
    destination.mkdir(parents=True)
    texts = []
    with (destination / 'NOTICES.txt').open('xb') as output:
        output.write(b'Collected third-party notices. Original file mappings: NOTICE_INDEX.json\n')
        for digest, content in sorted(groups.items()):
            output.write(f'\n========== SHA-256 {digest} ==========\n'.encode('ascii'))
            offset = output.tell()
            output.write(content)
            output.write(b'\n\n')
            texts.append({'sha256': digest, 'offset': offset, 'size': len(content)})
    index = {'schema': 1, 'application': APP_NAME, 'version': APP_VERSION,
             'review_status': status,
             'scope': 'Collected notice superset; not a proven Windows binary dependency map.',
             'content_file': 'NOTICES.txt', 'offset_unit': 'bytes', 'texts': texts, 'files': files}
    (destination / 'NOTICE_INDEX.json').write_text(json.dumps(index, indent=2) + '\n', encoding='utf-8')
    rows = [f'# {APP_NAME} {APP_VERSION}: third-party notices', '',
            'The application code is GPL-3.0-only. Dependencies retain their own licenses.', '',
            '## Reading the notices', '',
            '- `NOTICES.txt` contains the collected license, copyright and attribution texts.',
            '- `NOTICE_INDEX.json` maps every original notice path to its SHA-256 section.',
            '- Identical byte sequences are stored once. Each component keeps its original mapping.',
            '- Byte offsets and lengths in the index recover every original notice exactly.', '',
            'All collected component notice texts are retained conservatively, including source',
            'attributions and the Deno dependency superset. Inclusion does not establish that',
            'every listed component is linked into the Windows executable.', '',
            '## Recorded Python packages', '', '| Package | Version |', '| --- | --- |']
    packages = inventory.get('actual_bundled_python_packages', inventory.get('python_packages', []))
    rows.extend(f"| {p['name']} | {p['version']} |" for p in packages)
    rows.extend(['', 'The collection also covers Python runtime, FFmpeg/ffprobe, Deno and',
                 'available native-library notices. A notice filename in the index identifies',
                 'its component and original source location.', '', '## Review status', '',
                 f'**{status}**. Consolidation does not resolve missing source or notice evidence.'])
    if 'deno_package_notices_unverified' in review:
        rows.append(f"Deno package-level notice records still unverified: **{len(review['deno_package_notices_unverified'])}**.")
    for item in review.get('open_items', []):
        rows.append('- ' + str(item['detail']))
    rows.extend(['', f'Detailed inventories, hashes, source archives and investigation records are kept',
                 f'separately in `{APP_NAME}-{APP_VERSION}-license-evidence.zip` when prepared.',
                 'That archive is partial evidence, not complete FFmpeg corresponding source.',
                 f'Project repository: https://github.com/{inventory.get("repository", "beko201/BekoDLP")}', ''])
    (destination / 'THIRD_PARTY_NOTICES.md').write_text('\n'.join(rows), encoding='utf-8')
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'build/release-notices')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    index = consolidate(args.source, args.output)
    print(f"{len(index['files'])} notice files -> {len(index['texts'])} unique texts in 3 distribution files.")


if __name__ == '__main__':
    main()
