"""A folder updater acknowledges startup only after Qt displays the main window."""
import os
from pathlib import Path
import re
import sys


def startup_acknowledgement():
    name = os.environ.pop('BEKODLP_UPDATE_HEALTH_FILE', '')
    token = os.environ.pop('BEKODLP_UPDATE_HEALTH_TOKEN', '')
    if not name or not re.fullmatch('[a-f0-9]{64}', token) or not getattr(sys, 'frozen', False):
        return lambda: None
    root = Path(sys.executable).resolve().parent
    marker = Path(name)
    if (marker.name != 'started.txt' or marker.parent.is_symlink()
            or marker.parent.resolve().parent != root.parent
            or not marker.parent.name.startswith('.bekodlp-update-')):
        return lambda: None
    def acknowledge():
        try:
            with marker.open('x', encoding='ascii') as stream:
                stream.write(token)
        except OSError:
            pass  # The helper times out and restores the previous installation.
    return acknowledge
