"""Current managed stores, actual controls and bounded private metadata views."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.audio.jobs import atomic_json, read_json
from carr_library.operations import OperationFacade, ResourceBroker, ResourceLimits, ResourceRequest
from carr_library.operations.models import ResourceStateError
from carr_library.transcription_receipt import validate_settings

STAMP = '2026-11-27T00:00:00+00:00'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)


def asr_job(app, tmp_path, *, state=None):
    identity = uuid4().hex
    original = tmp_path / 'private-original-that-is-not-opened.wav'
    row = {'path': 'inputs/root-0/source.wav', 'root': 'inputs/root-0',
           'relative': 'source.wav', 'bytes': 1, 'sha256': sha256(b'x').hexdigest()}
    value = {'schema': 'carr-supplied-transcription-job/1', 'id': identity,
        'title': 'Private title is not dashboard metadata', 'created': STAMP,
        'settings': validate_settings({}), 'runtime_manifest_sha256': 'a' * 64,
        'model_sha256': 'b' * 64, 'inputs': [row],
        'source_provenance': [dict(row, path=str(original), root=str(tmp_path))]}
    path = app.root / 'transcription-jobs' / identity / 'job.json'
    save(path, value)
    if state is not None:
        save(path.parent / 'execution.json', {'schema': value['schema'], 'state': state,
            'run_id': 'c' * 32, 'exit_code': 0 if state == 'complete' else None, 'updated': STAMP})
    return identity, path


def render_job(app, *, state='running', steps=5, scenes=2):
    identity = uuid4().hex
    path = app.root / 'audio-projects' / identity / 'audio' / 'render-journal.json'
    save(path, {'schema_version': 1, 'run_key': '1' * 64, 'source_sha256': '2' * 64,
                'state': state, 'completed_steps': steps, 'scene_count': scenes,
                'message': 'Private script diagnostics', 'output': '/private/master.wav'})
    return identity, path


def test_actual_assistant_and_import_batch_share_view_without_running_sources(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace, other = app.create_workspace('First'), app.create_workspace('Second')
    source = tmp_path / 'original.txt'
    source.write_text('PRIVATE SOURCE NEVER SHOWN', encoding='utf-8')
    batch = app.plan_import_batch(workspace, [source])
    other_batch = app.plan_import_batch(other, [source])
    assistant = AssistantService(app)
    identity = assistant.submit(assistant.preview('List books', Scope(workspace)))
    studio, _ = render_job(app)
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(min_free_disk_bytes=0))
    facade = OperationFacade(app, broker=broker, assistant=assistant)
    with broker.reserve(ResourceRequest(), operation_id='studio:' + studio, timeout=0):
        rows = {row.key: row for row in facade.list(workspace=workspace)}
        assert set(rows) == {'import:' + batch['batch_id'], 'assistant:' + identity, 'studio:' + studio}
        assert rows['studio:' + studio].live is True
        assert rows['assistant:' + identity].live is None
        assert all(row.verification == 'not_performed' for row in rows.values())
        assert 'PRIVATE' not in repr(rows) and str(source) not in repr(rows)
        result = facade.control('assistant', identity, 'cancel', workspace=workspace)
        assert result == {'operation_id': 'assistant:' + identity, 'action': 'cancel',
                          'requested': True, 'acknowledged': False}
        assert assistant.store.control(identity) == 'cancel'
        assert facade.get('assistant', identity).status == 'queued'
        with pytest.raises(PermissionError):
            facade.get('import', other_batch['batch_id'], workspace=workspace)
        with pytest.raises(ValueError, match='does not expose'):
            facade.control('studio', studio, 'cancel')
    assert source.read_text(encoding='utf-8') == 'PRIVATE SOURCE NEVER SHOWN'


def test_render_counter_does_not_use_scene_count_as_a_denominator(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    identity, _ = render_job(app, steps=9, scenes=2)
    row = OperationFacade(app).get('studio', identity)
    assert (row.completed, row.total, row.unit, row.controls) == (9, None, 'checkpoints', ())
    assert row.live is None


def test_actual_asr_control_uses_stop_flag_without_verifying_missing_inputs_or_loading_model(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    identity, path = asr_job(app, tmp_path, state='running')
    before = path.read_bytes()
    facade = OperationFacade(app)
    row = facade.get('asr', identity)
    assert row.controls == ('pause', 'cancel')
    assert row.total == 1 and row.completed is None
    assert not (path.parent / 'inputs').exists()
    facade.control('asr', identity, 'pause')
    assert (path.parent / 'pause.flag').is_file()
    requested = facade.control('asr', identity, 'cancel')
    assert (path.parent / 'stop.flag').is_file() and requested['acknowledged'] is False
    assert path.read_bytes() == before
    assert facade.get('asr', identity).status == 'running'


def test_asr_status_from_another_run_does_not_supply_progress(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    identity, path = asr_job(app, tmp_path, state='complete')
    status = {'protocol': 'supplied-whisper-v43-hardened-1', 'run_id': 'd' * 32,
              'done': 1, 'total': 1, 'private_path': str(tmp_path)}
    save(path.parent / 'job_status.json', status)
    facade = OperationFacade(app)
    old = facade.get('asr', identity)
    assert (old.status, old.completed, old.verification) == ('complete', None, 'not_performed')
    status['run_id'] = 'c' * 32
    save(path.parent / 'job_status.json', status)
    current = facade.get('asr', identity)
    assert (current.completed, current.total, current.controls) == (1, 1, ())
    assert str(tmp_path) not in repr(current)


def test_prepared_asr_is_recorded_without_creating_execution_or_claiming_completion(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    identity, path = asr_job(app, tmp_path)
    view = OperationFacade(app).get('asr', identity)
    assert (view.status, view.completed, view.total, view.controls) == ('prepared', 0, 1, ())
    assert not (path.parent / 'execution.json').exists()
    assert not (app.root / 'runtime').exists()


def test_translation_uses_pure_validator_without_provider_or_credential_construction(tmp_path, monkeypatch):
    from carr_library.assistant.translation import translation_approval
    from carr_library.assistant.translation_state import allocations, split_text, validate_translation_job
    from carr_library.providers import ProviderProfile, GOOGLE_TRANSLATION_ENDPOINT
    import carr_library.providers as providers
    app = LibraryApp(tmp_path / 'library')
    profile = ProviderProfile('synthetic-profile', 'PRIVATE PROFILE', 'google-translation-v2',
        GOOGLE_TRANSLATION_ENDPOINT, 'nmt', ('translation',),
        credential_ref='url-api-' + '8' * 32)
    text = 'PRIVATE ORIGINAL TRANSLATION TEXT'
    approval = translation_approval(profile, text, 'en', 'uk', 1000)
    value = dict(schema='carr-translation-job/1', id=approval.job_id, library_root=str(app.root),
        ledger_identity=[1, 2], created=STAMP, updated=STAMP, status='queued',
        profile=json.loads(json.dumps(asdict(profile))), title='PRIVATE TITLE',
        source_text=text, source_sha256=sha256(text.encode()).hexdigest(),
        source_language='en', target_language='uk', language_catalog_sha256='3' * 64,
        approval=json.loads(json.dumps(asdict(approval))),
        allocations=allocations(split_text(text), approval.budget_micros), receipts=[], pending=None)
    validate_translation_job(value, identity=approval.job_id)
    save(app.root / 'state' / 'translation' / (approval.job_id + '.json'), value)
    def forbidden(*args, **kwargs):
        pytest.fail('Dashboard constructed a provider gateway or credential vault.')
    monkeypatch.setattr(providers.ProviderGateway, '__init__', forbidden)
    monkeypatch.setattr(providers.WindowsCredentialVault, '__init__', forbidden)
    row = OperationFacade(app).list()[0]
    assert (row.kind, row.completed, row.total, row.controls) == ('translation', 0, 1, ())
    assert 'PRIVATE' not in repr(row) and profile.credential_ref not in repr(row)
    assert not (app.root / 'api-operations.sqlite').exists()


def test_restored_assistant_is_inert_and_completed_rows_are_only_observations(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Saved plans')
    service = AssistantService(app)
    identity = service.submit(service.preview('List books', Scope(workspace)))
    service.run(identity)
    facade = OperationFacade(app, assistant=service)
    assert facade.get('assistant', identity).status == 'complete'
    path = app.root / 'state' / 'assistant' / (identity + '.json')
    value = read_json(path, limit=64 * 1024 * 1024)
    value['data_dir'] = str(tmp_path / 'previous-library')
    save(path, value)
    row = facade.get('assistant', identity)
    assert row.status == 'attention' and row.controls == () and row.verification == 'not_performed'


def test_actual_prepared_document_request_is_visible_but_its_source_is_never_read(tmp_path):
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Prepared request')
    source = tmp_path / 'missing-original.txt'
    metadata = dict(title='PRIVATE TITLE', authors=(), language=None, edition=None, volume=None,
                    collections=(), work_id=uuid4().hex, edition_id=uuid4().hex)
    request = prepare_request(app.root, workspace, source, metadata, Config())
    row = OperationFacade(app).get('document', request['request_id'], workspace=workspace)
    assert row.status == 'pending' and row.controls == () and row.completed is None
    assert not source.exists() and 'PRIVATE' not in repr(row)


def test_malformed_or_aliased_checkpoint_cannot_hide_another_valid_job(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    first, path = render_job(app)
    second, other = render_job(app)
    path.write_text('PRIVATE malformed text', encoding='utf-8')
    rows = {row.id: row for row in OperationFacade(app).list()}
    assert rows[first].status == 'attention' and rows[second].status == 'running'
    assert 'PRIVATE' not in repr(rows)
    alias = tmp_path / 'external-alias'
    os.link(other, alias)
    assert next(row for row in OperationFacade(app).list() if row.id == second).status == 'attention'


def test_discovery_counts_non_job_entries_and_honours_cancel_and_byte_budget(tmp_path, monkeypatch):
    import carr_library.operations.facade as module
    app = LibraryApp(tmp_path / 'library')
    base = app.root / 'state' / 'assistant'
    base.mkdir(parents=True)
    for index in range(3):
        (base / ('irrelevant-' + str(index))).write_bytes(b'x')
    monkeypatch.setattr(module, '_MAX_DISCOVERY', 2)
    with pytest.raises(ResourceStateError, match='discovery'):
        OperationFacade(app).list(limit=2)
    monkeypatch.setattr(module, '_MAX_DISCOVERY', 10000)
    with pytest.raises(InterruptedError):
        OperationFacade(app).list(cancelled=lambda: True)
    render_job(app)
    monkeypatch.setattr(module, '_MAX_METADATA_BYTES', 10)
    with pytest.raises(ResourceStateError, match='read budget'):
        OperationFacade(app).list()


def test_resource_view_exposes_reservations_without_private_paths_or_native_tokens(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    directory = tmp_path / 'PRIVATE disk selection'
    directory.mkdir()
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(min_free_disk_bytes=0))
    with broker.reserve(ResourceRequest(memory_bytes=123, disk_bytes=321, disk_path=directory),
                        operation_id='private-user-label', timeout=0):
        value = OperationFacade(app, broker=broker).resources()
        assert value['usage'] == {'cpu_slots': 1, 'memory_bytes': 123, 'disk_bytes': 321}
        assert value['leases'][0]['operation_id'] == 'other'
        encoded = json.dumps(value)
        assert str(directory) not in encoded and 'private-user-label' not in encoded
        assert 'owner_token' not in encoded and 'owner_pid' not in encoded


def test_scope_rebinding_and_unknown_ids_are_rejected_without_new_stores(tmp_path):
    first, second = LibraryApp(tmp_path / 'first'), LibraryApp(tmp_path / 'second')
    facade = OperationFacade(first)
    with pytest.raises(ValueError):
        facade.get('asr', '../../secret')
    with pytest.raises(ValueError):
        facade.get('shell', uuid4().hex)
    with pytest.raises((ValueError, PermissionError)):
        facade.list(workspace='not-a-workspace')
    with pytest.raises(ValueError, match='resume'):
        facade.control('asr', uuid4().hex, 'resume')
    first.root = second.root
    with pytest.raises(ResourceStateError, match='changed'):
        facade.list()
    assert not (second.root / 'runtime').exists()



def test_recording_dashboard_reads_only_typed_receipt_and_redacts_capture_metadata(tmp_path, monkeypatch):
    from carr_library.audio.recording import RecordingOptions, RecordingService
    identity = 'e' * 32
    app = LibraryApp(tmp_path / 'library')
    value = {
        'schema': 'carr-recording/1', 'id': identity, 'filename': identity + '.wav',
        'title': 'PRIVATE recording title', 'created': STAMP,
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': 'd' * 64,
                   'name': 'PRIVATE device name'},
        'ffmpeg_sha256': 'c' * 64, 'completion': 'device-ended',
        'limits': asdict(RecordingOptions(max_seconds=1, sample_rate=24000)),
        'audio': {'sha256': 'a' * 64, 'size_bytes': 46, 'sample_rate': 24000,
                  'channels': 1, 'frames': 1, 'sample_width': 2, 'duration_seconds': 1 / 24000},
    }
    saved = app.root / 'state' / 'studio' / 'recordings' / (identity + '.json')
    save(saved, value)
    monkeypatch.setattr(RecordingService, '_audio', lambda *a: pytest.fail('Dashboard read PCM'))
    monkeypatch.setattr(RecordingService, '_runtime', lambda *a: pytest.fail('Dashboard selected an executable'))
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(min_free_disk_bytes=0))
    facade = OperationFacade(app, broker=broker)
    with broker.reserve(ResourceRequest(devices=('dshow:' + 'd' * 64,)),
                        operation_id='recording:' + identity, timeout=0):
        row = facade.get('recording', identity)
        assert row.status == 'complete' and row.completed == row.total == 1
        assert row.live is True and row.verification == 'not_performed' and row.controls == ()
        assert row.unit == 'PCM frames'
        assert 'PRIVATE' not in repr(row)
        view = facade.resources()
        assert view['leases'][0]['operation_id'] == 'recording:' + identity
        assert 'PRIVATE' not in repr(view) and str(app.root) not in repr(view)
    assert not saved.with_suffix('.wav').exists()
    assert next(row for row in facade.list() if row.id == identity).kind == 'recording'
