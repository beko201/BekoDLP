"""Portable executable discovery and runtime version information."""
import os
import platform
import re
import shutil
import sys


def portable_deno_path():
    """Return the app-local runtime when installed, independent of the working directory."""
    from app_paths import tools_root, managed_tool_path
    updated = managed_tool_path('deno')
    if updated:
        return updated
    deno = str(tools_root() / ('deno.exe' if os.name == 'nt' else 'deno'))
    return deno if os.path.isfile(deno) else None


def which_executable(names):
    for name in names:
        from app_paths import managed_tool_path
        updated = managed_tool_path(name.removesuffix('.exe'))
        if updated:
            return updated
        if name in ('yt-dlp', 'yt-dlp.exe'):
            from app_paths import tools_root
            portable = tools_root() / ('yt-dlp.exe' if os.name == 'nt' else 'yt-dlp')
            if portable.is_file():
                return str(portable)
        if os.name == 'nt' and name in ('ffmpeg', 'ffmpeg.exe', 'ffprobe', 'ffprobe.exe'):
            # Use the verified app-local version before an older system install.
            from app_paths import tools_root
            base = tools_root() if getattr(sys, 'frozen', False) else tools_root() / 'ffmpeg/bin'
            portable = str(base / (name if name.endswith('.exe') else name + '.exe'))
            if os.path.isfile(portable):
                return portable
        p = shutil.which(name)
        if p:
            return p
    return None


def env_info() -> str:
    """Return runtime environment details for log records."""
    from ytdlp_process import executable_version
    ver = executable_version()
    return f"yt-dlp {ver} | Python {platform.python_version()} | {sys.platform}"


def _normalize_version(v: str):
    """Normalize version components so zero-padded numeric versions compare equally.

    Convert numeric components to integers and preserve nonnumeric parts as strings.
    """
    parts = []
    for p in re.split(r'[.\-]', v or ''):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(p)
    return tuple(parts)
