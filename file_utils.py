"""Safe output names, media size estimates and scoped download cleanup."""
import gc
import math
import os
import re
import time

from app_logging import log_error, log_warning

try:
    import unidecode
except ImportError as error:
    raise ImportError("Unidecode required: pip install Unidecode") from error


def part_matcher(base_name: str):
    """Match the target filename and yt-dlp download artifacts.

    Include .part, .part-FragN, .ytdl and .temp files when cleaning up a download.
    """

    def match(n: str) -> bool:
        if n == base_name:
            return True
        if n.startswith(base_name):
            rest = n[len(base_name):]
            return rest.startswith(".part") or rest in (".ytdl", ".temp")
        return False
    return match


def remove_leftovers(folder: str, match, retries: int = 8, delay: float = 0.3):
    """Retry deletion while files are locked and log persistent failures with their paths."""
    pending = []
    for _ in range(retries):
        pending = []
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            return True  # The folder has already been removed.
        except OSError as error:
            log_warning("Could not read the download artifacts folder", error, [("folder", folder)],
                        event='artifacts_read_failed', component='cleanup')
            return False
        for name in names:
            if match(name):
                try:
                    os.remove(os.path.join(folder, name))
                except FileNotFoundError:
                    continue  # Another cleanup step already removed this file.
                except OSError as error:
                    pending.append((name, str(error)))
        if not pending:
            return True
        gc.collect()
        time.sleep(delay)
    log_error("WARNING: some download artifacts could not be removed", details=[
        ("folder", folder), ("files", "\n".join(f"{name}: {error}" for name, error in pending))],
        level='WARNING', event='artifacts_cleanup_failed', component='cleanup')
    return False


def safe_filesize(stream) -> int:
    """Return a positive size estimate, or zero if the size is unavailable.

    Disk checks still enforce minimum free-space headroom when this returns zero.
    """
    try:
        size = float(getattr(stream, 'filesize', 0) or 0)
        return math.ceil(size) if math.isfinite(size) and size > 0 else 0
    except Exception as error:
        log_warning("Could not read the format size", error, once_key=("filesize", type(stream).__name__))
        return 0


def format_mb(num_bytes) -> str:
    return f"{num_bytes / (1024 * 1024):.1f}MB"


_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{digit}" for digit in "123456789¹²³"}
    | {f"LPT{digit}" for digit in "123456789¹²³"}
)


def safe_filename(s: str) -> str:
    s = unidecode.unidecode(s)
    s = re.sub(r'[^\w\-\.\(\) ]', '_', s)
    # Trim trailing dots and spaces after truncation to keep Windows filenames valid.
    s = s[:200].strip().rstrip(' .')
    # Windows device names remain reserved with extensions, regardless of case.
    # Ignore spaces before the first dot when checking the device name.
    if s.partition('.')[0].rstrip(' ').upper() in _WINDOWS_RESERVED_NAMES:
        s = ("_" + s)[:200].rstrip(' .')
    return s or "video"
