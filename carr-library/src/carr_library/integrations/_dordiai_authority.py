"""Current-library approval and one existing owner lock for dordiai speech.

No owner modules are imported here. The existing probe remains the discovery
transport, and the existing PCM worker remains the only synthesis transport.
Authority is checked afresh per operation; no reviewed plan is hot-rebound.
"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Lock, RLock, get_ident

from ..durable_state import local_path, regular
from ..audio.voice_manifest import LocalModelManifest, _read_json_snapshot, digest_file
from ..audio.voice_ownership import capture_voice_ownership, hold_voice_ownership, settle_voice_cleanup


def _requested(cancelled):
    return (bool(cancelled()) if callable(cancelled)
            else cancelled is not None and cancelled.is_set())


def _cancel(cancelled):
    if _requested(cancelled):
        raise InterruptedError('Local voice operation cancelled.')


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False)


def _directory(path):
    selected = local_path(path, exists=True)
    info = regular(selected, directory=True)
    return selected, (info.st_dev, info.st_ino)


class AuthorityBusy(RuntimeError):
    """Another worker already owns this adapter's call-local authority."""


class DordiaiAuthority:
    """One captured approval; repeated checks never adopt a newer approval."""

    def __init__(self, runtime, *, configuration=None, state_root=None):
        self.runtime, _ = _directory(runtime)
        self.references, _ = _directory(self.runtime / 'models' / 'voices')
        directories = dict.fromkeys((*reversed(self.references.parents), self.references))
        self._directories = tuple(_directory(path) for path in directories)
        self.packages = self.references / 'packages'
        try:
            _directory(self.packages)
        except FileNotFoundError:
            self._has_packages = False
        else:
            self._has_packages = True
        self._configuration = None
        self._store = None
        self._state_root = None
        self._state_directories = ()
        self._binding = None
        if configuration is not None:
            from ..runtime_settings import validate_dordiai_configuration
            self._configuration = deepcopy(validate_dordiai_configuration(configuration))
            if self._configuration.get('enabled') is not True:
                raise ValueError('Use an explicitly enabled current voice configuration.')
            self._binding = self._configuration.get('management')
        if state_root is not None:
            if self._configuration is None:
                raise ValueError('Capture the current voice configuration with its library.')
            selected, _ = _directory(state_root)
            self._state_root = selected
            audio = selected / 'audio'
            paths = dict.fromkeys((*reversed(audio.parents), audio))
            self._state_directories = tuple(_directory(path) for path in paths)
            # Legacy runtime_settings has always accepted an explicit state
            # directory with any name.  Only managed library authority uses
            # the canonical library/state facade.
            if self._binding is not None:
                from ..audio.voice_settings import VoiceSettingsStore
                if selected.name != 'state':
                    raise ValueError('Use the current library state directory.')
                self._store = VoiceSettingsStore(selected.parent)
        if self._binding is not None:
            if self._store is None or self._binding['restored']:
                raise ValueError('Managed or restored voices require explicit current-library review.')
            if not self._has_packages:
                raise ValueError('The reviewed voice package ownership directory is missing.')
        self._ticket = (capture_voice_ownership(self.runtime,
                            expected=self._binding['ownership'] if self._binding else None)
                        if self._has_packages else None)
        self._pid = os.getpid()
        self._state, self._gate = RLock(), Lock()
        self._depth, self._thread = 0, None
        self._adapter = None
        self._invalid = False
        self._stack = None
        self._lease = None
        self._manifest = None
        self._index_digest = None
        self._profiles = {}
        self._owned = set()
        self._selected = set()
        self._check_base()

    def attach(self, adapter):
        if self._adapter is not None or adapter.runtime != self.runtime:
            raise ValueError('A voice authority belongs to one selected adapter.')
        self._adapter = adapter

    def _reject(self, message):
        self._invalid = True
        raise ValueError(message)

    def _check_base(self):
        try:
            self._read_base()
        except (OSError, ValueError):
            self._invalid = True
            raise

    def _read_base(self):
        if self._pid != os.getpid():
            raise ValueError('A voice authority cannot be used by another process.')
        if self._invalid:
            raise ValueError('Voice authority changed; close this provider and review it again.')
        for path, identity in self._directories:
            if _directory(path) != (path, identity):
                self._reject('The selected voice runtime directory changed.')
        try:
            _directory(self.packages)
        except FileNotFoundError:
            present = False
        else:
            present = True
        if present != self._has_packages:
            self._reject('Voice package ownership changed; reopen the selected provider.')
        if self._state_root is not None:
            from ..runtime_settings import _read_settings
            self._check_settings_scope()
            current = _read_settings(self._state_root)['dordiai']
            self._check_settings_scope()
            if current != self._configuration:
                self._reject('The current voice configuration changed; reload after explicit review.')

    def _check_settings_scope(self):
        for path, identity in self._state_directories:
            if _directory(path) != (path, identity):
                self._reject('The selected voice settings directory changed.')
        if self._store is not None:
            self._store.check()

    def _index(self):
        from ..audio.voice_packages import _index
        path = self.packages / 'voice-packages.json'
        try:
            value, checksum = _read_json_snapshot(path)
        except FileNotFoundError:
            active, inactive, checksum = {}, {}, None
        else:
            active, inactive = _index(value)
        if len(active) + len(inactive) > 1024:
            raise ValueError('The managed voice inventory exceeds its budget.')
        if self._manifest is not None and checksum is not None:
            relative = path.relative_to(self.runtime).as_posix()
            if dict(self._manifest.files).get(relative) != checksum:
                raise ValueError('The owner activation index differs from the reviewed manifest.')
        return active, inactive, checksum

    def _begin(self, cancelled, timeout):
        self._check_base()
        _cancel(cancelled)
        self._stack = ExitStack()
        if self._ticket is None:
            return
        self._lease = self._stack.enter_context(hold_voice_ownership(
            self._ticket, timeout=min(float(timeout), 60.0),
            cancelled=lambda: _requested(cancelled)))
        self._check_base()
        if self._binding is not None:
            manifest = LocalModelManifest.load(self._binding['manifest'], engine='dordiai-voices',
                                              expected_sha256=self._binding['sha256'])
            self._store._validate_runtime(manifest, self._configuration,
                                          cancelled=lambda: _requested(cancelled))
            from ..audio.voice_packages import runtime_directory, runtime_settings
            settings = runtime_settings(manifest.settings)
            backend = settings['reference_engine']
            if settings['models'] != 'models' or settings['references'] != 'models/voices':
                raise ValueError('The current speech bridge requires the reviewed models and reference directories.')
            model = runtime_directory(manifest, settings['reference_model'])
            if backend == 'holos':
                if settings['reference_model'] != 'models/Holos':
                    raise ValueError('The current Holos bridge requires its reviewed fixed model path.')
            elif backend == 'openvoice':
                # This owner branch receives the reviewed models directory;
                # ConvertedVoices resolves its OpenVoiceV2 and Piper assets there.
                # Its reference-model argument is retained exactly as configured.
                manifest.asset('converted_voices.py')
            elif backend == 'omnivoice':
                # ReferenceVoices loads this explicitly reviewed local entry,
                # then uses the selected package audio/transcript roles.
                manifest.asset('reference_voices.py')
                manifest.asset((model / 'use_onnx.py').relative_to(self.runtime).as_posix())
            else:
                raise ValueError('The selected reference backend has no reviewed speech protocol.')
            manifest.verify(cancelled=lambda: _requested(cancelled),
                            require_complete=True, ownership=self._lease)
            self._manifest = manifest
        active, inactive, self._index_digest = self._index()
        self._owned = set(active) | set(inactive)
        if self._manifest is not None:
            from ..audio.voice_packages import _document, _hash, _language, approved_profile
            settings = self._manifest.settings
            self._owned.update(settings.get('revoked_profiles', []))
            self._owned.update(settings.get('profile_reviews', {}))
            languages = settings.get('profile_languages', {})
            if type(languages) is not dict or len(languages) > 1024:
                raise ValueError('Invalid reviewed voice-language inventory.')
            for profile, version in active.items():
                _cancel(cancelled)
                path = self.packages / profile / version / 'voice-package.json'
                relative = path.relative_to(self.runtime).as_posix()
                raw, checksum = _read_json_snapshot(self._manifest.asset(relative),
                                                    cancelled=lambda: _requested(cancelled))
                if dict(self._manifest.files)[relative] != checksum:
                    raise ValueError('The active voice document changed after review.')
                document = _document(raw)
                if (document['id'], document['version']) != (profile, version):
                    raise ValueError('The active voice document has another identity.')
                if (document['engine']['id'] != runtime_settings(settings)['reference_engine']
                        or settings.get('managed_profiles') is not True
                        or not approved_profile(self._manifest, document)):
                    continue
                row = languages.get(profile, {})
                if (type(row) is not dict or set(row) != {'supported', 'candidates'}
                        or type(row['supported']) is not list or type(row['candidates']) is not list):
                    raise ValueError('Managed speech requires a reviewed per-profile language selection.')
                supported = tuple(_language(code) for code in row['supported'])
                candidates = tuple(_language(code) for code in row['candidates'])
                declared = {_language(code) for code in document['languages']}
                if (len(set(supported)) != len(supported)
                        or len(set(candidates)) != len(candidates)
                        or set(supported) & set(candidates)
                        or set(supported) | set(candidates) != declared):
                    raise ValueError('The reviewed voice languages differ from the active package.')
                references = {entry['role']: entry['sha256'] for entry in document['files'].values()}
                identity = (version, _hash(document), _hash(references),
                            sha256(_canonical(self._binding).encode('ascii')).hexdigest())
                self._profiles[profile] = {
                    'identity': identity, 'languages': supported, 'document': path,
                    'document_sha256': checksum,
                }
        self.checkpoint()

    def checkpoint(self):
        if self._depth <= 0 or self._thread != get_ident():
            raise ValueError('Voice authority requires this worker\'s active operation scope.')
        self._check_base()
        if self._lease is not None:
            self._lease.checkpoint()
            if self._index()[2] != self._index_digest:
                self._reject('The active voice package version changed during this operation.')
            if self._manifest is not None:
                if digest_file(self._manifest.path) != self._manifest.manifest_sha256:
                    self._reject('Managed voice approval changed during this operation.')
                for profile in self._selected:
                    row = self._profiles[profile]
                    if digest_file(row['document']) != row['document_sha256']:
                        self._reject('The selected managed voice document changed during this operation.')
            self._lease.checkpoint()
        self._check_base()

    def filter_inventory(self, value):
        self.checkpoint()
        result = dict(value)
        backend = self.runtime_configuration()['reference_engine']
        voices = []
        for voice in value['voices']:
            profile = voice['id']
            # Only an explicitly approved managed profile can select either
            # additional backend; raw legacy IDs continue to mean Holos.
            if backend != 'holos' and profile not in self._profiles:
                continue
            if profile in self._owned:
                approved = self._profiles.get(profile)
                if approved is None:
                    continue
                languages = [code for code in voice['languages'] if code in approved['languages']]
                if not languages:
                    continue
                voice = {**voice, 'languages': languages}
            voices.append(voice)
        result['voices'] = voices
        self.checkpoint()
        return result

    def runtime_configuration(self):
        """Return only the current operation's reviewed worker configuration."""
        self.checkpoint()
        if self._manifest is None:
            return {'reference_engine': 'holos', 'models': 'models',
                    'reference_model': 'models/Holos', 'references': 'models/voices'}
        from ..audio.voice_packages import runtime_settings
        return runtime_settings(self._manifest.settings)

    def profile_identity(self, profile, *, select=False):
        if not isinstance(profile, str):
            raise ValueError('Choose an explicit voice identity.')
        self.checkpoint()
        if profile in self._owned:
            row = self._profiles.get(profile)
            if row is None:
                raise ValueError('This managed voice lacks a current version-bound approval.')
            if select:
                self._selected.add(profile)
                self.checkpoint()
            return row['identity']
        return None

    def require_identity(self, profile, expected):
        if self.profile_identity(profile, select=True) != expected:
            self._reject('The reviewed voice version changed; select the current voice explicitly.')

    def capability_identity(self):
        self.checkpoint()
        if self._binding is None:
            return None, ()
        checksum = sha256(_canonical(self._binding).encode('ascii')).hexdigest()
        versions = tuple((profile, *row['identity'][:3])
                         for profile, row in sorted(self._profiles.items()))
        return checksum, versions

    def lock_digest(self, path):
        self.checkpoint()
        if self._lease is not None and Path(path) == self._lease.ticket.lock:
            checksum = self._lease.lock_sha256()
            self.checkpoint()
            return checksum
        return None

    def _retire(self):
        if self._adapter is None:
            return
        try:
            self._adapter._retire_authority_workers()
        except BaseException as original:
            from ..audio.cleanup import pending_cleanup
            from ..audio.models import AudioCleanupPending
            pending = (original if isinstance(original, AudioCleanupPending) else pending_cleanup(
                self._adapter._retire_authority_workers,
                message='The managed voice worker is still closing; package ownership is retained.'))
            try:
                settle_voice_cleanup(pending)
            except (KeyboardInterrupt, SystemExit) as deferred:
                raise deferred from original
            if isinstance(original, (KeyboardInterrupt, SystemExit)):
                raise original
            raise pending from original

    @contextmanager
    def operation(self, *, cancelled=None, timeout=60):
        """One call-local lease, shared only by nested calls on this worker."""
        if self._pid != os.getpid():
            raise ValueError('A voice authority cannot be used by another process.')
        _cancel(cancelled)
        thread = get_ident()
        with self._state:
            outer = self._depth == 0
            if not outer:
                if self._thread != thread:
                    raise AuthorityBusy('The selected voice provider already has an active operation.')
                self._depth += 1
            else:
                if not self._gate.acquire(blocking=False):
                    raise AuthorityBusy('The selected voice provider already has an active operation.')
                self._depth, self._thread = 1, thread
        try:
            if outer:
                self._begin(cancelled, timeout)
            self.checkpoint()
            _cancel(cancelled)
            yield self
            _cancel(cancelled)
            self.checkpoint()
            if outer and self._lease is not None:
                self._retire()
                self.checkpoint()
            if outer and self._manifest is not None:
                self._manifest.verify(cancelled=lambda: _requested(cancelled),
                                      require_complete=True, ownership=self._lease)
                self.checkpoint()
        finally:
            if outer:
                try:
                    if self._lease is not None or self._invalid:
                        self._retire()
                finally:
                    try:
                        if self._stack is not None:
                            self._stack.close()
                    finally:
                        self._stack = self._lease = self._manifest = None
                        self._index_digest = None
                        self._profiles, self._owned, self._selected = {}, set(), set()
                        with self._state:
                            self._depth, self._thread = 0, None
                        self._gate.release()
            else:
                with self._state:
                    self._depth -= 1
