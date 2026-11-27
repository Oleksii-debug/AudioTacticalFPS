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
        manifest['state'] = [row for row in manifest['state']
                             if row['filename'] != missing.removeprefix('state/')]
    else:
        payload = json.loads((folder / NAME).read_bytes())
        payload['tabs'][0]['output_path'] = 'unapproved'
        raw = json.dumps(payload).encode('utf-8')
        (folder / NAME).write_bytes(raw)
        row = next(item for item in manifest['state'] if item['filename'] == NAME.removeprefix('state/'))
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


def test_fresh_tab_save_never_overwrites_a_competing_create_at_publication(tmp_path, monkeypatch):
    from carr_library.speech import session
    store = session.ReaderDraftStore(tmp_path)
    resume = ReaderResume(ReadingDocument('The proposed reading copy.'))
    original = session.atomic_json
    winning = b'{"independent":"already published"}\n'
    def competing(path, value, *, replace=False):
        Path(path).write_bytes(winning)
        assert replace is False
        return original(path, value, replace=replace)
    from pathlib import Path
    monkeypatch.setattr(session, 'atomic_json', competing)
    with pytest.raises(FileExistsError):
        store.save_with_token(resume, expected_token=None)
    assert store.path.read_bytes() == winning
    assert resume.document.text == 'The proposed reading copy.'


@pytest.mark.parametrize('budget', ['bytes', 'history'])
def test_aggregate_recovery_budget_precedes_every_document_materialization(tmp_path, monkeypatch, budget):
    from carr_library.speech import tabs
    _, _, store, value, _, _ = prepared(tmp_path / 'library')
    if budget == 'bytes':
        sizes = [store.draft(tab).path.stat().st_size for tab in value.tabs]
        monkeypatch.setattr(tabs, 'MAX_TOTAL_DRAFT_BYTES', sum(sizes) - 1)
    else:
        records = [json.loads(store.draft(tab).path.read_bytes()) for tab in value.tabs]
        count = sum(tabs._history_characters(record) for record in records)
        monkeypatch.setattr(tabs, 'MAX_RETAINED_CHARACTERS', count - 1)
    monkeypatch.setattr(tabs.ReaderDraftStore, 'decode', staticmethod(
        lambda _record: pytest.fail('A document was materialized before aggregate admission.')))
    before = {tab.filename: store.draft(tab).path.read_bytes() for tab in value.tabs}
    with pytest.raises(ValueError, match='aggregate'):
        store.read_drafts(value)
    assert {tab.filename: store.draft(tab).path.read_bytes() for tab in value.tabs} == before


def test_recovery_growth_cannot_borrow_the_next_drafts_preflight_budget(tmp_path, monkeypatch):
    from carr_library.speech import tabs
    _, _, store, value, _, _ = prepared(tmp_path / 'library')
    original = tabs.read_json_snapshot
    target = store.draft(value.tabs[0]).path
    retained = target.read_bytes()
    seen = []
    def grow(path, *, max_bytes=None):
        if path == target and not seen:
            seen.append(max_bytes)
            path.write_bytes(retained + b' ' * 64)
        return original(path, max_bytes=max_bytes)
    monkeypatch.setattr(tabs, 'read_json_snapshot', grow)
    monkeypatch.setattr(tabs.ReaderDraftStore, 'decode', staticmethod(
        lambda _record: pytest.fail('Growing recovery reached history materialization.')))
    with pytest.raises(ValueError, match='budget'):
        store.read_drafts(value)
    assert seen == [len(retained)]
    assert target.read_bytes() == retained + b' ' * 64


def test_cancelled_recovery_never_publishes_or_materializes_another_draft(tmp_path, monkeypatch):
    from carr_library.speech import tabs
    _, _, store, value, _, _ = prepared(tmp_path / 'library')
    before = store.path.read_bytes()
    calls = []
    original = tabs.ReaderDraftStore.decode
    def decoded(record):
        calls.append(record)
        return original(record)
    monkeypatch.setattr(tabs.ReaderDraftStore, 'decode', staticmethod(decoded))
    with pytest.raises(InterruptedError, match='cancelled'):
        tabs.prepare_tabs(store.root, ReaderResume(ReadingDocument('not adopted')),
                          cancelled=lambda: bool(calls))
    assert len(calls) == 1 and store.path.read_bytes() == before


def test_snapshot_token_binds_actual_shared_reader_bytes_during_atomic_replacement(tmp_path, monkeypatch):
    from carr_library import durable_state
    path = tmp_path / 'snapshot.json'
    before, after = b'{ "source": "captured" }\n', b'{"source":"replacement"}\n'
    path.write_bytes(before)
    original = durable_state._open_json_file
    retained = tmp_path / 'retained-old.json'
    def opened(target):
        stream = original(target)
        target.rename(retained)
        target.write_bytes(after)
        return stream
    monkeypatch.setattr(durable_state, '_open_json_file', opened)
    value, token = durable_state.read_json_snapshot(path)
    assert value == {'source': 'captured'} and token == sha256(before).hexdigest()
    assert path.read_bytes() == after
