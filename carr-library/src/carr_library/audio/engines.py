"""Installed local speech engines behind a bounded PCM-file interface.

There is deliberately no cloud fallback, downloader, executable command from a
document, SSML interpretation, or fixed dordiai-voices catalogue. Optional
engines are selected explicitly by the user during local installation.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import inspect
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

from .model_manifest import (LocalModelManifest, ModelManifestError, digest_file,
                             local_path, offline_environment, read_json)
from .dordiai_discovery import DISCOVERY_PROTOCOL, REQUIRED_HELPERS, parse_discovery


ADAPTER_VERSION = 'local-speech-2'
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


def _number(value, minimum, maximum, label):
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise ValueError(f'{label} must be between {minimum} and {maximum}.')
    return float(value)


def _cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A speech cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Speech operation cancelled.')


def _timeout(value):
    try:
        valid = type(value) in (int, float) and value > 0 and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('The local engine timeout must be a positive finite number.')
    return float(value)


def _invoke(method, *args, cancelled):
    """Forward cooperative cancellation while retaining old custom adapters."""
    _cancelled(cancelled)
    try:
        inspect.signature(method).bind(*args, cancelled=cancelled)
    except (TypeError, ValueError):
        result = method(*args)
    else:
        result = method(*args, cancelled=cancelled)
    _cancelled(cancelled)
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate speech response field.')
        result[key] = value
    return result


def _sapi_json(data):
    try:
        return json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as exc:
        raise EngineUnavailable('SAPI 5 returned an invalid response.') from exc


def _request(text, language, rate, pitch, volume):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or '\x00' in text:
        raise ValueError(f'Provide between 1 and {MAX_TEXT} characters per speech segment.')
    if not isinstance(language, str) or not re.fullmatch('[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*', language):
        raise ValueError('Use a language code such as en, uk, or sk-SK.')
    _number(rate, -10, 10, 'Speech rate')
    _number(pitch, -12, 12, 'Speech pitch')
    _number(volume, 0, 1, 'Speech volume')
    try:
        text.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError('Speech text must contain valid Unicode.') from exc
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


def _wave_info(path, *, cancelled=lambda: False):
    _cancelled(cancelled)
    path = local_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('A speech engine did not return a regular WAV file.')
    if info.st_size > MAX_PCM_BYTES + 4096:
        raise ValueError('A speech segment exceeds the configured PCM budget.')
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels() not in (1, 2) or audio.getsampwidth() != 2 or
                not 8000 <= audio.getframerate() <= 192000 or audio.getcomptype() != 'NONE' or
                audio.getnframes() < 1):
            raise ValueError('A speech engine did not return valid PCM16 audio.')
        frames = audio.getnframes()
        remaining = frames * audio.getnchannels() * 2
        if remaining > MAX_PCM_BYTES:
            raise ValueError('A speech segment exceeds the configured PCM budget.')
        while remaining:
            _cancelled(cancelled)
            block = audio.readframes(min(65536, math.ceil(remaining / (2 * audio.getnchannels()))))
            if not block or len(block) > remaining or len(block) % (2 * audio.getnchannels()):
                raise ValueError('A speech engine returned a truncated WAV file.')
            remaining -= len(block)
        _cancelled(cancelled)
        return {'frames': frames, 'sample_rate': audio.getframerate(),
                'channels': audio.getnchannels(), 'sample_width': 2}


def _publish(source, target, *, cancelled=lambda: False):
    """Same-filesystem atomic publication that cannot replace another file."""
    from .jobs import publish_noreplace
    target = _output(target)
    _cancelled(cancelled)
    # The voice worker owns this private stage; moving it also avoids a
    # hardlink alias if the application exits before temporary cleanup.
    publish_noreplace(source, target)


def _stop(process):
    if os.name == 'nt':
        if process.poll() is not None:
            return
        taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/taskkill.exe'
        try:
            subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        # Every worker is started in its own session. The group can still own
        # live descendants after its leader has exited; poll() alone is not a
        # cleanup boundary for that group.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=.1)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run(command, *, data=b'', cancelled=lambda: False, timeout=300, env=None, cwd=None,
         return_stderr=False, output=None):
    """Bound both logs and lifetime; text is stdin data, never shell source."""
    timeout = _timeout(timeout)
    _cancelled(cancelled)
    started = time.monotonic()

    def check():
        _cancelled(cancelled)
        if time.monotonic() - started >= timeout:
            raise TimeoutError('The local engine exceeded its selected per-segment time budget.')

    options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {'start_new_session': True}
    # Local workers must not create report or TLS-key files through inherited
    # diagnostic settings. Copy explicit mappings as well; never change callers.
    environment = {key: value for key, value in (os.environ if env is None else env).items()
                   if key.upper() not in {'FFREPORT', 'SSLKEYLOGFILE'}}
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing, tempfile.TemporaryFile() as errors:
        incoming.write(data)
        incoming.seek(0)
        process = subprocess.Popen([str(item) for item in command], stdin=incoming, stdout=outgoing,
                                   stderr=errors, shell=False, env=environment, cwd=cwd, **options)
        try:
            while True:
                check()
                status = process.poll()
                if max(os.fstat(outgoing.fileno()).st_size, os.fstat(errors.fileno()).st_size) > MAX_LOG_BYTES:
                    raise RuntimeError('The local engine exceeded its diagnostic byte budget.')
                if output is not None:
                    try:
                        info = Path(output).lstat()
                    except FileNotFoundError:
                        info = None
                    if info is not None and (not stat.S_ISREG(info.st_mode)
                            or info.st_size > MAX_PCM_BYTES + 4096):
                        raise ValueError('A speech segment exceeds the configured PCM budget or is not a regular file.')
                if status is not None:
                    break
                time.sleep(.03)
            if process.returncode:
                # Engine diagnostics can contain book text, local paths or keys.
                # The normal application error carries only a bounded exit status.
                raise EngineUnavailable(f'Local engine failed with exit code {process.returncode}.')
            stream = errors if return_stderr else outgoing
            stream.seek(0)
            result = stream.read(MAX_LOG_BYTES + 1)
            check()
            return result
        finally:
            _stop(process)


_SAPI_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($request.operation -eq 'voices') {
    $rows = @($synth.GetInstalledVoices() | Where-Object {$_.Enabled} | ForEach-Object {
      $info = $_.VoiceInfo
      [PSCustomObject]@{id=$info.Id;name=$info.Name;language=$info.Culture.Name;
        description=$info.Description;default=($info.Id -eq $synth.Voice.Id);
        version=[System.Speech.Synthesis.SpeechSynthesizer].Assembly.GetName().Version.ToString()}
    })
    [Console]::Write((ConvertTo-Json -InputObject $rows -Compress -Depth 4))
  } elseif ($request.operation -eq 'speak') {
    $selectedVoices = @($synth.GetInstalledVoices() | Where-Object {
      $_.Enabled -and $_.VoiceInfo.Id -ceq $request.voice_id -and
      $_.VoiceInfo.Name -ceq $request.voice
    })
    if ($selectedVoices.Count -ne 1) { throw 'Requested installed voice is unavailable' }
    $synth.SelectVoice($selectedVoices[0].VoiceInfo.Name)
    if ($synth.Voice.Id -cne $request.voice_id) { throw 'Selected installed voice changed' }
    $synth.Rate = [int]$request.rate
    $synth.Volume = [int]$request.volume
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
      24000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
      [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile([string]$request.output,$format)
    $synth.Speak([string]$request.text)
    $synth.SetOutputToNull()
    [Console]::Write((ConvertTo-Json -InputObject ([ordered]@{
      complete=$true; voice_id=$synth.Voice.Id
    }) -Compress))
  } else { throw 'Unsupported speech operation' }
} finally { $synth.Dispose() }
'''


class SAPI5Engine:
    def __init__(self, powershell=None, *, engine_id='sapi5', timeout=300):
        if powershell is None:
            if os.name != 'nt':
                raise EngineUnavailable('SAPI 5 requires Windows and installed local voices.')
            powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        self.binary = _binary(powershell)
        self.id = engine_id
        self.timeout = _timeout(timeout)
        self._records = {}
        self._default = None

    def _call(self, request, *, cancelled=lambda: False):
        command = [self.binary, '-NoLogo', '-NoProfile', '-NonInteractive', '-Sta', '-EncodedCommand',
                   base64.b64encode(_SAPI_SCRIPT.encode('utf-16-le')).decode('ascii')]
        return _run(command, data=json.dumps(request, ensure_ascii=True).encode('utf-8'),
                    cancelled=cancelled, timeout=self.timeout, env=offline_environment(),
                    output=request.get('output'))

    def voices(self, *, cancelled=lambda: False):
        _cancelled(cancelled)
        result = _sapi_json(self._call({'operation': 'voices'}, cancelled=cancelled))
        if not isinstance(result, list) or len(result) > 4096:
            raise EngineUnavailable('SAPI 5 returned an invalid voice catalogue.')
        voices = []
        records = {}
        default = None
        for row in result:
            _cancelled(cancelled)
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip()
                                               or len(row[k]) > 2048
                                               for k in ('id', 'name', 'language', 'version')):
                raise EngineUnavailable('SAPI 5 returned an invalid voice record.')
            if (type(row.get('default', False)) is not bool
                    or not re.fullmatch('[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', row['language'])
                    or any(ord(c) < 32 or 127 <= ord(c) < 160
                           for field in ('id', 'name', 'version') for c in row[field])):
                raise EngineUnavailable('SAPI 5 returned an invalid voice record.')
            try:
                for field in ('id', 'name', 'version'):
                    row[field].encode('utf-8')
            except UnicodeEncodeError as exc:
                raise EngineUnavailable('SAPI 5 returned an invalid voice record.') from exc
            key = self.id + ':' + hashlib.sha256(row['id'].encode('utf-8')).hexdigest()[:24]
            if key in records:
                raise EngineUnavailable('SAPI 5 returned duplicate voice identities.')
            records[key] = row
            if row.get('default') is True:
                if default is not None:
                    raise EngineUnavailable('SAPI 5 returned ambiguous default voices.')
                default = key
            voices.append(Voice(key, row['name'], (row['language'],), self.id,
                                revision=row['version']))
        self._records, self._default = records, default
        return tuple(voices)

    def _record(self, voice, *, cancelled=lambda: False):
        _cancelled(cancelled)
        if not self._records:
            self.voices(cancelled=cancelled)
        if voice == self.id + ':default':
            voice = self._default
        if voice not in self._records:
            raise EngineUnavailable('Choose an installed SAPI 5 voice from the voice list.')
        return self._records[voice]

    def cache_identity(self, voice, *, cancelled=lambda: False):
        row = self._record(voice, cancelled=cancelled)
        return {'adapter': ADAPTER_VERSION, 'engine': self.id,
                'voice': hashlib.sha256(row['id'].encode('utf-8')).hexdigest(),
                'version': row['version'], 'binary_sha256': digest_file(self.binary, cancelled=cancelled)}

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False):
        _request(text, language, rate, pitch, volume)
        if pitch != 0:
            raise EngineUnavailable('This SAPI 5 adapter has no native pitch control; use an audio effect.')
        if rate != int(rate):
            raise ValueError('SAPI 5 speech rate is an integer from -10 to 10.')
        row, target = self._record(voice, cancelled=cancelled), _output(output)
        if language.lower().replace('_', '-').split('-')[0] != row['language'].lower().split('-')[0]:
            raise EngineUnavailable('The selected SAPI 5 voice does not advertise this language; select a matching voice.')
        with tempfile.TemporaryDirectory(prefix='.speech-', dir=target.parent) as staging:
            temporary = Path(staging) / 'speech.wav'
            result = self._call({'operation': 'speak', 'voice': row['name'], 'voice_id': row['id'], 'text': text,
                                 'output': str(temporary), 'rate': int(rate),
                                 'volume': round(volume * 100)}, cancelled=cancelled)
            response = _sapi_json(result)
            if (not isinstance(response, dict) or set(response) != {'complete', 'voice_id'}
                    or response.get('complete') is not True or response.get('voice_id') != row['id']):
                raise EngineUnavailable('SAPI 5 did not confirm a completed render.')
            _wave_info(temporary, cancelled=cancelled)
            if cancelled():
                raise InterruptedError('Speech operation cancelled.')
            _publish(temporary, target, cancelled=cancelled)
        return target


class ESpeakEngine:
    """Optional installed eSpeak NG CLI; text is UTF-8 stdin, without SSML."""
    id = 'espeak-ng'

    def __init__(self, executable, *, timeout=300):
        self.binary, self.timeout = _binary(executable), _timeout(timeout)
        self._voices = {}

    def voices(self, *, cancelled=lambda: False):
        result = _run([self.binary, '--voices'], timeout=30, cancelled=cancelled).decode('utf-8', errors='strict')
        voices = {}
        for line in result.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 5:
                continue
            language, name, token = fields[1], fields[3], fields[4]
            if not re.fullmatch('[A-Za-z0-9_+./-]+', token) or token.startswith('-'):
                continue
            key = self.id + ':' + token
            if key in voices:
                continue
            record = Voice(key, name.replace('_', ' '), (language,), self.id,
                           controls=('rate', 'pitch', 'volume'), license='Installed eSpeak NG; GPL-3.0-or-later')
            voices[key] = record
        _cancelled(cancelled)
        self._voices = voices
        if not voices:
            raise EngineUnavailable('eSpeak NG has no installed voices.')
        return tuple(self._voices.values())

    def cache_identity(self, voice, *, cancelled=lambda: False):
        _cancelled(cancelled)
        if voice not in self._voices:
            self.voices(cancelled=cancelled)
        if voice not in self._voices:
            raise EngineUnavailable('Choose an installed eSpeak NG voice.')
        return {'adapter': ADAPTER_VERSION, 'engine': self.id, 'voice': voice,
                'binary_sha256': digest_file(self.binary, cancelled=cancelled)}

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False):
        _request(text, language, rate, pitch, volume)
        self.cache_identity(voice, cancelled=cancelled)
        if language.lower().replace('_', '-').split('-')[0] not in {
                value.lower().replace('_', '-').split('-')[0] for value in self._voices[voice].languages}:
            raise EngineUnavailable('The selected eSpeak NG voice does not advertise this language.')
        target = _output(output)
        with tempfile.TemporaryDirectory(prefix='.speech-', dir=target.parent) as staging:
            temporary = Path(staging) / 'speech.wav'
            command = [self.binary, '-v', voice.split(':', 1)[1], '-s', str(round(175 * 2 ** (rate / 10))),
                       '-p', str(round(50 + pitch * 3)), '-a', str(round(100 * volume)),
                       '-b', '1', '-w', str(temporary), '--stdin']
            _run(command, data=text.encode('utf-8'), timeout=self.timeout,
                 cancelled=cancelled, env=offline_environment(), output=temporary)
            _wave_info(temporary, cancelled=cancelled)
            if cancelled():
                raise InterruptedError('Speech operation cancelled.')
            _publish(temporary, target, cancelled=cancelled)
        return target


class FliteEngine:
    """Basic English voices supplied by an explicitly selected FFmpeg build.

    It is a working local CPU option, not a neural or multilingual substitute.
    The generated filter contains only validated voice names and fixed paths.
    """
    id = 'flite'

    def __init__(self, ffmpeg, *, timeout=300):
        self.binary, self.timeout = _binary(ffmpeg), _timeout(timeout)
        self._voices = {}

    def voices(self, *, cancelled=lambda: False):
        message = _run([self.binary, '-hide_banner', '-nostdin', '-f', 'lavfi',
                        '-i', 'flite=list_voices=1', '-f', 'null', '-'], timeout=15,
                       env=offline_environment(), return_stderr=True,
                       cancelled=cancelled).decode('utf-8', errors='replace')
        names = re.findall(r'^\[Parsed_flite_\d+ @ [^\]]+\]\s+([A-Za-z0-9_-]{1,80})\s*$',
                           message, re.MULTILINE)
        self._voices = {self.id + ':' + name: Voice(self.id + ':' + name, 'Flite ' + name, ('en',),
                        self.id, controls=('volume',), license='Installed Flite voice/FFmpeg build terms')
                        for name in names}
        if not self._voices:
            raise EngineUnavailable('This FFmpeg build has no usable libflite voices.')
        return tuple(self._voices.values())

    def cache_identity(self, voice, *, cancelled=lambda: False):
        _cancelled(cancelled)
        if not self._voices:
            self.voices(cancelled=cancelled)
        if voice not in self._voices:
            raise EngineUnavailable('Choose an installed Flite voice.')
        return {'adapter': ADAPTER_VERSION, 'engine': self.id, 'voice': voice,
                'binary_sha256': digest_file(self.binary, cancelled=cancelled),
                'build_sha256': hashlib.sha256(_run([self.binary, '-version'], timeout=15,
                                                    cancelled=cancelled)).hexdigest()}

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False):
        _request(text, language, rate, pitch, volume)
        if language.lower().replace('_', '-').split('-')[0] != 'en':
            raise EngineUnavailable('These Flite voices support English only; select another local engine for this language.')
        if rate or pitch:
            raise EngineUnavailable('This Flite adapter has no native rate or pitch control; use an audio effect.')
        self.cache_identity(voice, cancelled=cancelled)
        target = _output(output)
        with tempfile.TemporaryDirectory(prefix='.speech-', dir=target.parent) as staging:
            temporary = Path(staging) / 'speech.wav'
            (Path(staging) / 'input.txt').write_text(text, encoding='utf-8')
            command = [self.binary, '-hide_banner', '-loglevel', 'error', '-nostdin', '-f', 'lavfi',
                       '-i', 'flite=textfile=input.txt:voice=' + voice.split(':', 1)[1],
                       '-af', 'volume=' + format(volume, '.8g'), '-ar', '24000', '-ac', '1',
                       '-c:a', 'pcm_s16le', '-f', 'wav', '-n', str(temporary)]
            _run(command, timeout=self.timeout, cancelled=cancelled, env=offline_environment(),
                 cwd=staging, output=temporary)
            _wave_info(temporary, cancelled=cancelled)
            if cancelled():
                raise InterruptedError('Speech operation cancelled.')
            _publish(temporary, target, cancelled=cancelled)
        return target


class PiperEngine:
    """Explicit local Piper CLI/model profile, with a reviewed asset manifest."""
    id = 'piper'

    def __init__(self, executable, manifest, *, timeout=300):
        self.binary = _binary(executable)
        self.manifest = LocalModelManifest.load(manifest, engine='piper')
        self.timeout = _timeout(timeout)
        self.model = self.manifest.settings.get('model')
        self.config = self.manifest.settings.get('config')
        self.language = self.manifest.settings.get('language', 'en')
        self.name = self.manifest.settings.get('name', self.manifest.model_id)
        if not isinstance(self.model, str) or not self.model.endswith('.onnx'):
            raise ModelManifestError('Choose the manifest-listed Piper ONNX model.')
        if not isinstance(self.config, str) or not self.config.endswith('.json'):
            raise ModelManifestError('Choose the manifest-listed Piper model configuration.')
        self.manifest.asset(self.model)
        self.manifest.asset(self.config)
        self.voice_id = 'piper:' + self.manifest.manifest_sha256[:24]

    def voices(self, *, cancelled=lambda: False):
        _cancelled(cancelled)
        self.manifest.verify(cancelled=cancelled)
        return (Voice(self.voice_id, self.name, (self.language,), self.id,
                      license=self.manifest.license, revision=self.manifest.revision),)

    def cache_identity(self, voice, *, cancelled=lambda: False):
        _cancelled(cancelled)
        if voice != self.voice_id:
            raise EngineUnavailable('Choose the configured local Piper voice.')
        self.manifest.verify(cancelled=cancelled)
        return {**self.manifest.cache_identity(), 'adapter': ADAPTER_VERSION,
                'binary_sha256': digest_file(self.binary, cancelled=cancelled)}

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False):
        _request(text, language, rate, pitch, volume)
        self.cache_identity(voice, cancelled=cancelled)
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
                 env=offline_environment(), cwd=self.manifest.root, output=temporary)
            _wave_info(temporary, cancelled=cancelled)
            if cancelled():
                raise InterruptedError('Speech operation cancelled.')
            _publish(temporary, target, cancelled=cancelled)
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
        if settings.get('protocol') != 'nvda-worker-jsonl-pcm24k-v1':
            raise ModelManifestError('Unsupported dordiai-voices worker protocol.')
        self.binary = _binary(self.manifest.asset(settings.get('python', 'python/python.exe')))
        self.worker = self.manifest.asset(settings.get('worker', 'nvda_worker.py'))
        self.catalog_path = self.manifest.asset(settings.get('catalog', 'models/voices/catalog.json'))
        self.runtime_root = self.worker.parent
        self.models = local_path(self.runtime_root / 'models')
        self.references = local_path(self.models / 'voices')
        if self.catalog_path != self.references / 'catalog.json':
            raise ModelManifestError('The selected catalogue does not belong to this worker model directory.')
        for filename in REQUIRED_HELPERS:
            relative = (self.runtime_root / filename).relative_to(self.manifest.root).as_posix()
            self.manifest.asset(relative)
        self.timeout = _number(timeout, .01, 86400, 'Speech timeout')
        self._profiles = {}
        self._voice_languages = {}
        self._languages = ()

    def voices(self, *, cancelled=lambda: False):
        _cancelled(cancelled)
        self.manifest.verify(cancelled=cancelled, require_complete=True)
        bridge = Path(__file__).with_name('dordiai_discovery.py')
        command = [self.binary, '-I', '-B', '-u', bridge,
                   self.runtime_root, self.models, self.references]
        raw = _run(command, cancelled=cancelled, timeout=min(self.timeout, 30),
                   env=offline_environment(), cwd=self.runtime_root)
        try:
            discovered = parse_discovery(raw)
        except (ValueError, UnicodeError) as exc:
            raise ModelManifestError('Invalid installed dordiai-voices discovery response.') from exc
        if not discovered:
            raise EngineUnavailable('No installed dordiai-voices profile has compatible language resources. '
                                    'Install and verify the required engine and voice packs first.')
        rows = []
        records, voice_languages = {}, {}
        for key, name, language_codes in discovered:
            identity = self.id + ':' + key
            records[identity] = key
            voice_languages[identity] = language_codes
            rows.append(Voice(identity, name, language_codes, self.id,
                              controls=('rate', 'pitch', 'volume'), license=self.manifest.license,
                              revision=self.manifest.revision))
        self.manifest.verify(cancelled=cancelled, require_complete=True)
        self._profiles, self._voice_languages = records, voice_languages
        self._languages = tuple(dict.fromkeys(code for row in rows for code in row.languages))
        return tuple(rows)

    def cache_identity(self, voice, *, cancelled=lambda: False):
        _cancelled(cancelled)
        if not self._profiles:
            self.voices(cancelled=cancelled)
        if voice not in self._profiles:
            raise EngineUnavailable('Choose an enabled profile from the installed dordiai-voices catalogue.')
        self.manifest.verify(require_complete=True, cancelled=cancelled)
        return {**self.manifest.cache_identity(), 'adapter': ADAPTER_VERSION, 'voice': voice,
                'protocol': 'nvda-worker-jsonl-pcm24k-v1', 'discovery': DISCOVERY_PROTOCOL}

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False):
        _request(text, language, rate, pitch, volume)
        self.cache_identity(voice, cancelled=cancelled)
        code = language.lower().replace('_', '-').split('-')[0]
        if code not in self._voice_languages[voice]:
            raise EngineUnavailable('This dordiai-voices profile has no compatible installed resources '
                                    'for the selected language.')
        self.manifest.verify(cancelled=cancelled, require_complete=True)
        target = _output(output)
        # Arguments are the versioned installed worker's fixed interface, not a
        # command supplied by a script, book, scene or model response.
        command = [str(self.binary), '-I', '-B', '-u', '-c', _DORDIAI_OFFLINE_BOOTSTRAP,
                   str(self.worker), str(self.models),
                   '--reference-model', str(self.models / 'Holos'),
                   '--references', str(self.references), '--reference-engine', 'holos',
                   '--preload-language', 'mixed']
        request = {'mode': 'reference', 'profile': self._profiles[voice], 'language': code,
                   'text': text, 'speed': 2 ** (rate / 10), 'stream': True,
                   'controls': {'pitch': round(50 + pitch * 3), 'timbre': 50, 'volume': round(volume * 100)},
                   'numberLanguage': 'auto', 'character': False, 'nativeProfile': code in {'en', 'ru'},
                   'nativeEnglish': False}
        if code in {'sk', 'pl', 'de', 'fr', 'da', 'it', 'es'}:
            request['nativeLocale'] = True
        elif code not in {'uk', 'en', 'ru'}:
            request.update(wideLocale=True, reportLoading=True)
        with tempfile.TemporaryDirectory(prefix='.speech-', dir=target.parent) as staging:
            temporary = Path(staging) / 'speech.wav'
            self._stream(command, request, temporary, cancelled)
            _wave_info(temporary, cancelled=cancelled)
            if cancelled():
                raise InterruptedError('Speech operation cancelled.')
            _publish(temporary, target, cancelled=cancelled)
        return target

    def _stream(self, command, request, target, cancelled):
        _cancelled(cancelled)
        _timeout(self.timeout)
        replies = queue.Queue(maxsize=2)
        outgoing = queue.Queue(maxsize=2)
        write_errors = queue.Queue(maxsize=1)
        finished = threading.Event()
        options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {'start_new_session': True}
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                       cwd=getattr(self, 'runtime_root', self.manifest.root),
                                       env=offline_environment(), shell=False, **options)
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
                                item = json.loads(line, object_pairs_hook=_unique_object)
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
            def write_requests():
                while not finished.is_set():
                    try:
                        data = outgoing.get(timeout=.05)
                    except queue.Empty:
                        continue
                    try:
                        process.stdin.write(data)
                        process.stdin.flush()
                    except (OSError, ValueError):
                        try:
                            write_errors.put_nowait(EngineUnavailable('dordiai-voices request stream failed.'))
                        except queue.Full:
                            pass
                        return
            writer = threading.Thread(target=write_requests, daemon=True, name='dordiai-voices request writer')
            writer.start()
            def send(value):
                data = (json.dumps(value, ensure_ascii=True) + '\n').encode('ascii')
                if len(data) > 128 * 1024:
                    raise ValueError('dordiai-voices request exceeds its byte budget.')
                try:
                    outgoing.put_nowait(data)
                except queue.Full as exc:
                    raise EngineUnavailable('dordiai-voices is not consuming bounded requests.') from exc
            started = time.monotonic()
            total = 0
            try:
                send(request)
                with wave.open(str(target), 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    while True:
                        try:
                            write_error = write_errors.get_nowait()
                        except queue.Empty:
                            write_error = None
                        if write_error is not None:
                            raise write_error
                        if cancelled():
                            try:
                                send({'mode': 'cancel'})
                                send({'mode': 'drop'})
                            except (OSError, EngineUnavailable):
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
                        if total > MAX_PCM_BYTES:
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
                writer.join(timeout=1)
                if process.stdin and not writer.is_alive():
                    process.stdin.close()
                reader.join(timeout=1)
                if process.stdout and not reader.is_alive():
                    process.stdout.close()


class VoiceRegistry:
    def __init__(self, engines=(), *, notices=()):
        self.engines = {}
        for engine in engines:
            if engine.id in self.engines:
                raise ValueError('An engine identifier is already registered.')
            self.engines[engine.id] = engine
        self.notices = list(notices)
        self._voices = None

    def voices(self, *, refresh=False, cancelled=lambda: False):
        _cancelled(cancelled)
        if self._voices is not None and not refresh:
            return self._voices
        result = []
        for engine in self.engines.values():
            try:
                result.extend(_invoke(engine.voices, cancelled=cancelled))
            except InterruptedError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError):
                self.notices.append({'engine': engine.id, 'available': False,
                                     'reason': 'The configured local engine could not enumerate usable voices.'})
        self._voices = tuple(result)
        return self._voices

    def capabilities(self):
        voices = self.voices()
        return tuple([{'engine': identity, 'available': any(v.engine == identity for v in voices),
                       'voices': sum(v.engine == identity for v in voices)} for identity in self.engines] + self.notices)

    def _engine(self, voice):
        if not isinstance(voice, str) or ':' not in voice:
            raise ValueError('Choose a local voice identifier from the voice registry.')
        key = voice.split(':', 1)[0]
        if key not in self.engines:
            raise EngineUnavailable(f'The local {key} engine is not installed or configured. No substitute was used.')
        return self.engines[key]

    def cache_identity(self, voice, *, cancelled=lambda: False):
        return _invoke(self._engine(voice).cache_identity, voice, cancelled=cancelled)

    def synthesize(self, text, output, *, voice, language='en', rate=0, pitch=0, volume=1,
                   cancelled=lambda: False):
        _cancelled(cancelled)
        _request(text, language, rate, pitch, volume)
        return self._engine(voice).synthesize(text, output, voice=voice, language=language, rate=rate,
                                             pitch=pitch, volume=volume, cancelled=cancelled)


def default_registry(*, dordiai_manifest=None, piper_executable=None, piper_manifest=None, ffmpeg=None):
    engines, notices = [], []
    if os.name == 'nt':
        root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        for identity, directory in (('sapi5', 'System32'), ('sapi5-32', 'SysWOW64')):
            binary = root / directory / 'WindowsPowerShell/v1.0/powershell.exe'
            if binary.is_file():
                engines.append(SAPI5Engine(binary, engine_id=identity))
    else:
        notices.append({'engine': 'sapi5', 'available': False, 'reason': 'SAPI 5 requires Windows.'})
    espeak = shutil.which('espeak-ng')
    if espeak:
        engines.append(ESpeakEngine(Path(espeak).resolve()))
    else:
        notices.append({'engine': 'espeak-ng', 'available': False, 'reason': 'eSpeak NG is not installed.'})
    selected_ffmpeg = ffmpeg or shutil.which('ffmpeg')
    if selected_ffmpeg:
        engines.append(FliteEngine(Path(selected_ffmpeg).resolve()))
    if dordiai_manifest is not None:
        engines.append(DordiaiVoicesEngine(dordiai_manifest))
    else:
        notices.append({'engine': 'dordiai-voices', 'available': False,
                        'reason': 'Select a verified installed dordiai-voices manifest; voices are never bundled automatically.'})
    if piper_manifest is not None or piper_executable is not None:
        if piper_manifest is None or piper_executable is None:
            raise ValueError('Piper needs both a selected local executable and a verified model manifest.')
        engines.append(PiperEngine(piper_executable, piper_manifest))
    else:
        notices.append({'engine': 'piper', 'available': False, 'reason': 'No verified local Piper voice is configured.'})
    notices.append({'engine': 'sapi4', 'available': False,
                    'reason': 'Legacy SAPI 4 requires a separately validated 32-bit helper; it is not yet supported.'})
    return VoiceRegistry(engines, notices=notices)
