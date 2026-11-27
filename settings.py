"""Versioned application preferences with validation and atomic persistence."""
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from copy import deepcopy
from cookie_options import COOKIE_DEFAULTS, COOKIE_BROWSERS, validate_cookie_options
from subtitle_options import SUBTITLE_DEFAULTS, SUBTITLE_MODES, validate_subtitle_options
from network_options import NETWORK_DEFAULTS, validate_network_options
from proxy_secrets import SecretProtectionError, protect_document, unprotect_document, has_plaintext_proxy
from naming_templates import NAMING_DEFAULTS, validate_naming_options
from cover_options import COVER_DEFAULTS
from chapter_options import CHAPTER_DEFAULTS
from range_options import RANGE_DEFAULTS, parse_ranges
from media_options import OUTPUT_DEFAULTS, AUDIO_BIT_DEPTHS, AUDIO_BITRATES, VIDEO_CODECS, AUDIO_CODECS, CONTAINERS, validate_output_options

from output_compat import (normalize_output, output_aliases, normalize_changes, canonical_document,
                           LEGACY_BITRATES, LEGACY_CODECS, HISTORICAL_CODEC_LABELS, legacy_preference_issues)

SCHEMA_VERSION = 1
MAX_WORKERS = 16
MAX_VIDEO_WORKERS = 4
MAX_FFMPEG_THREADS = 64
MAX_FRAGMENT_DOWNLOADS = 4
MAX_NETWORK_CONNECTIONS = 32
MAX_SETTINGS_BYTES = 1024 * 1024
QUALITIES = ("144p", "240p", "360p", "480p", "720p", "1080p", "1440p(2K)", "2160p(4K)")
BITRATES = LEGACY_BITRATES
CODECS = LEGACY_CODECS
_LEGACY_CODECS = HISTORICAL_CODEC_LABELS
DEFAULTS = dict(schema_version=SCHEMA_VERSION, language='en', last_folder="", workers=3,
                video_workers=1, ffmpeg_threads=0,
                fragment_downloads=1, network_connections=8,
                quality="1080p", log_max_bytes=5 * 1024 * 1024,
                log_backup_count=3, log_repeat_seconds=60)
DEFAULTS.update(OUTPUT_DEFAULTS)
DEFAULTS.update(NAMING_DEFAULTS)
DEFAULTS.update(COVER_DEFAULTS)
DEFAULTS.update(CHAPTER_DEFAULTS)
DEFAULTS.update(RANGE_DEFAULTS)
DEFAULTS.update(COOKIE_DEFAULTS)
DEFAULTS.update(SUBTITLE_DEFAULTS)
DEFAULTS.update(NETWORK_DEFAULTS)
DEFAULTS['saved_profiles'] = []


class UnsupportedSettingsVersion(ValueError):
    pass


class SettingsFileTooLarge(ValueError):
    pass


def validate_settings(data):
    if not isinstance(data, dict):
        raise ValueError("The settings file root must be a JSON object.")
    version = data.get("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version != SCHEMA_VERSION:
        raise UnsupportedSettingsVersion(f"Unsupported settings version: {version!r}")
    try:
        result = deepcopy(data)  # Preserve unknown fields without sharing nested mutable values.
    except RecursionError as error:
        raise ValueError('The settings data has excessive nesting.') from error
    result.update(normalize_output(result, preferences=True))
    issues = legacy_preference_issues(data)
    choices = {"language": ('en', 'tr'), 'chapter_mode': ('none', 'embed', 'split'), 'embed_cover': ('off', 'on'), 'trim_download_mode': ('section', 'full'), 'subtitle_mode': SUBTITLE_MODES, 'subtitle_auto': ('off', 'on'),
               'subtitle_format': ('srt', 'vtt'), "cookie_mode": ('none', 'file', 'browser'),
               "cookie_browser": COOKIE_BROWSERS, "quality": QUALITIES,
               "output_mode": ("video", "audio"), "container": tuple(CONTAINERS),
               "video_codec": tuple(VIDEO_CODECS), "audio_codec": tuple(AUDIO_CODECS),
               "audio_bit_depth": AUDIO_BIT_DEPTHS, "audio_bitrate": AUDIO_BITRATES, "video_quality": ("high", "balanced", "small")}
    ranges = {"workers": (1, MAX_WORKERS), "video_workers": (1, MAX_VIDEO_WORKERS),
              "ffmpeg_threads": (0, MAX_FFMPEG_THREADS), "log_max_bytes": (64 * 1024, 100 * 1024 * 1024),
              "fragment_downloads": (1, MAX_FRAGMENT_DOWNLOADS), "network_connections": (2, MAX_NETWORK_CONNECTIONS),
              "log_backup_count": (1, 10), "log_repeat_seconds": (1, 3600)}
    for key, default in DEFAULTS.items():
        value = result.get(key, default)
        valid = True
        if key in choices:
            valid = isinstance(value, str) and value in choices[key]
        elif key in ranges:
            low, high = ranges[key]
            valid = type(value) is int and low <= value <= high
        elif key in ('cookie_file', 'cookie_profile', 'subtitle_languages', 'proxy_mode', 'proxy_url', 'download_rate'):
            valid = isinstance(value, str) and len(value) <= 32768 and not any(c in value for c in '\x00\r\n')
        elif key == "last_folder":
            valid = isinstance(value, str) and "\x00" not in value
        if not valid:
            issues.append(key)
            value = default
        result[key] = value
    try:
        validate_output_options(result)
    except ValueError:
        issues.append("output profile")
        result.update(OUTPUT_DEFAULTS)
    try:
        validate_cookie_options(result)
    except ValueError:
        issues.append('cookie settings')
        result.update(COOKIE_DEFAULTS)
    try:
        validate_subtitle_options(result)
    except ValueError:
        issues.append('subtitle settings')
        result.update(SUBTITLE_DEFAULTS)
    try:
        validate_network_options(result)
    except ValueError:
        issues.append('network settings')
        result.update(NETWORK_DEFAULTS)
    try:
        validate_naming_options(result)
    except ValueError:
        issues.append('naming templates')
        result.update(NAMING_DEFAULTS)
    try:
        parse_ranges(result['trim_ranges'])
    except ValueError:
        issues.append('trim_ranges')
        result.update(RANGE_DEFAULTS)
    if result['trim_ranges'].strip() and result['chapter_mode'] == 'split':
        issues.append('chapter_mode')
        result['chapter_mode'] = 'none'
    from saved_profiles import recover_profiles
    result['saved_profiles'], invalid_profiles = recover_profiles(result['saved_profiles'])
    if invalid_profiles:
        issues.append('saved_profiles')
    return canonical_document(result), issues


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate settings key: {key}')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f'Non-finite JSON number is not allowed: {value}')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'Non-finite JSON number is not allowed: {value}')
    return number


def _read_document(path, *, raw=False):
    with open(path, 'rb') as stream:
        content = stream.read(MAX_SETTINGS_BYTES + 1)
    if len(content) > MAX_SETTINGS_BYTES:
        raise SettingsFileTooLarge('The settings file exceeds the 1 MiB limit.')
    try:
        document = json.loads(content.decode('utf-8-sig'), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant, parse_float=_finite_float)
        return document if raw else unprotect_document(document)
    except RecursionError as error:
        raise ValueError('The settings file has excessive JSON nesting.') from error


def _read(path, *, migrate=False):
    original = _read_document(path, raw=True)
    try:
        document = unprotect_document(original)
        plaintext = has_plaintext_proxy(original)
    except RecursionError as error:
        raise ValueError('The settings data has excessive nesting.') from error
    result = validate_settings(document)
    if migrate and plaintext:
        _atomic_write(path, result[0])
    return result


def _serialize(data):
    try:
        content = json.dumps(protect_document(canonical_document(data)), ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    except SecretProtectionError:
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError('Settings must contain finite, serializable JSON values.') from error
    if len(content.encode('utf-8')) > MAX_SETTINGS_BYTES:
        raise SettingsFileTooLarge('The settings data exceeds the 1 MiB limit.')
    return content


def _atomic_write(path, data):
    path = Path(path)
    content = _serialize(data)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.backup = Path(str(path) + ".bak")
        self.data = dict(DEFAULTS)
        self.writable = True

    def _protect_backup(self):
        try:
            _read(self.backup, migrate=True)
        except SecretProtectionError:
            raise
        except (FileNotFoundError, ValueError):
            pass  # Invalid/future backups are preserved, not rewritten.

    def load(self):
        """Return preferences and user-facing recovery or validation warnings."""
        self.data = deepcopy(DEFAULTS)
        self.writable = True
        try:
            self._protect_backup()
            self.data, issues = _read(self.path, migrate=True)
        except FileNotFoundError:
            if not self.backup.exists():
                return deepcopy(self.data), []
            try:
                self.data, issues = _read(self.backup, migrate=True)
            except (OSError, ValueError):
                self.writable = False
                raise
            return deepcopy(self.data), ["The primary settings file is missing; the backup was loaded.", *issues]
        except (OSError, ValueError) as error:
            # Preserve files with unsupported schemas or read errors.
            if isinstance(error, (OSError, UnsupportedSettingsVersion, SettingsFileTooLarge, SecretProtectionError)):
                self.writable = False
                raise
            preserved = Path(str(self.path) + f".corrupt-{time.time_ns()}")
            try:
                shutil.copy2(self.path, preserved)
            except OSError:
                self.writable = False
                raise
            try:
                self.data, issues = _read(self.backup, migrate=True)
            except FileNotFoundError:
                issues = []
            except (OSError, ValueError):
                self.writable = False
                raise error
            return deepcopy(self.data), [f"Could not read the settings file: {error}. Original: {preserved}. "
                                     "A valid backup or the default settings were loaded.", *issues]
        return deepcopy(self.data), (["Invalid preferences were reset to defaults: " + ", ".join(issues)] if issues else [])

    def save(self, changes):
        if not self.writable:
            return False
        data, issues = validate_settings({**self.data, **normalize_changes(changes, self.data)})
        if issues:
            raise ValueError("Invalid preferences: " + ", ".join(issues))
        try:
            previous_raw = _read_document(self.path, raw=True)
            previous_original = unprotect_document(previous_raw)
            previous, previous_issues = validate_settings(previous_original)
        except FileNotFoundError:
            previous = None
        except (UnsupportedSettingsVersion, SettingsFileTooLarge, SecretProtectionError):
            self.writable = False
            raise
        except ValueError:
            # Preserve corruption that occurred after load() before replacing the file.
            shutil.copy2(self.path, str(self.path) + f".corrupt-{time.time_ns()}")
            previous = None
        if previous is not None:
            # Retain edits made on disk to fields this save did not change.
            data, issues = validate_settings({**previous, **normalize_changes(changes, previous)})
            if issues:
                raise ValueError('Invalid preferences: ' + ', '.join(issues))
            if previous_original == canonical_document(data) and not previous_issues and not has_plaintext_proxy(previous_raw):
                self.data = data
                return False
            _serialize(data)  # Reject unsafe updates before replacing either runtime file.
            self._protect_backup()
            if not previous_issues:
                _atomic_write(self.backup, previous)
        _atomic_write(self.path, data)
        self.data = data
        return True
