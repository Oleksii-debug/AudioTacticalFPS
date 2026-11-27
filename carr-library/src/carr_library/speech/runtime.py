"""Installed local voices with measured PCM output and truthful capabilities.

SAPI, eSpeak, Piper and FFmpeg are invoked with fixed argument vectors. Text is
data, never a shell command, SSML or a user-supplied FFmpeg filter graph.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import locale
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import sysconfig
from tempfile import TemporaryDirectory
import wave

from ._io import (check_cancelled, digest, local_file, new_output, run_process,
                  system_powershell, text_value, windows_directory)


@dataclass(frozen=True)
class SpeechSettings:
    rate: int = 0
    volume: int = 100
    pitch: int | None = None
    punctuation: bool = False
    timeout_seconds: float = 180
    max_output_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        if type(self.rate) is not int or not -10 <= self.rate <= 10:
            raise ValueError('Speech rate must be an integer between -10 and 10.')
        if type(self.volume) is not int or not 0 <= self.volume <= 100:
            raise ValueError('Speech volume must be an integer from 0 to 100.')
        if self.pitch is not None and (type(self.pitch) is not int or not 0 <= self.pitch <= 99):
            raise ValueError('Supported native voice pitch must be from 0 to 99.')
        if type(self.punctuation) is not bool:
            raise ValueError('Punctuation reading must be a boolean.')
        if (type(self.timeout_seconds) not in (float, int) or not math.isfinite(self.timeout_seconds)
                or not 1 <= self.timeout_seconds <= 86400):
            raise ValueError('Set a speech-chunk timeout from 1 to 86400 seconds.')
        if type(self.max_output_bytes) is not int or not 4096 <= self.max_output_bytes < 4_000_000_000:
            raise ValueError('Set a WAV output budget from 4096 to 3999999999 bytes; split longer jobs.')


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    engine: str
    language: str
    capabilities: tuple[str, ...] = ('rate', 'volume')
    vendor: str = ''
    license: str = 'Installed voice; its separate voice license applies.'
    timing: str = 'Utterance duration measured; word timings unavailable.'

    def __post_init__(self):
        for name in ('id', 'name', 'engine', 'language', 'license', 'timing'):
            text_value(getattr(self, name), name, maximum=2048)
        text_value(self.vendor, 'vendor', maximum=512, empty=True)
        if (not isinstance(self.capabilities, tuple)
                or any(item not in ('rate', 'volume', 'pitch', 'punctuation', 'phonemes')
                       for item in self.capabilities)):
            raise ValueError('Invalid voice capabilities.')


@dataclass(frozen=True)
class SpeechResult:
    path: Path
    sha256: str
    duration_seconds: float
    sample_rate: int
    channels: int
    frames: int
    voice_id: str
    text_sha256: str
    chunks: int


def _wave_info(path):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getcomptype() != 'NONE' or audio.getsampwidth() not in (1, 2, 3, 4)
                or audio.getnchannels() not in (1, 2) or not 8000 <= audio.getframerate() <= 192000
                or audio.getnframes() <= 0):
            raise ValueError('The voice did not produce supported, nonempty PCM audio.')
        rate, channels, frames, width = (audio.getframerate(), audio.getnchannels(),
                                       audio.getnframes(), audio.getsampwidth())
        total = 0
        while block := audio.readframes(65536):
            total += len(block)
        if total != frames * channels * width:
            raise ValueError('The voice produced truncated audio.')
        return rate, channels, frames, width


def split_utterances(text, *, maximum=2400):
    """Bound each native call while retaining the exact source-character stream."""
    text_value(text)
    if type(maximum) is not int or maximum < 64:
        raise ValueError('A speech chunk must allow at least 64 characters.')
    start = 0
    while start < len(text):
        end = min(start + maximum, len(text))
        if end < len(text):
            matches = list(re.finditer(r'[.!?。！？][\s]+|\n\s*\n|\s+', text[start:end]))
            if matches:
                candidate = start + matches[-1].end()
                if candidate - start >= maximum // 2:
                    end = candidate
        yield text[start:end]
        start = end


class VoiceRegistry:
    """A registry only of actually enumerated voices; no imaginary catalog."""
    def __init__(self, providers=()):
        self._providers = list(providers)
        self._voices = {}
        self._owners = {}
        self.notices = []
        self.reload()

    @classmethod
    def discover(cls, *, include_flite=True, ffmpeg=None, include_powershell=False, dordiai=None):
        providers = []
        if os.name == 'nt':
            providers.append(NativeSapiProvider())
            if include_powershell:
                base = windows_directory()
                for architecture, folder in (('64', 'System32'), ('32', 'SysWOW64')):
                    executable = base / folder / 'WindowsPowerShell/v1.0/powershell.exe'
                    if executable.is_file():
                        providers.append(SapiProvider(executable, architecture=architecture))
        espeak = shutil.which('espeak-ng') or shutil.which('espeak')
        if espeak:
            providers.append(EspeakProvider(espeak))
        flite_executable = ffmpeg or shutil.which('ffmpeg')
        if include_flite and flite_executable:
            providers.append(FliteProvider(flite_executable))
        registry = cls(providers)
        try:
            if dordiai is not None:
                from ..integrations.dordiai_speech import install_dordiai_provider
                install_dordiai_provider(registry, dordiai)
            return registry
        except BaseException:
            registry.close()
            raise

    def reload(self):
        entries, owners, notices = {}, {}, []
        for provider in self._providers:
            try:
                values = provider.voices()
                if len(values) > 10000:
                    raise ValueError('The voice provider returned too many voices.')
                candidate = {}
                for voice in values:
                    if not isinstance(voice, Voice) or voice.id in entries or voice.id in candidate:
                        raise ValueError('Duplicate or invalid local voice identity.')
                    candidate[voice.id] = voice
                entries.update(candidate)
                owners.update({key: provider for key in candidate})
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                notices.append(f'{provider.name}: {exc}')
        self._voices, self._owners, self.notices = entries, owners, notices
        return self.voices()

    def add_provider(self, provider):
        self._providers.append(provider)
        return self.reload()

    def voices(self):
        return tuple(self._voices.values())

    def close(self):
        """Release every owned local worker, including a provider that failed discovery."""
        providers, self._providers = self._providers, []
        self._voices, self._owners = {}, {}
        errors = []
        for provider in providers:
            close = getattr(provider, 'close', None)
            if callable(close):
                try:
                    close()
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    errors.append(f'{provider.name}: {exc}')
        self.notices.extend(errors)

    def get(self, voice_id):
        try:
            return self._voices[voice_id]
        except (KeyError, TypeError) as exc:
            raise ValueError('Select an available installed voice and reload if it was removed.') from exc

    def fingerprint(self, voice_id):
        """Fingerprint visible installed runtime/voice assets for render caches.

        SAPI exposes an opaque token, not every proprietary driver's model file.
        Its identity remains a token/host fingerprint and is described as such;
        it is never represented as proof of a third-party model revision.
        """
        voice = self.get(voice_id)
        provider = self._owners[voice_id]
        identity = {'voice': asdict(voice), 'executable_sha256': digest(provider.executable)}
        if isinstance(provider, PiperProvider):
            model, config, _, _ = provider._models[voice_id]
            identity.update(model_sha256=digest(local_file(model)), config_sha256=digest(local_file(config)))
        elif isinstance(provider, LocalHelperProvider):
            provider._verify()
            identity['helper_contract'] = 'carr.local-voice.v1'
        elif isinstance(provider, SapiProvider):
            identity.update(token=provider._tokens[voice_id], architecture=provider.architecture,
                            model_identity='opaque-installed-SAPI-token')
            if isinstance(provider, NativeSapiProvider):
                from importlib.metadata import PackageNotFoundError, version
                try:
                    client_version = version('comtypes')
                except PackageNotFoundError:
                    client_version = 'unavailable'
                worker = Path(__file__).with_name('sapi_native_worker.py')
                identity.update(native_worker_sha256=digest(worker) if worker.is_file() else None,
                                native_protocol='carr.sapi-native.v1',
                                com_client='comtypes', com_client_version=client_version)
        elif os.name != 'nt' and isinstance(provider, (FliteProvider, EspeakProvider)):
            # Debian/Ubuntu native voice libraries contain the Flite model data;
            # include their actual bytes, not only the thin FFmpeg executable.
            prefix = 'libflite' if isinstance(provider, FliteProvider) else 'libespeak'
            architecture = sysconfig.get_config_var('MULTIARCH')
            directories = [Path('/usr/lib'), Path('/lib')]
            if architecture:
                directories += [Path('/usr/lib') / architecture, Path('/lib') / architecture]
            assets = {}
            for directory in directories:
                for path in directory.glob(prefix + '*.so*'):
                    actual = path.resolve(strict=True)
                    if actual.is_file():
                        assets[str(actual)] = digest(actual)
            identity['installed_voice_libraries'] = assets
        provider_fingerprint = getattr(provider, 'fingerprint', None)
        if callable(provider_fingerprint):
            identity['provider_fingerprint'] = provider_fingerprint(voice_id)
        return sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    def synthesize(self, text, voice_id, destination, *, settings=None, cancelled=lambda: False):
        text_value(text)
        options = settings or SpeechSettings()
        if not isinstance(options, SpeechSettings):
            raise ValueError('Validated SpeechSettings are required.')
        voice = self.get(voice_id)
        if options.pitch is not None and 'pitch' not in voice.capabilities:
            raise ValueError('This voice does not expose native pitch control.')
        if options.punctuation and 'punctuation' not in voice.capabilities:
            raise ValueError('This voice does not expose punctuation reading.')
        if options.rate and 'rate' not in voice.capabilities:
            raise ValueError('This voice does not expose synthesis rate control.')
        if options.volume != 100 and 'volume' not in voice.capabilities:
            raise ValueError('This voice does not expose synthesis volume control.')
        provider = self._owners[voice_id]
        count, frames, params = 0, 0, None
        target = Path(destination).absolute()
        with new_output(target, cancelled=cancelled) as staged:
            with wave.open(str(staged), 'wb') as combined:
                # A first-chunk cancellation must preserve its original error,
                # not be replaced by Wave_write.close complaining about unset
                # parameters. These are replaced before the first actual frame.
                combined.setnchannels(1)
                combined.setsampwidth(2)
                combined.setframerate(22050)
                for chunk in split_utterances(text):
                    check_cancelled(cancelled)
                    if not chunk.strip():
                        continue
                    part = staged.parent / 'utterance.wav'
                    try:
                        provider.render(chunk, voice, part, options, cancelled)
                        rate, channels, size, width = _wave_info(part)
                        if params is None:
                            params = rate, channels, width
                            combined.setframerate(rate)
                            combined.setnchannels(channels)
                            combined.setsampwidth(width)
                        elif params != (rate, channels, width):
                            raise ValueError('The voice changed audio format between chunks.')
                        if (frames + size) * channels * width + 128 > options.max_output_bytes:
                            raise ValueError('Speech exceeds the selected audio budget. Split it into chapters.')
                        with wave.open(str(part), 'rb') as segment:
                            while block := segment.readframes(65536):
                                check_cancelled(cancelled)
                                combined.writeframesraw(block)
                        frames += size
                        count += 1
                    finally:
                        part.unlink(missing_ok=True)
            rate, channels, frames, _ = _wave_info(staged)
            checksum = digest(staged)
        return SpeechResult(target, checksum, frames / rate, rate, channels, frames,
                            voice_id, sha256(text.encode('utf-8')).hexdigest(), count)


class SapiProvider:
    name = 'Installed SAPI 5'

    def __init__(self, executable, *, architecture='64'):
        if architecture not in ('32', '64'):
            raise ValueError('SAPI host architecture must be 32 or 64.')
        self.executable = (system_powershell(executable, architecture=architecture)
                           if os.name == 'nt' else local_file(executable))
        self.architecture = architecture
        self._tokens = {}

    def _request(self, data, *, cancelled=lambda: False, timeout=30, output=None, budget=512*1024*1024):
        if os.name != 'nt':
            raise RuntimeError('SAPI 5 requires a native Windows host.')
        helper = Path(__file__).with_name('sapi_worker.ps1')
        with TemporaryDirectory(prefix='research-sapi-') as temporary:
            request = Path(temporary) / 'request.json'
            request.write_text(json.dumps({'version': 1, **data}, ensure_ascii=False), encoding='utf-8')
            reply, _, _ = run_process([self.executable, '-NoLogo', '-NoProfile', '-NonInteractive',
                '-File', helper, '-Request', request], cancelled=cancelled, timeout=timeout,
                output_file=output, max_output=budget)
        value = json.loads(reply.decode('utf-8-sig'))
        if not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError('Invalid SAPI response.')
        return value

    def voices(self):
        result = self._request({'operation': 'voices'})
        if not isinstance(result.get('voices'), list):
            raise ValueError('Invalid SAPI voice list.')
        voices, tokens = [], {}
        for row in result['voices']:
            if not isinstance(row, dict):
                raise ValueError('Invalid SAPI voice record.')
            token = text_value(row.get('token'), 'SAPI token', maximum=4096)
            identifier = f'sapi5-{self.architecture}:' + sha256(token.encode('utf-8')).hexdigest()[:24]
            language = row.get('language') or 'und'
            try:
                language = locale.windows_locale[int(language.split(';')[0], 16)].replace('_', '-')
            except (ValueError, KeyError, AttributeError):
                language = 'und'
            voices.append(Voice(identifier, row['name'], f'SAPI 5 ({self.architecture}-bit)', language,
                                ('rate', 'volume', 'punctuation'), vendor=row.get('vendor') or ''))
            tokens[identifier] = token
        self._tokens = tokens
        return tuple(voices)

    def render(self, text, voice, output, settings, cancelled):
        response = self._request({'operation': 'render', 'token': self._tokens[voice.id], 'text': text,
            'output': str(output), 'rate': settings.rate, 'volume': settings.volume,
            'punctuation': settings.punctuation}, cancelled=cancelled,
            timeout=settings.timeout_seconds, output=output, budget=settings.max_output_bytes)
        if response.get('status') != 'rendered':
            raise ValueError('SAPI did not report completion.')


class NativeSapiProvider(SapiProvider):
    """Installed SAPI through a bounded local Python COM worker, no .NET host."""
    name = 'Installed SAPI 5 (native COM)'

    def __init__(self):
        self.executable = Path(sys.executable).resolve(strict=True)
        self.architecture = str(struct.calcsize('P') * 8)
        self._tokens = {}

    def _request(self, data, *, cancelled=lambda: False, timeout=15, output=None, budget=512*1024*1024):
        if os.name != 'nt':
            raise RuntimeError('Native SAPI 5 requires a Windows host.')
        import importlib.util
        if importlib.util.find_spec('comtypes') is None:
            raise RuntimeError('Native SAPI requires the packaged comtypes 1.5.0 dependency.')
        parent = Path(output).parent if output is not None else None
        with TemporaryDirectory(prefix='research-native-sapi-', dir=parent) as temporary:
            temporary = Path(temporary)
            request, reply = temporary / 'request.json', temporary / 'reply.json'
            payload = {'version': 1, **data}
            if output is not None:
                payload['output'] = str(temporary / 'speech.wav')
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(raw) > 128_000:
                raise ValueError('The native speech request exceeds its size budget.')
            request.write_bytes(raw)
            helper = Path(__file__).with_name('sapi_native_worker.py')
            command = ([self.executable, '--carr-sapi-native-worker', request, reply]
                       if getattr(sys, 'frozen', False) else [self.executable, '-I', helper, request, reply])
            _, _, returncode = run_process(command, cancelled=cancelled, timeout=timeout,
                output_file=temporary / 'speech.wav' if output is not None else reply,
                max_output=budget if output is not None else 2_000_000, allow_failure=True)
            if not reply.is_file() or reply.stat().st_size > 2_000_000:
                raise RuntimeError('The native SAPI worker did not return a bounded response.')
            value = json.loads(reply.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('version') != 1:
                raise ValueError('Invalid native SAPI response.')
            if returncode or value.get('status') == 'failed':
                phase = value.get('phase')
                phases = {'validating-request', 'loading-com-client', 'creating-speaker', 'enumerating-voices',
                          'selecting-voice', 'creating-audio-stream', 'opening-audio-stream', 'synthesizing', 'closing-audio-stream'}
                phase = phase if phase in phases else 'native operation'
                code = value.get('hresult')
                diagnostic = f' (HRESULT 0x{code:08X})' if type(code) is int and 0 <= code <= 0xffffffff else ''
                raise RuntimeError('Native SAPI failed during ' + phase + diagnostic + '.')
            if value.get('architecture') != self.architecture or value.get('adapter') != 'native-comtypes':
                raise ValueError('Native SAPI replied from an unexpected host architecture or adapter.')
            if output is not None:
                check_cancelled(cancelled)
                _wave_info(temporary / 'speech.wav')
                # Same-filesystem, no-overwrite publication into the parent's
                # already private chunk staging area; final WAV stays atomic.
                os.link(temporary / 'speech.wav', output)
            return value


class EspeakProvider:
    name = 'Installed eSpeak'

    def __init__(self, executable):
        self.executable = Path(executable).resolve(strict=True)
        local_file(self.executable)
        self._names = {}

    def voices(self):
        output, _, _ = run_process([self.executable, '--voices'], timeout=20)
        voices, names = [], {}
        for line in output.decode('utf-8', errors='replace').splitlines()[1:]:
            parts = line.split()
            if len(parts) < 5 or not re.fullmatch(r'[A-Za-z0-9_+/.\-]+', parts[4]):
                continue
            language, name, token = parts[1], parts[3], parts[4]
            identifier = 'espeak:' + token
            voices.append(Voice(identifier, name.replace('_', ' '), 'eSpeak', language,
                                ('rate', 'volume', 'pitch', 'punctuation'), vendor='eSpeak project',
                                license='Installed eSpeak and voice data licenses apply.'))
            names[identifier] = token
        self._names = names
        return tuple(voices)

    def render(self, text, voice, output, settings, cancelled):
        args = [self.executable, '-b', '1', '-v', self._names[voice.id], '-w', str(output),
                '-s', str(round(175 * (2 ** (settings.rate / 10)))), '-a', str(settings.volume)]
        if settings.pitch is not None:
            args.extend(['-p', str(settings.pitch)])
        if settings.punctuation:
            args.append('--punct')
        args.append('--stdin')
        run_process(args, input_bytes=text.encode('utf-8'), cancelled=cancelled,
                    timeout=settings.timeout_seconds, output_file=output, max_output=settings.max_output_bytes)


class FliteProvider:
    """Actually enumerated English CMU voices in a locally installed FFmpeg."""
    name = 'FFmpeg Flite'

    def __init__(self, executable):
        self.executable = Path(executable).resolve(strict=True)
        local_file(self.executable)
        self._names = {}

    def voices(self):
        _, diagnostics, _ = run_process([self.executable, '-hide_banner', '-nostdin', '-f', 'lavfi',
            '-i', 'flite=list_voices=1', '-f', 'null', '-'], timeout=20, allow_failure=True)
        names = re.findall(r'\[Parsed_flite_\d+[^\]]*\]\s+(awb|kal|kal16|rms|slt)\s*(?:\r?\n|$)',
                           diagnostics.decode('utf-8', errors='replace'))
        self._names = {'flite:' + name: name for name in names}
        return tuple(Voice(identifier, f'Flite {name} (English)', 'FFmpeg Flite', 'en',
                           ('volume',), vendor='CMU Flite',
                           license='Installed FFmpeg build and CMU Flite voice licenses apply.')
                     for identifier, name in self._names.items())

    def render(self, text, voice, output, settings, cancelled):
        # No document byte or external path enters the filter expression.
        # A fixed relative textfile in a private worker directory avoids FFmpeg
        # filter escaping ambiguities for Windows paths and hostile filenames.
        with TemporaryDirectory(prefix='research-flite-') as temporary:
            Path(temporary, 'speech.txt').write_text(text, encoding='utf-8')
            run_process([self.executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                '-f', 'lavfi', '-i', 'flite=textfile=speech.txt:voice=' + self._names[voice.id],
                '-af', 'volume=' + str(settings.volume / 100), '-ac', '1', '-ar', '22050',
                '-c:a', 'pcm_s16le', '-f', 'wav', str(output)], cwd=temporary,
                cancelled=cancelled, timeout=settings.timeout_seconds, output_file=output,
                max_output=settings.max_output_bytes)


class PiperProvider:
    """Explicit preinstalled Piper CLI plus hashed local voice files.

    Voice cards and rights are supplied by the user; no weight acquisition or
    automatic use of a downloaded voice name occurs at runtime.
    """
    name = 'Installed Piper'

    def __init__(self, executable, models):
        self.executable = Path(executable).resolve(strict=True)
        local_file(self.executable)
        self._models = {}
        self._voices = []
        for row in models:
            if not isinstance(row, dict):
                raise ValueError('A Piper voice manifest must be an object.')
            model = local_file(row['path'])
            config = local_file(row.get('config', str(model) + '.json'))
            for path, expected in ((model, row.get('sha256')), (config, row.get('config_sha256'))):
                if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected) or digest(path) != expected:
                    raise ValueError('Piper voice/config hash verification failed.')
            license_text = text_value(row.get('license'), 'voice license', maximum=2048)
            identifier = 'piper:' + row['sha256']
            if identifier in self._models:
                raise ValueError('Duplicate Piper model.')
            self._models[identifier] = (model, config, row['sha256'], row['config_sha256'])
            self._voices.append(Voice(identifier, row['name'], 'Piper', row['language'],
                                      ('rate', 'volume'), vendor='User-installed voice', license=license_text))

    def voices(self):
        return tuple(self._voices)

    def render(self, text, voice, output, settings, cancelled):
        model, config, model_hash, config_hash = self._models[voice.id]
        if digest(local_file(model)) != model_hash or digest(local_file(config)) != config_hash:
            raise ValueError('The installed Piper model changed; review and register it again.')
        run_process([self.executable, '--model', str(model), '--config', str(config),
                     '--output-file', str(output), '--length-scale', str(2 ** (-settings.rate / 10)),
                     '--volume', str(settings.volume / 100)], input_bytes=(text + '\n').encode('utf-8'),
                    cancelled=cancelled, timeout=settings.timeout_seconds, output_file=output,
                    max_output=settings.max_output_bytes)


class LocalHelperProvider:
    """Explicitly trusted local adapter contract, including future dordiai-voices.

    This implements our JSON v1 contract, not an assertion about a private
    vendor's unknown API. A helper is executable code and must be reviewed and
    explicitly enabled; executable trust does not follow from document data.
    """
    name = 'Opt-in local voice helper'

    def __init__(self, executable, *, sha256, engine_name, enabled=False, max_voices=1024):
        if enabled is not True:
            raise ValueError('Explicitly enable a reviewed local voice helper before running it.')
        self.executable = local_file(executable)
        self.expected_hash = sha256
        self.engine_name = text_value(engine_name, 'engine name', maximum=128)
        if type(max_voices) is not int or not 1 <= max_voices <= 10000:
            raise ValueError('Set a voice catalog limit from 1 to 10000.')
        self.max_voices = max_voices
        self._tokens = {}
        self._verify()

    def _verify(self):
        if (not isinstance(self.expected_hash, str) or not re.fullmatch('[0-9a-f]{64}', self.expected_hash)
                or digest(local_file(self.executable)) != self.expected_hash):
            raise ValueError('Local voice helper hash verification failed.')

    def _request(self, payload, *, cancelled=lambda: False, timeout=30, output=None, budget=512*1024*1024):
        self._verify()
        raw = json.dumps({'schema': 'carr.local-voice.v1', **payload}, ensure_ascii=False).encode('utf-8')
        reply, _, _ = run_process([self.executable], input_bytes=raw, cancelled=cancelled,
                                 timeout=timeout, output_file=output, max_output=budget)
        data = json.loads(reply.decode('utf-8'))
        if not isinstance(data, dict) or data.get('schema') != 'carr.local-voice.v1':
            raise ValueError('Unsupported local voice-helper response.')
        return data

    def voices(self):
        data = self._request({'operation': 'voices'})
        rows = data.get('voices')
        if not isinstance(rows, list) or len(rows) > self.max_voices:
            raise ValueError('Invalid local voice catalog size.')
        result, tokens = [], {}
        prefix = sha256(self.engine_name.encode('utf-8')).hexdigest()[:12]
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Invalid local voice record.')
            token = text_value(row.get('id'), 'voice id', maximum=512)
            identifier = f'helper:{prefix}:' + sha256(token.encode('utf-8')).hexdigest()[:24]
            if identifier in tokens:
                raise ValueError('The helper returned duplicate voice IDs.')
            result.append(Voice(identifier, row['name'], self.engine_name, row['language'],
                                tuple(row.get('capabilities', ())), license=row['license']))
            tokens[identifier] = token
        self._tokens = tokens
        return tuple(result)

    def render(self, text, voice, output, settings, cancelled):
        response = self._request({'operation': 'render', 'voice_id': self._tokens[voice.id],
            'text': text, 'output': str(output), 'settings': asdict(settings)}, cancelled=cancelled,
            timeout=settings.timeout_seconds, output=output, budget=settings.max_output_bytes)
        if response.get('status') != 'rendered':
            raise ValueError('The local voice helper did not report completion.')


def synthesize(text, voice_id, destination, *, settings=None, cancelled=lambda: False, registry=None):
    return (registry or VoiceRegistry.discover()).synthesize(text, voice_id, destination,
                                                           settings=settings, cancelled=cancelled)
