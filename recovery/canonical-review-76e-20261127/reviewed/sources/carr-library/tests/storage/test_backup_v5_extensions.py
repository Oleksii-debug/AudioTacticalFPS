"""V5 typed producer boundaries with real catalog, source bytes and pending jobs."""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json
from carr_library.extract.page_ocr import Config
from carr_library.catalog.intake import snapshot
from carr_library.ingest.worker import prepare_request, record_progress, load_request
from carr_library.library_backup import restore_backup, INCOMPLETE
from carr_library.reader.reading_state import ReadingLocation, ReadingState
from carr_library.reader.live_state import LiveState
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.storage.revisions import Storage

import runpy

# Preserve the exact historical writer envelope while exercising today's reader.
create_backup = runpy.run_path(str(Path(__file__).with_name('legacy_state_writer.py')))['create_backup']


def source_location(app, workspace):
    with closing(Storage(app.database)) as storage:
        row = storage.db.execute(
            'SELECT r.edition,r.id,r.asset,p.ordinal,p.digest,p.raw FROM revision r '
            'JOIN page p ON p.revision=r.id WHERE r.workspace=? ORDER BY p.ordinal LIMIT 1',
            (workspace,)).fetchone()
    return ReadingLocation(workspace, row[0], row[1], 'logical_segment', row[3], row[4], row[2]), row[5]


def configured(tmp_path):
    app = LibraryApp(tmp_path / 'дані')
    workspace = app.create_workspace('Library')
    app.add_text(workspace, 'Source', 'Точне джерело.\r\nДруга частина.')
    location, text = source_location(app, workspace)
    ReadingState(app.root).remember(location)
    LiveState(app.root).remember(location, 3, len(text), len(text), 'explicit-local-voice')
    document = ReadingDocument(text, title='Edited voice copy')
    document.replace(0, 5, 'Інше')
    bound = ReaderDraftStore(app.root, slot='bound')
    bound.save(ReaderResume(document, 'explicit-local-voice', cursor=2, source_locations=(location,)))
    unbound = ReaderDraftStore(app.root, slot='manual')
    unbound.save(ReaderResume(ReadingDocument('Private independent draft')))
    atomic_json(app.root / 'state/preferences.json', {'independent': True})
    settings = dict(version=1, enabled=True, runtime_directory=str(tmp_path / 'external-engine'),
                    python_executable=str(tmp_path / 'private-python.exe'), expected_worker_sha256='a' * 64)
    atomic_json(app.root / 'dordiai-voices.json', settings)
    return app, workspace, location, text, bound, unbound, settings


def test_v5_full_round_trip_preserves_typed_drafts_exact_sources_and_disables_private_engine(tmp_path, monkeypatch):
    app, workspace, location, text, bound, unbound, settings = configured(tmp_path)
    forbidden = {settings['runtime_directory'], settings['python_executable']}
    original_stat = Path.stat
    def no_external_probe(path, *args, **kwargs):
        assert str(path) not in forbidden, 'backup or restore probed a declared private runtime'
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', no_external_probe)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['version'] == 5
    assert manifest['extensions']['workspaces'] == [workspace]
    assert manifest['extensions']['independent_state'] is True
    assert len(manifest['extensions']['reader_sessions']) == 2
    assert {entry['reference'] for entry in manifest['extensions']['required_inputs']} >= forbidden
    destination = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(destination)
    assert source_location(restored, workspace) == (location, text)
    assert ReadingState(destination).resume(workspace, location.edition) == location
    position = LiveState(destination).resume(workspace, location.edition)
    assert (position.location, position.offset, position.end, position.text_length) == (location, 3, len(text), len(text))
    assert ReaderDraftStore(destination, slot='bound').load().document.to_dict() == bound.load().document.to_dict()
    assert ReaderDraftStore(destination, slot='manual').load().document.text == unbound.load().document.text
    recovered = json.loads((destination / 'dordiai-voices.json').read_text())
    assert recovered == {**settings, 'enabled': False}
    assert json.loads((app.root / 'dordiai-voices.json').read_text())['enabled'] is True
    assert not (destination / INCOMPLETE).exists()


def test_v5_workspace_scope_omits_unbound_drafts_global_settings_and_private_runtime(tmp_path):
    app, workspace, location, text, bound, unbound, settings = configured(tmp_path)
    folder = tmp_path / 'scoped'
    digest = create_backup(app, folder, authorized_workspaces={workspace})
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['version'] == 5
    assert manifest['extensions']['independent_state'] is False
    assert {entry['filename'] for entry in manifest['state']} == {'speech/' + bound.path.name}
    assert {entry['filename'] for entry in manifest['extensions']['files']} == {'reading-state.json', 'live-reading-state.json'}
    destination = restore_backup(folder, tmp_path / 'restored', digest)
    assert ReaderDraftStore(destination, slot='bound').load().source_locations == (location,)
    assert not (destination / 'state/speech' / unbound.path.name).exists()
    assert not (destination / 'state/preferences.json').exists()
    assert not (destination / 'dordiai-voices.json').exists()


def test_scope_never_partially_exports_a_catalog_with_unauthorized_library(tmp_path):
    app, workspace, *_ = configured(tmp_path)
    app.create_workspace('Another private library')
    target = tmp_path / 'forbidden'
    with pytest.raises(ValueError, match='authorized'):
        create_backup(app, target, authorized_workspaces={workspace})
    assert not target.exists()


@pytest.mark.parametrize('damage', ['source-text', 'source-identity', 'live-length'])
def test_invalid_typed_source_binding_fails_before_backup_publication(tmp_path, damage):
    app, workspace, location, text, bound, *_ = configured(tmp_path)
    if damage == 'source-text':
        bound.save(ReaderResume(ReadingDocument('Different source'), source_locations=(location,)))
    elif damage == 'source-identity':
        bound.save(ReaderResume(ReadingDocument(text), source_locations=(replace(location, text_sha256='f' * 64),)))
    else:
        LiveState(app.root).remember(location, 0, len(text), len(text) + 1, 'explicit-local-voice')
    target = tmp_path / 'bad'
    with pytest.raises(ValueError, match='source'):
        create_backup(app, target)
    assert not target.exists()


@pytest.mark.parametrize('damage', ['sidecar-bytes', 'required-input', 'scope', 'unlisted'])
def test_v5_corruption_or_resealed_semantic_tampering_never_publishes_destination(tmp_path, damage):
    app, workspace, *_ = configured(tmp_path)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    path = folder / 'manifest.json'
    value = json.loads(path.read_text())
    if damage == 'sidecar-bytes':
        (folder / 'extensions/live-reading-state.json').write_bytes(b'{}')
    elif damage == 'unlisted':
        (folder / 'extensions/unlisted.json').write_bytes(b'{}')
    else:
        if damage == 'scope':
            value['extensions']['workspaces'] = ['foreign-workspace']
        else:
            value['extensions']['required_inputs'] = []
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        digest = sha256(raw).hexdigest()
    destination = tmp_path / 'restored'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, digest)
    assert not destination.exists()


def test_v5_keeps_canonical_cancelled_import_resume_identity(tmp_path):
    app, workspace, *_ = configured(tmp_path)
    original = tmp_path / 'pending.txt'
    original.write_text('First page.\fSecond page.\fThird page.', encoding='utf-8')
    metadata = dict(title='Pending', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    expected = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=expected)
    count = 0
    def cancelled():
        nonlocal count
        count += 1
        return count >= 2
    initial = app._import_file(workspace, original, **metadata, cancelled=cancelled,
                              _request_id=request['request_id'], _expected_manifest=expected)
    assert initial['completed'] == 1
    record_progress(app.root, request['request_id'], state='cancelled', result=initial)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    assert json.loads((folder / 'manifest.json').read_text())['version'] == 5
    original.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    rebound = load_request(restored.root, request['request_id'])
    assert rebound['data_dir'] == str(restored.root)
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    assert (completed['id'], completed['edition']) == (initial['id'], initial['edition'])


def test_extension_publication_failure_keeps_incomplete_marker(tmp_path, monkeypatch):
    app, *_ = configured(tmp_path)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    destination = tmp_path / 'restored'
    original_link = os.link
    def fail(source, target, *args, **kwargs):
        if Path(target) == destination / 'live-reading-state.json':
            raise OSError('simulated extension disk full')
        return original_link(source, target, *args, **kwargs)
    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(OSError, match='disk full'):
        restore_backup(folder, destination, digest)
    assert (destination / INCOMPLETE).exists()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(destination)


def test_cancelled_extension_copy_does_not_publish_success(tmp_path):
    app, *_ = configured(tmp_path)
    target = tmp_path / 'cancelled'
    with pytest.raises(InterruptedError):
        create_backup(app, target, cancelled=lambda: True)
    assert not target.exists()
