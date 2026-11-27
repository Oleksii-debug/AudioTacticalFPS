"""Dordiai PCM bridge for the existing local speech pipeline.

Only an explicitly enabled declaration opens the separately installed runtime.
The shared local renderer continues to own WAV validation and publication.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from threading import Event, Lock
from time import monotonic
import wave

from ..integrations import dordiai_voices as bridge
from .dordiai_settings import default_settings, load_settings, validate_settings

_MAX_ASSETS = 100000
_MAX_ASSET_BYTES = 64 * 1024**3
_LANGUAGE = re.compile(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*')
_ACTIVE = {}
_ACTIVE_LOCK = Lock()


def configuration(root=None):
    if root is not None:
        return load_settings(root)
    enabled = os.environ.get('CARR_DORDIAI_ENABLED', '0')
    if enabled not in ('0', '1'):
        raise ValueError('CARR_DORDIAI_ENABLED must be 0 or 1.')
    result = default_settings()
    if enabled == '0':
        return result
    result.update(enabled=True, runtime_directory=os.environ.get('CARR_DORDIAI_RUNTIME'),
                  python_executable=os.environ.get('CARR_DORDIAI_PYTHON') or None,
                  expected_worker_sha256=os.environ.get('CARR_DORDIAI_WORKER_SHA256',
                                                       result['expected_worker_sha256']))
    return validate_settings(result)


def _scope_key(root):
    return str(Path(root).absolute()) if root is not None else None


@contextmanager
def _cancellation(root, cancelled):
    from .local import _check_cancel, RenderCancelled
    event = Event()
    key = _scope_key(root)
    with _ACTIVE_LOCK:
        _ACTIVE.setdefault(key, set()).add(event)

    def check():
        _check_cancel(cancelled)
        if event.is_set():
            raise RenderCancelled('Dordiai-voices settings changed; no partial speech was published.')
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
    """A native Disable action signals owned work without blocking the UI."""
    with _ACTIVE_LOCK:
        for event in tuple(_ACTIVE.get(_scope_key(root), ())):
            event.set()


def _adapter(settings, *, timeout=120):
    from .local import _speech_path, SpeechUnavailable
    settings = validate_settings(settings)
    if not settings['enabled']:
        raise bridge.DordiaiUnavailable('Dordiai-voices is disabled. Connect an installed reviewed worker first.')
    root = _speech_path(settings['runtime_directory'])
    if not root.is_absolute():
        raise SpeechUnavailable('Select an absolute runtime path for this operating system.')
    executable = settings['python_executable']
    if executable is not None:
        executable = _speech_path(executable)
        if not executable.is_absolute():
            raise SpeechUnavailable('Select an absolute private interpreter path for this operating system.')
    return bridge.DordiaiVoicesAdapter(root, python_executable=executable,
        expected_worker_sha256=settings['expected_worker_sha256'],
        request_timeout=min(float(timeout), 1800.0))


def validate_connection(settings):
    """Check selected paths and worker bytes; never start the private process."""
    with _adapter(settings):
        pass
    return validate_settings(settings)


def entries(*, configuration_root=None, cancelled=None):
    from .local import Voice, _Entry, SpeechUnavailable, RenderCancelled
    try:
        settings = configuration(configuration_root)
        if not settings['enabled']:
            return ()
        with _cancellation(configuration_root, cancelled) as check, _adapter(settings) as adapter:
            result = []
            for item in adapter.voices(cancelled=check):
                check()
                if (not 0 < len(item.id) <= 4096 or not 0 < len(item.name) <= 512
                        or any(ord(char) < 32 for char in item.id + item.name)):
                    raise bridge.DordiaiProtocolError('Unsupported dordiai-voices inventory label.')
                for language in item.languages:
                    if _LANGUAGE.fullmatch(language) is None:
                        raise bridge.DordiaiProtocolError('Unsupported dordiai-voices language tag.')
                    token = json.dumps([item.id, language], ensure_ascii=True, separators=(',', ':'))
                    identity = 'dordiai-voices:' + sha256(token.encode('ascii')).hexdigest()
                    voice = Voice(identity, 'dordiai-voices', item.name + ' (' + language + ')', language,
                                  ('local', 'wav', 'rate', 'volume', 'pitch-relative', 'neutral-style'))
                    result.append(_Entry(voice, adapter.python, item.id,
                        dict(configuration=settings, configuration_root=str(configuration_root)
                             if configuration_root is not None else None,
                             language=language, worker_sha256=adapter.expected_worker_sha256),
                        adapter.runtime))
                    if len(result) > 4096:
                        raise bridge.DordiaiProtocolError('Dordiai-voices inventory exceeds its voice budget.')
            check()
            return tuple(result)
    except bridge.DordiaiCancelled:
        raise RenderCancelled('Local voice discovery was cancelled.') from None
    except (bridge.DordiaiError, OSError, ValueError) as exc:
        raise SpeechUnavailable('Dordiai-voices is unavailable: ' + str(exc)) from None


def _unchanged_declaration(entry):
    if configuration(entry.identity['configuration_root']) != entry.identity['configuration']:
        from .local import SpeechUnavailable
        raise SpeechUnavailable('Dordiai-voices configuration changed. Reload the installed voices.')


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _plain(path, *, directory=False):
    from .local import SpeechUnavailable
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise SpeechUnavailable('Dordiai-voices identity requires plain, unlinked installed assets.')
    return info


def fingerprint(entry, *, cancelled=None):
    """Hash exact worker, model, interpreter and complete CARR adapter sources.

    No stat-only memoization is used: same-size/coarse-timestamp model rewrites
    cannot reuse stale speech. The private tree has explicit file/byte budgets.
    """
    from . import local, dordiai_settings
    from ..studio import models as studio_models
    from ..audio import ffmpeg as audio_ffmpeg
    from .local import SpeechUnavailable, SpeechError
    root = entry.data_path
    check_root = entry.identity['configuration_root']
    with _cancellation(check_root, cancelled) as check:
        _unchanged_declaration(entry)
        _plain(root, directory=True)
        paths = {entry.executable.resolve(): 'interpreter'}
        dependencies = [Path(__file__), Path(local.__file__), Path(dordiai_settings.__file__),
                        Path(studio_models.__file__), Path(audio_ffmpeg.__file__),
                        Path(bridge.__file__), Path(bridge.__file__).with_name('_dordiai_probe.py')]
        for path in dependencies:
            paths[path.resolve()] = 'adapter/' + path.name
        manifest = root.parent / 'manifest.ini'
        if manifest.exists():
            paths[manifest] = 'runtime-manifest'
        root_entries = 0
        for path in root.iterdir():
            check()
            root_entries += 1
            if root_entries > _MAX_ASSETS:
                raise SpeechUnavailable('Dordiai-voices exceeds its asset inventory budget.')
            if path.is_file() and path.suffix in {'.py', '.json', '.ini', '.toml', '.dll', '.pyd', '.exe', '.so', '.dylib'}:
                paths[path] = 'runtime/' + path.name
        directories = []
        for name in ('models', 'python'):
            folder = root / name
            if not folder.exists():
                continue
            pending = [folder]
            while pending:
                check()
                current = pending.pop()
                before = _plain(current, directory=True)
                directories.append((current, _stamp(before)))
                for path in current.iterdir():
                    check()
                    info = path.lstat()
                    _plain(path, directory=stat.S_ISDIR(info.st_mode))
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    else:
                        paths[path] = 'runtime/' + path.relative_to(root).as_posix()
                    if len(paths) + len(directories) + len(pending) > _MAX_ASSETS:
                        raise SpeechUnavailable('Dordiai-voices exceeds its asset inventory budget.')
        identity, total = sha256(), 0
        for path, label in sorted(paths.items(), key=lambda item: item[1]):
            check()
            before = _plain(path)
            total += before.st_size
            if total > _MAX_ASSET_BYTES:
                raise SpeechUnavailable('Dordiai-voices exceeds its asset fingerprint byte budget.')
            digest, size = sha256(), 0
            with path.open('rb') as stream:
                if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                    raise SpeechError('Dordiai-voices assets changed while opening.')
                while block := stream.read(1024 * 1024):
                    check()
                    size += len(block)
                    if size > before.st_size:
                        raise SpeechError('Dordiai-voices assets grew while fingerprinting.')
                    digest.update(block)
            if size != before.st_size or _stamp(_plain(path)) != _stamp(before):
                raise SpeechError('Dordiai-voices assets changed while fingerprinting.')
            identity.update(label.encode('utf-8') + b'\0' + digest.digest())
        for folder, before in directories:
            if _stamp(_plain(folder, directory=True)) != before:
                raise SpeechError('Dordiai-voices asset inventory changed while fingerprinting.')
        _unchanged_declaration(entry)
        check()
        identity.update(json.dumps(dict(adapter=bridge.ADAPTER_API_VERSION,
            worker=entry.identity['worker_sha256'], voice=entry.native_id,
            language=entry.identity['language']), sort_keys=True).encode('utf-8'))
        return identity.hexdigest()


def write_pcm(entry, request, stage, *, cancelled=None, timeout_seconds=600, max_output_bytes=None):
    """Write only the shared renderer's private stage; publication stays local.py."""
    from .local import SpeechError, SpeechBudgetExceeded, RenderCancelled, _MAX_WAV_BYTES
    from ..studio.models import split_text
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0):
        raise ValueError('Speech timeout must be a positive finite number.')
    limit = min(_MAX_WAV_BYTES, max_output_bytes) if max_output_bytes is not None else _MAX_WAV_BYTES
    if limit <= 44:
        raise SpeechBudgetExceeded('The speech chunk exceeds its output byte budget.')
    deadline = monotonic() + timeout_seconds
    root = entry.identity['configuration_root']
    with _cancellation(root, cancelled) as cancelled_check:
        def check():
            cancelled_check()
            if monotonic() >= deadline:
                raise SpeechError('Dordiai-voices exceeded the speech execution time budget.')
            return False

        _unchanged_declaration(entry)
        try:
            with _adapter(entry.identity['configuration'], timeout=timeout_seconds) as adapter:
                if adapter.runtime != entry.data_path or adapter.python != entry.executable:
                    raise SpeechError('The selected dordiai-voices runtime path changed.')
                frames = 0
                with wave.open(str(stage), 'wb') as audio:
                    audio.setparams((1, 2, bridge.SAMPLE_RATE, 0, 'NONE', 'PCM'))
                    for _, _, text in split_text(request.text, bridge.MAX_TEXT_CHARACTERS):
                        check()
                        if not text.strip():
                            continue
                        stream = adapter.stream_pcm(text, entry.native_id, entry.identity['language'],
                            speed=request.rate, pitch=round(50 + request.pitch * 50),
                            volume=round(request.volume * 100), cancelled=check)
                        try:
                            for block in stream:
                                check()
                                if ((block.sample_rate, block.channels, block.sample_width) != (bridge.SAMPLE_RATE, 1, 2)
                                        or type(block.data) is not bytes or not block.data or len(block.data) % 2):
                                    raise SpeechError('Dordiai-voices returned unsupported PCM.')
                                frames += block.frames
                                if frames * 2 + 44 > limit:
                                    raise SpeechBudgetExceeded('The speech chunk exceeded its output byte budget.')
                                audio.writeframesraw(block.data)
                        finally:
                            stream.close()
                if not frames:
                    raise SpeechError('Dordiai-voices produced no audio.')
                check()
                _unchanged_declaration(entry)
        except bridge.DordiaiCancelled:
            raise RenderCancelled('Dordiai-voices rendering was cancelled; no output was published.') from None
        except bridge.DordiaiTimeout:
            raise SpeechError('Dordiai-voices exceeded the speech execution time budget.') from None
        except bridge.DordiaiError as exc:
            raise SpeechError('Dordiai-voices rendering failed: ' + str(exc)) from None
