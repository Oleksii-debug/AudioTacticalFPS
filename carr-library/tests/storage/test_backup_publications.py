"""Actual completed publication receipts survive typed backup without output authority."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json, state_lock
from carr_library.library_backup import restore_backup
from carr_library.studio.publishing import PublicationService, validate_publication_receipt
from carr_library.studio.render import ProductionService

_spec = importlib.util.spec_from_file_location('publication_backup_book_fixture',
    Path(__file__).parents[1] / 'studio' / 'test_book_audio.py')
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
book = _fixtures.book


def publication(e, path, format):
    plan = _fixtures.plan(e, 'document')
    _, ready = _fixtures.prepared(e, plan)
    e.service.run(plan.id, e.output, approval=ready.fingerprint)
    published = PublicationService(e.production)
    if format == 'epub':
        receipt = published.export_epub(plan.job_id(1), path)
    else:
        receipt = published.export_daisy(plan.job_id(1), path, publisher='Synthetic local backup fixture')
    return published, receipt


@pytest.mark.parametrize('format,suffix', [('epub', '.epub'), ('daisy-2.02', '.zip')])
def test_real_publication_backup_delete_restore_requires_explicit_external_file(book, tmp_path, format, suffix):
    e = book
    output = tmp_path / ('actual-publication' + suffix)
    publisher, receipt = publication(e, output, format)
    before = (sha256(output.read_bytes()).hexdigest(), output.stat().st_mtime_ns)
    backup = tmp_path / 'backup'
    with state_lock(publisher.receipts, receipt['id']):
        assert list(publisher.receipts.glob('.*.lock'))
        archive = e.app.backup(backup)
    e.service.close()
    e.production.close()
    shutil.rmtree(e.app.root)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', archive))
    production = ProductionService(restored.root / 'state' / 'studio', voices=e.voices)
    try:
        reopened = PublicationService(production)
        assert reopened.publications() == (receipt,)
        assert not list(reopened.receipts.glob('.*.lock'))
        assert not any(path.suffix in ('.epub', '.zip') for path in restored.root.rglob('*'))
        assert (sha256(output.read_bytes()).hexdigest(), output.stat().st_mtime_ns) == before
        with pytest.raises(ValueError, match='explicitly select'):
            reopened.verify(receipt['id'])
        calls = tuple(e.voices.received)
        checked = reopened.verify(receipt['id'], output=output)
        assert checked['status'] == 'complete' and checked['studio_root'] == str(production.root)
        assert tuple(e.voices.received) == calls
        assert (sha256(output.read_bytes()).hexdigest(), output.stat().st_mtime_ns) == before
        assert read_json(reopened.receipts / (receipt['id'] + '.json')) == checked
    finally:
        production.close()


@pytest.mark.parametrize('change', ['format', 'digest', 'source-path', 'duplicate-artifact', 'active', 'filename'])
def test_passive_publication_receipt_tamper_refuses_backup_without_following_output(book, tmp_path, change):
    e = book
    output = tmp_path / 'actual-publication.epub'
    publisher, receipt = publication(e, output, 'epub')
    value = deepcopy(receipt)
    if change == 'format':
        value['format'] = 'arbitrary-executable'
    elif change == 'digest':
        value['source']['project_sha256'] = 'not-a-digest'
    elif change == 'source-path':
        value['source']['artifacts'][0]['name'] = '../outside'
    elif change == 'duplicate-artifact':
        value['source']['artifacts'].append(deepcopy(value['source']['artifacts'][0]))
    elif change == 'active':
        value['status'] = 'running'
    else:
        value['id'] = 'f' * 32
    atomic_json(publisher.receipts / (receipt['id'] + '.json'), value, replace=True)
    output.unlink()  # Passive backup must not attempt to repair or reopen it.
    with pytest.raises((ValueError, RuntimeError)):
        e.app.backup(tmp_path / 'refused')
    assert not output.exists()


def test_missing_external_publication_does_not_block_historical_receipt_backup(book, tmp_path):
    e = book
    output = tmp_path / 'removed-publication.epub'
    publisher, receipt = publication(e, output, 'epub')
    output.unlink()
    assert validate_publication_receipt(receipt, receipt['id']) == receipt
    backup = tmp_path / 'historical-backup'
    archive = e.app.backup(backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', archive))
    saved = restored.root / 'state' / 'studio' / 'publications' / (receipt['id'] + '.json')
    assert read_json(saved) == receipt and not output.exists()
