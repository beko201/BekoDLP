"""Console-tool subprocess execution and process-control dependency."""
import os
import subprocess

try:
    import psutil
except ImportError as error:
    raise ImportError("psutil required: pip install psutil") from error


def _hidden_window_kwargs():
    """Hide console windows for FFmpeg and FFprobe on Windows.

    DEVNULL provides a valid stdin handle for windowed executables that have no
    console. Other platforms need no extra options. These options are intended
    for console tools, not GUI programs such as Explorer.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                "stdin": subprocess.DEVNULL}
    return {}


NO_WINDOW = _hidden_window_kwargs()


def run_cmd_capture(cmd_list, timeout=None, *, on_start=None, on_finish=None):
    proc = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, **NO_WINDOW)
    try:
        if on_start is not None:
            on_start(proc)
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            proc.kill()
            output, _ = proc.communicate()
            error.output = output
            raise
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, cmd_list, output=output)
        return output
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        finally:
            try:
                proc.stdout.close()
            finally:
                if on_finish is not None:
                    on_finish(proc)
