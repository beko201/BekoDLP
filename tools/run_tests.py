"""Run unittest discovery with logs isolated from the application's error log."""
import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import utils


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-p', '--pattern', default='test*.py')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='yd-tests-') as folder, \
            patch.object(utils, 'SETTINGS_FILE', str(Path(folder, 'settings.json'))), \
            patch.object(utils, 'ERROR_LOG_FILE', str(Path(folder, 'errors.log'))), \
            patch.object(utils, 'DEBUG_LOG_FILE', str(Path(folder, 'debug.log'))), \
            patch.object(utils, 'FALLBACK_DEBUG_LOG_FILE', str(Path(folder, 'debug-fallback.log'))), \
            patch.object(utils, 'FALLBACK_ERROR_LOG_FILE', str(Path(folder, 'fallback.log'))):
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=args.pattern)
        result = unittest.TextTestRunner(verbosity=2 if args.verbose else 0).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
