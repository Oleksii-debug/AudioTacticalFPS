"""Explicit current-profile adapter for the canonical local JSON voice contract.

No executable is discovered or run at import/registration. Both supported launch
forms use the current engine process owner; replies never choose arguments,
environment, working directory or output paths. Private native code remains an
explicitly reviewed installation, not an operating-system sandbox.
"""
from contextlib import contextmanager, ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
from tempfile import gettempdir, mkdtemp
import time

from . import engines
from .jobs import checked_directory, file_identity, regular_file, verified_reader
from .model_manifest import LocalModelManifest, ModelManifestError, offline_environment

ENGINE = 'local-voice-helper'
PROTOCOL = 'carr.local-voice.v1'
ADAPTER = 'carr.local-voice/current-1'
MAX_REQUEST_BYTES = 128_000
MAX_VOICES = 10000
_CONTROLS = frozenset(('rate', 'volume', 'pitch', 'punctuation', 'phonemes'))
_ACTIVE_CONTROLS = frozenset(('rate', 'volume', 'pitch', 'punctuation'))


def _text(value, label, maximum):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)):
        raise ModelManifestError('Invalid local voice-helper ' + label + '.')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ModelManifestError('Invalid local voice-helper Unicode.') from None
    return value


def _json(raw):
    if type(raw) is not bytes or len(raw) > engines.MAX_LOG_BYTES:
        raise engines.EngineUnavailable('The local voice-helper reply exceeds its byte budget.')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate helper field.')
            value[key] = item
        return value
    def nonfinite(_):
        raise ValueError('Nonfinite helper number.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=nonfinite)
    except (UnicodeError, ValueError, RecursionError):
        raise engines.EngineUnavailable('The local voice helper returned invalid bounded JSON.') from None
    if type(value) is not dict or value.get('schema') != PROTOCOL:
        raise engines.EngineUnavailable('The local voice helper returned an unsupported protocol.')
    return value


def _regular(path):
    info = regular_file(path)
    if info.st_nlink != 1:
        raise ModelManifestError('A selected helper launch file must not have hard-link aliases.')
    return info



def _directory_identity(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('The owned local helper directory is not an ordinary directory.')
    checked_directory(path)
    return info.st_dev, info.st_ino


def _remove_private_directory(path, *, identity, parent_identity):
    """Settle only this directory after the current process/reader owner exits."""
    deadline = time.monotonic() + 5
    while True:
        if _directory_identity(path.parent) != parent_identity:
            raise ValueError('The local helper temporary parent changed before cleanup.')
        try:
            current = _directory_identity(path)
        except FileNotFoundError:
            # A completed removal is harmless only under the same retained parent.
            if _directory_identity(path.parent) != parent_identity:
                raise ValueError('The local helper temporary parent changed during cleanup.')
            return
        if current != identity:
            raise ValueError('The owned local helper temporary directory changed before cleanup.')
        try:
            shutil.rmtree(path)
        except OSError as exc:
            # This is filesystem cleanup after engines._run has settled its
            # exact process family and streams, and _lease has closed its
            # selected input readers. It does not extend inference time.
            if (os.name != 'nt' or getattr(exc, 'winerror', None) not in (32, 33)
                    or time.monotonic() >= deadline):
                raise
            time.sleep(min(.02, max(0, deadline - time.monotonic())))
        else:
            return


@contextmanager
def _private_directory(*, prefix, dir=None):
    """Retain an exact private directory without an unsafe later finalizer."""
    parent = Path(gettempdir() if dir is None else dir).absolute()
    parent_identity = _directory_identity(parent)
    path = Path(mkdtemp(prefix=prefix, dir=parent))
    identity = _directory_identity(path)
    try:
        if _directory_identity(parent) != parent_identity:
            raise ValueError('The local helper temporary parent changed during creation.')
        yield path
    finally:
        _remove_private_directory(path, identity=identity, parent_identity=parent_identity)


class LocalVoiceHelperEngine:
    """Manifest-admitted native or fixed isolated-Python JSON v1 helper."""
    id = 'helper'

    def __init__(self, manifest, *, timeout=None):
        self.manifest = LocalModelManifest.load(manifest, engine=ENGINE)
        settings = self.manifest.settings
        required = {'protocol', 'launch', 'executable', 'engine_name'}
        optional = {'max_voices', 'timeout_seconds'}
        if (type(settings.get('launch')) is not str
                or settings['launch'] not in ('native', 'python')
                or settings.get('protocol') != PROTOCOL):
            raise ModelManifestError('Choose the fixed native or isolated-Python local voice protocol.')
        if settings['launch'] == 'python':
            required |= {'script'}
        if set(settings) - required - optional or not required <= set(settings):
            raise ModelManifestError('Unsupported local voice-helper settings.')
        self.launch = settings['launch']
        self.binary = self.manifest.asset(settings['executable'])
        self.script = self.manifest.asset(settings['script']) if self.launch == 'python' else None
        if self.script is not None and self.script.suffix.lower() != '.py':
            raise ModelManifestError('The fixed Python helper requires an inventory-covered .py script.')
        if self.binary.suffix.lower() in ('.bat', '.cmd', '.ps1') or (os.name == 'nt' and self.binary.suffix.lower() != '.exe'):
            raise ModelManifestError('Choose a native executable; command-shell launchers are not accepted.')
        self.engine_name = _text(settings['engine_name'], 'engine name', 128)
        self.max_voices = settings.get('max_voices', 1024)
        if type(self.max_voices) is not int or not 1 <= self.max_voices <= MAX_VOICES:
            raise ModelManifestError('Choose a local voice-helper catalogue limit from 1 to 10000.')
        self.timeout = engines._timeout(settings.get('timeout_seconds', 180) if timeout is None else timeout)
        if self.timeout > 86400:
            raise ModelManifestError('The local voice-helper timeout must not exceed one day.')
        for path in (self.binary,) + ((self.script,) if self.script is not None else ()):
            _regular(path)
        if not self.manifest.trusted_weights:
            raise ModelManifestError('Explicitly review the local helper code before enabling it.')
        self._records = {}

    @contextmanager
    def _lease(self, cancelled):
        """Keep the selected launch bytes open while the existing owner settles."""
        engines._cancelled(cancelled)
        self.manifest.verify(require_complete=True, cancelled=cancelled)
        listed = dict(self.manifest.files)
        paths = ((self.manifest.path, self.manifest.manifest_sha256),
                 (self.binary, listed[self.binary.relative_to(self.manifest.root).as_posix()]))
        if self.script is not None:
            paths += ((self.script, listed[self.script.relative_to(self.manifest.root).as_posix()]),)
        with ExitStack() as stack:
            for path, expected in paths:
                info = _regular(path)
                stream = stack.enter_context(verified_reader(path, expected_identity=file_identity(info)))
                digest = sha256()
                remaining = info.st_size
                while remaining:
                    engines._cancelled(cancelled)
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ModelManifestError('The selected helper launch file was truncated.')
                    digest.update(block)
                    remaining -= len(block)
                if stream.read(1) or digest.hexdigest() != expected:
                    raise ModelManifestError('The selected helper launch bytes differ from the reviewed manifest.')
            engines._cancelled(cancelled)
            yield
            # Verify the complete pinned pack again, including non-launch assets.
            self.manifest.verify(require_complete=True, cancelled=cancelled)

    def _call(self, request, *, directory, cancelled, output=None):
        raw = json.dumps({'schema': PROTOCOL, **request}, ensure_ascii=True,
                         allow_nan=False, separators=(',', ':')).encode('utf-8')
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('The local voice-helper request exceeds its byte budget.')
        command = [self.binary]
        if self.script is not None:
            command += ['-I', '-B', '-X', 'utf8', self.script]
        environment = offline_environment()
        for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONPYCACHEPREFIX',
                    'LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH'):
            environment.pop(key, None)
        environment.update(TEMP=str(directory), TMP=str(directory), TMPDIR=str(directory),
                           PYTHONDONTWRITEBYTECODE='1')
        with self._lease(cancelled):
            raw = engines._run(command, data=raw, cancelled=cancelled,
                timeout=min(self.timeout, 30) if request['operation'] == 'voices' else self.timeout,
                env=environment, cwd=directory, output=output)
            result = _json(raw)
        return result

    def voices(self, *, cancelled=lambda: False):
        engines._cancelled(cancelled)
        with _private_directory(prefix='carr-local-voice-catalogue-') as directory:
            value = self._call({'operation': 'voices'}, directory=Path(directory),
                               cancelled=cancelled)
        if set(value) != {'schema', 'voices'} or type(value['voices']) is not list or len(value['voices']) > self.max_voices:
            raise engines.EngineUnavailable('The local voice-helper catalogue has invalid fields or size.')
        records, rows = {}, []
        prefix = sha256(self.engine_name.encode('utf-8')).hexdigest()[:12]
        for record in value['voices']:
            engines._cancelled(cancelled)
            required = {'id', 'name', 'language', 'license'}
            if (type(record) is not dict or not required <= set(record)
                    or set(record) - required - {'capabilities'}):
                raise engines.EngineUnavailable('The local voice-helper catalogue contains invalid voice fields.')
            token = _text(record['id'], 'voice token', 512)
            name = _text(record['name'], 'voice name', 2048)
            language = _text(record['language'], 'voice language', 128)
            if re.fullmatch('[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*', language) is None:
                raise engines.EngineUnavailable('The local voice helper must declare an actual language code.')
            license_name = _text(record['license'], 'voice license', 2048)
            capabilities = record.get('capabilities', [])
            if (type(capabilities) is not list or len(capabilities) > len(_CONTROLS)
                    or any(type(item) is not str or item not in _CONTROLS for item in capabilities)
                    or len(set(capabilities)) != len(capabilities)):
                raise engines.EngineUnavailable('The local voice helper returned invalid capabilities.')
            identity = 'helper:' + prefix + ':' + sha256(token.encode('utf-8')).hexdigest()[:24]
            if identity in records:
                raise engines.EngineUnavailable('The local voice helper returned duplicate voice identities.')
            checked = dict(id=token, name=name, language=language,
                           license=license_name, capabilities=tuple(capabilities))
            records[identity] = checked
            rows.append(engines.Voice(identity, name, (language,), self.id,
                controls=tuple(control for control in capabilities if control in _ACTIVE_CONTROLS),
                license=license_name, revision=self.manifest.revision))
        engines._cancelled(cancelled)
        self._records = records
        return tuple(rows)

    def _selected(self, voice, *, cancelled):
        if type(voice) is not str or re.fullmatch('helper:[0-9a-f]{12}:[0-9a-f]{24}', voice) is None:
            raise engines.EngineUnavailable('Choose an exact voice from the reviewed local helper.')
        self.voices(cancelled=cancelled)
        if voice not in self._records:
            raise engines.EngineUnavailable('The selected local helper voice is unavailable. No substitute was used.')
        return self._records[voice]

    def cache_identity(self, voice, *, cancelled=lambda: False):
        record = self._selected(voice, cancelled=cancelled)
        visible = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        return {**self.manifest.cache_identity(), 'adapter': ADAPTER, 'protocol': PROTOCOL,
                'voice': sha256(visible).hexdigest(), 'launch': self.launch}

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   punctuation=False, cancelled=lambda: False):
        engines._cancelled(cancelled)
        engines._request(text, language, rate, pitch, volume)
        if type(punctuation) is not bool:
            raise ValueError('Punctuation reading must be boolean.')
        if rate != int(rate):
            raise ValueError('This helper contract requires an integer speech rate from -10 to 10.')
        target = engines._output(output)
        record = self._selected(voice, cancelled=cancelled)
        expected_language = record['language'].lower().replace('_', '-')
        requested_language = language.lower().replace('_', '-')
        if not (requested_language == expected_language
                or '-' not in requested_language and requested_language == expected_language.split('-')[0]):
            raise engines.EngineUnavailable('The selected local helper voice does not provide the requested language.')
        controls = record['capabilities']
        if ((rate != 0 and 'rate' not in controls) or (pitch != 0 and 'pitch' not in controls)
                or (volume != 1 and 'volume' not in controls)
                or (punctuation and 'punctuation' not in controls)):
            raise engines.EngineUnavailable('The selected helper voice does not support a requested control.')
        options = {'rate': int(rate), 'volume': round(volume * 100),
                   'pitch': None if pitch == 0 else round(50 + pitch * 3),
                   'punctuation': punctuation, 'timeout_seconds': self.timeout,
                   'max_output_bytes': engines.MAX_PCM_BYTES}
        with _private_directory(prefix='.speech-', dir=target.parent) as directory:
            stage = Path(directory)
            pcm = stage / 'speech.wav'
            value = self._call({'operation': 'render', 'voice_id': record['id'],
                'text': text, 'output': str(pcm), 'settings': options},
                directory=stage, cancelled=cancelled, output=pcm)
            if set(value) != {'schema', 'status'} or value['status'] != 'rendered':
                raise engines.EngineUnavailable('The local helper did not confirm its fixed render contract.')
            observed = _regular(pcm)
            if observed.st_size > engines.MAX_PCM_BYTES + 4096:
                raise engines.EngineUnavailable('The local helper exceeded its PCM output budget.')
            with verified_reader(pcm, expected_identity=file_identity(observed)):
                engines._wave_info(pcm, cancelled=cancelled)
                if self._selected(voice, cancelled=cancelled) != record:
                    raise engines.EngineUnavailable('The selected helper voice changed while rendering.')
                engines._cancelled(cancelled)
                self.manifest.verify(require_complete=True, cancelled=cancelled)
            engines._publish(pcm, target, cancelled=cancelled)
        return target
