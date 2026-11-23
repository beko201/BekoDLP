"""Own subprocess descendants before they can outlive their original parent.

Windows: create suspended, assign to a non-inheritable kill-on-close Job Object,
then resume. POSIX: create a new session before exec and terminate its group.
https://learn.microsoft.com/windows/win32/procthread/job-objects
"""
import os
import signal
import subprocess
import threading
import time

import psutil

IS_WINDOWS = os.name == 'nt'


class PosixProcessGroup:
    def __init__(self, pid):
        self.pid = pid
        self.lock = threading.Lock()
        self.terminated = False

    def terminate(self):
        with self.lock:
            if self.terminated:
                return
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.terminated = True

    def close(self):
        self.terminate()


class WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        self.lock = threading.Lock()
        self.handle = None
        self.terminated = False
        self.kernel = kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        size_t = ctypes.c_size_t

        class BasicLimits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                        ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', size_t), ('MaximumWorkingSetSize', size_t),
                        ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', size_t),
                        ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', size_t), ('JobMemoryLimit', size_t),
                        ('PeakProcessMemoryUsed', size_t), ('PeakJobMemoryUsed', size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in ('TotalUserTime', 'TotalKernelTime',
                'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                (name, wintypes.DWORD) for name in ('TotalPageFaultCount', 'TotalProcesses',
                                                  'ActiveProcesses', 'TotalTerminatedProcesses')]

        self.Accounting = Accounting
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = args, result
        # NULL security attributes keep the job handle out of child processes.
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, proc):
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE; avoid depending on Popen internals.
        handle = self.kernel.OpenProcess(0x0100 | 0x0001, False, proc.pid)
        if not handle:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, handle):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(handle)

    def terminate(self):
        with self.lock:
            if not self.handle or self.terminated:
                return
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            # TerminateJobObject is asynchronous. Wait for all owned descendants,
            # even when the original parent already exited and cannot be queried.
            deadline = time.monotonic() + 3
            while True:
                info = self.Accounting()
                if not self.kernel.QueryInformationJobObject(self.handle, 1, self.ctypes.byref(info),
                                                             self.ctypes.sizeof(info), None):
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
                if not info.ActiveProcesses:
                    self.terminated = True
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError('The owned process job did not terminate in time.')
                time.sleep(.01)

    def close(self):
        with self.lock:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


def spawn_owned_process(args, **kwargs):
    """Contain the process before user code can execute; never fall back to scans."""
    if not IS_WINDOWS:
        proc = subprocess.Popen(args, **dict(kwargs, start_new_session=True))
        proc._owned_process_tree = PosixProcessGroup(proc.pid)
        return proc
    job = WindowsJob()
    proc = None
    try:
        # Popen closes the initial thread handle. psutil.resume() resumes the
        # suspended process only after successful assignment to the job.
        flags = kwargs.get('creationflags', 0) | 0x00000004  # CREATE_SUSPENDED
        proc = subprocess.Popen(args, **dict(kwargs, creationflags=flags))
        job.assign(proc)
        proc._owned_process_tree = job
        psutil.Process(proc.pid).resume()
        return proc
    except BaseException:
        job.close()  # Also kills the process if it was already assigned.
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=3)
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
        raise
