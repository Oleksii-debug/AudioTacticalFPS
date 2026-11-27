"""Atomic valid-draft recovery, not a simulated native screen-reader test."""
import json

import pytest

from carr_library.speech import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume


def test_complete_reading_draft_survives_restart(tmp_path):
    store = ReaderDraftStore(tmp_path, slot='manual')
    document = ReadingDocument('Оригінал\r\nOriginal text.')
    document.replace(0, 8, 'Читання')
    state = ReaderResume(document, 'flite:slt', 0, 70, 5, 12.5)
    target = store.save(state)
    assert target.is_relative_to(tmp_path / 'state' / 'speech')
    recovered = ReaderDraftStore(tmp_path, slot='manual').load()
    assert recovered.document.source_text == document.source_text
    assert recovered.document.text == document.text
    assert (recovered.voice_id, recovered.volume, recovered.cursor, recovered.audio_seconds) == ('flite:slt', 70, 5, 12.5)
    assert recovered.document.undo()
    assert recovered.document.text == recovered.document.source_text


def test_distinct_sources_have_distinct_recovery_slots(tmp_path):
    first = ReaderDraftStore(tmp_path, slot='library-A/edition/revision/1-3')
    second = ReaderDraftStore(tmp_path, slot='library-B/edition/revision/1-3')
    first.save(ReaderResume(ReadingDocument('Private source A')))
    assert second.load() is None
    second.save(ReaderResume(ReadingDocument('Private source B')))
    assert first.load().document.source_text == 'Private source A'
    assert second.load().document.source_text == 'Private source B'


def test_invalid_recovery_fails_closed_and_preserves_previous(tmp_path):
    store = ReaderDraftStore(tmp_path)
    document = ReadingDocument('ABC')
    store.save(ReaderResume(document))
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save(ReaderResume(document, cursor=4))
    assert store.path.read_bytes() == before
    record = json.loads(before)
    record['document']['source_text'] = 'Tampered'
    store.path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='checksum'):
        store.load()


def test_reader_draft_is_in_complete_library_backup(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Reader recovery')
    store = ReaderDraftStore(app.root)
    document = ReadingDocument('Original unchanged')
    document.replace(0, 8, 'Spoken')
    store.save(ReaderResume(document, 'flite:awb', volume=60, cursor=3))
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    recovered_root = tmp_path / 'restored'
    restore_backup(backup, recovered_root, expected_manifest_sha256=checksum)
    restored = ReaderDraftStore(recovered_root).load()
    assert restored.document.source_text == 'Original unchanged'
    assert restored.document.text == 'Spoken unchanged'
    assert restored.voice_id == 'flite:awb' and restored.volume == 60



def test_generic_picker_recovers_unidentified_legacy_edits_as_separate_copy(tmp_path):
    old = ReaderDraftStore(tmp_path, slot='unrecoverable-old-revision-name')
    document = ReadingDocument('Original legacy source.', title='Old saved narration')
    document.set_text('User edits from an earlier release.')
    old.save(ReaderResume(document, 'flite:slt', rate=1, volume=65, cursor=3))
    before = old.path.read_bytes()
    listing = ReaderDraftStore(tmp_path).list_saved()
    summary, = listing.copies
    assert summary.key == old.key and summary.title == document.title
    assert not summary.source_recorded and 'unbound reading copy' in summary.label
    assert summary.reading_revision == 1 and summary.source_characters == len(document.source_text)
    resumed, copied = ReaderDraftStore(tmp_path).open_saved(summary)
    assert copied.path != old.path
    assert not resumed.document.source_bound
    assert resumed.document.text == document.text
    assert (resumed.voice_id, resumed.rate, resumed.volume, resumed.cursor) == ('flite:slt', 1, 65, 3)
    resumed.document.set_text('New recovered edits.')
    copied.save(resumed)
    assert old.path.read_bytes() == before
    refreshed = ReaderDraftStore(tmp_path).list_saved()
    selected = next(item for item in refreshed.copies if item.key == copied.key)
    reopened, _ = ReaderDraftStore(tmp_path).open_saved(selected)
    assert reopened.document.text == 'New recovered edits.'
    assert reopened.document.undo() and reopened.document.text == document.text


def test_listing_does_not_replay_unselected_edit_histories(tmp_path, monkeypatch):
    store = ReaderDraftStore(tmp_path)
    document = ReadingDocument('Original', title='A discoverable draft')
    document.set_text('Saved edited reading')
    store.save(ReaderResume(document))
    monkeypatch.setattr(ReadingDocument, 'from_dict',
        lambda *args: pytest.fail('Discovery must not replay every saved history.'))
    listing = store.list_saved()
    assert len(listing.copies) == 1 and listing.copies[0].reading_revision == 1


def test_saved_picker_detects_changes_after_listing_and_preserves_latest_bytes(tmp_path):
    store = ReaderDraftStore(tmp_path)
    first = ReaderResume(ReadingDocument('Original'))
    store.save(first)
    summary, = store.list_saved().copies
    first.document.set_text('Newer saved work from another reader.')
    store.save(first)
    latest = store.path.read_bytes()
    with pytest.raises(ValueError, match='draft changed'):
        store.open_saved(summary)
    assert store.path.read_bytes() == latest
    assert len(tuple(store.root.glob('*.reader.json'))) == 1


def test_saved_picker_detects_change_during_selected_load(tmp_path, monkeypatch):
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Original')))
    summary, = store.list_saved().copies
    real_load = ReaderDraftStore.load
    changed = False

    def load_then_change(selected):
        nonlocal changed
        result = real_load(selected)
        if not changed:
            changed = True
            newer = ReaderResume(ReadingDocument.from_dict(result.document.to_dict()))
            newer.document.set_text('Concurrent edited draft.')
            selected.save(newer)
        return result

    monkeypatch.setattr(ReaderDraftStore, 'load', load_then_change)
    with pytest.raises(ValueError, match='draft changed'):
        store.open_saved(summary)
    assert real_load(store).document.text == 'Concurrent edited draft.'
    assert len(tuple(store.root.glob('*.reader.json'))) == 1


@pytest.mark.parametrize('phase', ['listing', 'opening'])
def test_saved_draft_cancellation_preserves_all_snapshots(tmp_path, phase):
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Saved text')))
    before = {path.name: path.read_bytes() for path in store.root.glob('*.json')}
    summary, = store.list_saved().copies
    with pytest.raises(InterruptedError, match='cancelled'):
        if phase == 'listing':
            store.list_saved(cancelled=lambda: True)
        else:
            store.open_saved(summary, cancelled=lambda: True)
    assert {path.name: path.read_bytes() for path in store.root.glob('*.json')} == before


def test_saved_draft_listing_is_bounded_and_retains_skipped_files(tmp_path):
    import os
    stores = [ReaderDraftStore(tmp_path, slot=f'saved-{index}') for index in range(3)]
    for index, store in enumerate(stores):
        store.save(ReaderResume(ReadingDocument(f'Source {index}', title=f'Draft {index}')))
        os.utime(store.path, ns=((index + 1) * 1_000_000_000, (index + 1) * 1_000_000_000))
    malformed = stores[0].root / ('a' * 64 + '.reader.json')
    malformed.write_text('not JSON', encoding='utf-8')
    unknown = stores[0].root / 'unknown.reader.json'
    unknown.write_text('keep this unknown file', encoding='utf-8')
    before = {path.name: path.read_bytes() for path in stores[0].root.glob('*.json')}
    listing = stores[0].list_saved(limit=1)
    assert len(listing.copies) == 1 and listing.copies[0].key == stores[2].key
    assert listing.truncated and listing.skipped == 4
    budgeted = stores[0].list_saved(max_bytes=1)
    assert not budgeted.copies and budgeted.truncated
    assert {path.name: path.read_bytes() for path in stores[0].root.glob('*.json')} == before


@pytest.mark.parametrize('options', [{'limit': 0}, {'limit': 101}, {'limit': True},
    {'max_bytes': 0}, {'max_bytes': 64 * 1024 * 1024 + 1}, {'cancelled': None}])
def test_saved_draft_listing_rejects_invalid_budgets(tmp_path, options):
    with pytest.raises(ValueError, match='budget'):
        ReaderDraftStore(tmp_path).list_saved(**options)


def test_invalid_recovery_links_cannot_escape_state_directory(tmp_path):
    from carr_library.durable_state import atomic_json
    store = ReaderDraftStore(tmp_path)
    store._family = 'a' * 64
    outside = tmp_path / 'outside.json'
    outside.write_text('Unrelated original', encoding='utf-8')
    atomic_json(store._links_path(), {'schema': 'carr.reader-recovery-links.v1',
                                     'copies': ['../../outside']})
    with pytest.raises(ValueError, match='links'):
        store.latest_copy()
    assert outside.read_text(encoding='utf-8') == 'Unrelated original'


def test_missing_latest_recovery_is_not_silently_replaced_by_an_older_draft(tmp_path):
    from carr_library.durable_state import atomic_json
    store = ReaderDraftStore(tmp_path)
    store._family = 'a' * 64
    store.save(ReaderResume(ReadingDocument('Older saved work')))
    before = store.path.read_bytes()
    atomic_json(store._links_path(), {'schema': 'carr.reader-recovery-links.v1',
                                     'copies': [store.key, 'b' * 64]}, replace=True)
    with pytest.raises(ValueError, match='most recent recovery copy is missing'):
        store.latest_copy()
    assert store.path.read_bytes() == before


def test_seed_if_absent_preserves_saved_edits_history_and_voice_settings(tmp_path):
    store = ReaderDraftStore(tmp_path, slot='transcription-reading')
    candidate = ReaderResume(ReadingDocument('Verified original transcript.'))
    assert store.save_if_absent(candidate) is candidate
    edited = store.load()
    edited.document.set_text('First user correction.')
    edited.document.set_text('Second user correction.')
    assert edited.document.undo()
    saved = ReaderResume(edited.document, 'flite:awb', rate=2, volume=60, cursor=3, audio_seconds=4)
    store.save(saved)
    before = store.path.read_bytes()
    reopened = ReaderDraftStore(tmp_path, slot='transcription-reading').save_if_absent(
        ReaderResume(ReadingDocument('Verified original transcript.')))
    assert reopened.document.text == 'First user correction.'
    assert reopened.document.source_text == 'Verified original transcript.'
    assert (reopened.voice_id, reopened.rate, reopened.volume, reopened.cursor, reopened.audio_seconds) == (
        'flite:awb', 2, 60, 3, 4)
    assert reopened.document.redo() and reopened.document.text == 'Second user correction.'
    assert store.path.read_bytes() == before


def test_seed_if_absent_rejects_invalid_existing_state_without_overwrite(tmp_path):
    store = ReaderDraftStore(tmp_path)
    invalid = b'{"schema":"unknown","document":"unreadable but preserved"}'
    store.path.write_bytes(invalid)
    with pytest.raises(ValueError):
        store.save_if_absent(ReaderResume(ReadingDocument('New transcript')))
    assert store.path.read_bytes() == invalid


def test_concurrent_first_seed_never_overwrites_the_winning_draft(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store = ReaderDraftStore(tmp_path, slot='concurrent-seed')
    gate = Barrier(2)

    def seed(label):
        local = ReaderDraftStore(tmp_path, slot='concurrent-seed')
        candidate = ReaderResume(ReadingDocument(label))
        gate.wait(timeout=5)
        try:
            return local.save_if_absent(candidate).document.text
        except RuntimeError as exc:
            assert 'already being changed' in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(seed, label) for label in ('First seed', 'Second seed')]
        results = [future.result(timeout=10) for future in futures]
    winner = store.load().document.text
    assert winner in ('First seed', 'Second seed')
    assert any(result == winner for result in results)
    assert all(result in (None, winner) for result in results)
    before = store.path.read_bytes()
    assert store.save_if_absent(ReaderResume(ReadingDocument('Later seed'))).document.text == winner
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('operation', ['save', 'seed', 'separate'])
@pytest.mark.parametrize('failure', ['invalid-links', 'busy-links', 'write-links'])
def test_published_reading_draft_survives_discovery_failure(tmp_path, monkeypatch, operation, failure):
    from contextlib import nullcontext
    from carr_library.speech import session
    from carr_library.durable_state import state_lock

    store = ReaderDraftStore(tmp_path, slot='published-draft')
    store._family = 'a' * 64
    original = ReaderResume(ReadingDocument('Original retained source.'))
    if operation != 'seed':
        store.save(original)
    previous = store.path.read_bytes() if store.path.exists() else None
    links = store._links_path()
    previous_links = links.read_bytes() if links.exists() else None
    candidate = ReaderResume(ReadingDocument.from_dict(original.document.to_dict()),
                             'fixture:voice', rate=2, volume=65, cursor=3)
    candidate.document.set_text('Saved user edits with undo history.')
    atomic_json = session.atomic_json
    if failure == 'invalid-links':
        links.write_bytes(b'{"schema":"invalid-recovery-links"}')
    elif failure == 'write-links':
        other = ReaderDraftStore(tmp_path, slot='another-published-copy')
        other._family = store._family
        other.save(original)

        def fail_link_write(path, value, *, replace=False):
            if path == links:
                raise OSError('Synthetic discovery write failure.')
            return atomic_json(path, value, replace=replace)
        monkeypatch.setattr(session, 'atomic_json', fail_link_write)
    unchanged_links = links.read_bytes() if links.exists() else None
    lock = state_lock(store.root, 'recovery-' + store._family) if failure == 'busy-links' else nullcontext()
    with lock:
        if operation == 'save':
            assert store.save(candidate) == store.path
            saved = store
        elif operation == 'seed':
            assert store.save_if_absent(candidate) is candidate
            saved = store
        else:
            resumed, saved = store.separate_copy(candidate)
            assert isinstance(resumed, ReaderResume) and saved.path != store.path
            assert resumed.document.to_dict() == candidate.document.to_dict()
        assert 'Reading draft saved.' in saved.discovery_warning
        assert 'Automatic source recovery could not be updated' in saved.discovery_warning
    reopened = saved._referenced_store(saved.key).load()
    assert reopened.document.to_dict() == candidate.document.to_dict()
    assert (reopened.voice_id, reopened.rate, reopened.volume, reopened.cursor) == ('fixture:voice', 2, 65, 3)
    assert reopened.document.undo() and reopened.document.text == original.document.source_text
    assert set(json.loads(saved.path.read_bytes())) == {
        'schema', 'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds'}
    assert (links.read_bytes() if links.exists() else None) == unchanged_links
    if operation == 'separate':
        assert store.path.read_bytes() == previous

    # A later successful link update clears this operation's warning.
    monkeypatch.setattr(session, 'atomic_json', atomic_json)
    if failure == 'invalid-links':
        if previous_links is None:
            links.unlink()
        else:
            links.write_bytes(previous_links)
    assert saved.save(candidate) == saved.path
    assert saved.discovery_warning == ''
    assert saved.latest_copy().key == saved.key


@pytest.mark.parametrize('operation', ['save', 'seed', 'separate'])
def test_draft_failure_before_publication_still_raises_and_preserves_saved_files(tmp_path, monkeypatch, operation):
    from carr_library.speech import session

    store = ReaderDraftStore(tmp_path, slot='prepublication-failure')
    store._family = 'a' * 64
    original = ReaderResume(ReadingDocument('Previously saved original.'))
    if operation != 'seed':
        store.save(original)
    before = {path.name: path.read_bytes() for path in store.root.glob('*.json')}
    candidate = ReaderResume(ReadingDocument('Replacement that was not published.'))
    atomic_json = session.atomic_json

    def fail_draft_write(path, value, *, replace=False):
        if path.name.endswith('.reader.json'):
            raise OSError('Synthetic draft write failure before publication.')
        return atomic_json(path, value, replace=replace)

    monkeypatch.setattr(session, 'atomic_json', fail_draft_write)
    with pytest.raises(OSError, match='before publication'):
        if operation == 'save':
            store.save(candidate)
        elif operation == 'seed':
            store.save_if_absent(candidate)
        else:
            store.separate_copy(candidate)
    assert {path.name: path.read_bytes() for path in store.root.glob('*.json')} == before
    assert getattr(store, 'discovery_warning', '') == ''
