"""Collect build-environment license evidence; this is not a compliance certificate.

Run after installing requirements and build tools. The output deliberately lists
source/embedded-library reviews that package metadata alone cannot establish.
"""
import argparse
import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_info import APP_NAME, APP_VERSION, UPDATE_REPOSITORY
from tools.install_deno import VERSION as DENO_VERSION
from ytdlp_process import require_executable, executable_version
from utils import which_executable


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_notice(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return digest(target)


def runtime_distributions(requirements_path):
    # packaging is installed by PyInstaller in the documented build environment.
    from packaging.requirements import Requirement
    pending = []
    for line in Path(requirements_path).read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            pending.append(Requirement(line))
    found = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker and not any(requirement.marker.evaluate({'extra': extra})
                                          for extra in ('', *requirement.extras)):
            continue
        package = distribution(requirement.name)
        key = re.sub(r'[-_.]+', '-', package.metadata['Name']).lower()
        if key in found:
            continue
        found[key] = package
        pending.extend(Requirement(value) for value in package.requires or [])
    return sorted(found.values(), key=lambda package: package.metadata['Name'].lower())


def collect_distribution(package, output):
    name, version = package.metadata['Name'], package.version
    folder_name = re.sub(r'[^A-Za-z0-9_.-]', '_', f'{name}-{version}')
    folder = output / 'python' / folder_name
    files = []
    for entry in package.files or []:
        relative = PurePosixPath(str(entry).replace('\\', '/'))
        if relative.is_absolute() or '..' in relative.parts or any(':' in part for part in relative.parts):
            continue
        if not (re.match(r'(?i)(license|copying|notice|authors|copyright)', relative.name)
                or any(part.lower() == 'licenses' for part in relative.parts)):
            continue
        source = Path(package.locate_file(entry))
        if source.is_file():
            target = folder.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(output.resolve()):
                raise ValueError('A package notice path escapes the output folder.')
            files.append({'file': target.relative_to(output).as_posix(),
                          'sha256': copy_notice(source, target)})
    # EJS embeds its ISC/MIT notices in the leading generated bundle comment.
    if name.lower().replace('_', '-') == 'yt-dlp-ejs':
        source = Path(package.locate_file('yt_dlp_ejs/yt/solver/lib.min.js'))
        if source.is_file():
            text = source.read_text(encoding='utf-8')
            if text.startswith('/*!') and '*/' in text:
                target = folder / 'bundled-javascript-notices.txt'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text[:text.index('*/') + 2] + '\n', encoding='utf-8')
                files.append({'file': target.relative_to(output).as_posix(), 'sha256': digest(target)})
    license_id = package.metadata.get('License-Expression') or package.metadata.get('License') or 'Not declared'
    # Qt wheels also ship a commercial notice. Copying it does not select it.
    selected_license = None
    if name.lower().startswith(('pyside6', 'shiboken6')):
        target = folder / 'GPL-3.0.txt'
        files.append({'file': target.relative_to(output).as_posix(),
                      'sha256': copy_notice(ROOT / 'LICENSE', target)})
        selected_license = 'GPL-3.0-only (where offered by this component)'
    return {'name': name, 'version': version, 'declared_license': license_id,
            'selected_license': selected_license,
            'upstream': package.metadata.get_all('Project-URL') or [], 'notices': files}


def command_text(executable, option):
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    result = subprocess.run([str(executable), option], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=15, check=True, **kwargs)
    return result.stdout + result.stderr


def collect_ffmpeg(output):
    tools = []
    for name in ('ffmpeg', 'ffprobe'):
        executable = which_executable([name])
        if not executable:
            raise FileNotFoundError(f'{name} is missing from PATH.')
        executable = Path(executable).resolve()
        version = command_text(executable, '-version')
        if '--enable-nonfree' in version:
            raise ValueError(f'{name} was built with --enable-nonfree; do not redistribute this build.')
        folder = output / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'version-and-configuration.txt').write_text(version, encoding='utf-8')
        (folder / 'license-output.txt').write_text(command_text(executable, '-L'), encoding='utf-8')
        copied = []
        for basename in ('LICENSE', 'README.txt'):
            source = executable.parent.parent / basename
            if source.is_file():
                target = folder / basename
                copied.append({'file': target.relative_to(output).as_posix(),
                               'sha256': copy_notice(source, target)})
        tools.append({'name': name, 'version': version.splitlines()[0], 'sha256': digest(executable),
                      'notices': copied})
    return tools


def collect_deno(output, *, fetch=False):
    executable = ROOT / 'tools/runtime/deno.exe'
    if not executable.is_file():
        raise FileNotFoundError('Run tools/install_deno.py before collecting notices.')
    version = command_text(executable, '--version').splitlines()[0]
    if not re.fullmatch(r'deno ' + re.escape(DENO_VERSION) + r'(?: \([^\r\n]*\))?', version):
        raise ValueError('The installed Deno version does not match tools/install_deno.py.')
    folder = output / 'deno'
    folder.mkdir(parents=True, exist_ok=True)
    license_path = folder / 'LICENSE.md'
    url = f'https://raw.githubusercontent.com/denoland/deno/v{DENO_VERSION}/LICENSE.md'
    if fetch:
        with urlopen(url, timeout=30) as response:
            content = response.read(65537)
        if len(content) > 65536 or not content.startswith(b'MIT License') or b'Deno authors' not in content:
            raise ValueError('The upstream Deno license response is unexpected.')
        license_path.write_bytes(content)
    return {'name': 'Deno', 'version': version, 'sha256': digest(executable), 'license_url': url,
            'license_collected': license_path.is_file(),
            'license_sha256': digest(license_path) if license_path.is_file() else None}


def collect_ytdlp(output, *, fetch=False):
    executable = require_executable()
    version = executable_version(executable)
    if not re.fullmatch(r'[0-9]{4}\.[0-9]{2}\.[0-9]{2}', version):
        raise ValueError('Collect yt-dlp notices from an official dated release.')
    folder = output / 'yt-dlp'
    folder.mkdir(parents=True, exist_ok=True)
    inventory_path = folder / 'binary-inventory.json'
    previous = json.loads(inventory_path.read_text(encoding='utf-8')) if inventory_path.is_file() else {}
    binary_hash = digest(executable)
    notices = []
    for name in ('LICENSE', 'THIRD_PARTY_LICENSES.txt'):
        path = folder / name
        url = f'https://raw.githubusercontent.com/yt-dlp/yt-dlp/{version}/{name}'
        if fetch:
            with urlopen(url, timeout=30) as response:
                content = response.read(2 * 1024 ** 2 + 1)
            if len(content) > 2 * 1024 ** 2 or 'license' not in content.decode('utf-8').lower():
                raise ValueError('Unexpected upstream yt-dlp notice response.')
            path.write_bytes(content)
        if path.is_file():
            notices.append({'file': path.relative_to(output).as_posix(), 'sha256': digest(path), 'url': url})
    matching_source = (fetch or previous.get('version') == version and previous.get('sha256') == binary_hash
                       and previous.get('notices') == notices and previous.get('license_collected'))
    record = {'name': 'yt-dlp', 'version': version, 'sha256': binary_hash,
              'notices': notices, 'license_collected': len(notices) == 2 and bool(matching_source),
              'scope': 'Official standalone executable and its bundled dependencies; '
                       'upstream notices do not replace the executable dependency review.'}
    inventory_path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


def reset_stale_review(output):
    """Keep an older review as evidence, without attaching it to a new version."""
    path = output / 'RELEASE_REVIEW.json'
    if not path.is_file():
        return
    previous = json.loads(path.read_text(encoding='utf-8'))
    if (previous.get('application'), previous.get('version')) == (APP_NAME, APP_VERSION):
        return
    identifier = digest(path)
    retained = output / 'verification' / ('previous-review-' + identifier)
    retained.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, retained.with_suffix('.json'))
    markdown = output / 'RELEASE_REVIEW.md'
    if markdown.is_file():
        shutil.copy2(markdown, retained.with_suffix('.md'))
    review = {'application': APP_NAME, 'version': APP_VERSION, 'phase': 'prebuild',
              'status': 'REVIEW_REQUIRED', 'previous_review_sha256': identifier,
              'open_items': [{'id': 'actual-binary-review', 'status': 'pending-build',
                              'detail': 'Audit the actual executable and package for this version; '
                                        'the retained earlier review is not its binary review.'}]}
    path.write_text(json.dumps(review, indent=2) + '\n', encoding='utf-8')
    markdown.write_text(f'# {APP_NAME} {APP_VERSION} license review\n\n'
                        '**REVIEW_REQUIRED**. Prebuild notice collection only.\n'
                        'The earlier review was retained under verification/.\n'
                        'The actual executable and package still require an audit.\n', encoding='utf-8')


def collect(output, *, fetch=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    copy_notice(ROOT / 'LICENSE', output / 'LICENSE')
    packages = [collect_distribution(package, output)
                for package in runtime_distributions(ROOT / 'requirements.txt')]
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise FileNotFoundError('The Python runtime LICENSE.txt is missing.')
    copy_notice(python_license, output / 'python-runtime/LICENSE.txt')
    external = collect_ffmpeg(output)
    deno = collect_deno(output, fetch=fetch)
    ytdlp = collect_ytdlp(output, fetch=fetch)
    findings = [
        'Publish the exact application source and build instructions for this binary release.',
        'Provide corresponding source/build materials for the shipped GPL/LGPL components. Upstream homepages alone do not establish compliance.',
        'Review FFmpeg static-library licenses, copyright notices, exact versions, sources, patches and build scripts. The top-level FFmpeg GPL text is not a complete audit of its external libraries.',
        'Review only the Qt modules/plugins actually bundled, including their third-party notices and corresponding-source obligations. Qt commercial notices copied from wheels do not select a commercial license.',
        'Review third-party components embedded in Deno and the standalone yt-dlp executable, including its Python, EJS and curl-cffi dependencies.',
        'Compare this installed dependency inventory with the final PyInstaller bundle. Optional modules/plugins included by packaging may require additional notices.',
    ]
    findings.extend(f'No installed notice text was found for {package["name"]}.'
                    for package in packages if not package['notices'])
    if not deno['license_collected']:
        findings.append('Fetch the pinned Deno MIT license using --fetch-upstream, or provide the verified upstream text.')
    if not ytdlp['license_collected']:
        findings.append('Fetch the exact yt-dlp release license and third-party notices using --fetch-upstream.')
    manifest = {'application': APP_NAME, 'version': APP_VERSION, 'python': sys.version.split()[0],
                'repository': UPDATE_REPOSITORY, 'status': 'REVIEW_REQUIRED',
                'scope': 'Installed runtime dependency inventory, not a final bundle or legal clearance.',
                'python_packages': packages, 'external_tools': external + [deno, ytdlp], 'review_required': findings}
    (output / 'inventory.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    rows = [f'# {APP_NAME} {APP_VERSION} — Third-party notices', '',
            'This is a collected license inventory, not a completed distribution compliance review.', '',
            'The application itself is GPL-3.0-only. Dependencies retain their own licenses.', '',
            '| Installed package | Version | Declared license |', '| --- | --- | --- |']
    rows.extend(f'| {item["name"]} | {item["version"]} | {str(item["declared_license"]).replace(chr(10), " ").replace("|", "/")} |'
                for item in packages)
    rows.extend(['', 'Python runtime notices: `python-runtime/LICENSE.txt`.',
                 'FFmpeg/FFprobe configuration and notices: `ffmpeg/` and `ffprobe/`.',
                 'Deno MIT notice: `deno/LICENSE.md` when collected.', '', '## Review before publication', ''])
    rows.extend(f'- {finding}' for finding in findings)
    rows.extend(['', '## Acknowledgements', '',
                 '[ytdlp-interface](https://github.com/ErrorFlynn/ytdlp-interface) by ErrorFlynn',
                 'was consulted for feature comparison and inspiration.'])
    reset_stale_review(output)
    (output / 'THIRD_PARTY_NOTICES.md').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/release-notices')
    parser.add_argument('--fetch-upstream', action='store_true', help='Download exact upstream Deno and yt-dlp notices.')
    args = parser.parse_args()
    manifest = collect(args.output, fetch=args.fetch_upstream)
    print(f'Collected {len(manifest["python_packages"])} package inventories in {args.output}')
    print('Status: REVIEW_REQUIRED. See THIRD_PARTY_NOTICES.md and inventory.json.')


if __name__ == '__main__':
    main()
