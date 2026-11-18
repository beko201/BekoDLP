"""Expose bundled command-line tools before application imports run."""
import os
import sys


if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    os.environ['PATH'] = os.pathsep.join(
        (sys._MEIPASS, os.environ.get('PATH', '')))
