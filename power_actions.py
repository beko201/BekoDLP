"""Explicit Windows power actions, called only after a user-selected countdown."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess

from process_utils import NO_WINDOW


def run_power_action(action):
    if action not in ('sleep', 'hibernate', 'shutdown'):
        raise ValueError('Unknown power action.')
    if os.name != 'nt':
        raise OSError('Power actions are supported on Windows only.')
    if action == 'shutdown':
        # /t 0 does not imply /f. Other applications are never forcibly closed.
        executable = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/shutdown.exe'
        try:
            subprocess.run([str(executable), '/s', '/t', '0'], check=True, timeout=10, **NO_WINDOW)
        except subprocess.SubprocessError as error:
            raise OSError(str(error)) from error
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    class LUID(ctypes.Structure):
        _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]
    class PRIVILEGE(ctypes.Structure):
        _fields_ = [('Luid', LUID), ('Attributes', wintypes.DWORD)]
    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [('PrivilegeCount', wintypes.DWORD), ('Privileges', PRIVILEGE * 1)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                                            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x20 | 0x8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    previous = TOKEN_PRIVILEGES()
    previous_size = wintypes.DWORD(ctypes.sizeof(previous))
    adjusted = False
    try:
        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Attributes = 2
        if not advapi.LookupPrivilegeValueW(None, 'SeShutdownPrivilege', ctypes.byref(privileges.Privileges[0].Luid)):
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.set_last_error(0)
        if not advapi.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), ctypes.sizeof(previous),
                                            ctypes.byref(previous), ctypes.byref(previous_size)) or ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        adjusted = True
        power = ctypes.WinDLL('powrprof', use_last_error=True)
        power.SetSuspendState.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte]
        power.SetSuspendState.restype = ctypes.c_ubyte
        if not power.SetSuspendState(action == 'hibernate', False, False):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if adjusted:
            advapi.AdjustTokenPrivileges(token, False, ctypes.byref(previous), 0, None, None)
        kernel.CloseHandle(token)
