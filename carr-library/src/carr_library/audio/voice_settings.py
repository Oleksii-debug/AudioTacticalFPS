"""Reviewed voice-package authority inside the existing engine settings file.

This facade does not create another speech registry. It updates only the current
dordiai management binding and preserves every unrelated setting and preference.
"""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path, PureWindowsPath
import re

from ..durable_state import atomic_json, local_path, regular, state_lock
from .voice_manifest import LocalModelManifest, ModelManifestError, digest_file

_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_OWNED = ('runtime', 'references', 'packages', 'lock')


def _path(value):
    if (type(value) is not str or not 1 <= len(value) <= 32768
            or any(ord(char) < 32 for char in value) or '://' in value
            or value.startswith(chr(92)) or value.startswith('//')):
        raise ValueError('Choose an explicit absolute local voice path.')
    native, windows = Path(value), PureWindowsPath(value)
    if (not native.is_absolute() and not windows.is_absolute()
            or '..' in native.parts or '..' in windows.parts
            or any(':' in part for part in windows.parts[1:])):
        raise ValueError('Voice paths cannot contain traversal or alternate streams.')
    return value


def validate_management_binding(value):
    """Pure decoder; backup validation must not open an external installation."""
    if (type(value) is not dict
            or set(value) != {'schema', 'revision', 'manifest', 'sha256', 'restored', 'ownership'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['revision']) is not int or not 1 <= value['revision'] < 2**63
            or type(value['restored']) is not bool
            or type(value['sha256']) is not str or not _DIGEST.fullmatch(value['sha256'])):
        raise ValueError('Invalid reviewed voice-package binding.')
    _path(value['manifest'])
    ownership = value['ownership']
    if (type(ownership) is not dict or set(ownership) != {'schema', *_OWNED}
            or ownership['schema'] != 'carr-voice-ownership/1'):
        raise ValueError('Invalid voice-package ownership binding.')
    for name in _OWNED:
        row = ownership[name]
        if (type(row) is not dict or set(row) != {'path', 'device', 'inode'}
                or type(row['device']) is not int or row['device'] < 0
                or type(row['inode']) is not int or row['inode'] < 0):
            raise ValueError('Invalid voice-package directory or lock identity.')
        _path(row['path'])
    return deepcopy(value)


def _identity(path):
    info = regular(local_path(path, exists=True), directory=True)
    return info.st_dev, info.st_ino


def verify_voice_settings_bindings(value, *, cancelled=lambda: False):
    if (type(value) is not dict or value.get('kind') != 'local-voice-settings'
            or type(value.get('revision')) is not int or value['revision'] < 0
            or type(value.get('config')) is not dict or type(value.get('bindings')) is not dict):
        raise ValueError('Read the current local voice-package registration.')
    if value.get('restored'):
        raise ValueError('Restored voice authority requires explicit local revalidation.')
    config, bindings = value['config'], value['bindings']
    if set(config) != {'dordiai_manifest'} or set(bindings) != {'dordiai_manifest'}:
        raise ValueError('Register the reviewed manifest before managing voice packages.')
    if digest_file(config['dordiai_manifest'], cancelled=cancelled) != bindings['dordiai_manifest']:
        raise ValueError('The registered voice manifest changed; explicitly revalidate it.')
    return value


class VoiceManagementCommitted(RuntimeError):
    """The exact settings publication completed before a later cleanup failure."""

    def __init__(self, receipt, error):
        super().__init__('Voice settings were saved; a later scope or cleanup check failed. Reopen management before another change.')
        self.voice_management_receipt = deepcopy(receipt)
        self.cleanup_error = type(error).__name__


def preserve_management_commit(error, receipt):
    """Never retry or relabel a proven committed mutation as an unstarted one."""
    if isinstance(error, VoiceManagementCommitted):
        raise error
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        error.voice_management_receipt = deepcopy(receipt)
        raise error
    raise VoiceManagementCommitted(receipt, error) from error


class VoiceSettingsStore:
    """Library-root authority and CAS updates on current runtime_settings only."""

    def __init__(self, root, *, scope_guard=lambda: None):
        if not callable(scope_guard):
            raise TypeError('A callable current-library guard is required.')
        self.scope_guard = scope_guard
        scope_guard()
        self.root = local_path(root, exists=True)
        self.state_root = self.root / 'state'
        self._root_identity = _identity(self.root)
        self._state_identity = _identity(self.state_root)
        self.path = self.state_root / 'audio' / 'engines.json'
        self._audio_identity = _identity(self.path.parent) if self.path.parent.exists() else None

    def check(self):
        self.scope_guard()
        # One fresh lexical walk covers these nested captured directories.
        # Their identities are still read separately on every invocation.
        leaf = self.path.parent if self._audio_identity is not None else self.state_root
        if local_path(leaf, exists=True) != leaf:
            raise ValueError('The selected library changed; reopen local voice management.')
        for path, expected in ((self.root, self._root_identity),
                               (self.state_root, self._state_identity)):
            info = regular(path, directory=True)
            if (info.st_dev, info.st_ino) != expected:
                raise ValueError('The selected library changed; reopen local voice management.')
        if self._audio_identity is not None:
            info = regular(self.path.parent, directory=True)
            if (info.st_dev, info.st_ino) != self._audio_identity:
                raise ValueError('The selected engine settings directory changed.')
        return self.root

    def _configuration(self):
        from ..runtime_settings import _read_settings
        self.check()
        value = _read_settings(self.state_root)
        self.check()
        row = value['dordiai']
        if row is None or row.get('enabled') is not True:
            raise ValueError('Explicitly connect the installed dordiai runtime before managing packages.')
        return value, row

    def read(self):
        _settings, row = self._configuration()
        binding = row.get('management')
        if binding is None:
            return {'version': 1, 'kind': 'local-voice-settings', 'revision': 0,
                    'restored': False, 'config': {}, 'bindings': {},
                    'runtime_configuration': deepcopy(row)}
        binding = validate_management_binding(binding)
        return {'version': 1, 'kind': 'local-voice-settings', 'revision': binding['revision'],
                'restored': binding['restored'],
                'config': {'dordiai_manifest': binding['manifest']},
                'bindings': {'dordiai_manifest': binding['sha256']},
                'runtime_configuration': deepcopy(row)}

    @contextmanager
    def transaction(self):
        self.check()
        # An enabled current engine already has this directory; a stale facade
        # never recreates it before acquiring the ordinary state lock.
        regular(self.path.parent, directory=True)
        with state_lock(self.path.parent):
            self.check()
            yield
            self.check()

    def _validate_runtime(self, manifest, row, *, cancelled):
        runtime = local_path(row['runtime_directory'], exists=True)
        if manifest.root != runtime:
            raise ValueError('The package manifest must belong to the currently connected runtime.')
        worker = manifest.asset(manifest.settings.get('worker', 'nvda_worker.py'))
        if worker != runtime / 'nvda_worker.py':
            raise ValueError('Use the existing reviewed runtime worker.')
        from ..integrations.dordiai_voices import REVIEWED_WORKER_SHA256
        expected_worker = row.get('expected_worker_sha256', REVIEWED_WORKER_SHA256)
        if digest_file(worker, cancelled=cancelled) != expected_worker:
            raise ValueError('The selected manifest does not bind the connected reviewed worker.')
        python = manifest.asset(manifest.settings.get('python', 'python/python.exe'))
        selected_python = local_path(row.get('python_executable') or runtime / 'python' / 'python.exe', exists=True)
        if python != selected_python:
            raise ValueError('The package interpreter differs from the connected runtime interpreter.')
        return runtime

    def register_manifest(self, path, *, expected_revision, cancelled=lambda: False):
        """Explicitly review bytes and establish the owner's existing lock API."""
        from .voice_packages import _prepare_registration, _verify_manifest
        if cancelled():
            raise InterruptedError('Local voice registration cancelled before review.')
        self.check()
        previous = self.read()
        if type(expected_revision) is not int or previous['revision'] != expected_revision:
            raise ValueError('Voice settings changed; reopen the registration review.')
        manifest = LocalModelManifest.load(path, engine='dordiai-voices')
        _verify_manifest(manifest, cancelled=cancelled, allow_unprepared=True)
        self._validate_runtime(manifest, previous['runtime_configuration'], cancelled=cancelled)
        return _prepare_registration(self, previous, manifest, cancelled=cancelled)

    def register(self, config, *, expected_revision, cancelled=lambda: False):
        if type(config) is not dict or set(config) != {'dordiai_manifest'}:
            raise ValueError('Use the current engine dialog for engine settings; select only its reviewed package manifest here.')
        return self.register_manifest(config['dordiai_manifest'],
            expected_revision=expected_revision, cancelled=cancelled)

    def revalidate(self, *, expected_revision, cancelled=lambda: False):
        current = self.read()
        path = current['config'].get('dordiai_manifest')
        if not path:
            raise ValueError('Choose the reviewed package manifest explicitly.')
        return self.register_manifest(path, expected_revision=expected_revision, cancelled=cancelled)

    def _write_binding(self, previous, manifest, ticket, *, cancelled):
        from ..runtime_settings import _read_settings, validate_dordiai_configuration
        self.check()
        if cancelled():
            raise InterruptedError('Local voice management cancelled before settings publication.')
        if self.read() != previous:
            raise ValueError('Voice settings changed during maintenance; revalidate the installed files.')
        current = _read_settings(self.state_root)
        row = deepcopy(current['dordiai'])
        self._validate_runtime(manifest, row, cancelled=cancelled)
        binding = {'schema': 1, 'revision': previous['revision'] + 1,
                   'manifest': str(manifest.path), 'sha256': manifest.manifest_sha256,
                   'restored': False, 'ownership': ticket.identity}
        row['management'] = validate_management_binding(binding)
        current['dordiai'] = validate_dordiai_configuration(row)
        self.check()
        if cancelled():
            raise InterruptedError('Local voice management cancelled before settings publication.')
        # Capture the exact published state before any post-commit callback.
        receipt = {'version': 1, 'kind': 'local-voice-settings', 'revision': binding['revision'],
                   'restored': False, 'config': {'dordiai_manifest': binding['manifest']},
                   'bindings': {'dordiai_manifest': binding['sha256']},
                   'runtime_configuration': deepcopy(current['dordiai'])}
        atomic_json(self.path, current, replace=self.path.exists())
        try:
            self.check()
            if self.read() != receipt:
                raise ValueError('The saved voice authority changed immediately after publication.')
        except BaseException as error:
            preserve_management_commit(error, receipt)
        return receipt


def load_registered_voice_config(root):
    return verify_voice_settings_bindings(VoiceSettingsStore(root).read())['config']


def voice_configuration_status(root):
    value = VoiceSettingsStore(root).read()
    return {'version': 1, 'revision': value['revision'], 'mode': 'LOCAL_ONLY',
            'restored_requires_revalidation': value['restored'],
            'configured': sorted(value['bindings']), 'inference_tested': False}
