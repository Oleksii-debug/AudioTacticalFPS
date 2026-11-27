"""Views reuse actual import/assistant/studio stores without starting workers."""
from pathlib import Path
import importlib.util
import sys
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations import OperationFacade, ResourceBroker, ResourceRequest
from carr_library.speech import VoiceRegistry
from carr_library.studio.render import ProductionService
from carr_library.assistant.translation import TranslationService
from carr_library.ui.translation_dialog import translation_approval


def _fixtures():
    path = Path(__file__).resolve().parents[1] / 'assistant' / 'test_translation.py'
    spec = importlib.util.spec_from_file_location('operations_translation_fixtures', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_translation = _fixtures()
connection = _translation.connection


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


def test_actual_translation_v1_and_v2_views_do_not_contact_provider_or_grant_export(tmp_path, connection, monkeypatch):
    case = _translation.bound_ready(tmp_path, connection)
    identity, _ = _translation.submit_bound(case)
    case.service.resume(identity)
    approval, cost = translation_approval(case.profile, 'A separate pasted excerpt.', 'en', 'uk', '1')
    legacy = case.service.submit(case.profile, 'A separate pasted excerpt.', 'en', 'uk',
                                 authorization=approval, per_call_cost=cost)
    other = case.app.create_workspace('Different selected library')
    before_calls, before_vault = len(connection['calls']), len(case.vault.calls)
    before = {key: case.service.jobs.path(key).read_bytes() for key in (identity, legacy)}
    monkeypatch.setattr(case.service, 'resume', lambda *_args, **_kwargs: pytest.fail('Dashboard resumed translation.'))
    monkeypatch.setattr(case.service, 'export', lambda *_args, **_kwargs: pytest.fail('Dashboard exported translation.'))
    facade = OperationFacade(case.app, translation=case.service)
    selected = {row.id: row for row in facade.list(workspace=case.workspace)}
    assert (selected[identity].workspace, selected[identity].status) == (case.workspace, 'complete')
    assert selected[identity].completed == selected[identity].total
    assert selected[legacy].workspace is None and selected[legacy].status == 'queued'
    assert selected[identity].controls == selected[legacy].controls == ()
    assert {row.id for row in facade.list(workspace=other)} == {legacy}
    with pytest.raises(PermissionError, match='another selected library'):
        facade.get('translation', identity, workspace=other)
    assert all('Witness evidence' not in repr(row) and 'credential' not in repr(row) for row in selected.values())
    assert (len(connection['calls']), len(case.vault.calls)) == (before_calls, before_vault)
    assert before == {key: case.service.jobs.path(key).read_bytes() for key in before}
    case.app.archive_workspace(case.workspace)
    assert identity not in {row.id for row in facade.list()}
    with pytest.raises(PermissionError, match='archived'):
        facade.get('translation', identity, workspace=case.workspace)
    assert len(connection['calls']) == before_calls


def test_dashboard_preserves_existing_64_mib_assistant_checkpoint_contract(tmp_path, connection):
    case = _translation.bound_ready(tmp_path, connection, first=1, last=1, raw='Synthetic small excerpt.')
    approval, cost = translation_approval(case.profile, case.source.text, 'en', 'uk', '1')
    identity = case.service.submit(case.profile, case.source.text, 'en', 'uk',
                                   authorization=approval, per_call_cost=cost)
    record = case.service.jobs.load(identity)
    # Existing generic job metadata is opaque to the translation validator.
    # This real valid checkpoint fits its 64 MiB writer but exceeds read_json's
    # unrelated 32 MiB default; dashboard use must retain the owning contract.
    record['retained_fixture_metadata'] = 'x' * (32 * 1024**2)
    case.service.jobs.save(record)
    path = case.service.jobs.path(identity)
    assert 32 * 1024**2 < path.stat().st_size < 64 * 1024**2
    before = len(connection['calls'])
    view = OperationFacade(case.app).get('translation', identity)
    assert view.status == 'queued' and (view.completed, view.total) == (0, 1)
    assert len(connection['calls']) == before


def test_workspace_asr_uses_existing_hashed_store_and_no_fabricated_stop_controls(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'data')
    first, second = app.create_workspace('First'), app.create_workspace('Second')
    source = tmp_path / 'synthetic.wav'
    source.write_bytes(b'Synthetic media; no model or acoustic execution.')
    service, other = app.transcription(first), app.transcription(second)
    identity, another = service.submit([source]), other.submit([source])
    before = (service._path(identity) / 'job.json').read_bytes()
    facade = OperationFacade(app, transcription=service)
    monkeypatch.setattr(service, 'run', lambda *_args, **_kwargs: pytest.fail('Listing started a worker.'))
    with pytest.raises(ValueError, match='does not expose'):
        facade.control('asr', identity, 'pause', workspace=first)
    with pytest.raises(ValueError, match='does not expose'):
        facade.control('asr', identity, 'cancel', workspace=first)
    selected = facade.list(workspace=first)
    assert [(row.id, row.workspace, row.status, row.controls) for row in selected] == [(identity, first, 'ready', ())]
    assert selected[0].checkpoint == (service._path(identity) / 'job.json').relative_to(app.root).as_posix()
    assert selected[0].key == 'asr:' + service.jobs_root.parent.name + ':' + identity
    assert {row.id for row in facade.list(workspace=second)} == {another}
    with pytest.raises((OSError, ValueError)):
        facade.get('asr', identity, workspace=second)
    assert (service._path(identity) / 'job.json').read_bytes() == before
    assert not (service._path(identity) / 'stop.flag').exists()
    assert not (service._path(identity) / 'control.json').exists()
    assert not (app.root / 'runtime').exists()
    app.archive_workspace(first)
    assert {row.id for row in facade.list()} == {another}
    with pytest.raises(PermissionError, match='archived'):
        facade.list(workspace=first)


def test_copied_asr_job_id_cannot_collapse_workspace_identity(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    first, second = app.create_workspace('A'), app.create_workspace('B')
    source = tmp_path / 'synthetic.wav'
    source.write_bytes(b'Original synthetic media.')
    service, other = app.transcription(first), app.transcription(second)
    identity = service.submit([source])
    record = service.get(identity)
    record['workspace'] = second
    atomic_json(other._path(identity) / 'job.json', record)
    facade = OperationFacade(app)
    rows = [row for row in facade.list() if row.id == identity]
    assert len(rows) == 2 and len({row.key for row in rows}) == 2
    assert {row.workspace for row in rows} == {first, second}
    assert facade.get('asr', identity, workspace=first).workspace == first
    assert facade.get('asr', identity, workspace=second).workspace == second


def test_inconsistent_completed_asr_inventory_is_attention_without_output_reads(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Journal observation')
    source = tmp_path / 'synthetic.wav'
    source.write_bytes(b'Synthetic preserved media.')
    service = app.transcription(workspace)
    identity = service.submit([source])
    job = service.get(identity)
    job.update(status='completed', done=1, failed=0, receipts=['receipts/claimed.json'], artifacts=[])
    atomic_json(service._path(identity) / 'job.json', job, replace=True)
    monkeypatch.setattr(service, 'verify', lambda *_args, **_kwargs: pytest.fail('Dashboard scanned output bytes.'))
    facade = OperationFacade(app, transcription=service)
    view = facade.list(workspace=workspace)[0]
    assert view.status == 'attention' and view.controls == () and view.completed is None
    with pytest.raises(RuntimeError, match='incomplete output inventory'):
        facade.get('asr', identity, workspace=workspace)


def test_injected_services_from_another_root_never_gain_controls_or_data(tmp_path):
    first, second = LibraryApp(tmp_path / 'a'), LibraryApp(tmp_path / 'b')
    a, b = first.create_workspace('A'), second.create_workspace('B')
    with pytest.raises(RuntimeError, match='another library or data folder'):
        OperationFacade(first, transcription=second.transcription(b))
    foreign_translation = TranslationService(second.root / 'state' / 'translation', library=second)
    with pytest.raises(RuntimeError, match='another library or data folder'):
        OperationFacade(first, translation=foreign_translation)
    service = first.transcription(a)
    facade = OperationFacade(first, transcription=service)
    service.jobs_root = second.transcription(b).jobs_root
    with pytest.raises(RuntimeError, match='another library or data folder'):
        facade.list(workspace=a)


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
