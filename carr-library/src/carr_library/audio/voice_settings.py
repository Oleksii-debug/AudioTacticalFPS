"""Explicit library-local speech registrations, shared by desktop and CLI.

Only local paths and their reviewed digests are persisted. Runtime/model assets
stay in their original installation. Restored registrations require review
before any configured program can be executed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import re

from .projects import ProjectStore, atomic_json, read_json
from .voice_manifest import digest_file, local_path


_DIGEST = re.compile('[0-9a-f]{64}\\Z')


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 32768
            or any(ord(char) < 32 for char in value) or '://' in value
            or value.startswith(('\\\\', '//'))):
        raise ValueError('Voice registrations require explicit absolute local file paths.')
    native, windows = Path(value), PureWindowsPath(value)
    windows_absolute = windows.is_absolute() and re.fullmatch('[A-Za-z]:', windows.drive)
    if (not native.is_absolute() and not windows_absolute
            or '..' in native.parts or '..' in windows.parts
            or any(':' in part for part in windows.parts[1:])):
        raise ValueError('Voice registration paths cannot contain traversal or alternate streams.')
    return value


def validate_voice_config(value):
    """Pure strict decoder, including foreign-OS paths, suitable for backup."""
    if type(value) is not dict or set(value) - {'ffmpeg', 'espeak_ng', 'include_sapi', 'piper', 'dordiai_manifest'}:
        raise ValueError('Unsupported local voice configuration fields.')
    if 'include_sapi' in value and type(value['include_sapi']) is not bool:
        raise ValueError('include_sapi must be true or false.')
    for name in ('ffmpeg', 'espeak_ng'):
        if name in value and value[name] is not None and value[name] is not False:
            _path(value[name])
    if 'piper' in value:
        row = value['piper']
        if type(row) is not dict or set(row) != {'executable', 'manifest'}:
            raise ValueError('A Piper registration requires an executable and a reviewed manifest.')
        _path(row['executable'])
        _path(row['manifest'])
    if 'dordiai_manifest' in value:
        _path(value['dordiai_manifest'])
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def _files(config):
    result = {name: config[name] for name in ('ffmpeg', 'espeak_ng') if isinstance(config.get(name), str)}
    if 'piper' in config:
        result.update({'piper.' + key: value for key, value in config['piper'].items()})
    if 'dordiai_manifest' in config:
        result['dordiai_manifest'] = config['dordiai_manifest']
    return result


def validate_voice_settings_document(value):
    """Decode stored registration without reading or running external assets."""
    if (type(value) is not dict
            or set(value) != {'version', 'kind', 'revision', 'restored', 'config', 'bindings'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'local-voice-settings'
            or type(value['revision']) is not int or not 0 <= value['revision'] < 2**63
            or type(value['restored']) is not bool or type(value['bindings']) is not dict):
        raise ValueError('Unsupported persisted local voice registration.')
    config = validate_voice_config(value['config'])
    if set(value['bindings']) != set(_files(config)) or any(
            not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            for digest in value['bindings'].values()):
        raise ValueError('Every explicit voice file needs its reviewed SHA256 binding.')
    return {**value, 'config': config, 'bindings': dict(value['bindings'])}


def _cancelled(cancelled):
    from .voices import _cancel
    _cancel(cancelled)
    return False


def _bindings(config, *, cancelled=None):
    result = {}
    for name, raw in _files(config).items():
        _cancelled(cancelled)
        if not Path(raw).is_absolute():
            raise ValueError('This voice registration belongs to a different operating system; select local paths again.')
        path = local_path(raw)
        if not path.is_file():
            raise ValueError('A voice registration must identify regular local files.')
        result[name] = digest_file(path, cancelled=lambda: _cancelled(cancelled))
    return result


def verify_voice_settings_bindings(value, *, cancelled=None):
    value = validate_voice_settings_document(value)
    if value['restored']:
        raise ValueError('Restored voice settings require explicit revalidation in Local voice engines before use.')
    if _bindings(value['config'], cancelled=cancelled) != value['bindings']:
        raise ValueError('A registered local voice runtime or manifest changed. Review and register it again.')
    return value


class VoiceSettingsStore:
    def __init__(self, root):
        self.projects = ProjectStore(root)
        self.path = self.projects.studio / 'voice-settings.json'

    def read(self):
        value = read_json(self.path, maximum=256 * 1024) if os.path.lexists(self.path) else {
            'version': 1, 'kind': 'local-voice-settings', 'revision': 0,
            'restored': False, 'config': {}, 'bindings': {}}
        return validate_voice_settings_document(value)

    def register(self, config, *, expected_revision, cancelled=None):
        """Validate installed bytes and enumeration, then atomically publish.

        Enumeration is not an acoustic or neural-inference acceptance test.
        A candidate-only profile may be registered but is never selectable for
        production until its own supported languages are reviewed.
        """
        from .voices import VoiceRegistry
        config = validate_voice_config(config)
        bindings = _bindings(config, cancelled=cancelled)
        _cancelled(cancelled)
        registry = VoiceRegistry(config)
        statuses = {row['engine']: row for row in registry.engine_status()}
        required = [('piper', 'piper'), ('dordiai_manifest', 'dordiai-voices'),
                    ('ffmpeg', 'flite'), ('espeak_ng', 'espeak-ng')]
        for key, engine in required:
            if key in config and config[key] is not False and config[key] is not None:
                if statuses[engine]['status'] not in ('available', 'configured'):
                    raise ValueError('The explicitly selected ' + engine + ' installation did not pass validation.')
        if _bindings(config, cancelled=cancelled) != bindings:
            raise ValueError('The selected voice files changed during validation; select them again.')
        value = {'version': 1, 'kind': 'local-voice-settings', 'revision': 0,
                 'restored': False, 'config': config, 'bindings': bindings}
        with self.projects.transaction():
            previous = self.read()
            if previous['revision'] != expected_revision:
                raise ValueError('Voice settings changed in another window. Reopen them before saving.')
            _cancelled(cancelled)
            value['revision'] = previous['revision'] + 1
            atomic_json(self.path, value)
        return value

    def import_config(self, path, *, expected_revision=None, cancelled=None):
        from .voices import load_voice_config
        current = self.read()
        return self.register(load_voice_config(path),
            expected_revision=current['revision'] if expected_revision is None else expected_revision,
            cancelled=cancelled)

    def revalidate(self, *, expected_revision, cancelled=None):
        return self.register(self.read()['config'], expected_revision=expected_revision, cancelled=cancelled)

    def reset(self, *, expected_revision):
        """Detach optional registrations without reading or deleting any assets."""
        with self.projects.transaction():
            previous = self.read()
            if previous['revision'] != expected_revision:
                raise ValueError('Voice settings changed in another window. Reopen them before saving.')
            value = {'version': 1, 'kind': 'local-voice-settings', 'revision': previous['revision'] + 1,
                     'restored': False, 'config': {}, 'bindings': {}}
            atomic_json(self.path, value)
        return value


def load_registered_voice_config(root):
    return verify_voice_settings_bindings(VoiceSettingsStore(root).read())['config']


def voice_configuration_status(root):
    value = VoiceSettingsStore(root).read()
    return {'version': 1, 'revision': value['revision'], 'mode': 'LOCAL_ONLY',
            'restored_requires_revalidation': value['restored'],
            'configured': sorted(value['bindings']), 'inference_tested': False}
