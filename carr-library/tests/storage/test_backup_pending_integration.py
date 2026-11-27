"""Current pending imports plus both historical backup families, using real files."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.backup_historical import DIRECTORY as HISTORICAL
from carr_library.library_backup import create_backup, restore_backup, verify_backup, INCOMPLETE
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


def _seal(folder, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _bytes(folder):
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob('*') if path.is_file()}


def _planned(tmp_path, *, text='Queued original quotation.'):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pending imports')
    source = tmp_path / 'original.txt'
    source.write_text(text, encoding='utf-8')
    batch = app.plan_import_batch(workspace, [source])
    return app, workspace, source, batch


def test_v5_keeps_published_reading_and_unpublished_source_and_rebacks_up(tmp_path):
    app, workspace, source, batch = _planned(tmp_path)
    published = app.add_text(workspace, 'Published', 'Published immutable original.')
    page = ReadingDesk(app, workspace, published['edition']).page(1)
    ReadingState(app.root).remember(page.location)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    manifest = verify_backup(backup, digest)
    assert manifest['version'] == 5
    assert {entry['path'] for entry in manifest['sidecars']} == {'reading-state.json'}
    assert len(manifest['assets']) == 1 and len(manifest['pending']['assets']) == 2
    source.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    assert ReadingState(restored.root).resume(workspace, published['edition']) == page.location
    rebound = restored.import_batch(workspace, batch['batch_id'])
    before, after = batch['rows'][0], rebound['rows'][0]
    for key in before:
        if key != 'source':
            assert after[key] == before[key]
    assert Path(after['source']).parent == restored.assets_dir
    second = tmp_path / 'second'
    second_digest = create_backup(restored, second)
    verify_backup(second, second_digest)
    final = restored.run_import_batch(workspace, batch['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 1
    assert len(restored.search(workspace, 'Queued')) == 1
    assert all(path.stat().st_nlink == 1 for path in restored.assets_dir.iterdir())


def test_current_html_queue_uses_canonical_retained_format_registry(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('HTML pending original')
    source = tmp_path / 'document.html'
    source.write_text('<html><body><p>Exact railway original.</p></body></html>', encoding='utf-8')
    batch = app.plan_import_batch(workspace, [source])
    assert batch['counts']['queued'] == 1
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    manifest = verify_backup(backup, digest)
    assert manifest['pending']['assets'][0]['filename'].endswith('.html')
    source.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    final = restored.run_import_batch(workspace, batch['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 1


def test_verify_only_copies_private_journal_and_cannot_create_source_wal(tmp_path, monkeypatch):
    from carr_library import pending_backup
    app, workspace, _, _ = _planned(tmp_path)
    app.add_text(workspace, 'Published', 'Original published text.')
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    # A restored journal may carry a WAL-mode header even after all WAL pages
    # were checkpointed. A read-only verifier must not create source-side shm.
    with closing(sqlite3.connect(backup / 'pending/imports.sqlite')) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    journal = backup / 'pending/imports.sqlite'
    entry = next(item for item in manifest['pending']['files'] if item['filename'] == 'imports.sqlite')
    entry.update(size_bytes=journal.stat().st_size, sha256=sha256(journal.read_bytes()).hexdigest())
    digest = _seal(backup, manifest)
    before = _bytes(backup)
    copied, original = [], pending_backup._copy
    def only_journal(source, target, metadata, cancelled):
        copied.append(metadata['filename'])
        assert metadata['filename'] == 'imports.sqlite'
        assert not Path(target).is_relative_to(backup)
        return original(source, target, metadata, cancelled)
    monkeypatch.setattr(pending_backup, '_copy', only_journal)
    verify_backup(backup, digest)
    assert copied == ['imports.sqlite']
    assert _bytes(backup) == before
    assert not (backup / 'pending/imports.sqlite-wal').exists()
    assert not (backup / 'pending/imports.sqlite-shm').exists()


def test_assistant_self_backup_pauses_after_real_pending_copy_and_resumes(tmp_path, monkeypatch):
    from carr_library import pending_backup
    app, workspace, source, _ = _planned(tmp_path, text='Q' * (2 * 1024 * 1024))
    output = tmp_path / 'output'
    output.mkdir()
    service = AssistantService(app)
    identity = service.submit(service.preview(
        'Backup "complete"', Scope(workspace, output_root=output,
                                   allow_full_backup=True, max_bytes=16 * 1024 * 1024)))
    event, writes = Event(), []
    original = pending_backup._write
    def copy_then_pause(path, raw):
        result = original(path, raw)
        if Path(path).parent.name == 'originals':
            assert Path(path).read_bytes() == source.read_bytes()
            writes.append(Path(path))
            event.set()
        return result
    with monkeypatch.context() as during_copy:
        during_copy.setattr(pending_backup, '_write', copy_then_pause)
        report = service.run(identity, cancelled=event.is_set)
    assert report.status == 'paused', report.message
    assert len(writes) == 1 and not writes[0].exists()
    assert not (output / 'complete').exists()
    assert service.store.load(identity)['status'] == 'paused'
    reopened = AssistantService(LibraryApp(app.root))
    completed = reopened.resume(identity)
    assert completed.status == 'complete', completed.message
    target = output / 'complete'
    digest = completed.results[0]['manifest_sha256']
    assert verify_backup(target, digest)['version'] == 5
    before = _bytes(target)
    assert reopened.resume(identity).status == 'complete'
    assert _bytes(target) == before


def test_pending_import_writer_cannot_change_queue_during_snapshot(tmp_path, monkeypatch):
    from carr_library import pending_backup
    app, workspace, _, batch = _planned(tmp_path)
    original, checks = pending_backup._write, []
    def protected(path, raw):
        if Path(path).parent.name == 'import-batches':
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(app.set_import_batch_row, workspace, batch['batch_id'],
                                     batch['rows'][0]['row_id'], included=False)
                with pytest.raises(ValueError, match='already running'):
                    future.result(timeout=5)
            checks.append(Path(path).name)
        return original(path, raw)
    monkeypatch.setattr(pending_backup, '_write', protected)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    assert checks == [batch['batch_id'] + '.json']
    verify_backup(backup, digest)
    assert app.import_batch(workspace, batch['batch_id'])['rows'][0]['state'] == 'queued'


def test_restore_cancellation_after_one_real_asset_chunk_publishes_nothing(tmp_path, monkeypatch):
    from carr_library import pending_backup
    app, _, _, _ = _planned(tmp_path, text='R' * (2 * 1024 * 1024))
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    before = _bytes(backup)
    original, chunks = pending_backup._copy, []
    event = Event()
    def interrupted(source, target, entry, cancelled):
        if not str(entry['filename']).startswith('originals/'):
            return original(source, target, entry, cancelled)
        polls = 0
        def after_chunk():
            nonlocal polls
            polls += 1
            if polls == 2:
                assert Path(target).stat().st_size == 1024 * 1024
                chunks.append(Path(target))
                event.set()
            return event.is_set() or cancelled()
        return original(source, target, entry, after_chunk)
    monkeypatch.setattr(pending_backup, '_copy', interrupted)
    with pytest.raises(InterruptedError):
        restore_backup(backup, tmp_path / 'restored', digest, cancelled=event.is_set)
    assert len(chunks) == 1 and not chunks[0].exists()
    assert not (tmp_path / 'restored').exists()
    assert _bytes(backup) == before


def test_missing_pinned_queue_source_refuses_complete_backup(tmp_path):
    app, _, source, _ = _planned(tmp_path)
    source.unlink()
    with pytest.raises(ValueError, match='source bytes are missing'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_interrupted_pending_publication_keeps_restore_incomplete(tmp_path, monkeypatch):
    app, _, _, _ = _planned(tmp_path)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    target = tmp_path / 'restored'
    original = os.link
    def interrupted(source, destination, *args, **kwargs):
        if Path(destination).is_relative_to(target / 'import-batches'):
            raise OSError('Synthetic pending publication interruption')
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'link', interrupted)
    with pytest.raises(OSError, match='Synthetic'):
        restore_backup(backup, target, digest)
    assert (target / INCOMPLETE).exists()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(target)


def _legacy(tmp_path, version):
    app, workspace, source, batch = _planned(tmp_path)
    folder = tmp_path / 'legacy'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest.pop('sidecars')
    shutil.rmtree(folder / 'sidecars')
    if version == 3:
        manifest.pop('pending')
        shutil.rmtree(folder / 'pending')
    values = {
        'assistant/' + 'a' * 32 + '.json': b'{"alternate-job":"saved intent only"}\n',
        'profiles/offline.json': b'{"endpoint":"historical, never activated"}\n',
        'studio/project.json': 'Точний старий проєкт.\n'.encode('utf-8'),
        '/'.join(['a'] * 20) + '/empty.txt': b'',
    }
    records = []
    for name, raw in values.items():
        path = folder / 'state' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        records.append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    manifest.update(version=version, state=records, generated_audio_included=False)
    digest = _seal(folder, manifest)
    return app, workspace, source, batch, folder, manifest, digest, values


@pytest.mark.parametrize('version', [3, 4], ids=['canonical-state-v3', 'canonical-state-pending-v4'])
def test_legacy_state_is_inert_hash_bound_and_survives_current_rebackup(tmp_path, version):
    _, workspace, source, batch, folder, manifest, digest, values = _legacy(tmp_path, version)
    before = _bytes(folder)
    # Archive family versions and the contained catalog version are independent.
    assert manifest['version'] == version and manifest['schema_version'] == 5
    assert verify_backup(folder, digest) == manifest
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert _bytes(folder) == before
    archive = target / HISTORICAL / digest
    assert (archive / 'manifest.json').read_bytes() == (folder / 'manifest.json').read_bytes()
    for name, raw in values.items():
        assert (archive / 'files' / sha256(name.encode('utf-8')).hexdigest()).read_bytes() == raw
    assert not (target / 'state/assistant').exists()
    assert not (target / 'state/profiles').exists()
    assert not (target / 'state/studio').exists()
    reopened = LibraryApp(target)
    assert AssistantService(reopened).jobs() == ()
    source.unlink()
    if version == 4:
        final = reopened.run_import_batch(workspace, batch['batch_id'])
        assert final['state'] == 'complete'
    second = tmp_path / 'second'
    second_digest = create_backup(reopened, second)
    result = verify_backup(second, second_digest)
    assert result['version'] == 5
    again = restore_backup(second, tmp_path / 'again', second_digest)
    assert _bytes(again / HISTORICAL / digest) == _bytes(archive)


@pytest.mark.parametrize('version', [3, 4], ids=['canonical-v3', 'canonical-v4'])
def test_historical_and_current_manifest_families_cannot_be_blended(tmp_path, version):
    *_, folder, manifest, _, _ = _legacy(tmp_path, version)
    manifest['sidecars'] = []
    digest = _seal(folder, manifest)
    with pytest.raises(ValueError, match='unsupported library manifest'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


def test_inert_history_payload_requires_inner_provenance_even_with_new_outer_hash(tmp_path):
    *_, folder, _, digest, values = _legacy(tmp_path, 3)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    backup = tmp_path / 'backup'
    create_backup(restored, backup)
    original_name = next(iter(values))
    relative = HISTORICAL + '/' + digest + '/files/' + sha256(original_name.encode()).hexdigest()
    target = backup / 'sidecars' / relative
    target.write_bytes(b'x' * target.stat().st_size)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    entry = next(item for item in manifest['sidecars'] if item['path'] == relative)
    entry['sha256'] = sha256(target.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='checksum'):
        verify_backup(backup, _seal(backup, manifest))


@pytest.mark.parametrize('selected', ['source', 'restore_target', 'backup_target'])
def test_ancestor_aliases_cannot_be_hidden_before_component_validation(tmp_path, selected):
    app, _, _, _ = _planned(tmp_path)
    real = tmp_path / 'real'
    real.mkdir()
    linked = tmp_path / 'linked'
    linked.symlink_to(real, target_is_directory=True)
    backup = real / 'backup'
    digest = create_backup(app, backup)
    with pytest.raises(ValueError, match='links'):
        if selected == 'source':
            verify_backup(linked / 'backup', digest)
        elif selected == 'restore_target':
            restore_backup(backup, linked / 'restored', digest)
        else:
            create_backup(app, linked / 'another')
