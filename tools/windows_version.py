"""Windows executable version resources derived from the application identity."""
import re

from app_info import APP_NAME, APP_VERSION, APP_EXECUTABLE


def version_resource():
    if not re.fullmatch(r'\d+(?:\.\d+){0,3}', APP_VERSION):
        raise ValueError('Windows executable version must contain up to four numeric parts.')
    parts = tuple(int(part) for part in APP_VERSION.split('.'))
    if any(part > 65535 for part in parts):
        raise ValueError('Windows executable version parts must fit in 16 bits.')
    numeric = parts + (0,) * (4 - len(parts))
    strings = {'FileDescription': APP_NAME, 'FileVersion': APP_VERSION,
               'InternalName': APP_NAME, 'OriginalFilename': APP_EXECUTABLE,
               'ProductName': APP_NAME, 'ProductVersion': APP_VERSION}
    entries = ',\n'.join(f'                StringStruct({key!r}, {value!r})' for key, value in strings.items())
    return f'''# UTF-8
VSVersionInfo(
    ffi=FixedFileInfo(filevers={numeric!r}, prodvers={numeric!r},
        mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('040904B0', [
{entries}
    ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
'''
