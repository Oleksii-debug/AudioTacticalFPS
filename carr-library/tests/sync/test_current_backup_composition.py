"""Portable restoration projects typed sources to the final, published root."""
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import encode, read_json
from carr_library.ingest.worker import load_request, record_progress
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.project import ProjectStore
from carr_library.studio.render import ProductionService
from carr_library.sync import archive
from carr_library.sync.service import SyncService
from carr_library.sync.vault import Vault


def fixture_module(name):
    # Only test setup is file-bound. Product modules remain the actual imported
    # source/installed candidate under the caller's strict import mode.
    path = Path(__file__).parents[1] / 'storage' / name
    spec = importlib.util.spec_from_file_location('_portable_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def manual_payload(app, tmp_path, shape):
    fixture = fixture_module('test_studio_clip_inputs_backup.py')
    container = tmp_path / 'transfer-source'
    container.mkdir()
    _, checksum = fixture._backup(app, container / 'backup', shape)
    archive.write_json(container / archive.INVENTORY,
        {'kind': 'carr-portable-backup', 'version': 1, 'family_id': '4' * 32,
         'backup_checksum': checksum, 'label': 'Explicit historical backup',
         'created_at': datetime.now(timezone.utc).isoformat()})
    payload = tmp_path / 'payload'
    payload.mkdir()
    archive.pack_directory(container, payload / 'snapshot.zip')
    folder = tmp_path / 'portable'
    receipt = Vault(folder).publish(payload, expected_head=None)
    return folder, receipt.transfer_token


@pytest.mark.parametrize('shape', ['native', 'flat'])
def test_final_root_clip_history_and_pending_source_are_not_bound_to_deleted_staging(
        tmp_path, shape):
    fixture = fixture_module('test_studio_clip_inputs_backup.py')
    app, workspace, project, _, selected, raw, _, old_job = fixture.library.__wrapped__(tmp_path)
    source = tmp_path / 'pending-source.html'
    source.write_bytes(b'<html><body><p>Exact retained import for transfer.</p></body></html>')
    result = app.import_file(workspace, source)
    record_progress(app.root, result['request_id'], state='running')
    request = load_request(app.root, result['request_id'])
    source_raw = source.read_bytes()
    source.unlink()
    folder, token = manual_payload(app, tmp_path, shape)
    target = tmp_path / 'final-library'
    service = SyncService(app)
    assert service.inspect(folder, token)['verified']
    service.restore(folder, token, target)
    assert not list(tmp_path.glob('.carr-portable-restore-*'))

    store = ProjectStore(target / 'state/studio/projects')
    current = store.load(project.id)
    expected_input = target / 'state/studio/inputs' / selected.name
    assert current.revision == project.revision + 1
    assert current.scenes[0].cues[0].path == str(expected_input)
    assert expected_input.read_bytes() == raw
    assert current.fingerprint != project.fingerprint
    revisions = target / 'state/studio/projects' / project.id / 'revisions'
    assert (revisions / f'{project.revision:08d}-{project.fingerprint}.json').read_bytes() == encode(project.to_dict())
    assert (revisions / f'{current.revision:08d}-{current.fingerprint}.json').read_bytes() == encode(current.to_dict())
    restored_job = read_json(target / 'state/studio/jobs' / fixture.JOB_ID / 'job.json')
    assert restored_job == {**old_job, 'restore_requires_new_output': True}
    assert restored_job['project'] == old_job['project']

    after = load_request(target, request['request_id'])
    assert after['data_dir'] == str(target)
    assert Path(after['source']).is_relative_to(target)
    assert Path(after['source']).read_bytes() == source_raw
    assert after['job'] == request['job'] and after['result'] == request['result']
    assert 'attempt_id' not in after and 'publication' not in after
    recovered = LibraryApp(target)
    assert recovered.resume_import(workspace, request['request_id'])['state'] == 'complete'
    assert ProjectStore(app.root / 'state/studio/projects').load(project.id) == project
    assert selected.read_bytes() == raw

    # A subsequent ordinary backup remains valid and projects only once again.
    checksum = create_backup(recovered, tmp_path / 'again')
    next_root = restore_backup(tmp_path / 'again', tmp_path / 'next-library', checksum)
    next_project = ProjectStore(next_root / 'state/studio/projects').load(project.id)
    assert next_project.revision == current.revision + 1
    assert next_project.scenes[0].cues[0].path == str(next_root / 'state/studio/inputs' / selected.name)


def test_book_plan_source_and_restored_job_authority_survive_portable_roundtrip(tmp_path):
    from carr_library.studio.book_audio import BookAudioService
    fixture = fixture_module('test_backup_book_plan_bindings.py')
    context = fixture.book.__wrapped__(tmp_path)
    book = next(context)
    try:
        fixture._prepare(book)
        original = (book.service.plans / (book.plan.id + '.json')).read_bytes()
        service = SyncService(book.app)
        folder = tmp_path / 'portable'
        receipt = service.publish(folder, 'Reviewed Book plans', None)
        target = tmp_path / 'restored-book'
        service.restore(folder, receipt['token'], target)
        app = LibraryApp(target)
        production = ProductionService(target / 'state/studio', voices=book.voices)
        recovered = BookAudioService(app, book.workspace, production=production)
        try:
            plan = recovered.load(book.plan.id)
            assert plan.fingerprint == book.plan.fingerprint
            assert (recovered.plans / (plan.id + '.json')).read_bytes() == original
            for index in range(1, len(plan.parts) + 1):
                before, after = book.production.get(plan.job_id(index)), production.get(plan.job_id(index))
                assert after == {**before, 'restore_requires_new_output': True}
            destination = tmp_path / 'explicit-reviewed-output'
            destination.mkdir()
            review = recovered.review(plan, destination)
            with pytest.raises(ValueError, match='new output review'):
                recovered.prepare(plan, destination, approval=review.fingerprint)
            assert not list(destination.iterdir())
        finally:
            recovered.close()
            production.close()
    finally:
        context.close()


@pytest.mark.parametrize('shape', ['native', 'flat'])
def test_manual_tabs_empty_draft_undo_survive_portable_restore(tmp_path, shape):
    fixture = fixture_module('test_reading_tabs_backup.py')
    app, _, _, index, states = fixture.library.__wrapped__(tmp_path)
    from carr_library.speech.tabs import ReadingTabsStore
    folder, token = manual_payload(app, tmp_path, shape)
    target = tmp_path / 'restored-tabs'
    SyncService(app).restore(folder, token, target)
    store = ReadingTabsStore(target)
    assert store.load()[0] == index
    for tab, expected in zip(index.tabs, states):
        recovered = store.draft(tab).load()
        assert recovered.document.to_dict() == expected.document.to_dict()
        assert recovered.voice_id == expected.voice_id
        assert recovered.document.undo()
        assert recovered.document.text == recovered.document.source_text
