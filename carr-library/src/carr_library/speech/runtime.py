"""Installed local voices with measured PCM output and truthful capabilities.

SAPI, eSpeak, Piper and FFmpeg are invoked with fixed argument vectors. Text is
data, never a shell command, SSML or a user-supplied FFmpeg filter graph.
"""
from dataclasses import asdict, dataclass, replace
from contextlib import nullcontext
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
from time import monotonic
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


def _wave_info(path, *, cancelled=lambda: False, max_output_bytes=None):
    check_cancelled(cancelled)
    if max_output_bytes is not None and Path(path).stat().st_size > max_output_bytes:
        raise ValueError('Speech exceeds the selected audio-output budget.')
    with wave.open(str(path), 'rb') as audio:
        if (audio.getcomptype() != 'NONE' or audio.getsampwidth() not in (1, 2, 3, 4)
                or audio.getnchannels() not in (1, 2) or not 8000 <= audio.getframerate() <= 192000
                or audio.getnframes() <= 0):
            raise ValueError('The voice did not produce supported, nonempty PCM audio.')
        rate, channels, frames, width = (audio.getframerate(), audio.getnchannels(),
                                       audio.getnframes(), audio.getsampwidth())
        total = 0
        while block := audio.readframes(65536):
            check_cancelled(cancelled)
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


class _CancellableProvider:
    cancellable_discovery = True
    """Optional registry protocol; older custom providers retain voices()."""
    def discover_voices(self, *, cancelled=lambda: False):
        return self.voices(cancelled=cancelled)


class VoiceRegistry:
    """A registry only of actually enumerated voices; no imaginary catalog."""
    cancellable_fingerprint = True
    supports_expected_fingerprint = True

    def __init__(self, providers=(), *, cancelled=lambda: False, timeout_seconds=60):
        self._providers = list(providers)
        self._voices = {}
        self._owners = {}
        self.notices = []
        try:
            self.reload(cancelled=cancelled, timeout_seconds=timeout_seconds)
        except BaseException:
            self.close()
            raise

    @classmethod
    def discover(cls, *, include_flite=True, ffmpeg=None, include_powershell=False,
                 cancelled=lambda: False, timeout_seconds=60, extra_providers=(), dordiai=None):
        check_cancelled(cancelled)
        providers = []
        from ..bundled_engines import bundled_executable, executable_on_path
        if os.name == 'nt':
            providers.append(NativeSapiProvider())
            if include_powershell:
                base = windows_directory()
                for architecture, folder in (('64', 'System32'), ('32', 'SysWOW64')):
                    executable = base / folder / 'WindowsPowerShell/v1.0/powershell.exe'
                    if executable.is_file():
                        providers.append(SapiProvider(executable, architecture=architecture))
        espeak = executable_on_path('espeak-ng') or executable_on_path('espeak')
        if espeak:
            providers.append(EspeakProvider(espeak))
        flite_executable = ffmpeg
        if include_flite and flite_executable is None:
            flite_executable = bundled_executable('ffmpeg') or executable_on_path('ffmpeg')
        if include_flite and flite_executable:
            providers.append(FliteProvider(flite_executable))
        if dordiai is not None:
            from ..integrations.dordiai_speech import configured_provider
            selected = configured_provider(dordiai)
            if selected is not None:
                providers.append(selected)
        providers.extend(extra_providers)
        return cls(providers, cancelled=cancelled, timeout_seconds=timeout_seconds)

    def reload(self, *, cancelled=lambda: False, timeout_seconds=60):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 3600):
            raise ValueError('Set a finite voice discovery timeout from zero to 3600 seconds.')
        deadline = monotonic() + timeout_seconds
        callback = cancelled
        def cancelled():
            check_cancelled(callback)
            if monotonic() >= deadline:
                raise TimeoutError('Local voice discovery exceeded its overall time budget.')
            return False
        entries, owners, notices = {}, {}, []
        for provider in self._providers:
            check_cancelled(cancelled)
            try:
                discover = getattr(provider, 'discover_voices', None)
                if callable(discover):
                    values = discover(cancelled=cancelled)
                elif getattr(provider, 'cancellable_discovery', False):
                    values = provider.voices(cancelled=cancelled)
                else:
                    values = provider.voices()
                check_cancelled(cancelled)
                if len(values) > 10000:
                    raise ValueError('The voice provider returned too many voices.')
                candidate = {}
                for voice in values:
                    check_cancelled(cancelled)
                    if not isinstance(voice, Voice) or voice.id in entries or voice.id in candidate:
                        raise ValueError('Duplicate or invalid local voice identity.')
                    candidate[voice.id] = voice
                entries.update(candidate)
                owners.update({key: provider for key in candidate})
            except InterruptedError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                notices.append(f'{provider.name}: {exc}')
            check_cancelled(cancelled)
        check_cancelled(cancelled)
        self._voices, self._owners, self.notices = entries, owners, notices
        return self.voices()

    def add_provider(self, provider, *, cancelled=lambda: False, timeout_seconds=60):
        if any(owner is provider for owner in self._providers):
            raise ValueError('This local voice provider is already registered.')
        self._providers.append(provider)
        try:
            check_cancelled(cancelled)
            return self.reload(cancelled=cancelled, timeout_seconds=timeout_seconds)
        except BaseException:
            close = getattr(provider, 'close', None)
            try:
                if callable(close) and close() is False:
                    raise RuntimeError('Local voice worker cleanup remains unconfirmed.')
            except (Exception, KeyboardInterrupt) as exc:
                # Keep the discovery error and the exact failed cleanup owner;
                # other providers remain available to their current views.
                self.notices.append(f'{provider.name}: {exc}')
                if not getattr(self, '_cleanup_registered', False):
                    import atexit
                    atexit.register(self.close)
                    self._cleanup_registered = True
            else:
                self._providers.remove(provider)
            raise

    def voices(self):
        return tuple(self._voices.values())

    def close(self):
        """Close all owned workers and retain unconfirmed owners for retry."""
        import atexit
        providers, self._providers = self._providers, []
        self._voices, self._owners = {}, {}
        errors = []
        for provider in providers:
            close = getattr(provider, 'close', None)
            if callable(close):
                try:
                    if close() is False:
                        raise RuntimeError('Local voice worker cleanup remains unconfirmed.')
                except (Exception, KeyboardInterrupt) as exc:
                    errors.append(f'{provider.name}: {exc}')
                    self._providers.append(provider)
        self.notices.extend(errors)
        if self._providers and not getattr(self, '_cleanup_registered', False):
            atexit.register(self.close)
            self._cleanup_registered = True
        elif not self._providers and getattr(self, '_cleanup_registered', False):
            atexit.unregister(self.close)
            self._cleanup_registered = False
        return not self._providers

    def get(self, voice_id):
        try:
            return self._voices[voice_id]
        except (KeyError, TypeError) as exc:
            raise ValueError('Select an available installed voice and reload if it was removed.') from exc

    def fingerprint(self, voice_id, *, cancelled=lambda: False):
        """Fingerprint visible installed runtime/voice assets for render caches.

        SAPI exposes an opaque token, not every proprietary driver's model file.
        Its identity remains a token/host fingerprint and is described as such;
        it is never represented as proof of a third-party model revision.
        """
        voice = self.get(voice_id)
        provider = self._owners[voice_id]
        check_cancelled(cancelled)
        def checksum(path):
            return digest(path, cancelled=cancelled)
        identity = {'voice': asdict(voice), 'executable_sha256': checksum(provider.executable),
                    'registry_source': checksum(Path(__file__)),
                    'io_source': checksum(Path(__file__).with_name('_io.py'))}
        custom = getattr(provider, 'fingerprint', None)
        if callable(custom):
            identity['provider_identity'] = custom(voice, cancelled=cancelled)
        if isinstance(provider, PiperProvider):
            model, config, _, _ = provider._models[voice_id]
            identity.update(model_sha256=checksum(local_file(model)), config_sha256=checksum(local_file(config)))
        elif isinstance(provider, LocalHelperProvider):
            provider._verify(cancelled=cancelled)
            identity['helper_contract'] = 'carr.local-voice.v1'
            if provider.script is not None:
                identity['helper_script_sha256'] = provider.expected_script_hash
                identity['helper_launch'] = 'isolated-python-script.v1'
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
                identity.update(native_worker_sha256=checksum(worker) if worker.is_file() else None,
                                native_protocol='carr.sapi-native.v1',
                                com_client='comtypes', com_client_version=client_version)
            else:
                identity['powershell_worker_sha256'] = checksum(Path(__file__).with_name('sapi_worker.ps1'))
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
                    check_cancelled(cancelled)
                    actual = path.resolve(strict=True)
                    if actual.is_file():
                        assets[str(actual)] = checksum(actual)
            identity['installed_voice_libraries'] = assets
        check_cancelled(cancelled)
        return sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    def synthesize(self, text, voice_id, destination, *, settings=None, cancelled=lambda: False,
                   expected_fingerprint=None):
        text_value(text)
        options = settings or SpeechSettings()
        if not isinstance(options, SpeechSettings):
            raise ValueError('Validated SpeechSettings are required.')
        deadline = monotonic() + options.timeout_seconds
        def check():
            check_cancelled(cancelled)
            if monotonic() >= deadline:
                raise TimeoutError('The speech request exceeded its overall time budget.')
            return False
        check()
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
        operation = getattr(provider, 'operation', None)
        scope = operation(cancelled=check) if callable(operation) else nullcontext(check)
        with scope as guarded:
            return self._synthesize_checked(text, voice, provider, destination, options,
                                             deadline, guarded, expected_fingerprint)

    def _synthesize_checked(self, text, voice, provider, destination, options,
                            deadline, check, expected_fingerprint):
        voice_id = voice.id
        initial_identity = self.fingerprint(voice_id, cancelled=check)
        if expected_fingerprint is not None and initial_identity != expected_fingerprint:
            raise ValueError('The voice changed after the production plan was fingerprinted. Retry with its current identity.')
        count, frames, params = 0, 0, None
        target = Path(destination).absolute()
        def attest_output(staged):
            # Bind the receipt to the exact flushed file and final voice identity.
            if self.fingerprint(voice_id, cancelled=check) != initial_identity:
                raise ValueError('The installed voice assets changed during synthesis; no audio was published.')
            audio = local_file(staged)
            if audio.stat().st_size > options.max_output_bytes or digest(audio, cancelled=check) != checksum:
                raise ValueError('The staged WAV no longer matches its rendering receipt.')

        with new_output(target, cancelled=check, before_publish=attest_output) as staged:
            with wave.open(str(staged), 'wb') as combined:
                # A first-chunk cancellation must preserve its original error,
                # not be replaced by Wave_write.close complaining about unset
                # parameters. These are replaced before the first actual frame.
                combined.setnchannels(1)
                combined.setsampwidth(2)
                combined.setframerate(22050)
                for chunk in split_utterances(text):
                    check()
                    if not chunk.strip():
                        continue
                    part = staged.parent / 'utterance.wav'
                    try:
                        remaining_bytes = options.max_output_bytes - frames * (params[1] * params[2] if params else 0)
                        chunk_options = replace(options,
                            timeout_seconds=max(1.0, deadline - monotonic()),
                            max_output_bytes=max(4096, remaining_bytes))
                        provider.render(chunk, voice, part, chunk_options, check)
                        rate, channels, size, width = _wave_info(part, cancelled=check,
                            max_output_bytes=chunk_options.max_output_bytes)
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
                                check()
                                combined.writeframesraw(block)
                        frames += size
                        count += 1
                    finally:
                        part.unlink(missing_ok=True)
            rate, channels, frames, _ = _wave_info(staged, cancelled=check,
                                                  max_output_bytes=options.max_output_bytes)
            checksum = digest(staged, cancelled=check)
        return SpeechResult(target, checksum, frames / rate, rate, channels, frames,
                            voice_id, sha256(text.encode('utf-8')).hexdigest(), count)


class SapiProvider(_CancellableProvider):
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

    def voices(self, *, cancelled=lambda: False):
        result = self._request({'operation': 'voices'}, cancelled=cancelled)
        if not isinstance(result.get('voices'), list):
            raise ValueError('Invalid SAPI voice list.')
        voices, tokens = [], {}
        for row in result['voices']:
            check_cancelled(cancelled)
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
                       if getattr(sys, 'frozen', False) else [self.executable, '-I', '-B', '-X', 'utf8', helper, request, reply])
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


class EspeakProvider(_CancellableProvider):
    name = 'Installed eSpeak'

    def __init__(self, executable):
        self.executable = Path(executable).resolve(strict=True)
        local_file(self.executable)
        self._names = {}

    def voices(self, *, cancelled=lambda: False):
        output, _, _ = run_process([self.executable, '--voices'], timeout=20, cancelled=cancelled)
        voices, names = [], {}
        for line in output.decode('utf-8', errors='replace').splitlines()[1:]:
            check_cancelled(cancelled)
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


class FliteProvider(_CancellableProvider):
    """Actually enumerated English CMU voices in a locally installed FFmpeg."""
    name = 'FFmpeg Flite'

    def __init__(self, executable):
        self.executable = Path(executable).resolve(strict=True)
        local_file(self.executable)
        self._names = {}

    def voices(self, *, cancelled=lambda: False):
        _, diagnostics, _ = run_process([self.executable, '-hide_banner', '-nostdin', '-f', 'lavfi',
            '-i', 'flite=list_voices=1', '-f', 'null', '-'], timeout=20, allow_failure=True, cancelled=cancelled)
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


class PiperProvider(_CancellableProvider):
    """Explicit preinstalled Piper CLI plus hashed local voice files.

    Voice cards and rights are supplied by the user; no weight acquisition or
    automatic use of a downloaded voice name occurs at runtime.
    """
    name = 'Installed Piper'

    def __init__(self, executable, models, *, cancelled=lambda: False):
        check_cancelled(cancelled)
        self.executable = Path(executable).resolve(strict=True)
        local_file(self.executable)
        self._models = {}
        self._voices = []
        for row in models:
            check_cancelled(cancelled)
            if not isinstance(row, dict):
                raise ValueError('A Piper voice manifest must be an object.')
            model = local_file(row['path'])
            config = local_file(row.get('config', str(model) + '.json'))
            for path, expected in ((model, row.get('sha256')), (config, row.get('config_sha256'))):
                if (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)
                        or digest(path, cancelled=cancelled) != expected):
                    raise ValueError('Piper voice/config hash verification failed.')
            license_text = text_value(row.get('license'), 'voice license', maximum=2048)
            identifier = 'piper:' + row['sha256']
            if identifier in self._models:
                raise ValueError('Duplicate Piper model.')
            self._models[identifier] = (model, config, row['sha256'], row['config_sha256'])
            self._voices.append(Voice(identifier, row['name'], 'Piper', row['language'],
                                      ('rate', 'volume'), vendor='User-installed voice', license=license_text))

    def voices(self, *, cancelled=lambda: False):
        check_cancelled(cancelled)
        return tuple(self._voices)

    def render(self, text, voice, output, settings, cancelled):
        model, config, model_hash, config_hash = self._models[voice.id]
        if (digest(local_file(model), cancelled=cancelled) != model_hash
                or digest(local_file(config), cancelled=cancelled) != config_hash):
            raise ValueError('The installed Piper model changed; review and register it again.')
        run_process([self.executable, '--model', str(model), '--config', str(config),
                     '--output-file', str(output), '--length-scale', str(2 ** (-settings.rate / 10)),
                     '--volume', str(settings.volume / 100)], input_bytes=(text + '\n').encode('utf-8'),
                    cancelled=cancelled, timeout=settings.timeout_seconds, output_file=output,
                    max_output=settings.max_output_bytes)


class LocalHelperProvider(_CancellableProvider):
    """Explicitly trusted local adapter contract, including future dordiai-voices.

    This implements our JSON v1 contract, not an assertion about a private
    vendor's unknown API. A helper is executable code and must be reviewed and
    explicitly enabled; executable trust does not follow from document data.
    """
    name = 'Opt-in local voice helper'

    def __init__(self, executable, *, sha256, engine_name, enabled=False, max_voices=1024,
                 script=None, script_sha256=None):
        if enabled is not True:
            raise ValueError('Explicitly enable a reviewed local voice helper before running it.')
        self.executable = local_file(executable)
        self.expected_hash = sha256
        if (script is None) != (script_sha256 is None):
            raise ValueError('A Python helper script and its SHA256 must be configured together.')
        self.script = local_file(script) if script is not None else None
        self.expected_script_hash = script_sha256
        self.engine_name = text_value(engine_name, 'engine name', maximum=128)
        if type(max_voices) is not int or not 1 <= max_voices <= 10000:
            raise ValueError('Set a voice catalog limit from 1 to 10000.')
        self.max_voices = max_voices
        self._tokens = {}
        self._verify()

    def _verify(self, *, cancelled=lambda: False):
        if (not isinstance(self.expected_hash, str) or not re.fullmatch('[0-9a-f]{64}', self.expected_hash)
                or digest(local_file(self.executable), cancelled=cancelled) != self.expected_hash):
            raise ValueError('Local voice helper hash verification failed.')
        if self.script is not None and (
                not isinstance(self.expected_script_hash, str)
                or not re.fullmatch('[0-9a-f]{64}', self.expected_script_hash)
                or digest(local_file(self.script), cancelled=cancelled) != self.expected_script_hash):
            raise ValueError('Local voice helper script hash verification failed.')

    def _request(self, payload, *, cancelled=lambda: False, timeout=30, output=None, budget=512*1024*1024):
        self._verify(cancelled=cancelled)
        raw = json.dumps({'schema': 'carr.local-voice.v1', **payload}, ensure_ascii=False).encode('utf-8')
        command = [self.executable]
        if self.script is not None:
            # A reviewed, self-contained stdlib bridge uses a fixed interpreter
            # invocation. Source text stays in JSON; no caller-supplied argv,
            # shell, user site, startup hooks or bytecode writes are introduced.
            command.extend(('-I', '-B', '-S', str(self.script)))
        reply, _, _ = run_process(command, input_bytes=raw, cancelled=cancelled,
                                 timeout=timeout, output_file=output, max_output=budget)
        data = json.loads(reply.decode('utf-8'))
        if not isinstance(data, dict) or data.get('schema') != 'carr.local-voice.v1':
            raise ValueError('Unsupported local voice-helper response.')
        return data

    def voices(self, *, cancelled=lambda: False):
        data = self._request({'operation': 'voices'}, cancelled=cancelled)
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
