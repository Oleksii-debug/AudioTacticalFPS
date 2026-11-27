"""Machine-local engine registrations over the existing reviewed manifests.

The canonical state/audio/engines.json location is retained. Schema 1 contains
legacy executable declarations and stays inert until an explicit registration
replaces it, preserving its exact bytes in the local legacy directory.
This file is deliberately outside every portable backup/sync allowlist.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import unicodedata
from uuid import uuid4

from filelock import FileLock, Timeout

from .audio.jobs import (_json_bytes, _sync_directory, atomic_json, checked_directory,
                         file_identity, publish_new, regular_file, verified_reader)
from .audio.model_manifest import LocalModelManifest, ModelManifestError, _read_json_snapshot, digest_file

MAX_SETTINGS_BYTES = 512 * 1024
MAX_PROFILES = 100
ENGINES = frozenset(('piper', 'dordiai-voices', 'musicgen', 'ace-step', 'fluidsynth', 'supplied-whisper'))
SPEECH_ENGINES = frozenset(('piper', 'dordiai-voices'))
MUSIC_ENGINES = frozenset(('musicgen', 'ace-step', 'fluidsynth'))
PROFILE_ID = re.compile(r'[0-9a-f]{32}')
SHA256 = re.compile(r'[0-9a-f]{64}')
LEGACY_FIELDS = frozenset(('schema', 'ffmpeg', 'ffprobe', 'piper', 'voice_helpers', 'music_models', 'fluidsynth'))


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Local engine verification cancelled.')


def _label(value):
    if (type(value) is not str or not value.strip() or len(value) > 128
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)):
        raise ValueError('Use a nonempty local profile name of at most 128 characters.')
    value.encode('utf-8')
    value = unicodedata.normalize('NFC', value.strip())
    if len(value) > 128:
        raise ValueError('Use a local profile name of at most 128 normalized characters.')
    return value


def _absolute(value):
    if (type(value) is not str or not value or len(value) > 4096 or '\x00' in value
            or value.startswith(('\\\\', '//')) or not Path(value).is_absolute()
            or any(':' in part for part in Path(value).parts[1:])):
        raise ValueError('A local profile requires an absolute local manifest path.')
    value.encode('utf-8')
    return value


@dataclass(frozen=True)
class EngineProfile:
    id: str
    name: str
    engine: str
    manifest_path: str
    manifest_sha256: str
    enabled: bool

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('Unsupported local engine profile fields.')
        if (type(value['id']) is not str or not PROFILE_ID.fullmatch(value['id'])
                or type(value['engine']) is not str or value['engine'] not in ENGINES
                or type(value['enabled']) is not bool
                or type(value['manifest_sha256']) is not str
                or not SHA256.fullmatch(value['manifest_sha256'])):
            raise ValueError('Invalid local engine profile identity.')
        if _label(value['name']) != value['name']:
            raise ValueError('Local profile names must use their normalized form.')
        _absolute(value['manifest_path'])
        return cls(**value)


@dataclass(frozen=True)
class ProfileSnapshot:
    profiles: tuple[EngineProfile, ...]
    sha256: str | None
    legacy: bool = False

    def profile(self, identity):
        if type(identity) is not str or not PROFILE_ID.fullmatch(identity):
            raise ValueError('Choose a saved local engine profile.')
        for profile in self.profiles:
            if profile.id == identity:
                return profile
        raise ValueError('The local engine registration no longer exists.')


@dataclass(frozen=True)
class ReviewedEngine:
    manifest: LocalModelManifest
    files: int
    bytes: int

    def description(self):
        value = self.manifest
        return (f'Engine: {value.engine}\nModel: {value.model_id}\nRevision: {value.revision}\n'
                f'License: {value.license}\nManifest SHA-256: {value.manifest_sha256}\n'
                f'Verified local files: {self.files}; bytes: {self.bytes}\n'
                'Registration verifies identity and declared acceptance; it does not qualify inference quality.')


class EngineProfileStore:
    """Bounded, atomically written local preferences; reads never execute engines."""
    def __init__(self, library_root):
        self.library_root = checked_directory(library_root)
        info = self.library_root.stat()
        self._root_identity = info.st_dev, info.st_ino
        self.path = self.library_root / 'state/audio/engines.json'

    def _root(self):
        root = checked_directory(self.library_root)
        info = root.stat()
        if (info.st_dev, info.st_ino) != self._root_identity:
            raise ValueError('The selected library directory changed.')

    def snapshot(self):
        self._root()
        if not os.path.lexists(self.path):
            return ProfileSnapshot((), None)
        if regular_file(self.path).st_nlink != 1:
            raise ValueError('The local engine registry must not have a hard-link alias.')
        value, digest = _read_json_snapshot(self.path, max_bytes=MAX_SETTINGS_BYTES)
        if type(value) is not dict or type(value.get('schema')) is not int:
            raise ValueError('Unsupported local engine registry.')
        if value['schema'] == 1:
            if set(value) != LEGACY_FIELDS:
                raise ValueError('Unrecognized legacy engine settings; retain them for manual review.')
            return ProfileSnapshot((), digest, legacy=True)
        if (value['schema'] != 2 or set(value) != {'schema', 'library_root', 'profiles'}
                or value['library_root'] != str(self.library_root)
                or type(value['profiles']) is not list or len(value['profiles']) > MAX_PROFILES):
            raise ValueError('Local engine settings belong to another location or unsupported schema; register locally again.')
        rows = tuple(EngineProfile.from_dict(row) for row in value['profiles'])
        ids, names = set(), set()
        for row in rows:
            name = row.name.casefold()
            if row.id in ids or name in names:
                raise ValueError('Local engine profiles have duplicate identities or names.')
            ids.add(row.id)
            names.add(name)
        return ProfileSnapshot(rows, digest)

    @contextmanager
    def _writer(self):
        self._root()
        checked_directory(self.path.parent, create=True)
        lock_path = self.path.with_name('engines.json.lock')
        def checked_lock():
            if os.path.lexists(lock_path) and regular_file(lock_path).st_nlink != 1:
                raise ValueError('The local engine settings lock must not have a hard-link alias.')
        checked_lock()
        lock = FileLock(str(lock_path), timeout=0)
        try:
            with lock:
                self._root()
                checked_directory(self.path.parent)
                checked_lock()
                yield
        except Timeout as exc:
            raise RuntimeError('Local engine settings are being changed by another operation; retry.') from exc

    def _preserve_legacy(self, snapshot):
        if not snapshot.legacy:
            return
        identity = file_identity(regular_file(self.path))
        with verified_reader(self.path, expected_identity=identity) as stream:
            raw = stream.read(MAX_SETTINGS_BYTES + 1)
        if len(raw) > MAX_SETTINGS_BYTES or hashlib.sha256(raw).hexdigest() != snapshot.sha256:
            raise ValueError('Legacy engine settings changed before preservation.')
        directory = checked_directory(self.path.parent / 'legacy', create=True)
        target = directory / ('engines-schema1-' + snapshot.sha256 + '.json')
        if os.path.lexists(target):
            if (regular_file(target).st_nlink != 1
                    or digest_file(target, max_bytes=MAX_SETTINGS_BYTES) != snapshot.sha256):
                raise ValueError('The saved legacy engine record changed.')
            # A previous move may have succeeded before a directory-sync error.
            _sync_directory(directory)
            return
        publish_new(self.path, target, expected_identity=identity)

    def _save(self, snapshot, rows):
        self._root()
        current = self.snapshot()
        if current.sha256 != snapshot.sha256:
            raise ValueError('Local engine settings changed; review the current registrations.')
        value = {'schema': 2, 'library_root': str(self.library_root),
                 'profiles': [asdict(row) for row in rows]}
        if len(_json_bytes(value)) > MAX_SETTINGS_BYTES:
            raise ValueError('The local engine registry exceeds its byte budget.')
        self._preserve_legacy(snapshot)
        atomic_json(self.path, value)

    def inspect(self, manifest_path, *, expected_sha256=None, engine=None, cancelled=lambda: False):
        self._root()
        _cancel(cancelled)
        manifest = LocalModelManifest.load(manifest_path, engine=engine, expected_sha256=expected_sha256)
        if (manifest.root.is_relative_to(self.library_root)
                or self.library_root.is_relative_to(manifest.root)):
            raise ModelManifestError('Choose an engine installation separate from the library and its portable projects.')
        if not manifest.trusted_weights:
            raise ModelManifestError('Review the runtime and model before setting trusted_weights in its local manifest.')
        report = manifest.verify(require_complete=True, cancelled=cancelled)
        if manifest.engine == 'piper':
            executable = manifest.settings.get('executable')
            if type(executable) is not str:
                raise ModelManifestError('A registered Piper manifest must declare its inventory-covered executable.')
            manifest.asset(executable)
            from .audio.engines import PiperEngine
            adapter = PiperEngine(manifest.asset(executable), manifest.path)
            if adapter.manifest.manifest_sha256 != manifest.manifest_sha256:
                raise ModelManifestError('The local engine manifest changed during admission.')
        _cancel(cancelled)
        self._root()
        return ReviewedEngine(manifest, report['files'], report['bytes'])

    def register(self, manifest_path, *, name=None, expected_sha256=None, enabled=True, cancelled=lambda: False):
        if type(enabled) is not bool:
            raise ValueError('Choose an explicit enabled state.')
        reviewed = self.inspect(manifest_path, expected_sha256=expected_sha256, cancelled=cancelled)
        manifest = reviewed.manifest
        chosen_name = _label(manifest.model_id if name is None else name)
        with self._writer():
            _cancel(cancelled)
            snapshot = self.snapshot()
            same = next((row for row in snapshot.profiles
                         if row.engine == manifest.engine and row.manifest_path == str(manifest.path)
                         and row.manifest_sha256 == manifest.manifest_sha256), None)
            identity = same.id if same is not None else uuid4().hex
            if any(row.id != identity and row.name.casefold() == chosen_name.casefold() for row in snapshot.profiles):
                raise ValueError('Choose a distinct local profile name.')
            if same is None and len(snapshot.profiles) >= MAX_PROFILES:
                raise ValueError('The local engine profile-count budget is full.')
            # Recheck the captured manifest after waiting for the settings lock.
            LocalModelManifest.load(manifest.path, engine=manifest.engine,
                                    expected_sha256=manifest.manifest_sha256)
            profile = EngineProfile(identity, chosen_name, manifest.engine, str(manifest.path),
                                    manifest.manifest_sha256, enabled)
            rows = [replace(row, enabled=False) if row.manifest_path == str(manifest.path) else row
                    for row in snapshot.profiles if row.id != identity]
            rows.append(profile)
            self._save(snapshot, tuple(rows))
            return profile

    def review(self, identity, *, cancelled=lambda: False):
        profile = self.snapshot().profile(identity)
        reviewed = self.inspect(profile.manifest_path, expected_sha256=profile.manifest_sha256,
                                engine=profile.engine, cancelled=cancelled)
        if self.snapshot().profile(identity) != profile:
            raise ValueError('The local engine registration changed during verification.')
        return reviewed

    def admit(self, identity, *, engine=None, cancelled=lambda: False):
        profile = self.snapshot().profile(identity)
        if not profile.enabled or (engine is not None and profile.engine != engine):
            raise ModelManifestError('The selected local engine profile is disabled or belongs to another engine.')
        reviewed = self.review(identity, cancelled=cancelled)
        if self.snapshot().profile(identity) != profile:
            raise ValueError('The local engine registration changed during admission.')
        return reviewed

    def set_enabled(self, identity, enabled, *, cancelled=lambda: False):
        if type(enabled) is not bool:
            raise ValueError('Choose an explicit enabled state.')
        original = self.snapshot().profile(identity)
        if enabled:
            self.review(identity, cancelled=cancelled)
        with self._writer():
            _cancel(cancelled)
            snapshot = self.snapshot()
            if snapshot.profile(identity) != original:
                raise ValueError('The local profile changed; select it again.')
            rows = tuple(replace(row, enabled=enabled) if row.id == identity else row for row in snapshot.profiles)
            self._save(snapshot, rows)

    def remove(self, identity):
        with self._writer():
            snapshot = self.snapshot()
            snapshot.profile(identity)
            self._save(snapshot, tuple(row for row in snapshot.profiles if row.id != identity))

    def music_manifest(self, reference, engine, *, cancelled=lambda: False):
        match = re.fullmatch(r'profiles/([0-9a-f]{32})\.json', reference) if type(reference) is str else None
        if match is None or type(engine) is not str or engine not in MUSIC_ENGINES:
            raise ModelManifestError('Choose a saved local music profile; project files cannot select executable installations.')
        return self.admit(match.group(1), engine=engine, cancelled=cancelled).manifest


def profile_reference(profile):
    if not isinstance(profile, EngineProfile) or profile.engine not in MUSIC_ENGINES:
        raise ValueError('Choose a registered local music profile.')
    return 'profiles/' + profile.id + '.json'


class _ProfileSpeechEngine:
    """Reuse the existing speech adapter with admission before every operation."""
    def __init__(self, store, profile):
        self.store, self.profile_id, self.engine = store, profile.id, profile.engine
        self.id = 'local-' + profile.id
        self._adapter = None

    def _admit(self, cancelled):
        reviewed = self.store.admit(self.profile_id, engine=self.engine, cancelled=cancelled)
        if self._adapter is None:
            from .audio.engines import DordiaiVoicesEngine, PiperEngine
            manifest = reviewed.manifest
            self._adapter = (PiperEngine(manifest.asset(manifest.settings['executable']), manifest.path)
                             if self.engine == 'piper' else DordiaiVoicesEngine(manifest.path))
        if self._adapter.manifest.manifest_sha256 != reviewed.manifest.manifest_sha256:
            raise ModelManifestError('The local speech profile changed; refresh the voice catalogue.')
        return self._adapter

    def _original_voice(self, voice):
        if type(voice) is not str or not voice.startswith(self.id + ':'):
            raise ValueError('Choose a voice from this registered local engine.')
        return self.engine + ':' + voice[len(self.id) + 1:]

    def voices(self, *, cancelled=lambda: False):
        adapter = self._admit(cancelled)
        profile = self.store.snapshot().profile(self.profile_id)
        return tuple(replace(row, id=self.id + ':' + row.id.split(':', 1)[1], engine=self.id,
                             name=profile.name + ' — ' + row.name)
                     for row in adapter.voices(cancelled=cancelled))

    def cache_identity(self, voice, *, cancelled=lambda: False):
        adapter = self._admit(cancelled)
        return {**adapter.cache_identity(self._original_voice(voice), cancelled=cancelled),
                'local_profile_id': self.profile_id}

    def synthesize(self, text, output, *, voice, cancelled=lambda: False, **settings):
        adapter = self._admit(cancelled)
        return adapter.synthesize(text, output, voice=self._original_voice(voice),
                                  cancelled=cancelled, **settings)


def configured_voice_registry(library_root, *, ffmpeg=None):
    """Keep installed system voices and add only enabled local registrations."""
    from .audio.engines import VoiceRegistry, default_registry
    base = default_registry(ffmpeg=ffmpeg)
    if not Path(library_root).exists():
        return base  # Read-only discovery does not create a new library root.
    try:
        store = EngineProfileStore(library_root)
        snapshot = store.snapshot()
    except (OSError, ValueError, RuntimeError):
        base.notices.append({'engine': 'local-profiles', 'available': False,
                             'reason': 'Local engine registrations need review. Installed system voices remain available.'})
        return base
    engines = list(base.engines.values())
    engines.extend(_ProfileSpeechEngine(store, row) for row in snapshot.profiles
                   if row.enabled and row.engine in SPEECH_ENGINES)
    configured = {row.engine for row in snapshot.profiles if row.enabled and row.engine in SPEECH_ENGINES}
    notices = [row for row in base.notices if row.get('engine') not in configured]
    if snapshot.legacy:
        notices.append({'engine': 'local-profiles', 'available': False,
                        'reason': 'Legacy executable settings are preserved but inactive. Register reviewed manifests locally.'})
    return VoiceRegistry(engines, notices=notices)
