"""Actual passive tab backup/restore, bounded histories and manifest bindings."""
from hashlib import sha256
import json
import shutil
import subprocess
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library import durable_state, runtime_settings
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import encode_preferences
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.speech import tabs
from carr_library.speech.tabs import NAME, ReadingTab, ReadingTabs, ReadingTabsStore
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION


def _raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _seal(folder, manifest):
    raw = _raw(manifest)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Independent reading histories')
    app.add_text(workspace, 'Retained original', 'Immutable original. Незмінне джерело. 😀',
                 edition_id='original')
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    store = ReadingTabsStore(app.root)
    first, second = ReadingTab('primary', 'First copy'), ReadingTab('a' * 32, 'Друга копія')
    one = ReadingDocument('😀 Original first.\r\nSecond line.')
    one.replace(2, 10, 'Edited')
    two = ReadingDocument('Незмінний другий оригінал.')
    two.replace(0, len(two.text), '')
    states = (ReaderResume(one, 'unavailable:voice:first', 2, 71, 3, 4.25),
              ReaderResume(two, 'unavailable:voice:second', -2, 63, 0, 2.5))
    for tab, state in zip((first, second), states):
        store.draft(tab).save_with_token(state, expected_token=None)
    index = ReadingTabs((first, second), second.id)
    store.save(index, expected_token=None)
    return app, workspace, store, index, states


def _backup(app, target, shape):
    checksum = create_backup(app, target)
    manifest = json.loads((target / 'manifest.json').read_bytes())
    assert manifest['version'] == 5
    assert manifest['schema_version'] == CURRENT_SCHEMA_VERSION == 6
    if shape == 'native':
        return manifest, checksum
    entries = [{**row, 'filename': 'state/' + row['filename']} for row in manifest['state']]
    for row in manifest['sidecars']['files']:
        name = row['path']
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target / 'sidecars' / name, destination)
        entries.append({'filename': name, 'size_bytes': row['size_bytes'], 'sha256': row['sha256']})
    shutil.rmtree(target / 'sidecars')
    flat = {key: manifest[key] for key in
            ('kind', 'schema_version', 'schema_fingerprint', 'database', 'assets')}
    flat.update(version=4, sidecars=sorted(entries, key=lambda row: row['filename']),
                generated_audio_included=False)
    if 'pending' in manifest:
        flat['pending'] = manifest['pending']
    return flat, _seal(target, flat)


def _change(folder, manifest, name, *, raw=None, remove=False, renamed=None):
    collection = 'sidecars' if type(manifest.get('sidecars')) is list else 'state'
    relative = name if collection == 'sidecars' else name.removeprefix('state/')
    matches = [row for row in manifest[collection] if row['filename'] == relative]
    assert len(matches) == 1
    row, = matches
    path = folder / name
    if remove:
        path.unlink()
        manifest[collection].remove(row)
    else:
        if renamed is not None:
            changed = folder / renamed
            path.rename(changed)
            path = changed
            row['filename'] = renamed if collection == 'sidecars' else renamed.removeprefix('state/')
        if raw is not None:
            path.write_bytes(raw)
        captured = path.read_bytes()
        row.update(size_bytes=len(captured), sha256=sha256(captured).hexdigest())
    return _seal(folder, manifest)


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_tabs_roundtrip_keeps_independent_undo_and_passive_settings(library, tmp_path, monkeypatch, shape):
    app, workspace, store, index, states = library
    names = (NAME, *(tab.filename for tab in index.tabs))
    exact = {name: (app.root / name).read_bytes() for name in names}

    def no_activation(*_args, **_kwargs):
        pytest.fail('Passive backup or restoration activated an engine or child.')

    monkeypatch.setattr(subprocess, 'Popen', no_activation)
    monkeypatch.setattr(runtime_settings, 'voice_registry', no_activation)
    monkeypatch.setattr(runtime_settings, 'audio_runtime', no_activation)
    folder = tmp_path / 'backup'
    _, checksum = _backup(app, folder, shape)
    shutil.rmtree(app.root)
    restored = restore_backup(folder, tmp_path / 'relocated', checksum)
    assert {name: (restored / name).read_bytes() for name in names} == exact
    fresh = ReadingTabsStore(restored)
    recovered, token = fresh.load()
    assert recovered == index and token == sha256(exact[NAME]).hexdigest()
    for tab, expected in zip(index.tabs, states):
        actual = fresh.draft(tab).load()
        assert actual.document.to_dict() == expected.document.to_dict()
        assert (actual.voice_id, actual.rate, actual.volume, actual.cursor, actual.audio_seconds) == (
            expected.voice_id, expected.rate, expected.volume, expected.cursor, expected.audio_seconds)
        assert actual.document.undo()
        assert actual.document.text == actual.document.source_text
    assert states[1].document.text == '' and states[1].document.dirty
    assert LibraryApp(restored).edition_metadata(workspace, 'original').title == 'Retained original'


@pytest.mark.parametrize('shape', ('native', 'flat'))
@pytest.mark.parametrize('change', (
    'missing-draft', 'unknown-field', 'duplicate-tab', 'foreign-active',
    'malformed-index', 'wrong-draft-schema', 'changed-source-hash',
))
def test_resealed_invalid_tab_collection_never_publishes(library, tmp_path, shape, change):
    app, _, _, index, _ = library
    folder = tmp_path / 'backup'
    manifest, _ = _backup(app, folder, shape)
    name = NAME
    if change == 'missing-draft':
        checksum = _change(folder, manifest, index.tabs[1].filename, remove=True)
    else:
        if change in ('wrong-draft-schema', 'changed-source-hash'):
            name = index.tabs[0].filename
        value = json.loads((folder / name).read_bytes())
        if change == 'unknown-field':
            value['tabs'][0]['output_path'] = 'C:\\unapproved\\output.wav'
        elif change == 'duplicate-tab':
            value['tabs'][1]['id'] = value['tabs'][0]['id']
        elif change == 'foreign-active':
            value['active'] = 'b' * 32
        elif change == 'wrong-draft-schema':
            value['schema'] = 'unrelated-private-state'
        elif change == 'changed-source-hash':
            value['document']['source_sha256'] = '0' * 64
        raw = b'not valid JSON\n' if change == 'malformed-index' else _raw(value)
        checksum = _change(folder, manifest, name, raw=raw)
    destination = tmp_path / 'must-remain-absent'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(folder, destination, checksum)
    assert not destination.exists()


@pytest.mark.parametrize('change', ('missing-draft', 'malformed-index', 'wrong-draft-schema'))
def test_snapshot_rejects_invalid_live_tab_membership(library, tmp_path, change):
    app, _, _, index, _ = library
    if change == 'missing-draft':
        (app.root / index.tabs[1].filename).unlink()
    elif change == 'malformed-index':
        (app.root / NAME).write_bytes(b'not valid JSON\n')
    else:
        target = app.root / index.tabs[0].filename
        value = json.loads(target.read_bytes())
        value['schema'] = 'unrelated-private-state'
        target.write_bytes(_raw(value))
    retained = {p.relative_to(app.root).as_posix(): p.read_bytes()
                for p in (app.root / 'state' / 'speech').glob('*.json')}
    destination = tmp_path / 'must-remain-absent'
    with pytest.raises((ValueError, RestoreError)):
        create_backup(app, destination)
    assert not destination.exists()
    assert all((app.root / name).read_bytes() == raw for name, raw in retained.items())


@pytest.mark.parametrize('shape', ('native', 'flat'))
@pytest.mark.parametrize('malformed', (False, True), ids=('valid', 'malformed'))
def test_mixed_case_index_is_typed_without_renaming_user_data(library, tmp_path, shape, malformed):
    app, _, _, _, _ = library
    folder = tmp_path / 'backup'
    manifest, _ = _backup(app, folder, shape)
    alias = 'state/speech/READING-TABS.JSON'
    raw = b'{"schema":"unsupported"}\n' if malformed else (folder / NAME).read_bytes()
    checksum = _change(folder, manifest, NAME, raw=raw, renamed=alias)
    destination = tmp_path / 'restored'
    if malformed:
        with pytest.raises((ValueError, RestoreError)):
            restore_backup(folder, destination, checksum)
        assert not destination.exists()
    else:
        restored = restore_backup(folder, destination, checksum)
        assert (restored / alias).read_bytes() == raw
        assert 'READING-TABS.JSON' in {p.name for p in (restored / 'state/speech').iterdir()}


@pytest.mark.parametrize('shape', ('native', 'flat'))
@pytest.mark.parametrize('budget', ('bytes', 'history'))
def test_aggregate_budget_precedes_reading_document_materialization(
        library, tmp_path, monkeypatch, shape, budget):
    app, _, _, index, _ = library
    folder = tmp_path / 'backup'
    _, checksum = _backup(app, folder, shape)
    raw = [(folder / tab.filename).read_bytes() for tab in index.tabs]
    if budget == 'bytes':
        monkeypatch.setattr(tabs, 'MAX_TOTAL_DRAFT_BYTES', sum(map(len, raw)) - 1)
    else:
        total = sum(tabs._history_characters(json.loads(value)) for value in raw)
        monkeypatch.setattr(tabs, 'MAX_RETAINED_CHARACTERS', total - 1)

    def forbidden_decode(_record):
        pytest.fail('A retained history was materialized before aggregate admission.')

    monkeypatch.setattr(ReaderDraftStore, 'decode', staticmethod(forbidden_decode))
    destination = tmp_path / 'must-remain-absent'
    with pytest.raises(ValueError, match='budget'):
        restore_backup(folder, destination, checksum)
    assert not destination.exists()


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_cancel_after_actual_captured_draft_stops_before_publication(
        library, tmp_path, monkeypatch, shape):
    app, _, _, index, _ = library
    folder = tmp_path / 'backup'
    _, checksum = _backup(app, folder, shape)
    stop, captured = Event(), []
    original = durable_state.read_json_snapshot
    watched = index.tabs[0].filename.rsplit('/', 1)[1]

    def observe(path, *, max_bytes=None):
        result = original(path, max_bytes=max_bytes)
        if path.name == watched:
            captured.append(path)
            stop.set()
        return result

    monkeypatch.setattr(durable_state, 'read_json_snapshot', observe)
    destination = tmp_path / 'must-remain-absent'
    with pytest.raises(InterruptedError):
        restore_backup(folder, destination, checksum, cancelled=stop.is_set)
    assert len(captured) == 1
    assert not destination.exists()


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_unindexed_legacy_reader_drafts_remain_recoverable(library, tmp_path, shape):
    app, _, _, index, states = library
    (app.root / NAME).unlink()
    folder = tmp_path / 'backup'
    _, checksum = _backup(app, folder, shape)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    assert not (restored / NAME).exists()
    for tab, expected in zip(index.tabs, states):
        actual = ReaderDraftStore(restored, slot=tab.slot).load()
        assert actual.document.to_dict() == expected.document.to_dict()
