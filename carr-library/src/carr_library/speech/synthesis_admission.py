"""Reader-only cooperative admission over the existing speech and resource owners.

The selected provider object and its registered route decide whether synthesis
is basic. Display names and voice IDs never grant an exemption. Heavy routes
reserve the configured CPU/RAM allowance and the reader's bounded output-volume
footprint. These are cooperative reservations, not measured consumption, OS RAM
limits or GPU admission: current reader adapters declare no GPU device/budget.
Studio production already owns its admission and does not call this helper.
"""
from dataclasses import dataclass
from pathlib import Path
import stat
import threading
from time import monotonic
from uuid import uuid4

from ._io import check_cancelled, local_file
from .runtime import (EspeakProvider, FliteProvider, NativeSapiProvider,
                      SapiProvider, PiperProvider, LocalHelperProvider,
                      SpeechSettings, VoiceRegistry)
from ..audio.cleanup import _pending_owners, retained_context
from ..audio.voice_manifest import local_path as local_asset
from ..durable_state import local_path, regular
from ..operations.broker import ResourceBroker
from ..operations.models import ResourceRequest, ResourceUnavailable


# The combined WAV and one current utterance can coexist. The additional
# allowance covers the bounded request/stdout/stderr protocol files, not private
# model caches or a claim of a hard filesystem quota.
PROTOCOL_SCRATCH_BYTES = 16 * 1024 * 1024
OWNER_CHECK_SECONDS = 5.0
MAX_IDENTITY_FILES = 16


@dataclass(frozen=True)
class _Route:
    provider: object
    detail: tuple
    basic: bool


def _route(registry, voice_id):
    registry.get(voice_id)
    provider = registry._owners[voice_id]
    if type(provider) in (NativeSapiProvider, SapiProvider, EspeakProvider, FliteProvider):
        return _Route(provider, (), True)
    from .studio_voice_adapter import StudioRegistry, StudioVoiceProvider
    if type(provider) is StudioVoiceProvider:
        native = provider.registry
        if type(native) is StudioRegistry:
            source, route = provider._voices.get(voice_id), native._routes.get(voice_id)
            if source is None or not isinstance(route, tuple) or len(route) != 3:
                raise ValueError('The registered voice route changed. Reload voices.')
            model = native._model_engines.get(route[0])
            detail = (id(native), source, route, id(model))
            return _Route(provider, detail,
                          route[0] in ('sapi5', 'flite', 'espeak-ng') and model is None)
        return _Route(provider, (id(native),), False)
    return _Route(provider, (), False)


def _identity_stamps(route, voice_id, cancelled):
    """Bounded metadata capture only; content verification happens under lease."""
    provider, paths = route.provider, []
    executable = getattr(provider, 'executable', None)
    if executable is not None:
        paths.append(executable)
    if isinstance(provider, PiperProvider):
        paths.extend(provider._models[voice_id][:2])
    if isinstance(provider, LocalHelperProvider) and provider.script is not None:
        paths.append(provider.script)
    from .studio_voice_adapter import StudioRegistry, StudioVoiceProvider
    if type(provider) is StudioVoiceProvider and type(provider.registry) is StudioRegistry:
        native = provider.registry
        if len(native._routes) > 10000 or len(native._model_engines) > 2:
            raise ValueError('The registered voice identity inventory exceeds its bounded contract.')
        paths.append(provider.store.path)
        paths.extend(route[1] for route in native._routes.values())
        for model in native._model_engines.values():
            check_cancelled(cancelled)
            # Complete asset hashes are already pinned by this loaded manifest.
            # Its canonical verification stays inside the reservation; do not
            # walk or hash thousands of model files merely to join the queue.
            paths.extend((model.manifest.path, model.binary))
    # Unknown trusted extension callbacks have no invented asset declaration.
    # Their actual provider object and fresh callback digest remain bound, but
    # their callback internals cannot be forcibly interrupted by this adapter.
    paths = tuple(sorted({str(Path(path)) for path in paths}))
    if len(paths) > MAX_IDENTITY_FILES:
        raise ValueError('The local voice identity inventory exceeds its bound.')
    stamps = []
    for value in paths:
        check_cancelled(cancelled)
        path = local_asset(value)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('The selected voice identity must use regular local files.')
        stamps.append((str(path), info.st_dev, info.st_ino, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns))
    check_cancelled(cancelled)
    return tuple(stamps)


def _directory(path):
    path = local_path(path, exists=True)
    info = regular(path, directory=True)
    return path, (info.st_dev, info.st_ino)


class ReaderSynthesisRequest:
    """One captured reader request; no executor, renderer or persistent store."""

    def __init__(self, registry, voice, settings, *, root, cancelled, dispatch, verify):
        if not isinstance(registry, VoiceRegistry) or not isinstance(settings, SpeechSettings):
            raise TypeError('Use the current speech registry and validated native voice settings.')
        if not all(callable(value) for value in (cancelled, dispatch, verify)):
            raise TypeError('Reader cancellation, owner dispatch and source verification are required.')
        if registry.get(voice.id) != voice:
            raise ValueError('The selected voice changed. Reload voices.')
        self.registry, self.voice, self.settings = registry, voice, settings
        self.root = Path(root) if root is not None else None
        self.route = _route(registry, voice.id)
        self._cancelled, self._dispatch, self._verify = cancelled, dispatch, verify
        self._owner_thread = threading.get_ident()
        self._retired, self._lock = threading.Event(), threading.Lock()
        self._started, self._owners = False, ()

    @property
    def basic(self):
        return self.route.basic

    @property
    def finished(self):
        return self._retired.is_set()

    @property
    def cleanup_owners(self):
        with self._lock:
            return self._owners

    def cancelled(self):
        return self._retired.is_set() or bool(self._cancelled())

    def _check_route(self):
        check_cancelled(self.cancelled)
        current = _route(self.registry, self.voice.id)
        if (self.registry.get(self.voice.id) != self.voice
                or current.provider is not self.route.provider
                or current.detail != self.route.detail or current.basic != self.route.basic):
            raise ValueError('The selected voice provider changed. Reload voices.')

    def _owner_check(self, *, rendering):
        # Each dispatch has its own revocation event. A late queued callback
        # cannot become live again when the reader clears its global Cancel.
        done, active, errors = threading.Event(), threading.Event(), []
        active.set()

        def verify():
            if not active.is_set() or self.cancelled():
                done.set()
                return
            try:
                if threading.get_ident() != self._owner_thread:
                    raise RuntimeError('Reader verification must run on its window owner thread.')
                check_cancelled(self.cancelled)
                self._verify(rendering=rendering)
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        deadline = monotonic() + OWNER_CHECK_SECONDS
        try:
            self._dispatch(verify)
            while not done.wait(min(.025, max(0, deadline - monotonic()))):
                check_cancelled(self.cancelled)
                if monotonic() >= deadline:
                    raise TimeoutError('The reader could not confirm the current request. Try Read again.')
            check_cancelled(self.cancelled)
            if errors:
                raise errors[0]
        finally:
            active.clear()

    def _invoke(self, content, output, cancelled):
        self._check_route()
        self._owner_check(rendering=True)
        check_cancelled(cancelled)
        self._check_route()
        return self.registry.synthesize(content, self.voice.id, output,
                                        settings=self.settings, cancelled=cancelled)

    def run(self, content, destination, *, admission_timeout=60):
        with self._lock:
            if self._started:
                raise RuntimeError('This reader synthesis request has already started.')
            self._started = True
        try:
            self._check_route()
            if self.basic:
                # Ordinary native voices do not wait behind model reservations.
                return self._invoke(content, destination, self.cancelled)
            if self.root is None:
                raise ValueError('Open this local voice from the current library before synthesizing.')
            root = _directory(self.root)
            output = local_file(destination, must_exist=False)
            parent = _directory(output.parent)
            stamps = _identity_stamps(self.route, self.voice.id, self.cancelled)
            self._check_route()
            broker = ResourceBroker.for_app(root[0])
            limits = broker.limits
            if limits.memory_bytes <= 0:
                raise ResourceUnavailable('Set a positive local speech memory allowance before using this voice.')
            request = ResourceRequest(cpu_slots=limits.cpu_slots, memory_bytes=limits.memory_bytes,
                disk_bytes=2 * self.settings.max_output_bytes + PROTOCOL_SCRATCH_BYTES,
                disk_path=parent[0])
            with retained_context(broker.reserve(request,
                    operation_id='reader.synthesis.' + uuid4().hex,
                    cancelled=self.cancelled, timeout=admission_timeout)) as lease:
                lease.checkpoint()
                if _directory(self.root) != root or _directory(output.parent) != parent:
                    raise ValueError('The library or output folder changed while waiting for speech resources.')
                checked = local_file(output, must_exist=False)
                if checked != output:
                    raise ValueError('The selected speech output changed while waiting for resources.')
                self._check_route()
                if _identity_stamps(self.route, self.voice.id, lease.cancelled) != stamps:
                    raise ValueError('The selected voice files changed while waiting. Reload voices.')
                fingerprint = self.registry.fingerprint(self.voice.id, cancelled=lease.cancelled)
                self._check_route()
                self._owner_check(rendering=False)
                if self.registry.fingerprint(self.voice.id, cancelled=lease.cancelled) != fingerprint:
                    raise ValueError('The selected voice runtime changed during confirmation. Reload voices.')
                if (_identity_stamps(self.route, self.voice.id, lease.cancelled) != stamps
                        or _directory(self.root) != root or _directory(output.parent) != parent):
                    raise ValueError('The selected speech files or folders changed during confirmation.')
                lease.checkpoint()
                return self._invoke(content, output, lease.cancelled)
        except BaseException as error:
            with self._lock:
                self._owners = _pending_owners(error)
            raise
        finally:
            self._retired.set()


def defer_synthesis_files(requests, cleanup):
    """Release outer reader files only after every causal native owner is quiet.

    The callback is file-only. Its closure retains the exact outer directory;
    it must not call wx or discard a player whose Close remains unconfirmed.
    No retry thread or substitute native owner is created.
    """
    owners = {id(owner): owner for request in requests for owner in request.cleanup_owners}
    remaining, guard = set(owners), threading.Lock()
    if not remaining:
        cleanup()
        return

    def completed(identity):
        with guard:
            if identity not in remaining:
                return
            remaining.remove(identity)
            ready = not remaining
        if ready:
            cleanup()

    for identity, owner in owners.items():
        owner.add_quiescent_callback(lambda identity=identity: completed(identity))
