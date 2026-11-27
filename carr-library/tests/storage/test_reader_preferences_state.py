"""Passive reader state uses the same typed checks in state-only recovery."""
from hashlib import sha256
import json
import subprocess

import pytest

from carr_library.runtime_settings import load_voice_preferences, save_voice_preferences
from carr_library.speech.presentation import PresentationStore, ReadingPresentation
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderResume
from carr_library.speech.tabs import ReadingTab, ReadingTabs, ReadingTabsStore
from carr_library.state_backup import snapshot_state, restore_state


def retained_state(root):
    root.mkdir()
    tabs = ReadingTabsStore(root)
    entry = ReadingTab('primary', 'Незалежна копія')
    document = ReadingDocument('😀 Original.\r\nДруга строка.')
    document.replace(2, 10, 'Edited')
    document.add_bookmark('Exact edited position', 3)
    resume = ReaderResume(document, 'unavailable:uk', -2, 61, 3, 1.5)
    tabs.draft(entry).save_with_token(resume, expected_token=None)
    tabs.save(ReadingTabs((entry,), entry.id), expected_token=None)
    PresentationStore(root).save(ReadingPresentation(zoom=125), expected_token=None)
    preferences = {'favorites': ['unavailable:uk'], 'defaults': {'uk': 'unavailable:uk'},
                   'profiles': {'Тихо': {'voice_id': 'unavailable:uk', 'language': 'uk',
                                         'rate': -2, 'volume': 61}}}
    save_voice_preferences(root / 'state', preferences,
                           expected=load_voice_preferences(root / 'state'))
    return tabs, entry, resume, preferences


def test_actual_state_only_relocation_preserves_drafts_marks_appearance_and_dormant_voices(
        tmp_path, monkeypatch):
    root = tmp_path / 'library'
    tabs, entry, resume, preferences = retained_state(root)
    monkeypatch.setattr(subprocess, 'Popen',
        lambda *_a, **_k: pytest.fail('Passive reader state recovery launched an engine.'))
    stage = tmp_path / 'snapshot'
    entries = snapshot_state(root / 'state', stage)
    expected = {item['filename']: (stage / item['filename']).read_bytes() for item in entries}
    restored = tmp_path / 'relocated'
    restored.mkdir()
    restore_state(stage, restored / 'state', entries)
    assert {name: (restored / 'state' / name).read_bytes() for name in expected} == expected
    recovered, _ = ReadingTabsStore(restored).load()
    assert recovered == ReadingTabs((entry,), entry.id)
    saved = ReadingTabsStore(restored).draft(entry).load()
    assert saved.document.to_dict() == resume.document.to_dict()
    assert (saved.voice_id, saved.rate, saved.volume, saved.cursor, saved.audio_seconds) == (
        resume.voice_id, resume.rate, resume.volume, resume.cursor, resume.audio_seconds)
    assert saved.document.bookmark_position(saved.document.bookmarks[0].id) == 3
    assert saved.document.undo()
    assert saved.document.text == saved.document.source_text
    assert PresentationStore(restored).load()[0].zoom == 125
    assert load_voice_preferences(restored / 'state') == preferences


@pytest.mark.parametrize('damage', ['missing-draft', 'draft-schema', 'tab-field', 'appearance', 'voice-profile'])
def test_resigned_state_only_reader_damage_is_refused_before_caller_publication(tmp_path, damage):
    root = tmp_path / 'library'
    tabs, entry, _, _ = retained_state(root)
    stage = tmp_path / 'snapshot'
    entries = snapshot_state(root / 'state', stage)
    by_name = {item['filename']: item for item in entries}
    if damage == 'missing-draft':
        name = entry.filename[6:]
        (stage / name).unlink()
        entries = [item for item in entries if item['filename'] != name]
    else:
        if damage == 'draft-schema':
            name = entry.filename[6:]
        elif damage == 'tab-field':
            name = 'speech/reading-tabs.json'
        elif damage == 'appearance':
            name = 'speech/reading-presentation.json'
        else:
            name = 'audio/engines.json'
        value = json.loads((stage / name).read_bytes())
        if damage == 'draft-schema':
            value['schema'] = 'unknown-reader'
        elif damage == 'tab-field':
            value['tabs'][0]['output_path'] = 'not-authorized'
        elif damage == 'appearance':
            value['zoom'] = 301
        else:
            value['voice_preferences']['profiles']['Тихо']['volume'] = 101
        raw = (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')
        (stage / name).write_bytes(raw)
        by_name[name].update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    # restore_state is the internal copy/validate phase. Its caller may publish
    # only after return; an invalid staging directory never becomes authority.
    target = tmp_path / 'unpublished-state'
    with pytest.raises(ValueError):
        restore_state(stage, target, entries)
    assert tabs.draft(entry).load() is not None


def test_snapshot_refuses_a_live_tab_index_without_its_retained_draft(tmp_path):
    root = tmp_path / 'library'
    tabs, entry, _, _ = retained_state(root)
    before = tabs.path.read_bytes()
    tabs.draft(entry).path.unlink()
    with pytest.raises(ValueError, match='missing retained draft'):
        snapshot_state(root / 'state', tmp_path / 'snapshot')
    assert tabs.path.read_bytes() == before
