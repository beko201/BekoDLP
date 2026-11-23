"""Source resources and portable-install resources have separate locations."""
from pathlib import Path
import sys
import json
import re


def install_root():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def tools_root():
    return install_root() / 'tools' if getattr(sys, 'frozen', False) else install_root() / 'tools/runtime'


def licenses_root():
    return install_root() / 'licenses' if getattr(sys, 'frozen', False) else install_root() / 'build/release-notices'


def tool_updates_root():
    # Separate from packaged tools so application upgrades preserve tool choices.
    return install_root() / 'tool-updates'


def managed_tool_path(name):
    component = {'yt-dlp': 'ytdlp', 'ffmpeg': 'ffmpeg', 'ffprobe': 'ffmpeg', 'deno': 'deno'}.get(name)
    if component is None:
        return None
    root = tool_updates_root()
    try:
        if root.is_symlink() or root.is_junction():
            return None
        pointer = root / 'active.json'
        if pointer.stat().st_size > 16384:
            return None
        data = json.loads(pointer.read_text(encoding='utf-8'))
        token = data.get(component)
        if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
            return None
        folder = root / token
        names = ('ffmpeg', 'ffprobe') if component == 'ffmpeg' else (name,)
        if folder.is_symlink() or folder.is_junction():
            return None
        for executable in names:
            path = folder / (executable + '.exe')
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                return None
        return str(folder / (name + '.exe'))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
