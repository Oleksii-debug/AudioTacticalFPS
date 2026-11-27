"""Exact canonical root-v4 and state/reader-v5 recovery through the current reader."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_historical import DIRECTORY
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.progress import decode_preferences, load_preferences
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from .test_legacy_reading_backup import imported_history, canonical_session


def record(name, raw):
    return dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())


def seal(folder, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\r\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def contents(folder):
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob('*') if path.is_file()}


def canonical_preferences(**changes):
    value = dict(mode='beep_speech', interval_seconds=7, volume=27,
                 muted=False, allow_background=True, quiet_during_media=True)
    value.update(changes)
    return (json.dumps(dict(version=1, preferences=value), indent=2) + '\r\n').encode()


def source_fixture(tmp_path, *, family='common', pending=False, reading=True):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Canonical source with exact history')
    book = app.add_text(workspace, 'Reading source', 'First source.\fКиїв — друга частина.')
    location = ReadingDesk(app, workspace, book['edition']).page(2).location
    state = ReadingState(app.root)
    state.remember(location)
    bookmark = state.add_bookmark(location, 'Saved exact historical place')
    source = tmp_path / 'queued.txt'
    source.write_text('Queued canonical original.', encoding='utf-8')
    batch = app.plan_import_batch(workspace, [source]) if pending else None
    folder = tmp_path / 'canonical-backup'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    # This test transforms only the enclosing envelope of a real checked
    # database/originals/pending snapshot into the exact canonical wire format.
    for entry in manifest.pop('sidecars', []):
        path = folder / entry['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        (folder / 'sidecars' / entry['path']).rename(path)
    if (folder / 'sidecars').exists():
        shutil.rmtree(folder / 'sidecars')
    if not pending:
        manifest.pop('pending', None)
        if (folder / 'pending').exists():
            shutil.rmtree(folder / 'pending')
    payloads = {
        'state/audio/engines.json': b'{"executable":"C:\\external\\never-run.exe"}\r\n',
        'state/assistant/' + 'a' * 32 + '.json': b'{"foreign-approved-plan":"never run"}\r\n',
        'state/providers/offline.profile.json': b'{"credential_ref":"opaque-existing-reference"}\r\n',
    }
    if reading:
        payloads['reading-state.json'] = (folder / 'reading-state.json').read_bytes()
    else:
        (folder / 'reading-state.json').unlink()
    if family == 'common':
        payloads['progress-preferences.json'] = canonical_preferences()
    for name, raw in payloads.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    if family == 'common':
        manifest.update(version=4, sidecars=[record(name, raw) for name, raw in payloads.items()],
                        generated_audio_included=False)
    else:
        assert reading
        manifest.update(version=5, state=[record(name.removeprefix('state/'), raw)
            for name, raw in payloads.items() if name.startswith('state/')],
            reading_state=record('reading-state.json', payloads['reading-state.json']),
            generated_audio_included=False, pending=manifest.get('pending'))
    digest = seal(folder, manifest)
    return app, workspace, book, location, bookmark, source, batch, folder, manifest, digest, payloads


@pytest.mark.parametrize('family', ['common', 'legacy-v5'])
@pytest.mark.parametrize('pending', [False, True], ids=['no-pending', 'resumable-pending'])
def test_exact_canonical_formats_restore_reading_and_pending_without_activating_foreign_state(
        tmp_path, family, pending):
    (_, workspace, book, location, bookmark, source, batch, folder,
     manifest, digest, payloads) = source_fixture(tmp_path, family=family, pending=pending)
    before = contents(folder)
    assert verify_backup(folder, digest) == manifest
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert contents(folder) == before
    current = LibraryApp(restored)
    assert ReadingState(restored).resume(workspace, book['edition']) == location
    assert ReadingState(restored).bookmarks(workspace, book['edition']) == (bookmark,)
    assert (restored / 'reading-state.json').read_bytes() == payloads['reading-state.json']
    archive = restored / DIRECTORY / digest
    assert (archive / 'manifest.json').read_bytes() == before['manifest.json']
    for name, raw in payloads.items():
        assert (archive / 'files' / sha256(name.encode('utf-8')).hexdigest()).read_bytes() == raw
    for name in ('assistant', 'providers', 'audio'):
        assert not (restored / 'state' / name).exists()
    if family == 'common':
        preferences = load_preferences(restored)
        assert preferences.volume == .27 and preferences.background
        assert preferences.muted and preferences.voice_id == ''
        assert (restored / 'progress-preferences.json').read_bytes() == payloads['progress-preferences.json']
    source.unlink()
    if pending:
        completed = current.run_import_batch(workspace, batch['batch_id'])
        assert completed['state'] == 'complete'
        assert current.search(workspace, 'Queued canonical')[0].quote == 'Queued canonical'
    second = tmp_path / 'second'
    new_digest = create_backup(current, second)
    verify_backup(second, new_digest)
    again = restore_backup(second, tmp_path / 'again', new_digest)
    assert contents(again / DIRECTORY / digest) == contents(archive)
    assert (again / 'reading-state.json').read_bytes() == payloads['reading-state.json']


def test_empty_common_v4_inventory_is_a_valid_checked_snapshot(tmp_path):
    *_, folder, manifest, _, payloads = source_fixture(tmp_path, reading=False)
    for name in payloads:
        (folder / name).unlink()
    shutil.rmtree(folder / 'state')
    manifest['sidecars'] = []
    digest = seal(folder, manifest)
    assert verify_backup(folder, digest) == manifest
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert not (restored / 'reading-state.json').exists()
    assert (restored / DIRECTORY / digest / 'manifest.json').read_bytes() == (folder / 'manifest.json').read_bytes()


@pytest.mark.parametrize('forged_anchor', [False, True], ids=['exact-history', 'foreign-anchor'])
def test_common_v4_keeps_nested_session_and_current_reading_history_bound_to_database(
        imported_history, tmp_path, forged_anchor):
    from carr_library.reader.legacy_reading import (
        LegacyReadingStore, snapshot_session_files, validate_session_payload, project_from_snapshot,
    )
    from carr_library.reader.narration import NarrationStore
    app, workspace, _, snapshot, project, _ = imported_history
    path, raw, _ = canonical_session(app, snapshot)
    relative = path.relative_to(app.root).as_posix()
    app.update_metadata(workspace, snapshot.source_anchor.edition, title='Later metadata')
    folder = tmp_path / 'common-history'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entries = []
    for item in manifest['sidecars']:
        name = item['path']
        if name != relative:
            continue
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        (folder / 'sidecars' / name).rename(target)
        entries.append(dict(filename=name, size_bytes=item['size_bytes'], sha256=item['sha256']))
    shutil.rmtree(folder / 'sidecars')
    if forged_anchor:
        changed = json.loads(raw)
        changed['document']['source_anchor']['asset_sha256'] = 'f' * 64
        raw = json.dumps(changed).encode()
        (folder / relative).write_bytes(raw)
        entries = [record(relative, raw) if item['filename'] == relative else item for item in entries]
    manifest.update(version=4, sidecars=entries, generated_audio_included=False)
    digest = seal(folder, manifest)
    destination = tmp_path / 'current-history'
    if forged_anchor:
        with pytest.raises(ValueError, match='reading state|saved source'):
            restore_backup(folder, destination, digest)
        assert not destination.exists()
        return
    restored = restore_backup(folder, destination, digest)
    assert snapshot_session_files(restored) == {relative: raw}
    retained = validate_session_payload(relative, raw)
    LegacyReadingStore(restored).retain(raw)
    imported = project_from_snapshot(retained, title='Explicit recovered canonical session')
    NarrationStore(restored).save(imported)
    assert NarrationStore(restored).load(imported.id)[0] == imported
    assert LegacyReadingStore(restored).load(retained.archive_sha256, revision=2).text == snapshot.source_text
    again = tmp_path / 'history-again'
    final_digest = create_backup(LibraryApp(restored), again)
    verify_backup(again, final_digest)
    second = restore_backup(again, tmp_path / 'history-second', final_digest)
    assert snapshot_session_files(second) == {relative: raw}
    assert (second / DIRECTORY / digest / 'manifest.json').read_bytes() == (folder / 'manifest.json').read_bytes()


@pytest.mark.parametrize('family', ['common', 'legacy-v5'])
def test_current_reader_bindings_remain_mandatory_inside_canonical_envelopes(tmp_path, family):
    *_, folder, manifest, _, _ = source_fixture(tmp_path, family=family)
    path = folder / 'reading-state.json'
    value = json.loads(path.read_bytes())
    value['positions'][0]['location']['asset_sha256'] = 'f' * 64
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    replacement = record(path.name, raw)
    if family == 'common':
        manifest['sidecars'] = [replacement if item['filename'] == path.name else item
                                for item in manifest['sidecars']]
    else:
        manifest['reading_state'] = replacement
    digest = seal(folder, manifest)
    with pytest.raises(ValueError, match='exact saved source'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('damage', ['sidecars-and-state', 'null-pending', 'core-file', 'core-child',
                                    'unknown-root', 'case-alias', 'generated-without-consent'])
def test_common_manifest_cannot_mix_authorities_or_hide_foreign_names(tmp_path, damage):
    *_, folder, manifest, _, _ = source_fixture(tmp_path)
    if damage == 'sidecars-and-state':
        manifest['state'] = []
    elif damage == 'null-pending':
        manifest['pending'] = None
    else:
        name = {'core-file': 'library.sqlite', 'core-child': 'originals/anything.txt',
                'unknown-root': 'credentials.json', 'case-alias': 'State/other.json',
                'generated-without-consent': 'state/studio/cache/' + 'b' * 64 + '.wav'}[damage]
        manifest['sidecars'].append(record(name, b'changed'))
    digest = seal(folder, manifest)
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('family', ['common', 'legacy-v5'])
def test_declared_foreign_bytes_are_still_hash_checked(tmp_path, family):
    *_, folder, _, digest, _ = source_fixture(tmp_path, family=family)
    path = folder / 'state/audio/engines.json'
    path.write_bytes(b'X' * path.stat().st_size)
    with pytest.raises(ValueError, match='checksum'):
        verify_backup(folder, digest)


@pytest.mark.parametrize('budget', ['files', 'bytes'])
def test_restore_budget_counts_both_live_readers_and_exact_original_archive(tmp_path, budget):
    *_, folder, manifest, digest, _ = source_fixture(tmp_path)
    readers = [item for item in manifest['sidecars'] if not item['filename'].startswith('state/')]
    if budget == 'files':
        options = dict(max_sidecar_files=len(manifest['sidecars']) + len(readers))
    else:
        size = len((folder / 'manifest.json').read_bytes())
        options = dict(max_sidecar_bytes=size + sum(item['size_bytes'] for item in manifest['sidecars'])
                       + sum(item['size_bytes'] for item in readers) - 1)
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'rejected', digest, **options)
    assert not (tmp_path / 'rejected').exists()


def test_same_size_source_mutation_after_archive_copy_is_not_hidden_by_timestamps(tmp_path, monkeypatch):
    from carr_library import backup_legacy_state
    *_, folder, _, digest, _ = source_fixture(tmp_path)
    source = folder / 'state/audio/engines.json'
    before, raw = source.stat(), source.read_bytes()
    replacement = raw.replace(b'never-run', b'other-run')
    assert replacement != raw and len(replacement) == len(raw)
    stamp = backup_legacy_state._stamp
    monkeypatch.setattr(backup_legacy_state, '_stamp', lambda info: stamp(info)[:-1])
    original = backup_legacy_state._inventory
    calls = 0

    def interleaved(root, entries, cancelled, **kwargs):
        nonlocal calls
        if Path(root) == folder:
            calls += 1
            if calls == 2:
                source.write_bytes(replacement)
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original(root, entries, cancelled, **kwargs)

    monkeypatch.setattr(backup_legacy_state, '_inventory', interleaved)
    with pytest.raises(ValueError, match='checksum changed'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert calls == 2 and source.read_bytes() == replacement
    assert not (tmp_path / 'rejected').exists()


def test_cancellation_after_historical_payload_copy_keeps_source_and_destination_absent(tmp_path):
    *_, folder, _, digest, _ = source_fixture(tmp_path)
    before = contents(folder)
    observed = False

    def cancelled():
        nonlocal observed
        for stage in tmp_path.glob('.carr-library-restore-*'):
            payloads = stage / 'sidecars' / DIRECTORY / digest / 'files'
            if payloads.exists() and any(payloads.iterdir()):
                observed = True
                return True
        return False

    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(folder, tmp_path / 'cancelled', digest, cancelled=cancelled)
    assert observed and contents(folder) == before
    assert not (tmp_path / 'cancelled').exists()


@pytest.mark.parametrize('changes', [dict(volume=True), dict(volume=101), dict(volume=12.5),
    dict(interval_seconds=False), dict(interval_seconds=0), dict(interval_seconds=61),
    dict(muted=1), dict(allow_background='true'), dict(quiet_during_media=None),
    dict(mode='both'), dict(voice_id='old-local-executable')])
def test_distinct_canonical_progress_schema_is_exact_and_bounded(changes):
    with pytest.raises(ValueError):
        decode_preferences(canonical_preferences(**changes))


@pytest.mark.parametrize('mode', ['off', 'beep', 'speech', 'beep_speech'])
def test_canonical_progress_mapping_preserves_choices_without_admitting_voice(mode):
    selected = decode_preferences(canonical_preferences(mode=mode, volume=100))
    assert selected.mode == mode and selected.volume == 1 and selected.interval_seconds == 7
    assert selected.background and selected.suppress_during_media
    assert selected.muted == (mode in ('speech', 'beep_speech'))
    assert selected.voice_id == selected.voice_language == selected.voice_fingerprint == ''
