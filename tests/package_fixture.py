import io
import json
import hashlib
import zipfile

from app_info import APP_EXECUTABLE
from package_layout import REQUIRED_FILES, MANAGED_ENTRIES, MANIFEST_NAME, LAYOUT


def executable_payload():
    data = bytearray(128)
    data[:2] = b'MZ'
    data[60:64] = (64).to_bytes(4, 'little')
    data[64:70] = b'PE\x00\x00\x64\x86'
    return bytes(data)


def package_payload(version='1.1', executable=None, extra=None):
    files = {name: b'fixture' for name in REQUIRED_FILES | {'_internal/python313.dll'}}
    files[APP_EXECUTABLE] = executable if executable is not None else executable_payload()
    files.update(extra or {})
    manifest = {'schema': 1, 'app': 'BekoDLP', 'version': version, 'layout': LAYOUT,
                'health_protocol': 1, 'managed_entries': list(MANAGED_ENTRIES),
                'files': [{'path': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                          for name, content in sorted(files.items())]}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in files.items():
            archive.writestr(zipfile.ZipInfo('BekoDLP/' + name), content)
        archive.writestr(zipfile.ZipInfo('BekoDLP/' + MANIFEST_NAME), json.dumps(manifest))
    return output.getvalue()
