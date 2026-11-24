"""Protect persisted proxy credentials with Windows current-user DPAPI."""
import base64
import binascii
import ctypes
import os


class SecretProtectionError(ValueError):
    """Preserve unreadable protected records instead of resetting or overwriting them."""


def _crypt(data, *, decrypt=False):
    if os.name != 'nt':
        raise SecretProtectionError('Saving or opening protected proxy credentials requires Windows.')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    entropy_data = b'BekoDLP proxy persistence v1'
    entropy_buffer = ctypes.create_string_buffer(entropy_data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = Blob(len(entropy_data), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    try:
        # UI_FORBIDDEN, without LOCAL_MACHINE: protection belongs to this user.
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(result)):
            raise SecretProtectionError(f'Windows could not protect or open the proxy credentials (error {ctypes.get_last_error()}). '
                                        'Use the original Windows account and computer, or restore a readable backup.')
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


def has_plaintext_proxy(document):
    if isinstance(document, dict):
        return any((key == 'proxy_url' and isinstance(value, str) and '@' in value)
                   or has_plaintext_proxy(value) for key, value in document.items())
    if isinstance(document, list):
        return any(has_plaintext_proxy(value) for value in document)
    return False


def protect_document(document):
    if not has_plaintext_proxy(document):
        return document
    cache = {}

    def visit(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == 'proxy_url' and isinstance(item, str) and '@' in item:
                    if item not in cache:
                        data = _crypt(item.encode('utf-8'))
                        cache[item] = {'protection': 'windows-dpapi-v1',
                                       'data': base64.b64encode(data).decode('ascii')}
                    result[key] = dict(cache[item])
                else:
                    result[key] = visit(item)
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(document)


def unprotect_document(document):
    cache = {}

    def visit(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == 'proxy_url' and isinstance(item, dict):
                    if (set(item) != {'protection', 'data'} or item.get('protection') != 'windows-dpapi-v1'
                            or not isinstance(item.get('data'), str) or len(item['data']) > 128 * 1024):
                        raise SecretProtectionError('The protected proxy record is invalid or unsupported.')
                    encoded = item['data']
                    if encoded not in cache:
                        try:
                            binary = base64.b64decode(encoded, validate=True)
                            if not binary:
                                raise ValueError('Empty protected record')
                            cache[encoded] = _crypt(binary, decrypt=True).decode('utf-8')
                        except (binascii.Error, UnicodeError, ValueError) as error:
                            raise SecretProtectionError('Proxy credentials could not be opened; '
                                                        'the original protected file was retained.') from error
                    result[key] = cache[encoded]
                else:
                    result[key] = visit(item)
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(document)
