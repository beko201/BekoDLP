"""Expose bundled command-line tools before application imports run."""
import os
import sys


if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    os.environ['PATH'] = os.pathsep.join(
        (os.path.join(os.path.dirname(sys.executable), 'tools'), sys._MEIPASS, os.environ.get('PATH', '')))
