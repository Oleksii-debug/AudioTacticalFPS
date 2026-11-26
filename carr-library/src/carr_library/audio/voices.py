"""Installed, local speech engines; no downloads, remote fallback or SSML input.

The text is data in a private file, never a command, filter or PowerShell source.
Flite is a useful English compatibility voice, not a multilingual natural-voice
claim. SAPI5 is supplied by the user's Windows installation and voice licences.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Protocol
import wave


class VoiceUnavailable(RuntimeError):
    """The explicitly requested local engine cannot perform this operation."""


class VoiceCancelled(RuntimeError):
    """A cancelled utterance has not been published."""


@dataclass(frozen=True)
class Voice:
    id: str
    provider: str
    name: str
    languages: tuple[str, ...]
    capabilities: tuple[str, ...]
    identity: str
    notes: str = ''


@dataclass(frozen=True)
class SynthesisResult:
    path: Path
    frames: int
    sample_rate: int
    channels: int
    sha256: str
    voice_id: str
    provider: str
    engine_identity: str
    text_sha256: str
    timing: str = 'segment-only'

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate


@dataclass(frozen=True)
class ProviderAvailability:
    provider: str
    available: bool
    reason: str
    voices: int = 0


class LocalVoiceProvider(Protocol):
    provider_id: str

    def list_voices(self) -> tuple[Voice, ...]: ...

    def synthesize(self, text: str, voice_id: str, output: Path, *,
                   rate: float = 1.0, volume: float = 1.0,
                   sample_rate: int = 24000, cancel=None,
                   timeout: float = 300.0) -> SynthesisResult: ...


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _cancelled(cancel) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    if hasattr(cancel, 'is_set'):
        return bool(cancel.is_set())
    raise TypeError('Cancellation must be a callback or threading.Event.')


def _check_cancel(cancel) -> None:
    if _cancelled(cancel):
        raise VoiceCancelled('Speech generation was cancelled; no output was published.')


def _number(value, low: float, high: float, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be a finite number from {low} to {high}.')
    return float(value)


def _local_path(path: Path) -> Path:
    # UNC paths can cause network I/O even without a network library.
    raw = os.fspath(path)
    if raw.startswith(('\\\\', '//')) or '://' in raw or re.match(r'^[A-Za-z][A-Za-z0-9+.-]+:[/\\]', raw):
        raise ValueError('Speech files and executables must use local paths.')
    return Path(raw)


def _binary(path: Path) -> Path:
    result = _local_path(path)
    if not result.is_absolute():
        raise ValueError('Select an absolute installed speech-engine executable.')
    result = result.resolve(strict=True)
    if not stat.S_ISREG(result.stat().st_mode):
        raise ValueError('Speech-engine executable must be a regular local file.')
    return result


def _target(path: Path) -> Path:
    path = _local_path(path).absolute()
    if path.suffix.lower() != '.wav':
        raise ValueError('Speech segment output must be a new .wav file.')
    if path.is_symlink() or path.exists():
        raise FileExistsError('Speech output already exists; choose a new filename.')
    if not path.parent.is_dir() or path.parent.resolve() != path.parent:
        raise ValueError('Speech output requires an existing, non-symlink local directory.')
    return path


def _request(text, rate, volume, sample_rate, max_text_chars, cancel):
    if not isinstance(text, str) or not text.strip() or '\x00' in text:
        raise ValueError('Supply non-empty plain speech text without NUL characters.')
    try:
        encoded = text.encode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise ValueError('Speech text must contain valid Unicode.') from exc
    if len(text) > max_text_chars:
        raise ValueError(f'This engine accepts {max_text_chars} characters per segment; split the text into resumable segments.')
    rate = _number(rate, 0.5, 2.0, 'Relative speech rate')
    volume = _number(volume, 0.0, 1.0, 'Speech volume')
    if type(sample_rate) is not int or sample_rate not in (8000, 16000, 22050, 24000, 32000, 44100, 48000):
        raise ValueError('Unsupported PCM speech sample rate.')
    _check_cancel(cancel)
    return encoded, rate, volume


def _run(args: list[str], *, cwd: Path | None = None, cancel=None,
         timeout: float = 300.0, allowed_returncodes=(0,), log_limit=262144) -> tuple[bytes, bytes]:
    """Bounded output, closed stdin and a cancellable process; never a shell."""
    timeout = _number(timeout, 0.001, 86400.0, 'Speech worker time budget')
    _check_cancel(cancel)
    started = time.monotonic()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=out, stderr=err, shell=False, creationflags=flags)
        try:
            while True:
                _check_cancel(cancel)
                if time.monotonic() - started > timeout:
                    raise TimeoutError('Speech generation exceeded its configured time budget.')
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > log_limit:
                    raise VoiceUnavailable('Speech worker exceeded the diagnostic-output budget.')
                try:
                    process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            _check_cancel(cancel)
            if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > log_limit:
                raise VoiceUnavailable('Speech worker exceeded the diagnostic-output budget.')
            if process.returncode not in allowed_returncodes:
                # Engine errors may quote input. Do not leak private text in logs.
                raise VoiceUnavailable(f'Local speech worker failed (exit {process.returncode}); check the installed engine and voice.')
            out.seek(0)
            err.seek(0)
            return out.read(log_limit), err.read(log_limit)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def _publish(temp: Path, target: Path, *, sample_rate: int, voice: Voice,
             text_sha256: str, cancel) -> SynthesisResult:
    info = temp.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise VoiceUnavailable('Speech engine did not produce an independent regular WAV file.')
    try:
        with wave.open(str(temp), 'rb') as data:
            if (data.getnchannels(), data.getsampwidth(), data.getframerate(), data.getcomptype()) != (1, 2, sample_rate, 'NONE'):
                raise VoiceUnavailable('Speech output has an unexpected PCM format.')
            frames = data.getnframes()
            if frames <= 0:
                raise VoiceUnavailable('Speech engine produced no audio frames.')
            remaining = frames
            while remaining:
                block = data.readframes(min(remaining, 32768))
                if not block or len(block) % 2:
                    raise VoiceUnavailable('Speech engine produced truncated PCM data.')
                remaining -= len(block) // 2
                _check_cancel(cancel)
    except (wave.Error, EOFError) as exc:
        raise VoiceUnavailable('Speech engine did not produce a readable PCM WAV.') from exc
    checksum = _digest(temp)
    with temp.open('r+b') as stream:
        os.fsync(stream.fileno())
    _check_cancel(cancel)
    # Atomic no-clobber publication: a concurrently created destination wins.
    os.link(temp, target)
    if os.name != 'nt':
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return SynthesisResult(target, frames, sample_rate, 1, checksum, voice.id,
                           voice.provider, voice.identity, text_sha256)


class FFmpegFliteProvider:
    """Local FFmpeg's compiled-in Flite voices, enumerated from that build."""

    provider_id = 'flite'

    def __init__(self, ffmpeg: Path, *, max_text_chars: int = 32768):
        self.ffmpeg = _binary(ffmpeg)
        if type(max_text_chars) is not int or max_text_chars < 1:
            raise ValueError('Per-segment text budget must be a positive integer.')
        self.max_text_chars = max_text_chars
        self._voices = None

    def list_voices(self) -> tuple[Voice, ...]:
        if self._voices is None:
            stdout, stderr = _run([str(self.ffmpeg), '-hide_banner', '-nostdin',
                '-f', 'lavfi', '-i', 'flite=list_voices=1', '-f', 'null', '-'],
                timeout=15, allowed_returncodes=(0, 1))
            text = (stdout + stderr).decode('utf-8', errors='replace')
            names = sorted(set(re.findall(r'^\[Parsed_flite_\d+ @ [^\]]+\]\s+([A-Za-z0-9_-]+)\s*$', text, re.M)))
            if not names:
                raise VoiceUnavailable('This FFmpeg build has no discoverable compiled-in Flite voices.')
            version, _ = _run([str(self.ffmpeg), '-hide_banner', '-version'], timeout=10)
            identity = hashlib.sha256((_digest(self.ffmpeg) + version.decode('utf-8', 'replace') + '\n'.join(names)).encode()).hexdigest()
            self._voices = tuple(Voice(name, self.provider_id, f'Flite {name}', ('en',),
                ('plain-text', 'pcm-mono', 'volume', 'post-synthesis-time-stretch', 'segment-timing'),
                f'flite:{identity}:{name}',
                'Local English compatibility voice. Rate uses FFmpeg time-stretch; no word timing, stress, emotion or multilingual-quality guarantee.') for name in names)
        return self._voices

    def synthesize(self, text: str, voice_id: str, output: Path, *,
                   rate: float = 1.0, volume: float = 1.0,
                   sample_rate: int = 24000, cancel=None,
                   timeout: float = 300.0) -> SynthesisResult:
        encoded, rate, volume = _request(text, rate, volume, sample_rate, self.max_text_chars, cancel)
        target = _target(output)
        voices = {voice.id: voice for voice in self.list_voices()}
        if voice_id not in voices:
            raise ValueError('The selected Flite voice is not present in this local build.')
        with tempfile.TemporaryDirectory(prefix='.speech-', dir=target.parent) as directory:
            stage = Path(directory)
            (stage / 'utterance.txt').write_bytes(encoded)
            temp = stage / 'render.wav'
            # Only enumerated ASCII names and validated numbers enter a filter.
            source = f'flite=textfile=utterance.txt:voice={voice_id}'
            filters = f'atempo={rate:.9f},volume={volume:.9f}'
            _run([str(self.ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin',
                  '-protocol_whitelist', 'file', '-f', 'lavfi', '-i', source,
                  '-af', filters, '-ar', str(sample_rate), '-ac', '1', '-c:a', 'pcm_s16le',
                  '-f', 'wav', '-n', 'render.wav'], cwd=stage, cancel=cancel, timeout=timeout)
            return _publish(temp, target, sample_rate=sample_rate, voice=voices[voice_id],
                            text_sha256=hashlib.sha256(encoded).hexdigest(), cancel=cancel)


class Sapi5Provider:
    """System.Speech with installed SAPI5 voices in an isolated Windows process.

    Bitness follows the selected system PowerShell. A 64-bit application can
    explicitly create another provider with the system SysWOW64 PowerShell to
    inspect its installed 32-bit SAPI5 voices; SAPI4 is a separate unavailable
    capability. No execution policy, registry or voice installation is changed.
    """

    provider_id = 'sapi5'

    def __init__(self, powershell: Path | None = None, *, max_text_chars: int = 32768):
        if sys.platform != 'win32':
            raise VoiceUnavailable('SAPI5 is available only on Windows with installed compatible voices.')
        system = Path(os.environ.get('SystemRoot', r'C:\Windows')).resolve()
        allowed = {
            (system / 'System32/WindowsPowerShell/v1.0/powershell.exe').resolve(),
            (system / 'SysWOW64/WindowsPowerShell/v1.0/powershell.exe').resolve(),
        }
        self.powershell = _binary(powershell or system / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        if self.powershell not in allowed:
            raise ValueError('SAPI5 requires the installed Windows system PowerShell executable.')
        if self.powershell == (system / 'SysWOW64/WindowsPowerShell/v1.0/powershell.exe').resolve():
            self.provider_id = 'sapi5-x86'
        if type(max_text_chars) is not int or max_text_chars < 1:
            raise ValueError('Per-segment text budget must be a positive integer.')
        self.max_text_chars = max_text_chars
        self._voices = None
        self._worker = Path(__file__).with_name('sapi5.ps1').read_text(encoding='utf-8')

    def _call(self, request: dict, directory: Path, *, cancel=None, timeout=300.0) -> dict:
        (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        # Fixed packaged source only. Request text is never interpolated into it.
        stdout, _ = _run([str(self.powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
                          '-Command', self._worker], cwd=directory, cancel=cancel, timeout=timeout)
        try:
            result = json.loads(stdout.decode('utf-8-sig'))
        except (UnicodeError, ValueError) as exc:
            raise VoiceUnavailable('SAPI5 returned an invalid local worker response.') from exc
        if not isinstance(result, dict):
            raise VoiceUnavailable('SAPI5 returned an invalid local worker response.')
        return result

    def list_voices(self) -> tuple[Voice, ...]:
        if self._voices is None:
            with tempfile.TemporaryDirectory(prefix='carr-sapi5-') as directory:
                response = self._call({'schema': 1, 'action': 'list'}, Path(directory), timeout=20)
            rows = response.get('voices')
            if not isinstance(rows, list) or len(rows) > 2048:
                raise VoiceUnavailable('SAPI5 returned an invalid voice inventory.')
            result, identities = [], set()
            for row in rows:
                if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key] for key in ('id', 'name', 'language')):
                    raise VoiceUnavailable('SAPI5 returned invalid voice metadata.')
                if row['id'] in identities:
                    raise VoiceUnavailable('SAPI5 returned duplicate voice identities.')
                identities.add(row['id'])
                identity = hashlib.sha256(json.dumps({'voice': row,
                    'runtime': response.get('runtime'), 'bitness': response.get('bitness'),
                    'powershell': _digest(self.powershell)}, sort_keys=True).encode('utf-8')).hexdigest()
                result.append(Voice(row['id'], self.provider_id, row['name'], (row['language'],),
                    ('plain-text', 'pcm-mono', 'volume', 'sapi-relative-rate', 'segment-timing'),
                    f'{self.provider_id}:{identity}',
                    f"Installed SAPI5 voice, {response.get('bitness', 'unknown')}-bit host. Rate is the SAPI scale, not a guaranteed duration multiplier. Voice rights belong to its installed provider."))
            if not result:
                raise VoiceUnavailable('No enabled SAPI5 voice is installed for this Windows process architecture.')
            self._voices = tuple(result)
        return self._voices

    def synthesize(self, text: str, voice_id: str, output: Path, *,
                   rate: float = 1.0, volume: float = 1.0,
                   sample_rate: int = 24000, cancel=None,
                   timeout: float = 300.0) -> SynthesisResult:
        encoded, rate, volume = _request(text, rate, volume, sample_rate, self.max_text_chars, cancel)
        target = _target(output)
        voices = {voice.id: voice for voice in self.list_voices()}
        if voice_id not in voices:
            raise ValueError('The selected SAPI5 voice is not installed for this process architecture.')
        with tempfile.TemporaryDirectory(prefix='.speech-', dir=target.parent) as directory:
            stage = Path(directory)
            response = self._call({'schema': 1, 'action': 'synthesize', 'text': text,
                'voice_id': voice_id, 'sapi_rate': round(math.log2(rate) * 10),
                'volume': round(volume * 100), 'sample_rate': sample_rate},
                stage, cancel=cancel, timeout=timeout)
            if response.get('status') != 'complete' or response.get('voice_id') != voice_id:
                raise VoiceUnavailable('SAPI5 did not confirm the selected voice and completed output.')
            return _publish(stage / 'render.wav', target, sample_rate=sample_rate,
                voice=voices[voice_id], text_sha256=hashlib.sha256(encoded).hexdigest(), cancel=cancel)


class VoiceRegistry:
    """Explicit local providers; unavailable engines are reported, never guessed."""

    def __init__(self, providers=(), *, unavailable=()):
        self._providers = {}
        self._unavailable = list(unavailable)
        for provider in providers:
            if provider.provider_id in self._providers:
                raise ValueError('Duplicate speech provider ID.')
            self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> LocalVoiceProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise VoiceUnavailable('The selected local speech provider is unavailable; choose an installed provider.') from exc

    def list_voices(self) -> tuple[Voice, ...]:
        result = []
        for provider in self._providers.values():
            try:
                result.extend(provider.list_voices())
            except (VoiceUnavailable, OSError, TimeoutError):
                continue
        return tuple(result)

    def availability(self) -> tuple[ProviderAvailability, ...]:
        result = list(self._unavailable)
        for identity, provider in self._providers.items():
            try:
                voices = provider.list_voices()
                result.append(ProviderAvailability(identity, bool(voices),
                    'Installed local voices ready.' if voices else 'No local voices found.', len(voices)))
            except (VoiceUnavailable, OSError, TimeoutError) as exc:
                result.append(ProviderAvailability(identity, False, str(exc)))
        present = {row.provider for row in result}
        for identity, reason in (
            ('sapi4', 'A compatible isolated SAPI4 helper has not been validated on Windows 11.'),
            ('dordiai-voices', 'The actual dordiai-voices engine contract and installation have not yet been located and verified.'),
        ):
            if identity not in present:
                result.append(ProviderAvailability(identity, False, reason))
        return tuple(result)


def discover_local_providers(ffmpeg: Path | None = None) -> VoiceRegistry:
    """Discover local engines only; never install/download a voice or use an API.

    Pass the application's explicitly selected FFmpeg executable. When omitted,
    only Windows SAPI5 is discovered; no project-controlled PATH lookup occurs.
    """
    providers, unavailable = [], []
    try:
        providers.append(Sapi5Provider())
    except (VoiceUnavailable, OSError, ValueError) as exc:
        unavailable.append(ProviderAvailability('sapi5', False, str(exc)))
    if sys.platform == 'win32':
        x86 = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'SysWOW64/WindowsPowerShell/v1.0/powershell.exe'
        try:
            providers.append(Sapi5Provider(x86))
        except (VoiceUnavailable, OSError, ValueError) as exc:
            unavailable.append(ProviderAvailability('sapi5-x86', False, str(exc)))
    else:
        unavailable.append(ProviderAvailability('sapi5-x86', False, '32-bit SAPI5 discovery requires a Windows host.'))
    if ffmpeg is not None:
        try:
            providers.append(FFmpegFliteProvider(ffmpeg))
        except (VoiceUnavailable, OSError, ValueError) as exc:
            unavailable.append(ProviderAvailability('flite', False, str(exc)))
    else:
        unavailable.append(ProviderAvailability('flite', False, 'No explicitly selected local FFmpeg executable.'))
    return VoiceRegistry(providers, unavailable=unavailable)
