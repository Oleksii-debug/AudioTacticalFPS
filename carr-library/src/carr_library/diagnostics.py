"""Bounded opt-in diagnostic previews containing allowlisted technical states.

No documents, jobs, provider profiles, environment dump, host name, local paths,
credentials or exception text become report fields. Collection starts no engine.
"""
from dataclasses import dataclass
from importlib import metadata
import json
import os
import re
import sqlite3
import struct
import sys

from .durable_state import local_path, regular, _unique
from . import runtime_settings
from .research.export_file import capture_export_destination, write_export


MAX_SETTINGS_BYTES = 64 * 1024
MAX_REPORT_BYTES = 16 * 1024
_PACKAGES = (
    ('Application package', 'universal-research-library'),
    ('wxPython package', 'wxPython'),
    ('PDF reader package', 'pypdf'),
    ('Word reader package', 'python-docx'),
    ('Bibliography parser package', 'bibtexparser'),
)
_SETUP_FIELDS = (
    ('External FFmpeg setting', 'ffmpeg'),
    ('External FFprobe setting', 'ffprobe'),
    ('Piper settings', 'piper'),
    ('Local voice helper settings', 'voice_helpers'),
    ('Neural music settings', 'music_models'),
    ('FluidSynth setting', 'fluidsynth'),
    ('dordiai-voices setting', 'dordiai'),
)
_VERSION = re.compile(
    r'[0-9]{1,4}\.[0-9]{1,4}(?:\.[0-9]{1,4}){0,2}'
    r'(?:(?:a|b|rc|\.post|\.dev)[0-9]{1,4})?', re.ASCII)


@dataclass(frozen=True)
class DiagnosticPreview:
    """The exact immutable text shown for review and later saved."""
    text: str

    def __post_init__(self):
        if (type(self.text) is not str or not self.text
                or len(self.text.encode('utf-8')) > MAX_REPORT_BYTES):
            raise ValueError('The diagnostic preview exceeds its bounded text format.')


def _version(value):
    # Reject local build labels and arbitrary strings rather than trying to
    # remove secrets or filesystem paths from a free-form version response.
    if type(value) is str and len(value) <= 64 and _VERSION.fullmatch(value):
        return value
    return 'unavailable'


def _numeric_version(values):
    try:
        parts = tuple(values)[:3]
        if len(parts) == 3 and all(type(v) is int and 0 <= v <= 9999 for v in parts):
            return '.'.join(str(v) for v in parts)
    except (TypeError, ValueError):
        pass
    return 'unavailable'


def _settings(state_root):
    """Read only one bounded existing settings snapshot, never its target paths."""
    try:
        path = runtime_settings.settings_path(state_root)
        if not os.path.lexists(path):
            return 'not saved', {}
        path = local_path(path, exists=True)
        info = regular(path)
        if not 0 < info.st_size <= MAX_SETTINGS_BYTES:
            return 'unavailable', None
        with path.open('rb') as stream:
            raw = stream.read(MAX_SETTINGS_BYTES + 1)
        if len(raw) != info.st_size or len(raw) > MAX_SETTINGS_BYTES:
            return 'unavailable', None
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        # Reuse the producer's passive declaration schema. load_settings also
        # inspects configured executable locations; diagnostics need no such
        # reads and must not activate an installation to report saved settings.
        return 'readable', runtime_settings._normalize_settings(value)
    except Exception:
        # Even an exception message can contain a secret or a personal path.
        return 'unavailable', None


def _setup(value, key):
    if value is None:
        return 'unavailable'
    selected = value.get(key)
    if key == 'dordiai':
        if selected is None:
            return 'not configured'
        return 'configured' if selected.get('enabled') is True else 'disabled'
    if selected is None or selected == []:
        return 'not configured'
    return 'configured'


def collect_diagnostics(state_root):
    """Return a fixed-size projection; unavailable sources remain unavailable."""
    system = {'win32': 'Windows', 'linux': 'Linux', 'darwin': 'macOS'}.get(sys.platform, 'Other')
    implementation = {'cpython': 'CPython', 'pypy': 'PyPy'}.get(
        getattr(sys.implementation, 'name', None), 'Other Python')
    bits = struct.calcsize('P') * 8
    rows = [
        'Resonance diagnostic report',
        'Report format: 1',
        '',
        'Runtime versions',
        'Operating system family: ' + system,
        'Process architecture: ' + (str(bits) + '-bit' if bits in (32, 64) else 'unavailable'),
        'Python runtime: ' + implementation + ' ' + _numeric_version(sys.version_info),
        'SQLite runtime: ' + _numeric_version(sqlite3.sqlite_version_info),
    ]
    for label, package in _PACKAGES:
        try:
            version = _version(metadata.version(package))
        except Exception:
            version = 'unavailable'
        rows.append(label + ': ' + version)
    state, settings = _settings(state_root)
    rows.extend(('', 'Saved local engine declarations', 'Settings snapshot: ' + state))
    rows.extend(label + ': ' + _setup(settings, key) for label, key in _SETUP_FIELDS)
    rows.extend((
        '',
        'Configured means a saved declaration, not a successful engine test.',
        'Bundled runtime health, installed voices and model quality were not probed.',
        'Use Help, Check this installation for the separate application self-test.',
        '',
        'Privacy scope',
        'This report contains only the versions and categorical setup states shown above.',
        'Documents, transcripts, titles, filenames, paths, accounts, endpoints, credentials,',
        'environment variables and detailed logs are excluded. Nothing is sent automatically.',
    ))
    return DiagnosticPreview('\n'.join(rows) + '\n')


def save_diagnostic_preview(preview, *, private_root, choose_destination,
                            confirm_replacement, cancelled=lambda: False):
    """Save precisely the reviewed snapshot after the caller's explicit choices.

    A cancelled file picker or declined replacement creates no file. The existing
    export writer binds replacement approval to the captured destination state
    and uses no-clobber publication for an absent destination.
    """
    if not isinstance(preview, DiagnosticPreview):
        raise TypeError('Review a collected diagnostic preview before saving.')
    if cancelled():
        return None
    destination = choose_destination()
    if destination is None or cancelled():
        return None
    approval = capture_export_destination(destination, private_root=private_root)
    if not approval.require_absent and not confirm_replacement():
        return None
    return write_export(destination, preview.text, private_root=private_root,
        require_absent=approval.require_absent, approval=approval, cancelled=cancelled)
