"""Explicit local music registrations shared by native controls and the CLI.

Stored paths identify user-installed assets, never downloadable model IDs.
Configuration validates bytes but does not claim that neural inference ran.
"""
from pathlib import Path, PureWindowsPath
import json
import os
import re

from .music import MusicGenConfig, validate_musicgen_config
from .projects import ProjectStore, atomic_json, read_json
from .render import check_cancelled, file_sha256, local_file, tool_path
from ..native_runtime import _directory


_DIGEST = re.compile('[0-9a-f]{64}\\Z')
MUSICGEN_FIELDS = {'model_dir', 'manifest', 'manifest_sha256', 'python_executable',
                   'python_sha256', 'device', 'timeout_seconds', 'private_noncommercial'}
FLUIDSYNTH_FIELDS = {'soundfont', 'soundfont_sha256', 'soundfont_license',
                    'fluidsynth_library', 'fluidsynth_sha256'}


def _absolute(value, label):
    if (not isinstance(value, str) or not value or len(value) > 32768 or '\x00' in value
            or '://' in value or value.startswith(('\\\\', '//'))):
        raise ValueError(label + ' must be an explicit local path.')
    path = Path(value)
    windows = PureWindowsPath(value)
    absolute = path.is_absolute() or (windows.is_absolute() and re.fullmatch('[A-Za-z]:', windows.drive))
    if not absolute or '..' in path.parts or '..' in windows.parts:
        raise ValueError(label + ' must be an absolute local path without parent traversal.')
    return path


def _hash(value, label):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(label + ' must be the reviewed lowercase SHA256.')


def _config(record):
    return MusicGenConfig(Path(record['model_dir']), Path(record['manifest']),
        record['manifest_sha256'], record['python_executable'], record['device'], record['timeout_seconds'])


def validate_music_settings(value, *, check_files=False, cancelled=None):
    """Validate structure for backup; optionally authenticate all installed assets."""
    if (type(value) is not dict or set(value) != {'version', 'revision', 'musicgen', 'fluidsynth'}
            or type(value.get('version')) is not int or value['version'] != 1
            or type(value.get('revision')) is not int or value['revision'] < 0):
        raise ValueError('Unsupported local music settings.')
    neural, midi = value['musicgen'], value['fluidsynth']
    if neural is not None:
        if type(neural) is not dict or set(neural) != MUSICGEN_FIELDS:
            raise ValueError('Unsupported MusicGen registration fields.')
        for name in ('model_dir', 'manifest', 'python_executable'):
            _absolute(neural[name], name)
        for name in ('manifest_sha256', 'python_sha256'):
            _hash(neural[name], name)
        if neural['private_noncommercial'] is not True:
            raise ValueError('MusicGen is enabled only for the reviewed private noncommercial profile.')
        if not isinstance(neural['device'], str) or not re.fullmatch('cpu|cuda(?::[0-7])?', neural['device']):
            raise ValueError('Select CPU or an explicit CUDA device for MusicGen.')
        timeout = neural['timeout_seconds']
        if timeout is not None and (type(timeout) not in (int, float) or not .1 <= timeout <= 365*86400):
            raise ValueError('MusicGen timeout must be positive seconds or null.')
    if midi is not None:
        if type(midi) is not dict or set(midi) != FLUIDSYNTH_FIELDS:
            raise ValueError('Unsupported FluidSynth registration fields.')
        for name in ('soundfont', 'fluidsynth_library'):
            _absolute(midi[name], name)
        for name in ('soundfont_sha256', 'fluidsynth_sha256'):
            _hash(midi[name], name)
        license_note = midi['soundfont_license']
        if (not isinstance(license_note, str) or not license_note.strip()
                or len(license_note) > 4000 or '\x00' in license_note):
            raise ValueError('Record the SoundFont license and attribution before registration.')
    # Round-trip also rejects surrogate/non-JSON configuration data.
    normalized = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    if check_files:
        _verify_bindings(normalized, deep=True, cancelled=cancelled)
    return normalized


def _verified_file(path, expected, label, cancelled=None):
    path = local_file(path)
    if file_sha256(path, cancelled) != expected:
        raise ValueError(label + ' changed; review and register its current identity again.')
    return path


def _verify_bindings(value, *, deep=False, cancelled=None):
    check_cancelled(cancelled)
    neural, midi = value['musicgen'], value['fluidsynth']
    for record, names in ((neural, ('model_dir', 'manifest', 'python_executable')),
                           (midi, ('soundfont', 'fluidsynth_library'))):
        if record and any(not Path(record[name]).is_absolute() for name in names):
            raise ValueError('This music registration belongs to a different operating system; select local paths again.')
    if neural is not None:
        _directory(Path(neural['model_dir']))
        manifest = local_file(neural['manifest'])
        if manifest.stat().st_size > 1024**2:
            raise ValueError('The MusicGen manifest exceeds its byte allowance.')
        _verified_file(manifest, neural['manifest_sha256'], 'The MusicGen manifest', cancelled)
        python = Path(tool_path('Python', neural['python_executable']))
        if file_sha256(python, cancelled) != neural['python_sha256']:
            raise ValueError('The MusicGen Python runtime changed; review its current identity again.')
        if deep:
            validate_musicgen_config(_config(neural), cancelled=cancelled)
    if midi is not None:
        font = _verified_file(midi['soundfont'], midi['soundfont_sha256'], 'The SoundFont', cancelled)
        if font.suffix.lower() != '.sf2' or font.stat().st_size > 2*1024**3:
            raise ValueError('Select a bounded local SF2 SoundFont.')
        library = _verified_file(midi['fluidsynth_library'], midi['fluidsynth_sha256'], 'The FluidSynth runtime', cancelled)
        if library.suffix.lower() not in ('.dll', '.so', '.dylib') and not re.search(r'\.so\.[0-9.]+$', library.name):
            raise ValueError('Select the installed FluidSynth shared library, not a command or model.')


class MusicSettingsStore:
    def __init__(self, root):
        self.projects = ProjectStore(root)
        self.path = self.projects.studio/'music-settings.json'

    def read(self):
        value = read_json(self.path, maximum=128*1024) if os.path.lexists(self.path) else {
            'version': 1, 'revision': 0, 'musicgen': None, 'fluidsynth': None}
        return validate_music_settings(value)

    def save(self, value, *, expected_revision, cancelled=None, check_files=True):
        value = validate_music_settings(value, check_files=check_files, cancelled=cancelled)
        check_cancelled(cancelled)
        with self.projects.transaction():
            previous = self.read()
            if previous['revision'] != expected_revision:
                raise ValueError('Music settings changed in another window. Reopen them before saving.')
            value['revision'] = previous['revision'] + 1
            atomic_json(self.path, value)
        return value

    def remove(self, engine, *, expected_revision):
        if engine not in ('musicgen', 'fluidsynth'):
            raise ValueError('Choose a configurable local music engine.')
        value = self.read()
        value[engine] = None
        return self.save(value, expected_revision=expected_revision, check_files=False)

    def import_registration(self, path, *, cancelled=None):
        value = read_json(local_file(path), maximum=128*1024)
        current = self.read()
        return self.save(value, expected_revision=current['revision'], cancelled=cancelled)


def load_music_options(root):
    """Generate kwargs for the director; no engine selection or automatic fallback.

    Loading checks registration seals. Full model hashes are rechecked in the
    music worker path, so constructing a controller does not scan gigabytes.
    An unconfigured selected engine still fails in generate_music.
    """
    value = MusicSettingsStore(root).read()
    _verify_bindings(value)
    options = {}
    if value['musicgen'] is not None:
        options['musicgen_config'] = _config(value['musicgen'])
    if value['fluidsynth'] is not None:
        record = value['fluidsynth']
        options.update(soundfont=Path(record['soundfont']), soundfont_license=record['soundfont_license'],
                       fluidsynth_library=Path(record['fluidsynth_library']))
    return options


def music_configuration_status(root):
    value = MusicSettingsStore(root).read()
    return {'version': 1, 'revision': value['revision'], 'mode': 'LOCAL_ONLY',
        'procedural': {'configured': True, 'kind': 'algorithmic instrumental; motif recurrence'},
        'fluidsynth': {'configured': value['fluidsynth'] is not None, 'inference_tested': False,
                      'kind': 'local symbolic notes and an explicitly reviewed SoundFont'},
        'musicgen': {'configured': value['musicgen'] is not None, 'inference_tested': False,
                    'kind': 'optional local neural engine; requires a verified isolated Linux network namespace; unavailable on Windows'}}
