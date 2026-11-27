"""Views reuse actual import/assistant/studio stores without starting workers."""
from pathlib import Path
from dataclasses import asdict
import importlib.util
import json
import threading
from uuid import uuid4
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations import OperationFacade, ResourceBroker, ResourceRequest
from carr_library.speech import VoiceRegistry
from carr_library.studio.render import ProductionService


# Fixture code supplies a real bounded subprocess, never inference weights.
# Product imports remain the selected source or installed carr_library package.
_asr_spec = importlib.util.spec_from_file_location('facade_asr_fixture_helpers',
    Path(__file__).parents[1] / 'transcription' / 'test_resource_admission.py')
_asr = importlib.util.module_from_spec(_asr_spec)
_asr_spec.loader.exec_module(_asr)
engine, source, service = _asr.engine, _asr.source, _asr.service


def test_actual_facade_controls_only_selected_asr_attempt_and_never_resumes_it(service, source, engine):
    app = LibraryApp(service.directory.parent.parent)
    broker = _asr.policy(service)
    facade = OperationFacade(app, broker=broker, transcription=service)
    identity = service.submit([source])
    job = service.jobs / identity
    before_inputs = {p.name: p.read_bytes() for p in (job / 'inputs').iterdir()}
    with _asr.running(service, identity, engine('pause')) as running:
        thread, _, results, errors = running
        _asr.eventually(lambda: facade.get('asr', identity).live is True)
        selected = facade.get('asr', identity)
        assert selected.controls == ('pause', 'cancel') and selected.active_attempt_id
        requested = facade.control('asr', identity, 'pause', expected_attempt_id=selected.active_attempt_id)
        assert requested.status == 'running'  # request is not an acknowledgement
        _asr.eventually(lambda: (job / 'test_pause_seen.json').exists())
        facade.control('asr', identity, 'cancel', expected_attempt_id=selected.active_attempt_id)
        thread.join(5)
        assert not thread.is_alive() and not errors and results[0]['state'] == 'cancelled'
    assert facade.get('asr', identity).controls == () and broker.snapshot()['leases'] == []
    with pytest.raises(ValueError, match='resume'):
        facade.control('asr', identity, 'resume')

    # Only the original workstation's explicit run creates a fresh attempt.
    waiting = threading.Event()
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='unrelated-owner', timeout=0):
        with _asr.running(service, identity, engine('cooperative'),
                progress=lambda *args: waiting.set() if args[-1].startswith('Waiting') else None) as running:
            thread, _, results, errors = running
            assert waiting.wait(5)
            current = facade.get('asr', identity)
            assert current.active_attempt_id != selected.active_attempt_id and current.live is None
            for action in ('pause', 'cancel'):
                with pytest.raises(ValueError, match='attempt changed'):
                    facade.control('asr', identity, action, expected_attempt_id=selected.active_attempt_id)
            assert not (job / 'pause.flag').exists() and not (job / 'stop.flag').exists()
            facade.control('asr', identity, 'cancel', expected_attempt_id=current.active_attempt_id)
            thread.join(5)
            assert not thread.is_alive() and not errors and results[0]['state'] == 'cancelled'
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['unrelated-owner']
    assert before_inputs == {p.name: p.read_bytes() for p in (job / 'inputs').iterdir()}
    assert len(service.list()) == 1 and broker.snapshot()['leases'] == []


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_facade_rejects_stale_asr_journal_without_owned_active_attempt(service, source, action):
    app = LibraryApp(service.directory.parent.parent)
    identity = service.submit([source])
    job = service.jobs / identity
    value = service.get(identity, verify=False)
    value.update(state='running', active_attempt_id='a' * 32)
    atomic_json(job / 'job.json', value, replace=True)
    facade = OperationFacade(app, transcription=service)
    with pytest.raises(ValueError, match='already ended or changed'):
        facade.control('asr', identity, action, expected_attempt_id='a' * 32)
    assert not (job / 'pause.flag').exists() and not (job / 'stop.flag').exists()


def test_actual_existing_stores_share_view_and_controls_without_resubmission(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Shared operation view')
    other = app.create_workspace('Another library')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / 'source.txt'
    source.write_text('Preserved original body that must not enter a dashboard.', encoding='utf-8')
    imported = app.plan_import_batch(workspace, [source])
    other_imported = app.plan_import_batch(other, [source])
    assistant = AssistantService(app)
    plan = assistant.preview('List books', Scope(workspace, (), outputs))
    assistant_id = assistant.submit(plan)
    studio = ProductionService(app.root / 'state' / 'studio', voices=VoiceRegistry([]))
    project = studio.create('[Pause: 1]', title='No engine is needed for this saved plan')
    studio_id = studio.submit(project, outputs)
    broker = ResourceBroker.for_app(app.root)
    facade = OperationFacade(app, broker=broker, studio=studio, assistant=assistant)
    before_source = source.read_bytes()
    with broker.reserve(ResourceRequest(), operation_id='studio:' + studio_id, timeout=0):
        views = facade.list(workspace=workspace)
        assert {view.key for view in views} == {
            'import:' + imported['batch_id'], 'assistant:' + assistant_id, 'studio:' + studio_id}
        assert next(view for view in views if view.kind == 'studio').live is True
        assert next(view for view in views if view.kind == 'assistant').live is None
        assert all('Preserved original' not in repr(view) for view in views)
        assert facade.get('studio', studio_id).status == 'pending'
        facade.control('studio', studio_id, 'pause')
        assert read_json(studio.jobs_root / studio_id / 'control.json') == {'action': 'pause'}
        assert facade.get('studio', studio_id).status == 'pending'  # requested != acknowledged
        facade.control('assistant', assistant_id, 'cancel')
        assert assistant.store.control(assistant_id) == 'cancel'
        with pytest.raises(PermissionError):
            facade.get('import', other_imported['batch_id'], workspace=workspace)
        with pytest.raises(ValueError, match='resume'):
            facade.control('studio', studio_id, 'resume')
        with pytest.raises(ValueError, match='does not expose'):
            facade.control('import', imported['batch_id'], 'cancel')
    assert source.read_bytes() == before_source
    assert len(assistant.jobs()) == 1 and len(studio.jobs()) == 1
    assert not list(outputs.iterdir())
    assert not tuple(studio.cache.iterdir())


def test_actual_completed_assistant_and_corruption_are_reported_honestly(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Read-only dashboard')
    service = AssistantService(app)
    identity = service.submit(service.preview('List books', Scope(workspace)))
    service.run(identity)
    facade = OperationFacade(app, assistant=service)
    view = facade.get('assistant', identity)
    assert (view.status, view.completed, view.total, view.live) == ('complete', 1, 1, None)
    assert view.controls == ()
    # A dashboard must neither execute injected text nor display diagnostic
    # bodies that could contain private document or credential material.
    service.store.path(identity).write_text('SECRET malformed original body', encoding='utf-8')
    broken = facade.list()[0]
    assert broken.status == 'attention' and broken.controls == ()
    assert 'SECRET' not in repr(broken)


def test_translation_checkpoint_has_no_provider_side_effects(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    identity = uuid4().hex
    path = app.root / 'state' / 'translation' / 'jobs' / (identity + '.json')
    atomic_json(path, {'schema': 'carr-assistant-job/1', 'kind': 'translation/1',
        'id': identity, 'status': 'paused', 'receipts': [{'text': 'private translation'}],
        'chunks': ['private original', 'unfinished'], 'updated': '2026-11-27T00:00:00Z',
        'profile': {'credential_ref': 'opaque-private-ref'}})
    values = OperationFacade(app).list()
    assert [(value.kind, value.completed, value.total) for value in values] == [('translation', 1, 2)]
    assert all(value.live is None and value.controls == () for value in values)
    assert all('private' not in repr(value) for value in values)


@pytest.mark.parametrize('kind', ['studio', 'assistant'])
def test_malformed_status_does_not_hide_other_valid_saved_operations(tmp_path, kind):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Malformed checkpoint isolation')
    service = AssistantService(app)
    valid = service.submit(service.preview('List books', Scope(workspace)))
    bad = uuid4().hex
    if kind == 'studio':
        path = app.root / 'state' / 'studio' / 'jobs' / bad / 'job.json'
        payload = {'schema': 1, 'id': bad, 'status': [], 'nodes': {}}
    else:
        path = app.root / 'state' / 'assistant' / (bad + '.json')
        payload = {'schema': 'carr-assistant-job/1', 'id': bad, 'data_dir': str(app.root),
                   'workspace': workspace, 'status': 'pending', 'steps': [{'status': []}]}
    atomic_json(path, payload)
    facade = OperationFacade(app)
    rows = {value.id: value for value in facade.list()}
    assert rows[bad].status == 'attention' and rows[valid].status == 'queued'
    assert rows[bad].controls == ()
    with pytest.raises((ValueError, RuntimeError)):
        facade.get(kind, bad)


def test_cancelled_checkpoint_inspection_propagates_instead_of_becoming_attention(tmp_path, monkeypatch):
    import carr_library.operations.facade as module
    app = LibraryApp(tmp_path / 'data')
    identity = uuid4().hex
    atomic_json(app.root / 'state' / 'studio' / 'jobs' / identity / 'job.json',
                {'schema': 1, 'id': identity, 'status': 'pending', 'nodes': {}})
    def interrupted(*_args, **_kwargs):
        raise InterruptedError('Synthetic cancelled metadata read')
    monkeypatch.setattr(module, 'read_json', interrupted)
    with pytest.raises(InterruptedError, match='cancelled metadata'):
        OperationFacade(app).list()
    with pytest.raises(InterruptedError, match='cancelled'):
        OperationFacade(app).list(cancelled=lambda: True)


def test_facade_rejects_traversal_and_unknown_workspace(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    facade = OperationFacade(app)
    with pytest.raises(ValueError):
        facade.get('assistant', '../../source')
    with pytest.raises(ValueError):
        facade.get('shell', 'tool')
    with pytest.raises((ValueError, PermissionError)):
        facade.list(workspace='not-an-existing-library')
    assert not (app.root / 'runtime').exists()  # read-only views create no broker or job store


def test_canonical_transcription_view_uses_32hex_job_store_without_opening_live_status(tmp_path, monkeypatch):
    from carr_library.transcription import TranscriptionService
    app = LibraryApp(tmp_path / 'data')
    source = tmp_path / 'selected.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * 800)
    service = TranscriptionService(app.root / 'state/transcription')
    identity = service.submit([source])
    job = service.get(identity, verify=False)
    status = service.jobs / identity / 'job_status.json'
    status.write_text('Live worker-owned status must not be read by the dashboard.')
    source.unlink()  # The canonical immutable input snapshot is sufficient.
    opened, verified = [], []
    original_open, original_get = Path.open, TranscriptionService.get

    def guarded(path, *args, **kwargs):
        opened.append(path.name)
        assert path.name not in ('job_status.json', 'job_result.json', 'engine.json')
        return original_open(path, *args, **kwargs)

    def recorded(self, selected, **kwargs):
        verified.append(kwargs.get('verify', True))
        return original_get(self, selected, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded)
    monkeypatch.setattr(TranscriptionService, 'get', recorded)
    before = {path.relative_to(app.root) for path in app.root.rglob('*')}
    rows = OperationFacade(app).list()
    assert len(rows) == 1
    row = rows[0]
    assert (row.kind, row.id, row.status, row.completed, row.total) == ('asr', identity, 'queued', 0, 1)
    assert row.checkpoint == 'state/transcription/jobs/' + identity + '/job.json'
    assert 'not verified' in row.phase and row.controls == () and row.active_attempt_id is None
    assert len(identity) == 32 and verified == [False]
    assert 'selected.wav' not in json.dumps(asdict(row))
    assert before == {path.relative_to(app.root) for path in app.root.rglob('*')}
    assert service.get(identity, verify=False)['inputs'] == job['inputs']
    assert 'job_status.json' not in opened


@pytest.mark.parametrize('target', ['app', 'studio', 'assistant', 'asr'])
def test_relocated_service_cannot_control_a_stale_view(tmp_path, target):
    from carr_library.transcription import TranscriptionService
    app = LibraryApp(tmp_path / 'data')
    other = LibraryApp(tmp_path / 'other')
    workspace = app.create_workspace('Original scope')
    assistant = AssistantService(app)
    identity = assistant.submit(assistant.preview('List books', Scope(workspace)))
    studio = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]))
    asr = TranscriptionService(app.root / 'state/transcription')
    facade = OperationFacade(app, assistant=assistant, studio=studio, transcription=asr)
    assert facade.get('assistant', identity).status == 'queued'
    if target == 'app':
        app.root = other.root
    elif target == 'studio':
        studio.root = other.root / 'state/studio'
    elif target == 'assistant':
        assistant.store.directory = other.root / 'state/assistant'
    else:
        asr.directory = other.root / 'state/transcription'
    with pytest.raises(RuntimeError, match='data folder'):
        facade.control('assistant', identity, 'cancel')
    assert not (tmp_path / 'data/state/assistant' / (identity + '.control.json')).exists()


def test_archived_workspace_is_reauthorized_before_control(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Original library')
    assistant = AssistantService(app)
    identity = assistant.submit(assistant.preview('List books', Scope(workspace)))
    facade = OperationFacade(app, assistant=assistant)
    assert 'cancel' in facade.get('assistant', identity).controls
    app.archive_workspace(workspace)
    with pytest.raises((ValueError, PermissionError)):
        facade.control('assistant', identity, 'cancel')
    assert assistant.store.control(identity) == 'run'


def test_unrelated_namespace_entries_are_in_the_discovery_budget(tmp_path, monkeypatch):
    import carr_library.operations.facade as module
    app = LibraryApp(tmp_path / 'data')
    root = app.root / 'state/assistant'
    root.mkdir(parents=True)
    for index in range(4):
        (root / ('unrelated-' + str(index))).write_text('private')
    monkeypatch.setattr(module, '_MAX_DISCOVERY', 3)
    with pytest.raises(RuntimeError, match='bounded discovery'):
        OperationFacade(app).list(limit=3)


def test_giant_saved_timestamp_is_attention_without_exposing_checkpoint(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Timestamp guard')
    service = AssistantService(app)
    identity = service.submit(service.preview('List books', Scope(workspace)))
    value = read_json(service.store.path(identity))
    value['updated'] = 10**400
    atomic_json(service.store.path(identity), value, replace=True)
    row, = OperationFacade(app).list()
    assert row.status == 'attention' and row.controls == ()


def test_facade_owns_only_services_it_created(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'data')
    borrowed = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]))
    calls = []
    original = ProductionService.close
    def close(self):
        calls.append(self)
        return original(self)
    monkeypatch.setattr(ProductionService, 'close', close)
    OperationFacade(app, studio=borrowed).close()
    assert not calls and not borrowed._closed.is_set()
    owned = OperationFacade.for_app(app, controls=True)
    owned.close()
    owned.close()
    assert calls == [owned.services['studio']] and borrowed not in calls
