"""The native tools' public LibraryApp services share real scoped source data."""
import csv
import io
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.ris_preview import preview_ris
from carr_library.library_backup import create_backup, restore_backup
from carr_library.research import export_file


def seed(tmp_path):
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Academic workflow')
    collection = app.create_collection(workspace, 'Selected sources')
    topic = app.create_topic(workspace, 'Review question')
    imported = app.add_text(workspace, 'Джерело', 'An exact research passage.',
                           authors=('Author and Partners', 'Другий автор'),
                           language='uk', edition='First', collections=(collection,))
    assert imported['state'] == 'complete'
    app.add_text(workspace, 'Other source', 'Different source words.')
    hit, = app.search(workspace, 'exact research')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'PRIVATE RESEARCH NOTE')
    return app, workspace, collection


def test_public_tools_compose_sources_bibliography_reports_and_verified_matrix(tmp_path):
    app, workspace, collection = seed(tmp_path)
    report = app.catalog_report(workspace, collection=collection)
    assert 'Registered editions: 1.' in report
    assert 'Джерело' in report and 'PRIVATE RESEARCH NOTE' not in report
    csl = json.loads(app.export_catalog(workspace, collection=collection))
    assert len(csl) == 1 and csl[0]['title'] == 'Джерело'
    assert csl[0]['author'] == [{'literal': 'Author and Partners'}, {'literal': 'Другий автор'}]
    ris = app.export_catalog(workspace, collection=collection, format='ris')
    hints = preview_ris(ris.encode('utf-8'))
    assert len(hints) == 1 and hints[0].authors == ('Author and Partners', 'Другий автор')
    bibtex = app.export_catalog(workspace, collection=collection, format='bibtex')
    assert '@misc{' in bibtex and 'Джерело' in bibtex
    inventory = list(csv.DictReader(io.StringIO(app.export_catalog(
        workspace, collection=collection, format='csv'))))
    assert len(inventory) == 1 and json.loads(inventory[0]['title_json']) == 'Джерело'
    for format in ('csl-json', 'ris', 'bibtex', 'csv'):
        text = app.export_catalog(workspace, collection=collection, format=format)
        assert 'PRIVATE RESEARCH NOTE' not in text and 'exact research passage' not in text
    matrix = list(csv.DictReader(io.StringIO(app.export_review_matrix(
        workspace, collection=collection))))
    assert len(matrix) == 1
    assert json.loads(matrix[0]['quotation_json']) == 'exact research'
    assert json.loads(matrix[0]['research_note_json']) == 'PRIVATE RESEARCH NOTE'


def test_catalog_tools_reopen_after_checked_backup_restore(tmp_path):
    app, workspace, collection = seed(tmp_path)
    before = {format: app.export_catalog(workspace, format=format)
              for format in ('csl-json', 'ris', 'bibtex', 'csv')}
    matrix = app.export_review_matrix(workspace, collection=collection)
    report = app.catalog_report(workspace, collection=collection)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    destination = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(destination)
    assert {format: restored.export_catalog(workspace, format=format)
            for format in before} == before
    assert restored.export_review_matrix(workspace, collection=collection) == matrix
    assert restored.catalog_report(workspace, collection=collection) == report


def test_all_public_tools_recheck_workspace_and_cancellation(tmp_path):
    app, workspace, collection = seed(tmp_path)
    operations = [app.catalog_report, app.export_review_matrix, app.export_catalog]
    for operation in operations:
        with pytest.raises(TimeoutError):
            operation(workspace, collection=collection, cancelled=lambda: True)
        with pytest.raises(ValueError):
            operation(workspace, collection='foreign-collection')
    app.archive_workspace(workspace)
    for operation in operations:
        with pytest.raises(ValueError, match='archived'):
            operation(workspace)


def test_cancellation_during_file_flush_keeps_existing_export(tmp_path, monkeypatch):
    app, workspace, collection = seed(tmp_path)
    content = app.export_catalog(workspace, collection=collection)
    destination = tmp_path / 'bibliography.json'
    destination.write_bytes(b'Previous export must survive.')
    stopped = False
    fsync = export_file.os.fsync

    def cancel_after_flush(fd):
        nonlocal stopped
        fsync(fd)
        stopped = True

    monkeypatch.setattr(export_file.os, 'fsync', cancel_after_flush)
    with pytest.raises(TimeoutError, match='destination was not changed'):
        export_file.write_export(destination, content, private_root=app.root,
                                 cancelled=lambda: stopped)
    assert destination.read_bytes() == b'Previous export must survive.'
    assert list(tmp_path.glob('.research-export-*')) == []


def test_cancelled_export_without_previous_file_creates_nothing(tmp_path):
    destination = tmp_path / 'review.csv'
    with pytest.raises(TimeoutError):
        export_file.write_export(destination, 'Pending content',
                                 private_root=tmp_path / 'private', cancelled=lambda: True)
    assert not destination.exists() and list(tmp_path.glob('.research-export-*')) == []
