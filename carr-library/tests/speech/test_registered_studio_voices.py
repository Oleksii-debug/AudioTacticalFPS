"""Actual shared Flite speech plus explicitly labelled adapter-boundary checks."""
from hashlib import sha256
from pathlib import Path
import shutil
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio.voice_settings import VoiceSettingsStore
from carr_library.audio.voices import Voice as StudioVoice, VoiceRegistry as StudioRegistry
from carr_library.runtime_settings import voice_registry
from carr_library.speech import SpeechSettings, VoiceRegistry
from carr_library.speech.studio_voice_adapter import StudioVoiceProvider


@pytest.fixture
def registration(tmp_path):
    executable = shutil.which('ffmpeg')
    assert executable, 'This required registry integration needs actual installed FFmpeg.'
    store = VoiceSettingsStore(tmp_path)
    settings = store.register({'ffmpeg': str(Path(executable).resolve()),
                               'espeak_ng': False, 'include_sapi': False}, expected_revision=0)
    direct = StudioRegistry.for_library(tmp_path)
    selected = next((voice for voice in direct.list_voices() if voice.id == 'flite:slt'), None)
    assert selected is not None, 'The required local runtime must actually provide Flite slt.'
    return tmp_path, store, settings, direct, selected


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        return (stream.getframerate(), stream.getnchannels(), stream.getnframes(),
                stream.readframes(stream.getnframes()))


def test_reader_and_studio_share_exact_voice_ids_pcm_controls_and_provenance(registration):
    root, _, _, direct, selected = registration
    reader = voice_registry(root / 'state')
    assert {voice.id for voice in reader.voices()} == {voice.id for voice in direct.list_voices()}
    voice = reader.get(selected.id)
    assert voice.languages == selected.languages
    assert voice.candidate_languages == selected.candidate_languages
    assert voice.language == selected.languages[0]
    assert set(voice.capabilities) == {'rate', 'volume'}
    text = 'The same reviewed local voice reads the same original words.'
    options = SpeechSettings(rate=2, volume=70)
    first = reader.synthesize(text, voice.id, root / 'reader.wav', settings=options)
    second = direct.synthesize(text, root / 'studio.wav', voice_id=voice.id,
                               rate=2 ** .2, volume=.7, language=selected.languages[0])
    assert pcm(first.path) == pcm(second.path)
    assert first.voice_id == second.voice_id == selected.id
    assert first.frames > 0 and any(pcm(first.path)[3])
    assert first.text_sha256 == second.text_sha256 == sha256(text.encode()).hexdigest()
    assert len(reader.fingerprint(voice.id)) == 64
    assert reader.fingerprint(voice.id) == reader.fingerprint(voice.id)


def test_changed_registration_is_an_error_without_discovery_fallback(registration, monkeypatch):
    root, store, settings, _, selected = registration
    reader = voice_registry(root / 'state')
    store.reset(expected_revision=settings['revision'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Do not silently discover a replacement for a changed registered voice.')
    monkeypatch.setattr(VoiceRegistry, 'discover', forbidden)
    with pytest.raises(ValueError, match='changed'):
        reader.synthesize('Do not substitute this voice.', selected.id, root / 'never.wav')
    assert not (root / 'never.wav').exists()


def test_actual_registered_speech_respects_a_tighter_caller_output_budget(registration):
    root, _, _, _, selected = registration
    reader = voice_registry(root / 'state')
    with pytest.raises((ValueError, RuntimeError), match='budget|bounded'):
        reader.synthesize('A speech segment much longer than the selected tiny file allowance.',
                          selected.id, root / 'never.wav', settings=SpeechSettings(max_output_bytes=4096))
    assert not (root / 'never.wav').exists()


def test_optional_prosody_is_rejected_before_any_synthesis(registration):
    root, _, _, _, selected = registration
    reader = voice_registry(root / 'state')
    for settings in (SpeechSettings(pitch=30), SpeechSettings(punctuation=True)):
        with pytest.raises(ValueError, match='pitch|punctuation'):
            reader.synthesize('Preserve exact controls.', selected.id, root / 'never.wav', settings=settings)
    assert not (root / 'never.wav').exists()


@pytest.mark.parametrize('rate,relative', [(-5, .5), (0, 1.0), (5, 2.0)])
def test_sapi_facade_control_mapping_preserves_representable_native_rate(rate, relative, tmp_path):
    """A control-protocol spy only; native SAPI audio is covered by its Windows probe."""
    calls = []
    provider = object.__new__(StudioVoiceProvider)
    source = StudioVoice('sapi5:64:fixture', 'sapi5', 'Boundary fixture', ('en-US',),
                        ('pcm', 'rate', 'volume', 'local'))
    provider._voices = {source.id: source}
    provider._verify_registration = lambda **kwargs: None
    provider.registry = SimpleNamespace(synthesize=lambda *args, **kwargs: calls.append((args, kwargs)))
    options = SpeechSettings(rate=rate, volume=25, timeout_seconds=17, max_output_bytes=4096)
    provider.render('Exact text.', source, tmp_path / 'not-generated.wav', options, lambda: False)
    assert len(calls) == 1
    assert calls[0][0][0] == 'Exact text.'
    assert calls[0][1]['voice_id'] == source.id
    assert calls[0][1]['rate'] == relative
    assert calls[0][1]['volume'] == .25
    assert calls[0][1]['timeout_seconds'] == 17
    assert calls[0][1]['max_output_bytes'] == 4096
    assert not (tmp_path / 'not-generated.wav').exists()


@pytest.mark.parametrize('rate', [-10, -6, 6, 10])
def test_sapi_facade_rejects_unrepresentable_native_rates(rate, tmp_path):
    provider = object.__new__(StudioVoiceProvider)
    source = StudioVoice('sapi5:64:fixture', 'sapi5', 'Boundary fixture', ('en-US',), ('rate', 'volume'))
    provider._voices = {source.id: source}
    provider._verify_registration = lambda **kwargs: None
    def forbidden(*args, **kwargs):
        raise AssertionError('An unsupported rate must not launch an engine.')
    provider.registry = SimpleNamespace(synthesize=forbidden)
    with pytest.raises(ValueError, match='-5 to 5'):
        provider.render('Text.', source, tmp_path / 'never.wav', SpeechSettings(rate=rate), lambda: False)


def test_actual_registered_basic_flite_remains_available_while_heavy_policy_is_occupied(registration):
    from carr_library.operations.broker import ResourceBroker
    from carr_library.operations.models import ResourceRequest
    from carr_library.speech.synthesis_admission import ReaderSynthesisRequest
    root, _, _, _, selected = registration
    reader = voice_registry(root / 'state')
    broker = ResourceBroker.for_app(root)
    checks = []
    try:
        item = ReaderSynthesisRequest(reader, reader.get(selected.id), SpeechSettings(),
            root=root, cancelled=lambda: False, dispatch=lambda callback: callback(),
            verify=lambda **kwargs: checks.append('current reader'))
        assert item.basic
        with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots),
                            operation_id='fixture.heavy-production'):
            result = item.run('Actual Flite keeps the basic reading route available.',
                              root / 'basic-while-heavy.wav', admission_timeout=0)
            assert result.frames > 0 and any(pcm(result.path)[3])
            assert len(broker.snapshot()['leases']) == 1
        assert checks == ['current reader'] and not broker.snapshot()['leases']
    finally:
        reader.close()


@pytest.mark.parametrize('route', ['piper', 'dordiai-voices'])
def test_registered_model_route_cannot_claim_basic_status_through_display_metadata(tmp_path, route):
    from carr_library.speech.synthesis_admission import ReaderSynthesisRequest
    from carr_library.speech.studio_voice_adapter import RegisteredVoice
    provider = object.__new__(StudioVoiceProvider)
    native = object.__new__(StudioRegistry)
    source = StudioVoice('fixture:registered', 'sapi5', 'Misleading display label',
                         ('en',), ('pcm', 'rate', 'volume', 'local'))
    native._routes = {source.id: (route, tmp_path / 'not-executed', 'owned-token')}
    native._model_engines = {route: object()}
    provider.registry, provider._voices = native, {source.id: source}
    voice = RegisteredVoice(source.id, source.name, source.engine, 'en', ('rate', 'volume'),
                            languages=('en',))
    provider.voices = lambda: (voice,)
    registry = VoiceRegistry([provider])
    try:
        item = ReaderSynthesisRequest(registry, voice, SpeechSettings(), root=tmp_path,
            cancelled=lambda: False, dispatch=lambda callback: callback(), verify=lambda **kwargs: None)
        assert not item.basic
        assert item.route.provider is provider
        assert item.route.detail[2] == native._routes[source.id]
    finally:
        registry.close()


@pytest.mark.parametrize('cancel_after_identity', [False, True])
def test_legacy_registered_identity_keeps_its_zero_argument_contract_inside_admission(
        registration, cancel_after_identity):
    from threading import Event
    from carr_library.operations.broker import ResourceBroker
    from carr_library.speech.synthesis_admission import ReaderSynthesisRequest
    root, _, _, _, selected = registration
    reader = voice_registry(root / 'state')
    provider = reader._owners[selected.id]
    original, cancelled, calls = provider.registry, Event(), []
    broker = ResourceBroker.for_app(root)

    class LegacyRegistry:
        def fingerprint(self):
            assert len(broker.snapshot()['leases']) == 1
            calls.append('legacy zero-argument callback')
            result = original.fingerprint()
            if cancel_after_identity:
                cancelled.set()
            return result
        def synthesize(self, *args, **kwargs):
            return original.synthesize(*args, **kwargs)

    provider.registry = LegacyRegistry()
    item = ReaderSynthesisRequest(reader, reader.get(selected.id),
        SpeechSettings(max_output_bytes=4 * 1024 * 1024), root=root,
        cancelled=cancelled.is_set, dispatch=lambda callback: callback(), verify=lambda **kwargs: None)
    assert not item.basic  # An unknown implementation cannot borrow the native exemption.
    target = root / 'legacy-admitted.wav'
    try:
        if cancel_after_identity:
            with pytest.raises(InterruptedError, match='cancelled'):
                item.run('A cancellation after the opaque callback.', target)
            assert len(calls) == 1 and not target.exists()
        else:
            result = item.run('An actual registered Flite voice through a legacy identity callback.', target)
            assert len(calls) == 2 and result.frames > 0 and any(pcm(result.path)[3])
        assert not broker.snapshot()['leases']
    finally:
        reader.close()
