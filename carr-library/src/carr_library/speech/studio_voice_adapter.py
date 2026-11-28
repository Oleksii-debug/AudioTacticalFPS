"""Use reviewed Studio voice registrations through the native reader contract.

IDs and model manifests are never translated. The facade exposes only controls
the selected implementation actually supports. Enumeration is not inference or
a qualification of pronunciation, accent, or an untested candidate language.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from .runtime import SpeechSettings, Voice
from ._io import check_cancelled
from ..audio.voice_settings import VoiceSettingsStore, verify_voice_settings_bindings
from ..audio.voices import MAX_OUTPUT_BYTES, VoiceRegistry as StudioRegistry


@dataclass(frozen=True)
class RegisteredVoice(Voice):
    languages: tuple[str, ...] = ()
    candidate_languages: tuple[str, ...] = ()


class StudioVoiceProvider:
    name = 'Reviewed local Studio voices'
    failure_is_fatal = True

    def __init__(self, library_root):
        self.store = VoiceSettingsStore(library_root)
        self.registry = StudioRegistry.for_library(library_root)
        self.registration = verify_voice_settings_bindings(self.store.read())
        if self.registry._registered_settings != self.registration:
            raise ValueError('Voice registration changed during loading. Reload voices.')
        self._voices = {}

    def _verify_registration(self, *, cancelled=None):
        current = verify_voice_settings_bindings(self.store.read(), cancelled=cancelled)
        if current != self.registration:
            raise ValueError('Voice registration changed. Reload voices before using it.')

    def voices(self):
        self._verify_registration()
        available = self.registry.list_voices(refresh=True)
        statuses = {row['engine']: row for row in self.registry.engine_status()}
        for key, engine in (('piper', 'piper'), ('dordiai_manifest', 'dordiai-voices'),
                            ('ffmpeg', 'flite'), ('espeak_ng', 'espeak-ng')):
            if (key in self.registration['config']
                    and self.registration['config'][key] is not None
                    and self.registration['config'][key] is not False
                    and statuses.get(engine, {}).get('status') not in ('available', 'configured')):
                raise ValueError('The registered ' + engine + ' installation is unavailable. '
                                 'Review its local registration; no replacement voice was selected.')
        result, owners = [], {}
        for source in available:
            if not source.languages or source.id in owners:
                raise ValueError('A reviewed voice must have a unique ID and supported languages.')
            capabilities = tuple(control for control in ('rate', 'volume') if
                control in source.capabilities or
                (control == 'rate' and 'rate_via_time_stretch' in source.capabilities))
            description = 'Utterance duration measured; word timings unavailable. '
            if source.engine == 'sapi5':
                description += 'Native SAPI rate is supported from -5 to 5; larger requests are rejected. '
            elif 'rate_via_time_stretch' in source.capabilities:
                description += 'Rate uses time stretching, not native voice prosody. '
            else:
                description += 'Rate uses the registered engine-relative scale. '
            description += 'Default reading language: ' + source.languages[0] + '. '
            description += 'Supported languages: ' + ', '.join(source.languages[:32])
            if len(source.languages) > 32:
                description += ' (complete declaration retained in voice metadata)'
            result.append(RegisteredVoice(source.id, source.name, source.engine, source.languages[0],
                capabilities, vendor=source.vendor, license=source.license_note, timing=description,
                languages=tuple(source.languages), candidate_languages=tuple(source.candidate_languages)))
            owners[source.id] = source
        self._verify_registration()
        self._voices = owners
        return tuple(result)

    def fingerprint(self, voice_id, *, cancelled=None):
        if cancelled is not None:
            check_cancelled(cancelled)
        self._verify_registration(cancelled=cancelled)
        if voice_id not in self._voices:
            raise ValueError('Select an available registered voice.')
        identity = {'schema': 1, 'voice_id': voice_id,
                    'registration_revision': self.registration['revision'],
                    'runtime': (self.registry.fingerprint(cancelled=cancelled)
                                if cancelled is not None and type(self.registry) is StudioRegistry
                                else self.registry.fingerprint()),
                    'reader_adapter_sha256': sha256(Path(__file__).read_bytes()).hexdigest()}
        if cancelled is not None:
            check_cancelled(cancelled)
        self._verify_registration(cancelled=cancelled)
        return sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def render(self, text, voice, output, settings, cancelled):
        if not isinstance(settings, SpeechSettings):
            raise ValueError('Validated SpeechSettings are required.')
        self._verify_registration(cancelled=cancelled)
        source = self._voices.get(voice.id)
        if source is None:
            raise ValueError('The selected registered voice is unavailable; reload voices.')
        if settings.pitch is not None or settings.punctuation:
            raise ValueError('This registered voice facade does not expose pitch or punctuation controls.')
        if settings.rate and not ({'rate', 'rate_via_time_stretch'} & set(source.capabilities)):
            raise ValueError('This registered voice does not expose rate control.')
        if settings.volume != 100 and 'volume' not in source.capabilities:
            raise ValueError('This registered voice does not expose volume control.')
        if settings.timeout_seconds > 3600:
            raise ValueError('Registered Studio speech supports at most 3600 seconds per utterance.')
        if source.engine == 'sapi5':
            if not -5 <= settings.rate <= 5:
                raise ValueError('This registered SAPI route supports native rate values from -5 to 5.')
            relative_rate = 2 ** (settings.rate / 5)
        else:
            relative_rate = 2 ** (settings.rate / 10)
        self.registry.synthesize(text, output, voice_id=source.id,
            language=source.languages[0], rate=relative_rate, volume=settings.volume / 100,
            timeout_seconds=settings.timeout_seconds,
            max_output_bytes=min(settings.max_output_bytes, MAX_OUTPUT_BYTES),
            cancelled=cancelled)
        self._verify_registration(cancelled=cancelled)
