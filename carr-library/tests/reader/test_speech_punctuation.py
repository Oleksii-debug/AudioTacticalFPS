"""Optional native punctuation without rewriting historical speech settings."""
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from carr_library.audio.engines import EngineUnavailable, Voice, VoiceRegistry
from carr_library.audio.ffmpeg import silence
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.reader.live_speech import (SpeechPositionStore, SpeechSelection,
    SpeechSession, SpeechSettings, decode_positions)
from carr_library.audio.models import Role, canonical


LEGACY_SETTINGS = {'voice': 'fixture:voice', 'language': 'en', 'rate': 0, 'pitch': 0, 'volume': .8}


def test_false_default_keeps_legacy_engine_options_and_project_models():
    current = SpeechSettings(**LEGACY_SETTINGS)
    assert current.engine_options() == LEGACY_SETTINGS
    assert SpeechSettings(**LEGACY_SETTINGS, punctuation=False).engine_options() == LEGACY_SETTINGS
    assert 'punctuation' not in asdict(Role('Narrator', voice='fixture:voice', language='en'))
    old_role = {'name': 'Narrator', 'voice': 'fixture:voice', 'language': 'en',
                'pan': 0.0, 'rate': 0, 'pitch': 0, 'volume': 1.0, 'effects': []}
    assert canonical(asdict(Role.from_dict(old_role))) == canonical(old_role)


@pytest.mark.parametrize('bad', [1, 0, None, '', 'true', [], {}])
def test_punctuation_requires_an_explicit_boolean(bad):
    with pytest.raises(ValueError, match='boolean'):
        SpeechSettings(**LEGACY_SETTINGS, punctuation=bad)


@pytest.mark.parametrize('settings', [SpeechSettings(**LEGACY_SETTINGS),
    SpeechSettings(**LEGACY_SETTINGS, punctuation=False)])
def test_default_position_writer_keeps_schema_one(tmp_path, settings):
    store = SpeechPositionStore(tmp_path)
    store.remember('Exact text.', 3, settings)
    raw = json.loads(store.path.read_bytes())
    assert raw['schema'] == 'carr-live-reading/1'
    assert raw['positions'][0]['settings'] == LEGACY_SETTINGS
    assert store.load('Exact text.')['settings'] == LEGACY_SETTINGS


def test_schema_two_coexists_with_original_rows_and_only_changes_selected_position(tmp_path):
    store = SpeechPositionStore(tmp_path)
    first = store.remember('First original.', 2, SpeechSettings(**LEGACY_SETTINGS))
    store.remember('Second original.', 4, SpeechSettings(**LEGACY_SETTINGS, punctuation=True))
    raw = json.loads(store.path.read_bytes())
    assert raw['schema'] == 'carr-live-reading/2'
    assert store.load('First original.') == first
    assert store.load('Second original.')['settings'] == {**LEGACY_SETTINGS, 'punctuation': True}
    store.remember('Second original.', 4, SpeechSettings(**LEGACY_SETTINGS))
    assert json.loads(store.path.read_bytes())['schema'] == 'carr-live-reading/1'
    assert store.load('First original.') == first


@pytest.mark.parametrize('schema,value', [
    ('carr-live-reading/1', True), ('carr-live-reading/2', 1),
    ('carr-live-reading/2', 'true'), ('carr-live-reading/3', True),
])
def test_unversioned_or_untyped_punctuation_is_never_accepted(tmp_path, schema, value):
    store = SpeechPositionStore(tmp_path)
    store.remember('Exact text.', 1, SpeechSettings(**LEGACY_SETTINGS))
    raw = json.loads(store.path.read_bytes())
    raw['schema'] = schema
    raw['positions'][0]['settings']['punctuation'] = value
    with pytest.raises(ValueError):
        decode_positions(json.dumps(raw).encode())


def test_reader_rejects_unsupported_punctuation_before_render():
    settings = SpeechSettings(**LEGACY_SETTINGS, punctuation=True)
    with pytest.raises(ValueError, match='punctuation'):
        settings.check_voice(Voice('fixture:voice', 'No punctuation', ('en',), 'fixture'))
    settings.check_voice(Voice('fixture:voice', 'Native punctuation', ('en',), 'fixture',
                              controls=('rate', 'volume', 'punctuation')))


class Recorder:
    id = 'fixture'
    def __init__(self, supported=True):
        self.calls = []
        self.supported = supported
    def voices(self, *, cancelled):
        cancelled()
        return (Voice('fixture:voice', 'Synthetic boundary', ('en',), self.id,
            controls=('rate', 'volume', 'punctuation') if self.supported else ('rate', 'volume')),)
    def cache_identity(self, voice, *, cancelled):
        cancelled()
        return {'voice': voice, 'synthetic': True}
    def synthesize(self, text, output, *, cancelled, **options):
        self.calls.append((text, dict(options)))
        silence(output, 800, sample_rate=8000)
        cancelled()
        return output


@pytest.mark.parametrize('punctuation', [False, True])
def test_real_reader_session_forwards_only_selected_punctuation_and_retains_resource_owner(tmp_path, punctuation):
    adapter = Recorder()
    registry = VoiceRegistry((adapter,))
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1, memory_bytes=64*1024**2))
    text = 'Exact, text!'
    settings = SpeechSettings(**LEGACY_SETTINGS, punctuation=punctuation)
    session = SpeechSession(tmp_path, text, settings, SpeechSelection(0, len(text)),
                            registry=registry, broker=broker)
    try:
        result = session.render_next()
        assert result.selection.start == 0 and result.selection.end == len(text)
        assert adapter.calls[0][0] == text
        captured = adapter.calls[0][1]
        assert ('punctuation' in captured) is punctuation
        if punctuation:
            assert captured['punctuation'] is True
        session.release(completed=True)
        session.checkpoint()
        row = SpeechPositionStore(tmp_path).load(text)
        assert row['cursor'] == len(text)
        assert row['settings'] == settings.engine_options()
        assert broker.snapshot()['leases'] == []
    finally:
        session.close()


def test_registry_does_not_forward_optional_control_to_unsupported_adapter(tmp_path):
    adapter = Recorder(supported=False)
    registry = VoiceRegistry((adapter,))
    with pytest.raises(EngineUnavailable, match='punctuation'):
        registry.synthesize('Text.', tmp_path / 'out.wav', voice='fixture:voice', punctuation=True)
    assert adapter.calls == [] and not (tmp_path / 'out.wav').exists()
