"""Existing explicit history import remains the only session recovery action."""
import json

import pytest

from carr_library.reader import legacy_reading as legacy
from carr_library.reader.narration import NarrationStore
from tests.ui.test_reading_import import flow, finish


def wrap(path):
    document = json.loads(path.read_bytes())
    value = dict(schema='carr.reader-session.v1', document=document,
                 voice_id='sapi5-32:old-unavailable-voice', rate=-4, volume=53,
                 cursor=4, audio_seconds=8.25)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return path.read_bytes()


def test_explicit_session_review_reuses_current_editor_without_applying_saved_voice(flow, monkeypatch):
    frame, dialogs, actions, opened, path = flow
    original = wrap(path)
    monkeypatch.setattr('carr_library.audio.engines.default_registry',
                        lambda **kwargs: pytest.fail('Import selected an old speech engine.'))
    actions.import_file()
    frame.settle()
    assert not dialogs.previews and not opened
    frame.settle()
    snapshot = dialogs.previews[0]
    assert snapshot.session.voice_id == 'sapi5-32:old-unavailable-voice'
    assert snapshot.session.cursor == 4 and snapshot.saved_revision == 0
    assert snapshot.selected_revision == 1
    assert not (frame.library.root / legacy.DIRECTORY).exists()
    assert 'not applied' in legacy.history_description(snapshot)
    frame.settle()
    project = opened[0][0][2]
    assert project.current_text == 'Selected historical text'
    assert project.legacy_origin.archive_sha256 == snapshot.archive_sha256
    assert legacy.LegacyReadingStore(frame.library.root).load(snapshot.archive_sha256).raw == original
    assert path.read_bytes() == original
    assert not (frame.library.root / 'state' / 'reading-speech.json').exists()


@pytest.mark.parametrize('stage', ['file', 'revision', 'preview', 'late_cancel', 'scope'])
def test_cancelled_or_stale_session_review_cannot_publish_or_open(flow, stage):
    frame, dialogs, actions, opened, path = flow
    original = wrap(path)
    if stage == 'file':
        dialogs.selected_path = None
    elif stage == 'revision':
        dialogs.choices = [None]
    elif stage == 'preview':
        dialogs.title = None
    elif stage == 'scope':
        other = frame.library.create_workspace('Another selection')
        dialogs.on_preview = lambda: setattr(frame, 'active', (other, None))
    actions.import_file()
    if stage == 'late_cancel':
        frame.cancel_event.set()
    finish(frame)
    assert not opened and not (frame.library.root / legacy.DIRECTORY).exists()
    assert not (frame.library.root / 'narration-projects').exists()
    assert path.read_bytes() == original


def test_retained_envelope_reopens_all_history_after_original_is_removed(flow):
    frame, dialogs, actions, opened, path = flow
    original = wrap(path)
    actions.import_file()
    finish(frame)
    first = opened[0][0][2]
    path.unlink()
    dialogs.choices = [0, 0]
    actions.open_retained()
    finish(frame)
    second = opened[1][0][2]
    assert second.id != first.id and second.current_text == 'Original'
    assert second.legacy_origin.archive_sha256 == first.legacy_origin.archive_sha256
    retained = legacy.LegacyReadingStore(frame.library.root).load(second.legacy_origin.archive_sha256)
    assert retained.raw == original and retained.session.audio_seconds == 8.25
    assert len(list((frame.library.root / legacy.DIRECTORY).glob('*.json'))) == 1
    assert NarrationStore(frame.library.root).load(second.id)[0] == second
