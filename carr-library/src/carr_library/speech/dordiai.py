"""Explicit public dordiai bridge for the canonical installed-voice registry.

The separately installed worker supplies PCM. VoiceRegistry alone validates,
joins and publishes the final WAV; this module is not a second speech engine.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from threading import Event, Lock
from time import monotonic
import wave

from ..integrations import dordiai_voices as bridge
from ._io import check_cancelled
from .dordiai_settings import default_settings, load_settings, validate_settings
from .runtime import Voice

_MAX_ASSETS = 100000
_MAX_ASSET_BYTES = 64 * 1024**3
_LANGUAGE = re.compile(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*')
_ACTIVE = {}
_ACTIVE_LOCK = Lock()


def configuration(root=None):
    """Library declarations are explicit; ambient enable flags are not consulted."""
    return load_settings(root) if root is not None else default_settings()


def _scope_key(root):
    return str(Path(root).absolute()) if root is not None else None


@contextmanager
def _cancellation(root, cancelled=None):
    callback = cancelled if cancelled is not None else lambda: False
    event = Event()
    key = _scope_key(root)
    with _ACTIVE_LOCK:
        _ACTIVE.setdefault(key, set()).add(event)

    def check():
        check_cancelled(callback)
        if event.is_set():
            raise InterruptedError('Dordiai-voices settings changed; no partial speech was published.')
        return False

    try:
        check()
        yield check
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE[key].discard(event)
            if not _ACTIVE[key]:
                del _ACTIVE[key]


def cancel_active(root=None):
    """Signal only operations belonging to the selected library."""
    with _ACTIVE_LOCK:
        for event in tuple(_ACTIVE.get(_scope_key(root), ())):
            event.set()


def _adapter(settings, *, timeout=120):
    settings = validate_settings(settings)
    if not settings['enabled']:
        raise bridge.DordiaiUnavailable('Dordiai-voices is disabled. Connect a reviewed installed worker first.')
    root = Path(settings['runtime_directory'])
    if not root.is_absolute():
        raise ValueError('Select an absolute runtime path for this operating system.')
    executable = settings['python_executable']
    if executable is not None and not Path(executable).is_absolute():
        raise ValueError('Select an absolute private interpreter path for this operating system.')
    return bridge.DordiaiVoicesAdapter(root, python_executable=executable,
        expected_worker_sha256=settings['expected_worker_sha256'],
        request_timeout=min(float(timeout), 1800.0))


def validate_connection(settings):
    """Check local paths and reviewed worker bytes without starting a process."""
    with _adapter(settings):
        pass
    return validate_settings(settings)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _plain(path, *, directory=False):
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Dordiai-voices identity requires plain, unlinked installed assets.')
    return info


class DordiaiProvider:
    """A default-off provider with bounded discovery and exact asset identity."""
    name = 'Opt-in dordiai-voices'

    def __init__(self, configuration_root):
        self.configuration_root = Path(configuration_root).absolute()
        self.settings = configuration(self.configuration_root)
        with _adapter(self.settings) as adapter:
            self.runtime = adapter.runtime
            self.executable = adapter.python
        self._tokens = {}

    def _unchanged(self):
        if configuration(self.configuration_root) != self.settings:
            raise ValueError('Dordiai-voices configuration changed. Reload the installed voices.')

    def operation(self, *, cancelled=lambda: False):
        """Keep Disable effective across hashing, rendering and publication."""
        return _cancellation(self.configuration_root, cancelled)

    def discover_voices(self, *, cancelled=lambda: False):
        return self.voices(cancelled=cancelled)

    def voices(self, *, cancelled=lambda: False):
        with _cancellation(self.configuration_root, cancelled) as check:
            self._unchanged()
            try:
                with _adapter(self.settings) as adapter:
                    voices, tokens = [], {}
                    for item in adapter.voices(cancelled=check):
                        check()
                        if (not 0 < len(item.id) <= 4096 or not 0 < len(item.name) <= 512
                                or any(ord(char) < 32 for char in item.id + item.name)):
                            raise bridge.DordiaiProtocolError('Unsupported dordiai-voices inventory label.')
                        for language in item.languages:
                            if _LANGUAGE.fullmatch(language) is None:
                                raise bridge.DordiaiProtocolError('Unsupported dordiai-voices language tag.')
                            token = json.dumps([item.id, language], ensure_ascii=True, separators=(',', ':'))
                            identifier = 'dordiai-voices:' + sha256(token.encode('ascii')).hexdigest()
                            if identifier in tokens:
                                raise bridge.DordiaiProtocolError('Duplicate dordiai-voices identity.')
                            voices.append(Voice(identifier, item.name + ' (' + language + ')',
                                'dordiai-voices', language, ('rate', 'volume', 'pitch'),
                                vendor='Separately installed dordiai-voices',
                                license='The separately installed worker, voice and model licenses apply.',
                                timing='Neutral delivery; measured utterance duration; word timings unavailable.'))
                            tokens[identifier] = (item.id, language)
                            if len(voices) > 4096:
                                raise bridge.DordiaiProtocolError('Dordiai-voices inventory exceeds its voice budget.')
                    check()
                    self._unchanged()
                    self._tokens = tokens
                    return tuple(voices)
            except bridge.DordiaiCancelled:
                raise InterruptedError('Dordiai-voices discovery was cancelled.') from None

    def fingerprint(self, voice, *, cancelled=lambda: False):
        """Hash adapter sources and every declared private worker/model asset.

        The external interpreter's global package environment is not represented
        as sealed. A runtime-local python tree, when supplied, is fully hashed.
        """
        from . import runtime, _io, dordiai_settings
        from .. import durable_state
        from ..ingest import writer_guard
        with _cancellation(self.configuration_root, cancelled) as check:
            self._unchanged()
            if voice.id not in self._tokens:
                raise ValueError('Reload the selected dordiai-voices profile.')
            root = self.runtime
            directories = [(root, _stamp(_plain(root, directory=True)))]
            paths = {self.executable: 'interpreter'}
            modules = (Path(__file__), Path(runtime.__file__), Path(_io.__file__),
                       Path(dordiai_settings.__file__), Path(durable_state.__file__),
                       Path(writer_guard.__file__), Path(bridge.__file__),
                       Path(bridge.__file__).with_name('_dordiai_probe.py'))
            package = Path(__file__).parent.parent
            for path in modules:
                paths[path.resolve()] = 'adapter/' + path.relative_to(package).as_posix()
            manifest = root.parent / 'manifest.ini'
            if manifest.exists():
                paths[manifest] = 'runtime-manifest'
            root_entries = 0
            for path in root.iterdir():
                check()
                root_entries += 1
                if root_entries > _MAX_ASSETS:
                    raise ValueError('Dordiai-voices exceeds its asset inventory budget.')
                if path.is_file() and path.suffix in {'.py', '.json', '.ini', '.toml', '.dll', '.pyd', '.exe', '.so', '.dylib'}:
                    paths[path] = 'runtime/' + path.name
            pending = [root / name for name in ('models', 'python') if (root / name).exists()]
            while pending:
                check()
                current = pending.pop()
                directories.append((current, _stamp(_plain(current, directory=True))))
                for path in current.iterdir():
                    check()
                    info = path.lstat()
                    _plain(path, directory=stat.S_ISDIR(info.st_mode))
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    else:
                        paths[path] = 'runtime/' + path.relative_to(root).as_posix()
                    if len(paths) + len(directories) + len(pending) > _MAX_ASSETS:
                        raise ValueError('Dordiai-voices exceeds its asset inventory budget.')
            identity, total = sha256(), 0
            for path, label in sorted(paths.items(), key=lambda item: item[1]):
                check()
                before = _plain(path)
                total += before.st_size
                if total > _MAX_ASSET_BYTES:
                    raise ValueError('Dordiai-voices exceeds its asset fingerprint byte budget.')
                digest, size = sha256(), 0
                with path.open('rb') as stream:
                    if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                        raise ValueError('Dordiai-voices assets changed while opening.')
                    while block := stream.read(1024 * 1024):
                        check()
                        size += len(block)
                        if size > before.st_size:
                            raise ValueError('Dordiai-voices assets grew while fingerprinting.')
                        digest.update(block)
                if size != before.st_size or _stamp(_plain(path)) != _stamp(before):
                    raise ValueError('Dordiai-voices assets changed while fingerprinting.')
                identity.update(label.encode('utf-8') + b'\0' + digest.digest())
            for folder, before in directories:
                if _stamp(_plain(folder, directory=True)) != before:
                    raise ValueError('Dordiai-voices asset inventory changed while fingerprinting.')
            self._unchanged()
            check()
            identity.update(json.dumps(dict(adapter=bridge.ADAPTER_API_VERSION,
                worker=self.settings['expected_worker_sha256'],
                voice=self._tokens[voice.id]), sort_keys=True).encode('utf-8'))
            return identity.hexdigest()

    def render(self, text, voice, output, settings, cancelled):
        """Produce only the registry's private utterance stage, never a final file."""
        deadline = monotonic() + settings.timeout_seconds
        with _cancellation(self.configuration_root, cancelled) as cancelled_check:
            def check():
                cancelled_check()
                if monotonic() >= deadline:
                    raise TimeoutError('Dordiai-voices exceeded the speech execution time budget.')
                return False

            self._unchanged()
            if voice.id not in self._tokens:
                raise ValueError('Reload the selected dordiai-voices profile.')
            profile, language = self._tokens[voice.id]
            try:
                with _adapter(self.settings, timeout=settings.timeout_seconds) as adapter:
                    if adapter.runtime != self.runtime or adapter.python != self.executable:
                        raise ValueError('The selected dordiai-voices runtime path changed.')
                    frames = 0
                    with wave.open(str(output), 'wb') as audio:
                        audio.setparams((1, 2, bridge.SAMPLE_RATE, 0, 'NONE', 'PCM'))
                        stream = adapter.stream_pcm(text, profile, language,
                            speed=2 ** (settings.rate / 10),
                            pitch=50 if settings.pitch is None else settings.pitch,
                            volume=settings.volume, cancelled=check)
                        try:
                            for block in stream:
                                check()
                                if ((block.sample_rate, block.channels, block.sample_width) != (bridge.SAMPLE_RATE, 1, 2)
                                        or type(block.data) is not bytes or not block.data or len(block.data) % 2):
                                    raise ValueError('Dordiai-voices returned unsupported PCM.')
                                frames += block.frames
                                if frames * 2 + 44 > settings.max_output_bytes:
                                    raise ValueError('Dordiai-voices exceeded its audio-output budget.')
                                audio.writeframesraw(block.data)
                        finally:
                            stream.close()
                    if not frames:
                        raise ValueError('Dordiai-voices produced no audio.')
                    check()
                    self._unchanged()
            except bridge.DordiaiCancelled:
                raise InterruptedError('Dordiai-voices rendering was cancelled; no output was published.') from None
            except bridge.DordiaiTimeout:
                raise TimeoutError('Dordiai-voices exceeded the speech execution time budget.') from None
