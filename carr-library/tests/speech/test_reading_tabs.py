"""Real reader recovery, exact-byte concurrency and relocated typed backup."""
from dataclasses import replace
from hashlib import sha256
import json
import shutil
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import read_json_snapshot, state_lock
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderResume
from carr_library.speech.tabs import NAME, MAX_TABS, ReadingTab, ReadingTabs, ReadingTabsStore


def prepared(root):
    app = LibraryApp(root)
    workspace = app.create_workspace('Independent reading copies')
    app.add_text(workspace, 'Retained original', 'Незмінне джерело. 😀', edition_id='original')
    store = ReadingTabsStore(root)
    first, second = ReadingTab('primary', 'First copy'), ReadingTab.new('Друга копія')
    one = ReadingDocument('😀 Original first.\r\nSecond line.')
    one.replace(2, 10, 'Edited')
    two = ReadingDocument('Незмінний другий оригінал.')
    two.replace(0, 9, 'Змінений')
    drafts = (ReaderResume(one, 'voice:missing-first', 2, 71, 3, 4.25),
              ReaderResume(two, 'voice:missing-second', -2, 63, 5, 2.5))
    for tab, resume in zip((first, second), drafts):
        store.draft(tab).save_with_token(resume, expected_token=None)
    value = ReadingTabs((first, second), second.id)
    token = store.save(value, expected_token=None)
    return app, workspace, store, value, token, drafts


def test_exact_json_snapshot_reports_actual_bytes_not_reserialized_value(tmp_path):
    path = tmp_path / 'state.json'
    raw = b'{ "value" : 1 }\n'
    path.write_bytes(raw)
    assert read_json_snapshot(path, max_bytes=len(raw)) == ({'value': 1}, sha256(raw).hexdigest())
    with pytest.raises(ValueError, match='budget'):
        read_json_snapshot(path, max_bytes=len(raw) - 1)
    for limit in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            read_json_snapshot(path, max_bytes=limit)


@pytest.mark.parametrize('which', ['draft', 'membership'])
def test_format_only_competing_write_is_preserved_and_stale_save_refused(tmp_path, which):
    _, _, store, value, token, drafts = prepared(tmp_path / 'library')
    target = store.path if which == 'membership' else store.draft(value.tabs[0]).path
    original = target.read_bytes()
    newer = (json.dumps(json.loads(original), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    assert newer != original
    if which == 'draft':
        draft = store.draft(value.tabs[0])
        _, draft_token = draft.load_with_token()
    target.write_bytes(newer)
    with pytest.raises(ValueError, match='Another window'):
        if which == 'membership':
            store.save(value, expected_token=token)
        else:
            draft.save_with_token(drafts[0], expected_token=draft_token)
    assert target.read_bytes() == newer


def test_fresh_instance_rejects_stale_membership_and_missing_draft(tmp_path):
    _, _, first, value, token, _ = prepared(tmp_path / 'library')
    second = ReadingTabsStore(first.root)
    second.save(replace(value, active=value.tabs[0].id), expected_token=token)
    with pytest.raises(ValueError, match='Another window'):
        first.save(value, expected_token=token)
    _, current = second.load()
    second.draft(value.tabs[1]).path.unlink()
    before = second.path.read_bytes()
    with pytest.raises(ValueError, match='every reading draft'):
        second.save(value, expected_token=current)
    assert second.path.read_bytes() == before


@pytest.mark.parametrize(('identity', 'label'), [
    ('../escape', 'Valid'), ('a' * 31, 'Valid'), ('A' * 32, 'Valid'),
    ('primary', ''), ('primary', ' '), ('primary', 'x' * 81),
    ('primary', 'Line\nbreak'), ('primary', '\ud800'), ('primary', '\x7f'),
])
def test_tab_labels_and_ids_cannot_carry_paths_or_invalid_text(identity, label):
    with pytest.raises(ValueError):
        ReadingTab(identity, label)


@pytest.mark.parametrize('change', ['extra', 'path', 'schema', 'duplicate', 'active', 'many'])
def test_membership_has_only_bounded_passive_fields(change):
    first = ReadingTab('primary', 'First')
    value = ReadingTabs((first,), first.id).to_dict()
    if change == 'extra': value['activate_engine'] = True
    elif change == 'path': value['tabs'][0]['path'] = '../outside'
    elif change == 'schema': value['schema'] = 'unknown'
    elif change == 'duplicate': value['tabs'].append(dict(value['tabs'][0]))
    elif change == 'active': value['active'] = 'f' * 32
    elif change == 'many':
        value['tabs'] = [{'id': f'{index:032x}', 'label': 'Copy'} for index in range(MAX_TABS + 1)]
        value['active'] = value['tabs'][0]['id']
    with pytest.raises(ValueError):
        ReadingTabs.from_dict(value)


def test_actual_backup_delete_and_relocate_keeps_two_independent_histories(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    app, workspace, store, value, _, drafts = prepared(root)
    original = {NAME: store.path.read_bytes()}
    original.update({tab.filename: store.draft(tab).path.read_bytes() for tab in value.tabs})
    monkeypatch.setattr(subprocess, 'Popen', lambda *_args, **_kwargs: pytest.fail('Passive recovery executed a child.'))
    folder = tmp_path / 'backup'
    with state_lock(root / 'state/speech', 'reading-tabs'):
        assert (root / 'state/speech/.reading-tabs.lock').is_file()
        digest = create_backup(app, folder)
    assert not list((folder / 'state/speech').glob('*.lock'))
    shutil.rmtree(root)
    restored = restore_backup(folder, tmp_path / 'relocated', digest)
    assert {name: (restored / name).read_bytes() for name in original} == original
    fresh = ReadingTabsStore(restored)
    recovered, _ = fresh.load()
    assert recovered == value
    for tab, expected in zip(value.tabs, drafts):
        saved = fresh.draft(tab).load()
        assert saved.document.to_dict() == expected.document.to_dict()
        assert (saved.voice_id, saved.rate, saved.volume, saved.cursor, saved.audio_seconds) == (
            expected.voice_id, expected.rate, expected.volume, expected.cursor, expected.audio_seconds)
        assert saved.document.undo()
        assert saved.document.text == saved.document.source_text
    assert LibraryApp(restored).edition_metadata(workspace, 'original').title == 'Retained original'


@pytest.mark.parametrize('change', ['missing-draft', 'unknown-field'])
def test_resigned_invalid_tab_collection_never_publishes_destination(tmp_path, change):
    app, _, store, value, _, _ = prepared(tmp_path / 'library')
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    if change == 'missing-draft':
        missing = value.tabs[1].filename
        (folder / missing).unlink()
        manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != missing]
    else:
        payload = json.loads((folder / NAME).read_bytes())
        payload['tabs'][0]['output_path'] = 'unapproved'
        raw = json.dumps(payload).encode('utf-8')
        (folder / NAME).write_bytes(raw)
        row = next(item for item in manifest['sidecars'] if item['filename'] == NAME)
        row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    destination = tmp_path / 'must-remain-absent'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, sha256(raw).hexdigest())
    assert not destination.exists()


def test_replaced_library_directory_invalidates_existing_store(tmp_path):
    root = tmp_path / 'library'
    _, _, store, _, _, _ = prepared(root)
    root.rename(tmp_path / 'original-library')
    root.mkdir()
    with pytest.raises(ValueError, match='directory changed'):
        store.load()
    assert not (root / NAME).exists()
