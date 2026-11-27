"""Canonical settings facade for the existing owned Dordiai speech provider.

Voice discovery, asset fingerprinting, warm-worker restart and PCM generation
remain in speech.dordiai.DordiaiProvider. This facade supplies canonical
installation-qualified voice IDs and create-only direct utterance publication.
"""
from __future__ import annotations

from pathlib import Path
from threading import Event
from time import monotonic

from .dordiai_voices import REVIEWED_WORKER_SHA256
from ..speech.dordiai import DordiaiProvider
from ..speech.dordiai_settings import default_settings


class DordiaiSpeechProvider(DordiaiProvider):
    def __init__(self, runtime_directory, *, python_executable=None,
                 expected_worker_sha256=REVIEWED_WORKER_SHA256,
                 configuration_root=None, settings_guard=lambda: None):
        from ..speech._io import check_cancelled
        declaration = default_settings()
        declaration.update(enabled=True, runtime_directory=str(runtime_directory),
                           python_executable=str(python_executable) if python_executable is not None else None,
                           expected_worker_sha256=expected_worker_sha256)
        self._requested_stop = Event()
        def unchanged():
            settings_guard()
            check_cancelled(self._requested_stop.is_set)
        super().__init__(configuration_root, settings=declaration, installation_qualified=True,
                         settings_guard=unchanged, clock=lambda: monotonic())

    def voices(self, *, cancelled=lambda: False):
        from ..speech._io import check_cancelled
        # A rejected concurrent request must not clear cancellation belonging
        # to an already active render/discovery operation.
        with self.operation(cancelled=cancelled) as active:
            self._requested_stop.clear()
            def stopped():
                active()
                check_cancelled(self._requested_stop.is_set)
                return False
            return super().voices(cancelled=stopped)

    def render(self, text, voice, output, settings, cancelled):
        from ..speech._io import check_cancelled, new_output, text_value
        from ..speech.runtime import SpeechSettings
        text_value(text)
        if not isinstance(settings, SpeechSettings):
            raise ValueError('Validated SpeechSettings are required.')
        if settings.punctuation:
            raise ValueError('The dordiai-voices worker does not expose punctuation reading.')
        try:
            _profile, language = self._tokens[voice.id]
        except (AttributeError, KeyError):
            raise ValueError('Choose a currently installed dordiai-voices voice and language.') from None
        if voice.engine != 'dordiai-voices' or voice.language != language:
            raise ValueError('The selected voice-language pair changed; reload the registry.')
        deadline = monotonic() + settings.timeout_seconds
        with self.operation(cancelled=cancelled) as active:
            self._requested_stop.clear()
            def stopped():
                active()
                check_cancelled(self._requested_stop.is_set)
                if monotonic() >= deadline:
                    raise TimeoutError('Dordiai-voices exceeded the utterance time budget.')
                return False
            before = self.fingerprint(voice, cancelled=stopped)
            with new_output(output, cancelled=stopped) as staged:
                try:
                    super().render(text, voice, staged, settings, stopped)
                except ValueError as exc:
                    if 'audio-output budget' in str(exc):
                        raise ValueError('Dordiai-voices exceeded its audio budget (audio-output budget).') from exc
                    raise
                stopped()
                if before != self.fingerprint(voice, cancelled=stopped):
                    raise ValueError('The dordiai-voices installation changed during rendering; retry.')
                stopped()

    def cancel(self):
        self._requested_stop.set()
        self.adapter.cancel()


def configured_provider(configuration=None, *, configuration_root=None, settings_guard=lambda: None):
    """Resolve one saved declaration without probing a disabled installation."""
    from ..runtime_settings import validate_dordiai_configuration
    value = validate_dordiai_configuration(configuration)
    if value is None or not value['enabled']:
        return None
    return DordiaiSpeechProvider(value['runtime_directory'],
        python_executable=value.get('python_executable'),
        expected_worker_sha256=value.get('expected_worker_sha256', REVIEWED_WORKER_SHA256),
        configuration_root=configuration_root, settings_guard=settings_guard)


def install_dordiai_provider(registry, configuration=None, *, cancelled=lambda: False,
                            configuration_root=None, settings_guard=lambda: None):
    provider = configured_provider(configuration, configuration_root=configuration_root,
                                   settings_guard=settings_guard)
    if provider is None:
        return None
    # add_provider owns the provider before discovery, including a failed close.
    # Do not erase that owner or replace its primary failure with another close.
    registry.add_provider(provider, cancelled=cancelled)
    return provider
