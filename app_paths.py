"""Source resources and portable-install resources have separate locations."""
from pathlib import Path
import sys


def install_root():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def tools_root():
    return install_root() / 'tools' if getattr(sys, 'frozen', False) else install_root() / 'tools/runtime'


def licenses_root():
    return install_root() / 'licenses' if getattr(sys, 'frozen', False) else install_root() / 'build/release-notices'
