"""Explicit local voice selections; saved rows never authorize execution.

This module connects the existing Piper and dordiai-voices adapters. It does not
install models, discover private directories, or import an external runtime.
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import re
import threading
from uuid import uuid4
from weakref import WeakValueDictionary

from ..durable_state import atomic_bytes, local_path, regular
from ..ingest.writer_guard import exclusive_writer
from .model_manifest import LocalModelManifest, digest_file

FILENAME = 'voice-settings.json'
MAX_BYTES = 65536
MAX_PROFILES = 32
_KIND = 'resonance-local-voice-settings'
_ENGINES = ('piper', 'dordiai-voices')
_FIELDS = {'id', 'name', 'engine', 'manifest', 'executable',
           'manifest_sha256', 'executable_sha256'}


def _plain(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value.strip() == value and all(char.isprintable() for char in value))


def _path_text(value):
    if value is None:
        return
    if (not isinstance(value, str) or not value or len(value) > 4096
            or value.startswith(('//', '\\\\')) or '://' in value
            or any(ord(char) < 32 for char in value)
            or any(part in ('.', '..') for part in re.split(r'[\\/]', value))):
        raise ValueError('Choose a regular local voice path.')
    windows = PureWindowsPath(value)
    parts = windows.parts if windows.drive else Path(value).parts
    if (not (Path(value).is_absolute() or windows.is_absolute())
            or '..' in parts or '.' in parts
            or any(':' in part for part in parts[1:])
            or (not windows.drive and ':' in value)):
        raise ValueError('Voice paths must be absolute, without traversal or alternate streams.')


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    name: str
    engine: str
    manifest: str | None
    executable: str | None
    manifest_sha256: str
    executable_sha256: str | None = None

    def __post_init__(self):
        if (not isinstance(self.id, str) or not re.fullmatch('[0-9a-f]{32}', self.id)
                or not _plain(self.name, 120) or self.engine not in _ENGINES
                or not isinstance(self.manifest_sha256, str)
                or not re.fullmatch('[0-9a-f]{64}', self.manifest_sha256)):
            raise ValueError('Invalid local voice profile.')
        _path_text(self.manifest)
        _path_text(self.executable)
        if self.engine == 'piper':
            if (not isinstance(self.executable_sha256, str)
                    or not re.fullmatch('[0-9a-f]{64}', self.executable_sha256)
                    or (self.manifest is None) != (self.executable is None)):
                raise ValueError('Piper needs a pinned executable and model manifest.')
        elif self.executable is not None or self.executable_sha256 is not None:
            raise ValueError('The existing dordiai adapter takes its interpreter from the manifest.')

    @property
    def connected(self):
        return self.manifest is not None

    def disconnected(self):
        return replace(self, manifest=None, executable=None)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate local voice setting.')
        value[key] = item
    return value


def decode_settings(raw, *, disconnected=False):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Local voice settings exceed their byte budget.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number.')))
        if (not isinstance(value, dict) or set(value) != {'kind', 'version', 'profiles'}
                or value['kind'] != _KIND or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['profiles'], list) or len(value['profiles']) > MAX_PROFILES):
            raise ValueError('Invalid local voice settings schema.')
        result = []
        for row in value['profiles']:
            if not isinstance(row, dict) or set(row) != _FIELDS:
                raise ValueError('Invalid local voice profile fields.')
            profile = VoiceProfile(**row)
            if disconnected and profile.connected:
                raise ValueError('Backup voice profiles must be disconnected from external paths.')
            if any(previous.id == profile.id for previous in result):
                raise ValueError('Duplicate local voice profile identity.')
            result.append(profile)
        return tuple(result)
    except (UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError('Invalid local voice settings.') from exc


def encode_settings(profiles, *, disconnected=False):
    rows = [asdict(row.disconnected() if disconnected else row) for row in profiles]
    raw = (json.dumps({'kind': _KIND, 'version': 1, 'profiles': rows}, ensure_ascii=False,
                      sort_keys=True, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')
    decode_settings(raw, disconnected=disconnected)
    return raw


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_nlink, value.st_mode,
            getattr(value, 'st_file_attributes', 0))


def _binding(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_nlink


def _read(path):
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size > MAX_BYTES:
        raise ValueError('Local voice settings exceed their byte budget.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _binding(opened) != _binding(before):
            raise ValueError('Local voice settings changed while opening.')
        raw = stream.read(MAX_BYTES + 1)
        if _stamp(os.fstat(stream.fileno())) != _stamp(opened):
            raise ValueError('Local voice settings changed while reading.')
    if _stamp(regular(path)) != _stamp(before) or len(raw) > MAX_BYTES:
        raise ValueError('Local voice settings changed while reading.')
    return raw


class VoiceProfileStore:
    def __init__(self, root):
        self.root = local_path(root, exists=True)
        regular(self.root, directory=True)
        self.path = self.root / FILENAME

    def load(self):
        if not os.path.lexists(self.path):
            return ()
        return decode_settings(_read(self.path))

    def get(self, identity):
        result = next((row for row in self.load() if row.id == identity), None)
        if result is None:
            raise ValueError('The selected local voice profile no longer exists.')
        return result

    def save(self, profile, *, expected=None):
        if not isinstance(profile, VoiceProfile):
            raise ValueError('Expected a reviewed local voice profile.')
        with exclusive_writer(self.root):
            rows = list(self.load())
            old = next((row for row in rows if row.id == profile.id), None)
            if old != expected:
                raise ValueError('The local voice profile changed. Reload it before saving.')
            rows = [row for row in rows if row.id != profile.id] + [profile]
            raw = encode_settings(rows)
            atomic_bytes(self.path, raw, replace=self.path.exists())
        return profile

    def remove(self, identity, *, expected):
        with exclusive_writer(self.root):
            rows = self.load()
            old = next((row for row in rows if row.id == identity), None)
            if old is None or old != expected:
                raise ValueError('The local voice profile changed. Reload before removing it.')
            atomic_bytes(self.path, encode_settings(row for row in rows if row.id != identity),
                         replace=True)


def _live_path(value):
    if not Path(value).is_absolute():
        raise ValueError('Select a local voice installation for this operating system.')
    return local_path(value, exists=True)


def _inspect(profile, *, cancelled=lambda: False, verify_assets=True):
    if not profile.connected:
        raise ValueError('This saved profile has no local paths. Select and review its installation again.')
    if cancelled():
        raise InterruptedError('Local voice review cancelled.')
    manifest_path = _live_path(profile.manifest)
    regular(manifest_path)
    model = LocalModelManifest.load(manifest_path, engine=profile.engine,
                                    expected_sha256=profile.manifest_sha256)
    if profile.engine == 'piper':
        binary = _live_path(profile.executable)
        regular(binary)
        if digest_file(binary, cancelled=cancelled) != profile.executable_sha256:
            raise ValueError('The selected Piper executable changed. Review this profile again.')
        from .engines import PiperEngine
        adapter = PiperEngine(binary, manifest_path)
    else:
        from .engines import DordiaiVoicesEngine
        adapter = DordiaiVoicesEngine(manifest_path)
    facts = {'files': len(model.files)}
    if verify_assets:
        facts = model.verify(cancelled=cancelled, require_complete=profile.engine == 'dordiai-voices')
    # Constructors must see the same manifest which was reviewed, including if
    # an external writer replaced it between the two independent reads.
    if adapter.manifest.manifest_sha256 != profile.manifest_sha256:
        raise ValueError('The voice manifest changed during review.')
    return {'engine': profile.engine, 'model': model.model_id, 'revision': model.revision,
            'license': model.license, 'files': facts['files'], 'manifest_sha256': model.manifest_sha256}


def prepare_profile(name, engine, manifest, *, executable=None, identity=None,
                    cancelled=lambda: False):
    if engine not in _ENGINES:
        raise ValueError('Choose an existing supported local voice adapter.')
    _path_text(str(manifest))
    path = _live_path(manifest)
    regular(path)
    model = LocalModelManifest.load(path, engine=engine)
    binary, checksum = None, None
    if engine == 'piper':
        if executable is None:
            raise ValueError('Select the existing Piper executable.')
        _path_text(str(executable))
        binary = _live_path(executable)
        regular(binary)
        checksum = digest_file(binary, cancelled=cancelled)
    elif executable is not None:
        raise ValueError('This adapter uses only its manifest-listed interpreter.')
    profile = VoiceProfile(identity or uuid4().hex, name, engine, str(path),
                           str(binary) if binary else None, model.manifest_sha256, checksum)
    return profile, _inspect(profile, cancelled=cancelled)


def validate_voice_settings_payload(raw):
    """Validate the disconnected, non-authorizing complete-backup representation."""
    return decode_settings(raw, disconnected=True)


def snapshot_voice_settings(root):
    """The library backup caller already holds the shared writer guard."""
    store = VoiceProfileStore(root)
    if not os.path.lexists(store.path):
        return {}
    raw = encode_settings(store.load(), disconnected=True)
    validate_voice_settings_payload(raw)
    return {FILENAME: raw}


class SessionVoiceProfiles:
    """One explicitly reviewed profile per optional engine for this app session."""
    def __init__(self, root, *, registry_factory=None):
        self.store = VoiceProfileStore(root)
        self._active = {}
        self._generation = 0
        self._lock = threading.RLock()
        self._factory = registry_factory

    def active_ids(self):
        with self._lock:
            return frozenset(row.id for row in self._active.values())

    def enable(self, identity, *, owner_trusted=False, cancelled=lambda: False):
        if owner_trusted is not True:
            raise ValueError('Review the exact installed runtime and permit it for this session.')
        profile = self.store.get(identity)
        facts = _inspect(profile, cancelled=cancelled)
        if cancelled():
            raise InterruptedError('Local voice review cancelled.')
        with self._lock:
            if self.store.get(identity) != profile:
                raise ValueError('The voice profile changed during review.')
            self._active[profile.engine] = profile
            self._generation += 1
        return facts

    def disable(self, identity=None):
        with self._lock:
            self._active = {key: row for key, row in self._active.items()
                            if identity is not None and row.id != identity}
            self._generation += 1

    def registry(self, *, ffmpeg=None):
        with self._lock:
            return _SessionRegistry(self, self._generation, tuple(self._active.values()), ffmpeg)

    def _current(self, generation, profiles, *, cancelled=lambda: False):
        with self._lock:
            if generation != self._generation or tuple(self._active.values()) != profiles:
                raise ValueError('Local voice session authorization changed. Reload voices.')
        for profile in profiles:
            if self.store.get(profile.id) != profile:
                raise ValueError('A saved voice profile changed. Review it again.')
            _inspect(profile, cancelled=cancelled, verify_assets=False)
        with self._lock:
            if generation != self._generation:
                raise ValueError('Local voice session authorization changed. Reload voices.')


class _SessionRegistry:
    def __init__(self, session, generation, profiles, ffmpeg):
        self._session, self._generation, self._profiles = session, generation, profiles
        self._ffmpeg, self._value = ffmpeg, None

    def _checked(self, *, cancelled=lambda: False):
        self._session._current(self._generation, self._profiles, cancelled=cancelled)
        if self._value is None:
            values = {'ffmpeg': self._ffmpeg}
            for row in self._profiles:
                if row.engine == 'piper':
                    values.update(piper_manifest=row.manifest, piper_executable=row.executable)
                else:
                    values['dordiai_manifest'] = row.manifest
            factory = self._session._factory
            if factory is None:
                from .engines import default_registry
                factory = default_registry
            self._value = factory(**values)
        return self._value

    @property
    def notices(self):
        return self._checked().notices

    def voices(self, *, refresh=False):
        return self._checked().voices(refresh=refresh)

    def capabilities(self):
        return self._checked().capabilities()

    def cache_identity(self, voice):
        return self._checked().cache_identity(voice)

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False):
        registry = self._checked(cancelled=cancelled)
        def stopped():
            with self._session._lock:
                return cancelled() or self._session._generation != self._generation
        return registry.synthesize(text, output, voice=voice, language=language, rate=rate,
                                   pitch=pitch, volume=volume, cancelled=stopped)


_sessions = WeakValueDictionary()
_sessions_lock = threading.Lock()


def session_for(root):
    from ..data_paths import guard_data_directory
    from .jobs import checked_directory
    path = checked_directory(guard_data_directory(root), create=True)
    key = str(path)
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            session = SessionVoiceProfiles(path)
            _sessions[key] = session
        return session
