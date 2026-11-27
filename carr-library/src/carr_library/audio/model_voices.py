"""Installed local speech engines behind a bounded PCM-file interface.

There is deliberately no cloud fallback, downloader, executable command from a
document, SSML interpretation, or fixed dordiai-voices catalogue. Optional
engines are selected explicitly by the user during local installation.
"""
# Adapted from repository PR99, commit 7cf3ee625b0f30342ae23470f2248a0d1de32e40.
# Protocol implementation only; no private worker, dependencies or model assets.
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import wave

from .voice_manifest import (LocalModelManifest, ModelManifestError, digest_file,
                             local_path, offline_environment, read_json)


ADAPTER_VERSION = 'reviewed-model-voice-4'
MAX_TEXT = 8000  # per worker request; long books are scheduled as many requests
MAX_LOG_BYTES = 1024 * 1024
MAX_PCM_BYTES = 256 * 1024 * 1024  # per request, not total project duration


class EngineUnavailable(RuntimeError):
    """A local engine or one of its advertised controls is unavailable."""


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    languages: tuple[str, ...]
    engine: str
    available: bool = True
    reason: str = ''
    controls: tuple[str, ...] = ('rate', 'volume')
    license: str = 'Installed separately; voice-specific terms apply'
    revision: str = ''
    candidate_languages: tuple[str, ...] = ()


def _number(value, minimum, maximum, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be between {minimum} and {maximum}.')
    return float(value)


def _request(text, language, rate, pitch, volume):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or '\x00' in text:
        raise ValueError(f'Provide between 1 and {MAX_TEXT} characters per speech segment.')
    if not isinstance(language, str) or not re.fullmatch('[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*', language):
        raise ValueError('Use a language code such as en, uk, or sk-SK.')
    _number(rate, -10, 10, 'Speech rate')
    _number(pitch, -12, 12, 'Speech pitch')
    _number(volume, 0, 1, 'Speech volume')
    return text


def _binary(path):
    path = local_path(path)
    if not path.is_file():
        raise EngineUnavailable('The selected local executable is unavailable.')
    return path


def _output(path):
    path = local_path(path, must_exist=False)
    if path.suffix.lower() != '.wav' or not path.parent.is_dir():
        raise ValueError('Choose a WAV file in an existing local directory.')
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    return path


def _output_budget(value):
    if type(value) is not int or not 4096 <= value < 4_000_000_000:
        raise ValueError('Select a speech output budget from 4096 to 3999999999 bytes.')
    # The caller's allowance is always respected. This adapter also retains its
    # existing per-segment ceiling; long projects are scheduled in chunks.
    return min(value, MAX_PCM_BYTES + 4096)


def _wave_info(path, *, max_output_bytes=MAX_PCM_BYTES + 4096):
    path = local_path(path)
    if path.stat().st_size > _output_budget(max_output_bytes):
        raise ValueError('A speech segment exceeds the configured PCM budget.')
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels() not in (1, 2) or audio.getsampwidth() != 2 or
                not 8000 <= audio.getframerate() <= 192000 or audio.getcomptype() != 'NONE' or
                audio.getnframes() < 1):
            raise ValueError('A speech engine did not return valid PCM16 audio.')
        frames = audio.getnframes()
        remaining = frames * audio.getnchannels() * 2
        while remaining:
            block = audio.readframes(min(65536, math.ceil(remaining / (2 * audio.getnchannels()))))
            if not block:
                raise ValueError('A speech engine returned a truncated WAV file.')
            remaining -= len(block)
        return {'frames': frames, 'sample_rate': audio.getframerate(),
                'channels': audio.getnchannels(), 'sample_width': 2}


def _publish(source, target):
    """Same-filesystem atomic publication that cannot replace another file."""
    target = _output(target)
    with Path(source).open('r+b') as stream:
        os.fsync(stream.fileno())
    os.link(source, target)
    try:
        descriptor = os.open(target.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass  # directory fsync is not exposed by every Windows filesystem
    finally:
        os.close(descriptor)


def _stop(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/taskkill.exe'
        try:
            subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run(command, *, data=b'', cancelled=lambda: False, timeout=300, env=None, cwd=None,
         return_stderr=False, output=None, max_output_bytes=MAX_PCM_BYTES + 4096):
    """Bound both logs and lifetime; text is stdin data, never shell source."""
    if cancelled():
        raise InterruptedError('Speech operation cancelled.')
    options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {'start_new_session': True}
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing, tempfile.TemporaryFile() as errors:
        incoming.write(data)
        incoming.seek(0)
        process = subprocess.Popen([str(item) for item in command], stdin=incoming, stdout=outgoing,
                                   stderr=errors, shell=False, env=env, cwd=cwd, **options)
        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancelled():
                    raise InterruptedError('Local engine operation cancelled.')
                if time.monotonic() - started > timeout:
                    raise TimeoutError('The local engine exceeded its selected per-segment time budget.')
                if max(os.fstat(outgoing.fileno()).st_size, os.fstat(errors.fileno()).st_size) > MAX_LOG_BYTES:
                    raise RuntimeError('The local engine exceeded its diagnostic byte budget.')
                if output is not None and Path(output).exists() and Path(output).stat().st_size > max_output_bytes:
                    raise ValueError('The local engine exceeded the selected audio output budget.')
                time.sleep(.03)
            if max(os.fstat(outgoing.fileno()).st_size, os.fstat(errors.fileno()).st_size) > MAX_LOG_BYTES:
                raise RuntimeError('The local engine exceeded its diagnostic byte budget.')
            if output is not None and Path(output).exists() and Path(output).stat().st_size > max_output_bytes:
                raise ValueError('The local engine exceeded the selected audio output budget.')
            if cancelled():
                raise InterruptedError('Local engine operation cancelled.')
            if process.returncode:
                # Engine diagnostics can contain book text, local paths or keys.
                # The normal application error carries only a bounded exit status.
                raise EngineUnavailable(f'Local engine failed with exit code {process.returncode}.')
            stream = errors if return_stderr else outgoing
            stream.seek(0)
            return stream.read(MAX_LOG_BYTES + 1)
        finally:
            _stop(process)


class PiperEngine:
    """Explicit OHF Piper CLI/model profile, with a reviewed asset manifest.

    The selected CLI must support the documented output_file, length_scale and
    volume options. No installer, server or implicit model downloader is used.
    """
    id = 'piper'

    def __init__(self, executable, manifest, *, timeout=300):
        self.binary = _binary(executable)
        self.manifest = LocalModelManifest.load(manifest, engine='piper')
        self.timeout = timeout
        self.model = self.manifest.settings.get('model')
        self.config = self.manifest.settings.get('config')
        self.language = self.manifest.settings.get('language', 'en')
        self.name = self.manifest.settings.get('name', self.manifest.model_id)
        if (set(self.manifest.settings) - {'model', 'config', 'language', 'name'}
                or not isinstance(self.language, str)
                or not re.fullmatch('[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*', self.language)
                or not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 256):
            raise ModelManifestError('Invalid Piper voice settings or language declaration.')
        if not isinstance(self.model, str) or not self.model.endswith('.onnx'):
            raise ModelManifestError('Choose the manifest-listed Piper ONNX model.')
        if not isinstance(self.config, str) or not self.config.endswith('.json'):
            raise ModelManifestError('Choose the manifest-listed Piper model configuration.')
        self.manifest.asset(self.model)
        self.manifest.asset(self.config)
        self.voice_id = 'piper:' + self.manifest.manifest_sha256[:24]

    def voices(self):
        self.manifest.verify()
        return (Voice(self.voice_id, self.name, (self.language,), self.id,
                      license=self.manifest.license, revision=self.manifest.revision),)

    def cache_identity(self, voice):
        if voice != self.voice_id:
            raise EngineUnavailable('Choose the configured local Piper voice.')
        self.manifest.verify()
        return {**self.manifest.cache_identity(), 'adapter': ADAPTER_VERSION,
                'binary_sha256': digest_file(self.binary)}

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False, max_output_bytes=MAX_PCM_BYTES + 4096):
        _request(text, language, rate, pitch, volume)
        max_output_bytes = _output_budget(max_output_bytes)
        if '[[' in text or ']]' in text:
            raise EngineUnavailable('This Piper CLI interprets bracketed phonemes. Use another engine for literal bracketed source text.')
        self.cache_identity(voice)
        if pitch:
            raise EngineUnavailable('Piper native pitch is not exposed; use a separate audio effect.')
        if language.lower().replace('_', '-').split('-')[0] != self.language.lower().replace('_', '-').split('-')[0]:
            raise EngineUnavailable('The selected Piper model does not advertise this language.')
        self.manifest.verify(cancelled=cancelled)
        target = _output(output)
        with tempfile.TemporaryDirectory(prefix='.speech-', dir=target.parent) as staging:
            temporary = Path(staging) / 'speech.wav'
            command = [self.binary, '--model', self.manifest.asset(self.model),
                       '--config', self.manifest.asset(self.config), '--output_file', temporary,
                       '--length_scale', format(2 ** (-rate / 10), '.8g'), '--volume', format(volume, '.8g')]
            _run(command, data=text.encode('utf-8'), timeout=self.timeout, cancelled=cancelled,
                 env=offline_environment(), cwd=self.manifest.root,
                 output=temporary, max_output_bytes=max_output_bytes)
            _wave_info(temporary, max_output_bytes=max_output_bytes)
            if cancelled():
                raise InterruptedError('Speech operation cancelled.')
            _publish(temporary, target)
        return target


_DORDIAI_OFFLINE_BOOTSTRAP = r'''
import pathlib, runpy, sys
worker = pathlib.Path(sys.argv[1])
def offline_audit(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.bind', 'socket.getaddrinfo',
                 'socket.sendto', 'urllib.Request', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        raise PermissionError('The local dordiai-voices worker denies network and child-process execution')
sys.addaudithook(offline_audit)
sys.path.insert(0, str(worker.parent))
sys.argv = sys.argv[1:]
runpy.run_path(str(worker), run_name='__main__')
'''


class DordiaiVoicesEngine:
    """dordiai-voices JSON-lines worker protocol, observed from local source.

    A separately reviewed manifest pins the complete local installation. The
    selected interpreter and its external dependencies remain trusted software;
    the audit hook is not a hostile-code/native-library sandbox. This adapter
    never imports that code into the host or republishes voice resources.
    """
    id = 'dordiai-voices'

    def __init__(self, manifest, *, timeout=900):
        self.manifest = LocalModelManifest.load(manifest, engine=self.id)
        settings = self.manifest.settings
        from .voice_packages import RUNTIME_FIELDS, runtime_settings
        if set(settings) - {'protocol', 'python', 'worker', 'catalog', 'profile_languages'} - RUNTIME_FIELDS:
            raise ModelManifestError('Unknown dordiai-voices runtime settings.')
        if settings.get('protocol') != 'nvda-worker-jsonl-pcm24k-v1':
            raise ModelManifestError('Unsupported dordiai-voices worker protocol.')
        self.runtime = runtime_settings(settings)
        self.binary = _binary(self.manifest.asset(settings.get('python', 'python/python.exe')))
        self.worker = self.manifest.asset(settings.get('worker', 'nvda_worker.py'))
        self.catalog_path = self.manifest.asset(settings.get('catalog', 'models/voices/catalog.json'))
        self.timeout = timeout
        self._profiles = {}
        self._profile_languages = {}
        self._language_codes = {}

    def voices(self):
        self.manifest.verify(require_complete=True)
        catalog = read_json(self.catalog_path)
        profiles = catalog.get('voices') if isinstance(catalog, dict) else None
        languages = catalog.get('languages') if isinstance(catalog, dict) else None
        if not isinstance(profiles, dict) or not isinstance(languages, dict) or len(profiles) > 4096:
            raise ModelManifestError('Invalid dordiai-voices catalogue.')
        managed, owned = {}, ()
        if self.manifest.settings.get('managed_profiles', False):
            from .voice_packages import managed_catalog
            profiles, managed, owned = managed_catalog(self.manifest, profiles)
            profiles = dict(profiles)
            languages = dict(languages)
            for identity in owned:
                profiles.setdefault(identity, {'name': identity})
            for identity, document in managed.items():
                profiles[identity] = {'name': document['name']}
                for code in document['languages']:
                    normalized = code.replace('_', '-').lower()
                    if not any(key.replace('_', '-').lower() == normalized for key in languages):
                        languages[code] = code
        language_codes = {}
        for code in languages:
            if not isinstance(code, str) or not re.fullmatch('[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*', code):
                raise ModelManifestError('Invalid dordiai-voices catalogue language.')
            canonical = code.replace('_', '-').lower()
            if canonical in language_codes:
                raise ModelManifestError('Duplicate dordiai-voices catalogue language.')
            language_codes[canonical] = code
        # This is Carr's reviewed manifest metadata, not an invented worker API.
        # A catalogue-wide resource list says nothing about one profile's
        # pronunciation support. Undeclared profiles remain unavailable.
        declarations = self.manifest.settings.get('profile_languages', {})
        if (type(declarations) is not dict or len(declarations) > 4096
                or set(declarations) - set(profiles)):
            raise ModelManifestError('Review languages for actual catalogue profiles only.')
        declared = {}
        for profile, record in declarations.items():
            if type(record) is not dict or set(record) != {'supported', 'candidates'}:
                raise ModelManifestError('A profile needs separate supported and candidate language lists.')
            values = {}
            for kind in ('supported', 'candidates'):
                selected = record[kind]
                if type(selected) is not list or len(selected) > 256:
                    raise ModelManifestError('Invalid per-profile language declaration.')
                normalized = []
                for code in selected:
                    canonical = code.replace('_', '-').lower() if isinstance(code, str) else ''
                    if canonical not in language_codes or canonical in normalized:
                        raise ModelManifestError('A declared profile language is missing or duplicated in its catalogue.')
                    normalized.append(canonical)
                values[kind] = tuple(normalized)
            if set(values['supported']) & set(values['candidates']):
                raise ModelManifestError('A profile language cannot be supported and a candidate at once.')
            declared[profile] = values
        rows = []
        records = {}
        supported_by_profile = {}
        for key, info in profiles.items():
            if not isinstance(key, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', key) or not isinstance(info, dict):
                raise ModelManifestError('Invalid dordiai-voices profile.')
            if info.get('disabled') is True or info.get('enabled') is False:
                continue
            name = info.get('name')
            if not isinstance(name, str) or not name or len(name) > 256:
                raise ModelManifestError('Invalid dordiai-voices profile label.')
            identity = self.id + ':' + key
            profile_languages = declared.get(key, {'supported': (), 'candidates': ()})
            supported = profile_languages['supported']
            candidates = profile_languages['candidates']
            reason = '' if supported else 'No reviewed per-profile language support.'
            if key in owned:
                from .voice_packages import approved_profile
                document = managed.get(key)
                if document is None:
                    supported = ()
                    reason = 'The owned profile is inactive, incompatible or unavailable; no legacy voice is substituted.'
                else:
                    actual = tuple(code.replace('_', '-').lower() for code in document['languages'])
                    if set(supported) - set(actual) or set(candidates) - set(actual):
                        raise ModelManifestError('A profile declaration exceeds its own package languages.')
                    if not approved_profile(self.manifest, document):
                        supported, candidates = (), actual
                        reason = 'The exact reference data and rights have not been explicitly reviewed.'
                    elif not supported:
                        candidates = actual
            if supported:
                records[identity] = key
                supported_by_profile[identity] = supported
            rows.append(Voice(identity, name, supported, self.id,
                              available=bool(supported),
                              reason=reason,
                              controls=('rate', 'pitch', 'volume'), license=self.manifest.license,
                              revision=self.manifest.revision,
                              candidate_languages=candidates))
        self._profiles, self._profile_languages, self._language_codes = records, supported_by_profile, language_codes
        return tuple(rows)

    def cache_identity(self, voice):
        if not self._profiles:
            self.voices()
        if voice not in self._profiles:
            raise EngineUnavailable('Choose an enabled profile from the installed dordiai-voices catalogue.')
        self.manifest.verify(require_complete=True)
        from .voice_packages import bridge_identity
        return {**self.manifest.cache_identity(), 'adapter': ADAPTER_VERSION, 'voice': voice,
                'protocol': 'nvda-worker-jsonl-pcm24k-v1',
                'reference_engine': self.runtime['reference_engine'],
                'owner_package_bridge_sha256': bridge_identity() if self.manifest.settings.get('managed_profiles') else None}

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False, max_output_bytes=MAX_PCM_BYTES + 4096):
        _request(text, language, rate, pitch, volume)
        max_output_bytes = _output_budget(max_output_bytes)
        self.cache_identity(voice)
        requested = language.lower().replace('_', '-')
        supported = self._profile_languages[voice]
        matching = [code for code in supported if code == requested or
                    ('-' not in code and requested.split('-')[0] == code) or
                    ('-' not in requested and code.split('-')[0] == requested)]
        if not matching:
            raise EngineUnavailable('This profile does not declare support for the requested language; candidate languages are not synthesis capabilities.')
        declared_code = requested if requested in matching else matching[0]
        code = self._language_codes[declared_code]
        base_language = declared_code.split('-')[0]
        self.manifest.verify(cancelled=cancelled, require_complete=True)
        target = _output(output)
        root = self.manifest.root
        from .voice_packages import runtime_directory
        paths = {key: runtime_directory(self.manifest, self.runtime[key])
                 for key in ('models', 'reference_model', 'references')}
        backend = self.runtime['reference_engine']
        # Arguments are the versioned installed worker's fixed interface, not a
        # command supplied by a script, book, scene or model response.
        command = [str(self.binary), '-I', '-B', '-u', '-c', _DORDIAI_OFFLINE_BOOTSTRAP,
                   str(self.worker), str(paths['models']),
                   '--reference-model', str(paths['reference_model']),
                   '--references', str(paths['references']), '--reference-engine', backend,
                   '--preload-language', 'mixed']
        request = {'mode': 'reference', 'profile': self._profiles[voice], 'language': code,
                   'text': text, 'speed': 2 ** (rate / 10), 'stream': backend != 'omnivoice',
                   'controls': {'pitch': round(50 + pitch * 3), 'timbre': 50, 'volume': round(volume * 100)},
                   'numberLanguage': 'auto', 'character': False, 'nativeProfile': backend == 'holos' and base_language in {'en', 'ru'},
                   'nativeEnglish': False}
        if backend == 'holos' and base_language in {'sk', 'pl', 'de', 'fr', 'da', 'it', 'es'}:
            request['nativeLocale'] = True
        elif backend == 'holos' and base_language not in {'uk', 'en', 'ru'}:
            request.update(wideLocale=True, reportLoading=True)
        with tempfile.TemporaryDirectory(prefix='.speech-', dir=target.parent) as staging:
            temporary = Path(staging) / 'speech.wav'
            self._stream(command, request, temporary, cancelled, max_output_bytes=max_output_bytes)
            _wave_info(temporary, max_output_bytes=max_output_bytes)
            if cancelled():
                raise InterruptedError('Speech operation cancelled.')
            _publish(temporary, target)
        return target

    def _stream(self, command, request, target, cancelled, *,
                max_output_bytes=MAX_PCM_BYTES + 4096):
        max_output_bytes = _output_budget(max_output_bytes)
        if cancelled():
            raise InterruptedError('Speech operation cancelled.')
        replies = queue.Queue(maxsize=2)
        finished = threading.Event()
        options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {'start_new_session': True}
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                       cwd=self.manifest.root, env=offline_environment(), shell=False, **options)
            def read_replies():
                try:
                    while not finished.is_set():
                        line = process.stdout.readline(4 * 1024 * 1024 + 1)
                        if not line:
                            item = EngineUnavailable('dordiai-voices closed its response stream.')
                        elif len(line) > 4 * 1024 * 1024:
                            item = ValueError('dordiai-voices response exceeded its byte budget.')
                        else:
                            try:
                                item = json.loads(line)
                            except (ValueError, UnicodeError):
                                item = ValueError('dordiai-voices returned invalid JSON.')
                        while not finished.is_set():
                            try:
                                replies.put(item, timeout=.1)
                                break
                            except queue.Full:
                                continue
                        if isinstance(item, Exception):
                            break
                except (OSError, ValueError):
                    if not finished.is_set():
                        try:
                            replies.put_nowait(EngineUnavailable('dordiai-voices response stream failed.'))
                        except queue.Full:
                            pass
            reader = threading.Thread(target=read_replies, daemon=True, name='dordiai-voices PCM reader')
            reader.start()
            def send(value):
                process.stdin.write((json.dumps(value, ensure_ascii=True) + '\n').encode('ascii'))
                process.stdin.flush()
            started = time.monotonic()
            total = 0
            try:
                send(request)
                with wave.open(str(target), 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    while True:
                        if cancelled():
                            try:
                                send({'mode': 'cancel'})
                                send({'mode': 'drop'})
                            except (OSError, BrokenPipeError):
                                pass
                            raise InterruptedError('Speech operation cancelled.')
                        if time.monotonic() - started > self.timeout:
                            raise TimeoutError('dordiai-voices exceeded its per-segment time budget.')
                        if os.fstat(errors.fileno()).st_size > MAX_LOG_BYTES:
                            raise EngineUnavailable('dordiai-voices exceeded its diagnostic byte budget.')
                        try:
                            row = replies.get(timeout=.05)
                        except queue.Empty:
                            continue
                        if isinstance(row, Exception):
                            raise row
                        if not isinstance(row, dict):
                            raise ValueError('dordiai-voices response is not an object.')
                        if 'error' in row:
                            raise EngineUnavailable('dordiai-voices rejected this local speech request.')
                        if 'loadingStage' in row:
                            if 'pcm' in row or 'chunk' in row or row.get('finished'):
                                raise ValueError('Ambiguous dordiai-voices loading response.')
                            continue
                        if row.get('finished') is True:
                            if 'pcm' in row or 'chunk' in row:
                                raise ValueError('Ambiguous dordiai-voices completion response.')
                            break
                        if ('chunk' in row) == ('pcm' in row):
                            raise ValueError('Expected one dordiai-voices PCM response.')
                        data = row.get('chunk', row.get('pcm'))
                        if not isinstance(data, str):
                            raise ValueError('dordiai-voices PCM is not an encoded string.')
                        try:
                            pcm = base64.b64decode(data, validate=True)
                        except (ValueError, TypeError) as exc:
                            raise ValueError('Invalid dordiai-voices PCM encoding.') from exc
                        if not pcm or len(pcm) % 2:
                            raise ValueError('dordiai-voices PCM is empty or not sample-aligned.')
                        total += len(pcm)
                        if total > MAX_PCM_BYTES or total + 44 > max_output_bytes:
                            raise ValueError('dordiai-voices segment exceeded the PCM byte budget.')
                        audio.writeframesraw(pcm)
                        if 'pcm' in row:
                            break
                        send({'mode': 'next'})
                if not total:
                    raise ValueError('dordiai-voices produced no audio.')
            finally:
                finished.set()
                _stop(process)
                if process.stdin:
                    process.stdin.close()
                reader.join(timeout=1)
                if process.stdout and not reader.is_alive():
                    process.stdout.close()


