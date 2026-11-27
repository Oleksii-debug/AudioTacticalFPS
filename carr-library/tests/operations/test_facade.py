"""Views reuse actual import/assistant/studio stores without starting workers."""
from pathlib import Path
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations import OperationFacade, ResourceBroker, ResourceRequest
from carr_library.speech import VoiceRegistry
from carr_library.studio.render import ProductionService


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
