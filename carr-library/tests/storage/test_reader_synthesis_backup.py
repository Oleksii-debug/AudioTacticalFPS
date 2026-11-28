"""Actual native and flat library backups retain optional synthesis data passively."""
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library import runtime_settings
from carr_library.durable_state import read_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import restore_backup
from carr_library.speech import ReadingDocument, VoiceRegistry
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.speech.tabs import ReadingTab, ReadingTabs, ReadingTabsStore
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION


# Use the existing real native5 -> supported flat4 producer, including its
# independent database/asset manifest, rather than manufacturing authority.
BACKUP = runpy.run_path(str(Path(__file__).with_name('test_reading_tabs_backup.py')))
BINDINGS = {'fixture.native-options': 'Ctrl+F9'}


def library(tmp_path, options):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Native synthesis recovery')
    raw = 'Immutable original! Незмінне джерело. 😀\r\nOriginal second line.'
    imported = app.add_text(workspace, 'Original', raw)
    KeybindingStore(app.root).save(
        [Command('fixture.native-options', 'Synthetic native-options fixture')], BINDINGS)
    store = ReadingTabsStore(app.root)
    first, second = ReadingTab('primary', 'First'), ReadingTab('a' * 32, 'Second')
    one = ReadingDocument(raw)
    one.replace(0, len('Immutable'), 'Reading')
    two = ReadingDocument('Second original retained after empty editing.')
    two.replace(0, len(two.text), '')
    states = (ReaderResume(one, 'unavailable:native', -2, 64, 3, 1.5,
                           pitch=options[0], punctuation=options[1]),
              ReaderResume(two, 'unavailable:other', 1, 41, 0, 0,
                           pitch=99, punctuation=True))
    for tab, state in zip((first, second), states):
        store.draft(tab).save_with_token(state, expected_token=None)
    index = ReadingTabs((first, second), second.id)
    store.save(index, expected_token=None)
    return app, workspace, imported, raw, store, index, states


@pytest.mark.parametrize('shape', ['native', 'flat'])
@pytest.mark.parametrize('options', [(None, False), (0, False), (None, True), (73, True)])
def test_backup_restores_exact_optional_settings_and_both_histories_without_voice_activation(
        tmp_path, monkeypatch, shape, options):
    app, workspace, imported, raw, store, index, states = library(tmp_path, options)
    before = {tab.filename: (app.root / tab.filename).read_bytes() for tab in index.tabs}
    keymap = KeybindingStore(app.root).path.read_bytes()
    def denied(*_args, **_kwargs):
        pytest.fail('Passive synthesis restoration discovered an engine, model or child.')
    monkeypatch.setattr(VoiceRegistry, 'discover', denied)
    monkeypatch.setattr(runtime_settings, 'voice_registry', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    folder = tmp_path / 'backup'
    manifest, checksum = BACKUP['_backup'](app, folder, shape)
    assert manifest['version'] == (5 if shape == 'native' else 4)
    assert manifest['schema_version'] == CURRENT_SCHEMA_VERSION == 6
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    new_store = ReadingTabsStore(restored)
    restored_index, _ = new_store.load()
    assert restored_index == index
    loaded = [new_store.draft(tab).load() for tab in index.tabs]
    assert [(state.pitch, state.punctuation) for state in loaded] == [options, (99, True)]
    assert [(state.voice_id, state.rate, state.volume, state.cursor, state.audio_seconds)
            for state in loaded] == [(state.voice_id, state.rate, state.volume, state.cursor, state.audio_seconds)
                                    for state in states]
    for tab, state, expected in zip(index.tabs, loaded, states):
        assert state.document.to_dict() == expected.document.to_dict()
        assert (app.root / tab.filename).read_bytes() == (folder / tab.filename).read_bytes() == before[tab.filename]
        assert (restored / tab.filename).read_bytes() == before[tab.filename]
    assert loaded[1].document.text == '' and loaded[1].document.undo()
    assert loaded[1].document.text == states[1].document.source_text
    assert loaded[0].document.undo() and loaded[0].document.text == raw
    assert KeybindingStore(restored).load() == BINDINGS
    assert KeybindingStore(restored).path.read_bytes() == keymap
    restored_app = LibraryApp(restored)
    assert restored_app.assets.resolve(imported['asset']).read_bytes() == raw.encode('utf-8')
    assert restored_app.search(workspace, 'Immutable')


@pytest.mark.parametrize('shape', ['native', 'flat'])
@pytest.mark.parametrize('change', ['pitch-boolean', 'pitch-range', 'punctuation-integer', 'unknown-field'])
def test_rehashed_invalid_native_options_are_rejected_by_both_backup_readers(
        tmp_path, shape, change):
    app, _, _, _, _, index, _ = library(tmp_path, (73, True))
    folder = tmp_path / 'backup'
    manifest, _ = BACKUP['_backup'](app, folder, shape)
    name = index.tabs[0].filename
    value = read_json(folder / name)
    if change == 'pitch-boolean': value['pitch'] = True
    elif change == 'pitch-range': value['pitch'] = 100
    elif change == 'punctuation-integer': value['punctuation'] = 1
    else: value['playback_pitch'] = 3
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    checksum = BACKUP['_change'](folder, manifest, name, raw=raw)
    manifest_before = (folder / 'manifest.json').read_bytes()
    target = tmp_path / 'refused'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(folder, target, checksum)
    assert not target.exists()
    assert (folder / name).read_bytes() == raw
    assert sha256((folder / 'manifest.json').read_bytes()).hexdigest() == checksum
    assert (folder / 'manifest.json').read_bytes() == manifest_before


@pytest.mark.parametrize('shape', ['native', 'flat'])
def test_reader_pause_settings_survive_real_backup_as_passive_exact_bytes(tmp_path, monkeypatch, shape):
    from dataclasses import replace
    app, _, imported, raw, store, index, states = library(tmp_path, (73, True))
    selected = replace(states[0], sentence_pause_ms=125, paragraph_pause_ms=750, section_pause_ms=2000)
    store.draft(index.tabs[0]).save(selected)
    original = (app.root / index.tabs[0].filename).read_bytes()
    def denied(*args, **kwargs):
        pytest.fail('Restoring extra reading pauses must not discover or launch a voice.')
    monkeypatch.setattr(VoiceRegistry, 'discover', denied)
    monkeypatch.setattr(runtime_settings, 'voice_registry', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    folder = tmp_path / 'backup'
    _, checksum = BACKUP['_backup'](app, folder, shape)
    root = restore_backup(folder, tmp_path / 'restored', checksum)
    loaded = ReadingTabsStore(root).draft(index.tabs[0]).load()
    assert (loaded.sentence_pause_ms, loaded.paragraph_pause_ms, loaded.section_pause_ms) == (125, 750, 2000)
    assert (loaded.pitch, loaded.punctuation, loaded.voice_id) == (73, True, selected.voice_id)
    assert loaded.document.to_dict() == selected.document.to_dict()
    assert (root / index.tabs[0].filename).read_bytes() == original
    assert (folder / index.tabs[0].filename).read_bytes() == original
    assert LibraryApp(root).assets.resolve(imported['asset']).read_bytes() == raw.encode('utf-8')


@pytest.mark.parametrize('shape', ['native', 'flat'])
@pytest.mark.parametrize('field,value', [
    ('sentence_pause_ms', True), ('paragraph_pause_ms', 30001), ('section_pause_ms', '1000')])
def test_rehashed_unbounded_pause_settings_are_refused_before_restore_effects(tmp_path, shape, field, value):
    app, _, _, _, _, index, _ = library(tmp_path, (73, True))
    folder = tmp_path / 'backup'
    manifest, _ = BACKUP['_backup'](app, folder, shape)
    name = index.tabs[0].filename
    candidate = read_json(folder / name)
    candidate[field] = value
    raw = (json.dumps(candidate, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    checksum = BACKUP['_change'](folder, manifest, name, raw=raw)
    before = (folder / 'manifest.json').read_bytes()
    destination = tmp_path / 'refused'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(folder, destination, checksum)
    assert not destination.exists()
    assert (folder / name).read_bytes() == raw
    assert (folder / 'manifest.json').read_bytes() == before
