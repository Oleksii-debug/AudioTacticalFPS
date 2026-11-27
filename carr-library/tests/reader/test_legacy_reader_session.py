"""Exact canonical reader-session envelopes through the current history store."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from carr_library.reader import legacy_reading as legacy
from carr_library.reader.narration import NarrationStore, encode_project
from tests.reader.test_legacy_reading_import import payload, raw


def envelope(*, bound=False):
    return dict(schema='carr.reader-session.v1', document=payload(bound=bound),
                voice_id='sapi5-32:historical-inert-voice', rate=-3, volume=71,
                cursor=9, audio_seconds=12.75)


@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('revision', [0, 1, 2, 3])
def test_envelope_retains_exact_bytes_all_revisions_and_passive_observations(bound, revision):
    data = raw(envelope(bound=bound))
    snapshot = legacy.decode_snapshot(data, revision=revision)
    original = legacy.decode_snapshot(raw(payload(bound=bound)), revision=revision)
    assert snapshot.raw == data and snapshot.archive_sha256 == sha256(data).hexdigest()
    assert snapshot.archive_sha256 != original.archive_sha256
    assert snapshot.text == original.text and snapshot.runs == original.runs
    assert snapshot.source_anchor == original.source_anchor
    assert snapshot.edits == original.edits and snapshot.saved_revision == 1
    assert asdict(snapshot.session) == dict(voice_id='sapi5-32:historical-inert-voice',
        rate=-3, volume=71, cursor=9, audio_seconds=12.75)
    assert snapshot.at(3).session == snapshot.session
    assert snapshot.origin.archive_sha256 == snapshot.archive_sha256
    description = legacy.history_description(snapshot)
    assert 'review only' in description and 'not applied' in description
    assert 'Saved character cursor at history revision 1: 9.' in description
    assert '12.75 seconds' in description and 'No audio is retained' in description


def test_saved_character_cursor_is_validated_against_saved_not_selected_revision():
    value = envelope()
    value['document'].update(source_text='x', source_sha256=sha256(b'x').hexdigest(),
        edits=[dict(start=0, end=1, replacement='Much longer saved revision', label='Expand')],
        revision=1)
    value['cursor'] = len(value['document']['edits'][0]['replacement'])
    snapshot = legacy.decode_snapshot(raw(value), revision=0)
    assert snapshot.text == 'x' and snapshot.session.cursor > len(snapshot.text)
    value['document']['revision'] = 0
    with pytest.raises(ValueError, match='saved revision'):
        legacy.decode_snapshot(raw(value), revision=1)


@pytest.mark.parametrize('field,bad', [
    ('voice_id', 1), ('voice_id', '\0'), ('voice_id', 'x' * 2049),
    ('rate', True), ('rate', -11), ('rate', 11), ('rate', 0.0),
    ('volume', True), ('volume', -1), ('volume', 101), ('volume', '80'),
    ('cursor', True), ('cursor', -1), ('cursor', 999), ('cursor', 1.0),
    ('audio_seconds', True), ('audio_seconds', -0.1), ('audio_seconds', 31_536_001),
    ('audio_seconds', float('nan')), ('audio_seconds', float('inf')), ('audio_seconds', '1'),
])
def test_invalid_session_observations_never_publish_an_archive(tmp_path, field, bad):
    value = envelope()
    value[field] = bad
    with pytest.raises(ValueError):
        legacy.LegacyReadingStore(tmp_path).retain(raw(value))
    assert not (tmp_path / legacy.DIRECTORY).exists()


@pytest.mark.parametrize('change', ['extra', 'missing', 'nested', 'invalid_document', 'duplicate'])
def test_session_envelope_fields_and_inner_history_are_exact(change):
    value = envelope()
    if change == 'extra':
        value['runtime'] = {'execute': 'never'}
    elif change == 'missing':
        del value['voice_id']
    elif change == 'nested':
        value['document'] = envelope()
    elif change == 'invalid_document':
        value['document']['source_sha256'] = '0' * 64
    else:
        with pytest.raises(ValueError):
            legacy.decode_snapshot(raw(value).replace(b'"cursor":9', b'"cursor":9,"cursor":0'))
        return
    with pytest.raises(ValueError):
        legacy.decode_snapshot(raw(value))


def test_boundary_observations_remain_inert_and_are_not_added_to_current_project(tmp_path, monkeypatch):
    value = envelope()
    value.update(voice_id='', rate=10, volume=100, cursor=0, audio_seconds=31_536_000)
    data = raw(value)
    monkeypatch.setattr('carr_library.audio.engines.default_registry',
                        lambda **kwargs: pytest.fail('Import activated a speech engine.'))
    snapshot = legacy.decode_snapshot(data)
    store = legacy.LegacyReadingStore(tmp_path)
    assert store.retain(data) == snapshot.archive_sha256
    project = legacy.project_from_snapshot(snapshot, title='Explicit current derivative')
    current = json.loads(encode_project(project))
    assert not {'voice_id', 'rate', 'volume', 'audio_seconds'} & set(current)
    assert project.legacy_origin.archive_sha256 == snapshot.archive_sha256
    assert project.cursor == 1
    projects = NarrationStore(tmp_path)
    digest = projects.save(project)
    assert projects.load(project.id) == (project, digest)
    assert store.load(snapshot.archive_sha256).session == snapshot.session
    assert not (tmp_path / 'state' / 'reading-speech.json').exists()


def test_bare_reading_history_contract_remains_unchanged():
    data = raw(payload())
    snapshot = legacy.decode_snapshot(data)
    assert snapshot.session is None and snapshot.raw == data
    assert 'Historical reader-session settings' not in legacy.history_description(snapshot)
    assert snapshot.at(3).session is None


def test_cancelled_session_validation_does_not_publish_even_with_a_valid_saved_revision(tmp_path):
    value = envelope()
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) >= 5
    with pytest.raises(InterruptedError):
        legacy.LegacyReadingStore(tmp_path).retain(raw(value), cancelled=cancelled)
    assert not (tmp_path / legacy.DIRECTORY).exists()
