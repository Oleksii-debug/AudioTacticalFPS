"""Real, local-only speech engines, discovered rather than invented.

SAPI 5 runs in isolated Windows PowerShell processes (both installed bitnesses).
FFmpeg Flite and eSpeak NG are optional user-installed local engines. No model,
voice, executable, or original text is ever downloaded or sent to a service.
One call renders one bounded utterance; the project scheduler owns long books.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import copy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

from .cleanup import retained_context, retained_temporary_directory, _pending_owners
from .process import owned_process
import time
from typing import Callable
import wave


MAX_TEXT_CHARS = 32000
MAX_LOG_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
SAMPLE_RATE = 24000
_LANGUAGE = re.compile(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z')
_FLITE_VOICE = re.compile(r'[a-zA-Z][a-zA-Z0-9_]{0,63}\Z')


@dataclass(frozen=True)
class Voice:
    id: str
    engine: str
    name: str
    languages: tuple[str, ...]
    capabilities: tuple[str, ...]
    vendor: str = ''
    license_note: str = 'User-installed runtime and voice; their terms apply.'
    candidate_languages: tuple[str, ...] = ()


@dataclass(frozen=True)
class SynthesisResult:
    path: Path
    sha256: str
    sample_rate: int
    channels: int
    frames: int
    duration_seconds: float
    voice_id: str
    engine: str
    text_sha256: str
    timing: str = 'utterance'
    rate_control: str = 'engine_relative'


def supports_language(voice, language):
    """Match declared language support, without inventing native accent quality."""
    if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
        raise ValueError('Use a valid language code such as en, uk or sk.')
    requested = language.casefold()
    for declared in voice.languages:
        declared = declared.replace('_', '-').casefold()
        if (declared == requested
                or ('-' not in requested and declared.split('-')[0] == requested)
                or ('-' not in declared and requested.split('-')[0] == declared)):
            return True
    return False


def load_voice_config(path):
    """Read an explicitly selected local configuration, never book instructions.

    Schema: version=1, kind='local-voice-engines', config={...}. Optional model
    manifests and executables are absolute local paths. No assets are copied.
    """
    from .voice_manifest import read_json
    selected = Path(path)
    if not selected.is_absolute():
        raise ValueError('Choose an absolute local voice configuration path.')
    value = read_json(selected, max_bytes=16 * 1024)
    if (type(value) is not dict or set(value) != {'version', 'kind', 'config'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'local-voice-engines' or type(value['config']) is not dict):
        raise ValueError('Unsupported local voice configuration.')
    from .voice_settings import validate_voice_config
    return validate_voice_config(value['config'])


def _number(value, minimum, maximum, label):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise ValueError(f'{label} must be between {minimum} and {maximum}.')
    return float(value)


def _cancel(cancelled):
    if cancelled is None:
        return
    callback = cancelled.is_set if hasattr(cancelled, 'is_set') else cancelled
    if not callable(callback):
        raise ValueError('Cancellation must be a callback or event.')
    if callback():
        raise InterruptedError('Speech synthesis cancelled; no output published.')


def _local_path(value):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw or '\x00' in raw
            or raw.startswith(('\\\\', '//')) or '://' in raw):
        raise ValueError('Choose a local file path.')
    path = Path(raw).absolute()
    from .render import _require_local_volume
    _require_local_volume(path)
    for ancestor in (path.parent, *path.parent.parents):
        info = ancestor.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Speech output directories must be ordinary local directories.')
    return path


def _destination(value):
    path = _local_path(value)
    if path.suffix.lower() != '.wav':
        raise ValueError('Speech synthesis writes PCM .wav files; convert after rendering.')
    if os.path.lexists(path):
        raise FileExistsError('Speech output already exists; choose a new filename.')
    return path


def _executable(value, fallback):
    if value is False:
        return None
    if fallback == 'ffmpeg' and value is None:
        from .runtime import resolve_active_audio_tool
        from .render import tool_path
        bundled = resolve_active_audio_tool('ffmpeg')
        if bundled is not None or shutil.which('ffmpeg'):
            return Path(tool_path('ffmpeg'))
        return None
    if value is None:
        found = shutil.which(fallback)
        return Path(found).resolve() if found else None
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError('An engine executable must be an explicit local path.')
    raw = os.fspath(value)
    if raw.startswith(('\\\\', '//')) or '://' in raw or '\x00' in raw:
        raise ValueError('An engine executable must be local.')
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError('A configured engine must be an existing absolute executable path.')
    from .render import _require_local_volume
    _require_local_volume(path)
    if not path.is_file():
        raise ValueError('A configured engine must be an existing absolute executable path.')
    if fallback == 'ffmpeg':
        from .render import tool_path
        return Path(tool_path('ffmpeg', path))
    return path.resolve()


def _run(command, directory, *, timeout_seconds, cancelled=None,
         allowed_returncodes=(0,), output=None, max_output_bytes=MAX_OUTPUT_BYTES):
    """Bound engine logs and time; private speech never enters public diagnostics."""
    _cancel(cancelled)
    log = directory / 'engine.log'
    with retained_context(log.open('wb')) as stream:
        options = {'stdin': subprocess.DEVNULL, 'stdout': stream, 'stderr': stream,
                   'cwd': directory, 'shell': False}
        with owned_process([os.fspath(part) for part in command], **options) as process:
            start = time.monotonic()
            while process.poll() is None:
                _cancel(cancelled)
                if time.monotonic() - start > timeout_seconds:
                    raise TimeoutError('The local speech engine exceeded this utterance time budget.')
                if log.stat().st_size > MAX_LOG_BYTES:
                    raise RuntimeError('The local speech engine exceeded its diagnostic budget.')
                if output is not None and output.exists() and output.stat().st_size > max_output_bytes:
                    raise RuntimeError('The local speech engine exceeded this utterance storage budget.')
                time.sleep(0.025)
            _cancel(cancelled)
            if process.returncode not in allowed_returncodes:
                raise RuntimeError(f'The local speech engine failed (exit {process.returncode}).')
    if log.stat().st_size > MAX_LOG_BYTES:
        raise RuntimeError('The local speech engine exceeded its diagnostic budget.')
    if output is not None and output.exists() and output.stat().st_size > max_output_bytes:
        raise RuntimeError('The local speech engine exceeded this utterance storage budget.')
    return log.read_text(encoding='utf-8', errors='replace')


def _digest(path, *, cancelled=None):
    _cancel(cancelled)
    digest = sha256()
    with path.open('rb') as stream:
        while block := stream.read(128 * 1024):
            _cancel(cancelled)
            digest.update(block)
    _cancel(cancelled)
    return digest.hexdigest()


def _sapi_hosts():
    if os.name != 'nt':
        return ()
    root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    is_64 = struct.calcsize('P') == 8 or bool(os.environ.get('PROCESSOR_ARCHITEW6432'))
    hosts = []
    if is_64:
        native = 'System32' if struct.calcsize('P') == 8 else 'Sysnative'
        hosts.extend((('x64', root / native / 'WindowsPowerShell/v1.0/powershell.exe'),
                      ('x86', root / 'SysWOW64/WindowsPowerShell/v1.0/powershell.exe')))
    else:
        hosts.append(('x86', root / 'System32/WindowsPowerShell/v1.0/powershell.exe'))
    return tuple((arch, path) for arch, path in hosts if path.is_file())


class VoiceRegistry:
    """Discover only usable local voices, with a stable identity per engine.

    config accepts ``ffmpeg``/``espeak_ng`` absolute executable paths (False
    disables discovery), ``include_sapi`` (default True), ``piper`` containing
    executable/manifest absolute paths, and ``dordiai_manifest``. Programs are
    trusted, explicitly installed runtimes, not executable paths from a script.
    ``rate`` is a relative request: Flite uses FFmpeg time stretch, SAPI uses its
    native -10..10 scale, and eSpeak uses words/minute. It is not a native-accent
    or timing guarantee. Unsupported phoneme/style/cloning controls are absent.
    """

    def __init__(self, config=None):
        if config is not None and type(config) is not dict:
            raise ValueError('Provide an explicit local voice configuration object.')
        config = dict(config or {})
        if set(config) - {'ffmpeg', 'espeak_ng', 'include_sapi', 'piper', 'dordiai_manifest'}:
            raise ValueError('Unknown local voice configuration option.')
        if type(config.get('include_sapi', True)) is not bool:
            raise ValueError('include_sapi must be true or false.')
        self.ffmpeg = _executable(config.get('ffmpeg'), 'ffmpeg')
        self.espeak = _executable(config.get('espeak_ng'), 'espeak-ng')
        self.include_sapi = config.get('include_sapi', True)
        self._voices = None
        self._routes = {}
        self._status = []
        self._sapi_metadata = {}
        self._model_engines = {}
        self._registered_settings = None
        if 'piper' in config:
            from .model_voices import PiperEngine
            selected = config['piper']
            if type(selected) is not dict or set(selected) != {'executable', 'manifest'}:
                raise ValueError('A Piper profile needs an executable and reviewed manifest.')
            self._model_engines['piper'] = PiperEngine(
                self._manifest_path(selected['executable']), self._manifest_path(selected['manifest']))
        if 'dordiai_manifest' in config:
            from .model_voices import DordiaiVoicesEngine
            self._model_engines['dordiai-voices'] = DordiaiVoicesEngine(self._manifest_path(config['dordiai_manifest']))

    @staticmethod
    def _manifest_path(value):
        from .voice_manifest import local_path
        if not isinstance(value, (str, os.PathLike)) or not Path(value).is_absolute():
            raise ValueError('Select an absolute local model manifest path.')
        return local_path(value)

    @classmethod
    def from_config_file(cls, path):
        return cls(load_voice_config(path))

    @classmethod
    def for_library(cls, root):
        """Use the library's explicitly reviewed registration, if present."""
        from .voice_settings import VoiceSettingsStore, verify_voice_settings_bindings
        value = verify_voice_settings_bindings(VoiceSettingsStore(root).read())
        registry = cls(value['config'])
        registry._registered_settings = value
        return registry

    def voices_for_language(self, language):
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise ValueError('Use a valid language code such as en, uk or sk.')
        return tuple(voice for voice in self.list_voices() if supports_language(voice, language))

    def choose_voice(self, language, preferred=None):
        available = self.voices_for_language(language)
        if preferred is not None:
            for voice in available:
                if voice.id == preferred:
                    return voice
            raise ValueError('The selected voice is unavailable or does not declare this language. No substitute was used.')
        if not available:
            raise ValueError('No installed, verified local voice declares this language. Configure a matching engine first.')
        exact = [voice for voice in available if language.casefold() in
                 {value.replace('_', '-').casefold() for value in voice.languages}]
        return (exact or list(available))[0]

    def list_voices(self, refresh=False, *, cancelled=None):
        _cancel(cancelled)
        cancellation = {} if cancelled is None else {'cancelled': lambda: bool(_cancel(cancelled))}
        if self._registered_settings is not None:
            from .voice_settings import verify_voice_settings_bindings
            verify_voice_settings_bindings(self._registered_settings, **cancellation)
        if self._voices is not None and not refresh:
            return self._voices
        rows, routes, statuses = [], {}, []
        sapi_metadata = {}
        if self.include_sapi:
            for arch, host in _sapi_hosts():
                try:
                    with retained_temporary_directory(prefix='carr-voice-discovery-', dir=None) as temp:
                        reply = self._sapi(host, Path(temp), {'operation': 'voices'}, 30, **cancellation)
                    voices = reply.get('voices')
                    if type(voices) is not list or len(voices) > 1000:
                        raise ValueError('Invalid installed-voice enumeration.')
                    sapi_metadata[arch] = reply.get('runtime', {})
                    for row in voices:
                        if (type(row) is not dict or set(row) != {'id', 'name', 'language', 'vendor'}
                                or any(not isinstance(row[k], str) or len(row[k]) > 2048
                                       for k in row)
                                or not row['id'] or not row['name']
                                or not _LANGUAGE.fullmatch(row['language'])):
                            raise ValueError('Invalid installed-voice information.')
                        identifier = 'sapi5:' + arch + ':' + sha256(row['id'].encode()).hexdigest()[:24]
                        if identifier in routes:
                            continue
                        rows.append(Voice(identifier, 'sapi5', row['name'] + f' ({arch})',
                            (row['language'],), ('pcm', 'rate', 'volume', 'local'), row['vendor']))
                        routes[identifier] = ('sapi5', host, row['id'])
                    statuses.append({'engine': 'sapi5-' + arch, 'status': 'available', 'voices': len(voices)})
                except InterruptedError:
                    raise
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    if _pending_owners(exc):
                        raise
                    statuses.append({'engine': 'sapi5-' + arch, 'status': 'unavailable',
                                     'reason': type(exc).__name__})
        if self.ffmpeg is not None:
            try:
                from .render import tool_path
                executable = tool_path('ffmpeg', self.ffmpeg)
                with retained_temporary_directory(prefix='carr-voice-discovery-', dir=None) as temp:
                    log = _run([executable, '-hide_banner', '-nostdin', '-f', 'lavfi',
                                '-i', 'flite=list_voices=1', '-f', 'null', '-'], Path(temp),
                               timeout_seconds=15, allowed_returncodes=(0, 1, 255), **cancellation)
                names = tuple(dict.fromkeys(re.findall(
                    r'\[Parsed_flite_\d+ @ [^\]]+\]\s+([A-Za-z][A-Za-z0-9_]*)\s*$',
                    log, re.MULTILINE)))
                if not names:
                    raise RuntimeError('This FFmpeg build has no enumerated Flite voices.')
                for name in names:
                    if not _FLITE_VOICE.fullmatch(name):
                        raise ValueError('Unexpected Flite voice identity.')
                    identifier = 'flite:' + name
                    rows.append(Voice(identifier, 'flite', 'Flite ' + name, ('en-US',),
                        ('pcm', 'rate_via_time_stretch', 'volume', 'local'), 'CMU Flite',
                        'Installed Flite voice and FFmpeg build terms apply; no bundled voices.'))
                    routes[identifier] = ('flite', self.ffmpeg, name)
                statuses.append({'engine': 'flite', 'status': 'available', 'voices': len(names)})
            except InterruptedError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                if _pending_owners(exc):
                    raise
                statuses.append({'engine': 'flite', 'status': 'unavailable', 'reason': type(exc).__name__})
        if self.espeak is not None:
            try:
                with retained_temporary_directory(prefix='carr-voice-discovery-', dir=None) as temp:
                    log = _run([self.espeak, '--voices'], Path(temp), timeout_seconds=15, **cancellation)
                seen = set()
                for line in log.splitlines():
                    values = line.split()
                    if len(values) < 5 or not values[0].isdigit() or not _LANGUAGE.fullmatch(values[1]):
                        continue
                    language = values[1]
                    if language in seen:
                        continue
                    seen.add(language)
                    identifier = 'espeak-ng:' + language
                    rows.append(Voice(identifier, 'espeak-ng', values[3].replace('_', ' '),
                        (language,), ('pcm', 'rate', 'volume', 'local'), 'eSpeak NG',
                        'User-installed eSpeak NG GPL runtime and language data; no bundled voices.'))
                    routes[identifier] = ('espeak-ng', self.espeak, language)
                if not seen:
                    raise RuntimeError('No eSpeak NG voices could be enumerated.')
                statuses.append({'engine': 'espeak-ng', 'status': 'available', 'voices': len(seen)})
            except InterruptedError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                if _pending_owners(exc):
                    raise
                statuses.append({'engine': 'espeak-ng', 'status': 'unavailable', 'reason': type(exc).__name__})
        for name in ('piper', 'dordiai-voices'):
            model = self._model_engines.get(name)
            if model is None:
                statuses.append({'engine': name, 'status': 'unavailable',
                    'reason': 'Select a reviewed local model/runtime manifest; assets are not bundled or downloaded.'})
                continue
            try:
                found = (model.voices() if cancelled is None else
                         model.voices(cancelled=lambda: bool(_cancel(cancelled))))
                model_rows, model_routes, profiles = [], {}, []
                for item in found:
                    languages = tuple(value.replace('_', '-') for value in item.languages)
                    candidates = tuple(value.replace('_', '-') for value in item.candidate_languages)
                    if (not isinstance(item.id, str) or not item.id.startswith(name + ':')
                            or item.id in routes or item.id in model_routes
                            or not isinstance(item.name, str) or not item.name.strip() or len(item.name) > 256
                            or any(not _LANGUAGE.fullmatch(value) for value in (*languages, *candidates))):
                        raise ValueError('The configured engine declared invalid voice identities or languages.')
                    profiles.append({'id': item.id, 'name': item.name,
                        'supported_languages': list(languages), 'candidate_languages': list(candidates),
                        'available': item.available, 'reason': item.reason})
                    if not item.available:
                        continue
                    if not languages:
                        raise ValueError('An available profile must declare its own supported languages.')
                    model_rows.append(Voice(item.id, name, item.name, languages,
                        ('pcm', 'rate', 'volume', 'local'), name,
                        'User-reviewed manifest license: ' + item.license, candidates))
                    model_routes[item.id] = (name, model.binary, item.id)
                rows.extend(model_rows)
                routes.update(model_routes)
                statuses.append({'engine': name, 'status': 'available' if model_rows else 'configured',
                                 'voices': len(model_rows), 'profiles': profiles,
                                 'inference': 'not verified by enumeration'})
            except InterruptedError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                if _pending_owners(exc):
                    raise
                statuses.append({'engine': name, 'status': 'unavailable',
                                 'reason': 'Configured assets did not pass voice enumeration: ' + type(exc).__name__})
        statuses.append({'engine': 'sapi4', 'status': 'unavailable', 'reason': 'Legacy compatibility is not verified.'})
        if not any(row['engine'].startswith('sapi5-') for row in statuses):
            statuses.append({'engine': 'sapi5', 'status': 'unavailable',
                             'reason': 'SAPI5 requires enabled installed Windows voices.'})
        if self.ffmpeg is None:
            statuses.append({'engine': 'flite', 'status': 'unavailable',
                             'reason': 'A local FFmpeg build with Flite is not configured.'})
        if self.espeak is None:
            statuses.append({'engine': 'espeak-ng', 'status': 'unavailable',
                             'reason': 'A local eSpeak NG executable is not configured.'})
        _cancel(cancelled)
        self._voices, self._routes, self._status = tuple(rows), routes, statuses
        self._sapi_metadata = sapi_metadata
        return self._voices

    def engine_status(self):
        self.list_voices()
        return tuple(dict(row) for row in self._status)

    def runtime_identity(self, *, cancelled=None):
        """Cache provenance, without exposing private installation paths.

        Covers executable bytes/version, helper bytes and installed voice
        identities. An OS voice's opaque external assets are *not* claimed to
        be individually hashed. Reload voices after changing installed engines.
        """
        voices = (self.list_voices() if cancelled is None else
                  self.list_voices(cancelled=cancelled))
        def digest(path):
            return _digest(path) if cancelled is None else _digest(path, cancelled=cancelled)
        executables = {}
        for engine, executable, _ in self._routes.values():
            key = (engine, executable)
            if key in executables:
                continue
            _cancel(cancelled)
            item = {'engine': engine, 'executable_sha256': digest(executable)}
            if engine in ('flite', 'espeak-ng'):
                with retained_temporary_directory(prefix='carr-voice-version-', dir=None) as temp:
                    log = _run([executable, '-version' if engine == 'flite' else '--version'],
                               Path(temp), timeout_seconds=15,
                               **({} if cancelled is None else {'cancelled': cancelled}))
                item['version_sha256'] = sha256(log.encode('utf-8')).hexdigest()
            executables[key] = item
        helper = Path(__file__).with_name('sapi_helper.ps1')
        model_identities = []
        for engine, model in self._model_engines.items():
            for voice in voices:
                if voice.engine == engine:
                    model_identities.append(model.cache_identity(voice.id) if cancelled is None else
                        model.cache_identity(voice.id, cancelled=lambda: bool(_cancel(cancelled))))
        return {'version': 2, 'voices': [asdict(voice) for voice in voices],
                'executables': sorted(executables.values(), key=lambda row: (row['engine'], row['executable_sha256'])),
                'sapi_helper_sha256': digest(helper), 'sapi_runtime': self._sapi_metadata,
                'model_manifests': model_identities,
                'model_adapter_sha256': digest(Path(__file__).with_name('model_voices.py')),
                'model_validator_sha256': digest(Path(__file__).with_name('voice_manifest.py')),
                'owner_package_adapter_sha256': digest(Path(__file__).with_name('voice_packages.py')),
                'registry_sha256': digest(Path(__file__)),
                'fingerprint_scope': 'executables, versions, helpers, installed identities, configured model manifests and verified assets'}

    def fingerprint(self, *, cancelled=None):
        identity = (self.runtime_identity() if cancelled is None else
                    self.runtime_identity(cancelled=cancelled))
        _cancel(cancelled)
        return sha256(json.dumps(identity, ensure_ascii=False,
                                 sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    @staticmethod
    def _sapi(host, directory, request, timeout_seconds, cancelled=None,
              max_output_bytes=MAX_OUTPUT_BYTES):
        helper = Path(__file__).with_name('sapi_helper.ps1').resolve(strict=True)
        request_path = directory / 'request.json'
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        _run([host, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned',
              '-File', helper, '-Request', request_path], directory,
             timeout_seconds=timeout_seconds, cancelled=cancelled,
             output=directory / 'speech.wav' if request['operation'] == 'synthesize' else None,
             max_output_bytes=max_output_bytes)
        response = directory / 'result.json'
        if not response.is_file() or response.is_symlink() or response.stat().st_size > MAX_LOG_BYTES:
            raise RuntimeError('The Windows voice helper did not provide a bounded result.')
        reply = json.loads(response.read_text(encoding='utf-8-sig'))
        if type(reply) is not dict or reply.get('version') != 1 or reply.get('ok') is not True:
            raise RuntimeError('The Windows voice helper did not complete successfully.')
        return reply

    def synthesize(self, text, output, *, voice_id, language=None, rate=1.0, volume=1.0,
                   cancelled=None, timeout_seconds=300, max_output_bytes=MAX_OUTPUT_BYTES):
        """Write an actual PCM WAV after validation; never replace an existing file."""
        if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS
                or '\x00' in text or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)):
            raise ValueError(f'Provide nonempty speech text in chunks of at most {MAX_TEXT_CHARS} characters.')
        rate = _number(rate, 0.5, 2.0, 'Relative speech rate')
        volume = _number(volume, 0, 1, 'Speech volume')
        timeout_seconds = _number(timeout_seconds, 0.1, 3600, 'Utterance time budget')
        if type(max_output_bytes) is not int or not 4096 <= max_output_bytes <= MAX_OUTPUT_BYTES:
            raise ValueError('Utterance storage budget must be between 4096 bytes and 512 MiB.')
        _cancel(cancelled)
        destination = _destination(output)
        voices = {voice.id: voice for voice in (self.list_voices() if cancelled is None else
                  self.list_voices(cancelled=cancelled))}
        if not isinstance(voice_id, str) or voice_id not in voices:
            raise ValueError('Select an available installed voice; reload voices after installing one.')
        voice = voices[voice_id]
        if language is not None:
            if not supports_language(voice, language):
                raise ValueError('The selected voice does not declare support for this language.')
        engine, executable, key = self._routes[voice_id]
        with retained_temporary_directory(prefix='.carr-speech-', dir=destination.parent) as temp:
            directory = Path(temp)
            rendered = directory / 'speech.wav'
            if engine == 'sapi5':
                self._sapi(executable, directory, {'operation': 'synthesize', 'voice_id': key,
                    'text': text, 'rate': int(round(5 * math.log2(rate))),
                    'volume': int(round(volume * 100)), 'sample_rate': SAMPLE_RATE},
                    timeout_seconds, cancelled, max_output_bytes=max_output_bytes)
            elif engine in self._model_engines:
                # The engine protocol uses a logarithmic rate scale. The public
                # registry keeps one relative-rate API for every engine.
                model = copy(self._model_engines[engine])
                model.timeout = timeout_seconds
                model.synthesize(text, rendered, voice=key, language=language or voice.languages[0],
                    rate=10 * math.log2(rate), pitch=0, volume=volume,
                    cancelled=lambda: bool(_cancel(cancelled)), max_output_bytes=max_output_bytes)
            else:
                # Only a fixed basename enters the filter. Authored text is file
                # content, never a command, SSML, shell expansion or filtergraph.
                (directory / 'speech.txt').write_text(text, encoding='utf-8')
                if engine == 'flite':
                    from .render import tool_path
                    executable = tool_path('ffmpeg', executable)
                    command = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                        '-protocol_whitelist', 'file,pipe', '-f', 'lavfi', '-i',
                        'flite=textfile=speech.txt:voice=' + key,
                        '-af', f'atempo={rate:.8g},volume={volume:.8g}', '-ac', '1',
                        '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le', '-f', 'wav', 'speech.wav']
                elif engine == 'espeak-ng':
                    command = [executable, '-v', key, '-s', str(round(175 * rate)),
                               '-a', str(round(100 * volume)), '-f', 'speech.txt', '-w', 'speech.wav']
                else:
                    raise RuntimeError('The selected voice has no validated local route.')
                _run(command, directory, timeout_seconds=timeout_seconds,
                     cancelled=cancelled, output=rendered, max_output_bytes=max_output_bytes)
            _cancel(cancelled)
            if (not rendered.is_file() or rendered.is_symlink()
                    or not 44 <= rendered.stat().st_size <= max_output_bytes):
                raise RuntimeError('The local engine did not produce a bounded waveform.')
            try:
                with wave.open(str(rendered), 'rb') as reader:
                    channels, frames, sample_rate = reader.getnchannels(), reader.getnframes(), reader.getframerate()
                    if (reader.getsampwidth() != 2 or reader.getcomptype() != 'NONE'
                            or channels not in (1, 2) or not 8000 <= sample_rate <= 96000 or frames <= 0):
                        raise RuntimeError('The local engine returned an unsupported PCM waveform.')
                    # Streaming read verifies the advertised audio is complete.
                    read_frames = 0
                    while block := reader.readframes(8192):
                        _cancel(cancelled)
                        read_frames += len(block) // (channels * 2)
                    if read_frames != frames:
                        raise RuntimeError('The local engine returned a truncated waveform.')
            except (wave.Error, EOFError) as exc:
                raise RuntimeError('The local engine returned an invalid WAV file.') from exc
            digest = _digest(rendered, cancelled=cancelled)
            with rendered.open('r+b') as stream:
                os.fsync(stream.fileno())
            _cancel(cancelled)
            # Same-filesystem exclusive link is an atomic no-replace publish.
            # If links are unavailable, fail clearly; never weaken overwrite safety.
            os.link(rendered, destination)
            return SynthesisResult(destination, digest, sample_rate, channels, frames,
                frames / sample_rate, voice_id, engine, sha256(text.encode('utf-8')).hexdigest(),
                rate_control='time_stretch' if engine == 'flite' else 'engine_relative')


def synthesize(text, output, *, voice_id, config=None, **kwargs):
    """Convenience function for one utterance; reuse a registry for a project."""
    return VoiceRegistry(config).synthesize(text, output, voice_id=voice_id, **kwargs)
