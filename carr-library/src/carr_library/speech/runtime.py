"""Plain-text live speech through Audio Studio's sole installed-engine registry.

The reader never interprets script directions or installs a second synthesizer.
It joins bounded PCM segments using the existing streaming WAV implementation.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from ..audio import engines
from ..audio.ffmpeg import concatenate, wav_info
from ..audio.jobs import publish_new, sha256_file
from ..audio.production import speech_segments
from ._io import check_cancelled, local_file, text_value


@dataclass(frozen=True)
class SpeechSettings:
    rate: int = 0
    volume: int = 80
    max_output_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        if type(self.rate) is not int or not -10 <= self.rate <= 10:
            raise ValueError('Speech rate must be an integer from -10 to 10.')
        if type(self.volume) is not int or not 0 <= self.volume <= 100:
            raise ValueError('Speech volume must be an integer from 0 to 100.')
        if type(self.max_output_bytes) is not int or not 4096 <= self.max_output_bytes <= 512 * 1024 * 1024:
            raise ValueError('Choose a live-reading output budget from 4096 bytes to 512 MiB.')


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    engine: str
    language: str
    capabilities: tuple
    timing: str = 'Measured utterance duration; word highlighting is unavailable.'


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


class VoiceRegistry:
    def __init__(self, registry):
        self.registry = registry

    @classmethod
    def discover(cls, *, library_root=None):
        if library_root is None:
            registry = engines.default_registry()
        else:
            from ..audio.engine_settings import configured_registry
            registry = configured_registry(library_root)
        return cls(registry)

    @property
    def notices(self):
        return tuple(row.get('reason', '') if isinstance(row, dict) else str(row)
                     for row in self.registry.notices)

    def voices(self):
        return tuple(Voice(v.id, v.name, v.engine, v.languages[0] if v.languages else 'en', v.controls)
                     for v in self.registry.voices() if v.available)

    def synthesize(self, text, voice_id, destination, *, settings=None,
                   cancelled=lambda: False, guard=lambda: None):
        text_value(text, 'selected reading text')
        settings = settings or SpeechSettings()
        if not isinstance(settings, SpeechSettings):
            raise ValueError('Validated speech settings are required.')
        voice = next((v for v in self.voices() if v.id == voice_id), None)
        if voice is None:
            raise ValueError('The saved local voice is unavailable; choose a voice explicitly.')
        if settings.rate != 0 and 'rate' not in voice.capabilities:
            raise ValueError('This local voice has no native rate control.')
        if settings.volume != 100 and 'volume' not in voice.capabilities:
            raise ValueError('This local voice has no volume control.')
        output = local_file(destination, must_exist=False)
        if output.exists():
            raise FileExistsError(output)
        guard()
        check_cancelled(cancelled)
        paths, total = [], 0
        with TemporaryDirectory(prefix='.live-reading-', dir=output.parent) as folder:
            for number, piece in enumerate(speech_segments(text), 1):
                guard()
                check_cancelled(cancelled)
                segment = Path(folder) / f'{number:06d}.wav'
                self.registry.synthesize(piece, segment, voice=voice.id, language=voice.language,
                    rate=settings.rate, volume=settings.volume / 100, cancelled=cancelled)
                check_cancelled(cancelled)
                wav_info(segment)
                total += segment.stat().st_size
                if total > settings.max_output_bytes:
                    raise ValueError('The live-reading audio exceeds its memory/disk budget; select a shorter passage.')
                paths.append(segment)
            joined = Path(folder) / 'joined.wav'
            concatenate(paths, joined, cancelled=cancelled)
            info = wav_info(joined)
            checksum = sha256_file(joined, cancelled=cancelled)
            guard()
            check_cancelled(cancelled)
            publish_new(joined, output)
        return SpeechResult(output, checksum, info.frames / info.rate, info.rate,
                            info.channels, info.frames, voice.id, sha256(text.encode('utf-8')).hexdigest(), len(paths))
