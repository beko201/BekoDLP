"""Record provider evidence for the exact shipped FFmpeg; never infer third-party pins."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import ssl
import sys
from urllib.request import Request, urlopen

import certifi

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_VERSION
BASE = ROOT / 'build/license-audit'
CONTEXT = ssl.create_default_context(cafile=certifi.where())


def get(url):
    with urlopen(Request(url, headers={'User-Agent': f'BekoDLP-license-evidence/{APP_VERSION}'}),
                 context=CONTEXT, timeout=30) as response:
        return response.read()


def main():
    evidence = BASE / ('upstream/recheck-' + datetime.now(timezone.utc).date().isoformat())
    evidence.mkdir(parents=True, exist_ok=True)
    queries = {'gyan-release.json': 'https://api.github.com/repos/GyanD/codexffmpeg/releases/tags/9.0.2',
               'gyan-root.json': 'https://api.github.com/repos/GyanD/codexffmpeg/contents/',
               'gyan-source-request-comments.json': 'https://api.github.com/repos/GyanD/codexffmpeg/issues/254/comments',
               'ffmpeg-core-commit.json': 'https://api.github.com/repos/FFmpeg/FFmpeg/commits/946fcce07b',
               'gyan-builds.html': 'https://www.gyan.dev/ffmpeg/builds/'}
    receipts = []
    objects = {}
    for filename, url in queries.items():
        try:
            content = get(url)
            (evidence / filename).write_bytes(content)
            receipts.append({'url': url, 'file': str((evidence / filename).relative_to(BASE)),
                             'sha256': hashlib.sha256(content).hexdigest(), 'status': 'retrieved'})
            if filename.endswith('.json'):
                objects[filename] = json.loads(content)
        except Exception as error:
            receipts.append({'url': url, 'status': 'unavailable', 'error': str(error)})
    release = objects['gyan-release.json']
    commit = objects['ffmpeg-core-commit.json']['sha']
    if not commit.startswith('946fcce07b') or '946fcce07b' not in release['body']:
        raise ValueError('The current provider source reference changed; manual review required.')
    archive = BASE / 'sources' / f'ffmpeg-core-{commit}.tar.gz'
    url = f'https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/{commit}'
    if not archive.exists():
        archive.write_bytes(get(url))
    receipts.append({'url': url, 'file': str(archive.relative_to(BASE)),
                     'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                     'scope': 'FFmpeg core source only; excludes external dependency sources.'})
    configuration = (ROOT / 'build/release-notices/ffmpeg/version-and-configuration.txt').read_text(encoding='utf-8')
    tools = {}
    for name in ('ffmpeg', 'ffprobe'):
        path = ROOT / 'dist/BekoDLP/tools' / (name + '.exe')
        with path.open('rb') as stream:
            tools[name] = {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}
    replies = [{'url': item['html_url'], 'text': item['body']} for item in objects.get('gyan-source-request-comments.json', [])
               if item['user']['login'] == 'GyanD']
    result = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'status': 'PROVIDER_MATERIALS_REQUIRED', 'provider': 'GyanD/codexffmpeg',
              'release': '9.0.2', 'tools': tools, 'core_source_commit': commit,
              'release_assets': [{'name': a['name'], 'url': a['browser_download_url'], 'digest': a.get('digest')}
                                 for a in release['assets']],
              'repository_root': [item['name'] for item in objects.get('gyan-root.json', [])],
              'enabled_options': re.findall(r'--enable-[\w-]+', configuration),
              'provider_reply_on_similar_request': replies, 'evidence': receipts,
              'unverified': ['Exact source revisions, patches, build scripts and notices for statically linked external dependencies.',
                             'A core source archive and enable flags do not establish those dependency identities.'],
              'communication': 'No messages sent. Provider requests an official-email request identifying the application/organization.'}
    (BASE / 'ffmpeg-source-recheck.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print('Core FFmpeg commit:', commit)
    print('Provider assets:', len(release['assets']))
    print('Status:', result['status'])


if __name__ == '__main__':
    main()
