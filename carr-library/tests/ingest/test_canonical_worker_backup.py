"""Retained canonical worker metadata and inert legacy backup boundaries.

These use the current real owned worker and existing publication barriers.
Legacy watermark bytes never become authority for a new process or receipt.
"""
import pytest

from carr_library.application import LibraryApp
from carr_library.audio import engines
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from tests.ingest.test_worker_publication_boundary import source_file, publication_barrier


@pytest.mark.parametrize('conflict', ('language', 'authors', 'title', 'edition', 'volume', 'work', 'collections'))
def test_another_requests_fresh_publication_cannot_hide_metadata_or_membership_failure(
        tmp_path, monkeypatch, conflict):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Concurrent request identities')
    collection = app.create_collection(workspace, 'Required membership')
    source = source_file(tmp_path, 'docx')
    wanted = dict(title='Requested title', authors=('Research; Team',), language='uk',
                  edition='Second', volume='II', collections=(collection,),
                  work_id='shared-work', edition_id='shared-edition')
    other = dict(wanted)
    changes = dict(language='en', authors=('Research', 'Team'), title='Different title',
                   edition='First', volume='I', work_id='other-work', collections=())
    field = 'work_id' if conflict == 'work' else conflict
    other[field] = changes[field]
    marker = tmp_path / 'before-request-publication'
    processes = publication_barrier(monkeypatch, marker, before=True)
    original_popen = engines._start_process
    competing = []

    def launch(command, *args, **kwargs):
        if (isinstance(command, list) and '-c' in command
                and "runpy.run_module('carr_library.ingest.worker'" in command[command.index('-c') + 1]):
            assert not competing
            # A genuine independent publication after the supervisor's watermark,
            # before its actual child. No row/result is fabricated or retargeted.
            competing.append(app._import_file(workspace, source, **other))
            assert competing[0]['state'] == 'complete'
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(engines, '_start_process', launch)
    result = app.import_file(workspace, source, **wanted, cancelled=marker.exists, timeout=30)
    assert result['state'] == ('cancelled' if conflict == 'collections' else 'failed'), result
    assert 'recovered_publication' not in result
    assert len(processes) == 1 and processes[0].poll() is not None
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert tuple(db.execute('SELECT language,edition,volume FROM catalog_edition').fetchone()) == (
            other['language'], other['edition'], other['volume'])
        assert tuple(row[0] for row in db.execute('SELECT name FROM catalog_author ORDER BY position')) == other['authors']
    if conflict == 'collections':
        assert app.browse(workspace, collection=collection) == ()


@pytest.mark.parametrize('boundary', (None, 0))
def test_legacy_watermark_survives_backup_validation_but_is_not_restored(tmp_path, boundary):
    from carr_library.library_backup import create_backup, restore_backup

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Legacy request backup')
    source = source_file(tmp_path, 'txt')
    result = worker.run_request(app.root, workspace, source,
        dict(title=None, authors=(), language=None, edition=None, volume=None,
             collections=(), work_id=None, edition_id=None), Config())
    assert result['state'] == 'complete'
    request = worker.load_request(app.root, result['request_id'])
    request['publication_watermark'] = boundary
    request['state'] = 'running'  # The parent never received its final reply.
    request.pop('result')
    worker._store(app.root, result['request_id'], request)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    source.unlink()

    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = worker.load_request(restored_root, result['request_id'])
    assert not {'publication_watermark', 'attempt', 'publication_candidate'} & restored.keys()
    reopened = LibraryApp(restored_root)
    resumed = reopened.resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'complete' and resumed['id'] == result['id']
    with reopened.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1


@pytest.mark.parametrize('boundary', (True, -1, 2 ** 63, '0'))
def test_malformed_legacy_watermark_cannot_enter_backup(tmp_path, boundary):
    from carr_library.library_backup import create_backup

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Rejected legacy request')
    result = worker.run_request(app.root, workspace, source_file(tmp_path, 'txt'),
        dict(title=None, authors=(), language=None, edition=None, volume=None,
             collections=(), work_id=None, edition_id=None), Config())
    assert result['state'] == 'complete'
    request = worker.load_request(app.root, result['request_id'])
    request['publication_watermark'] = boundary
    worker._store(app.root, result['request_id'], request)
    with pytest.raises(ValueError, match='publication boundary'):
        create_backup(app, tmp_path / 'rejected-backup')


