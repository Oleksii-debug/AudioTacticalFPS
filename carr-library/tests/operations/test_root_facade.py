"""Read actual product journals without starting engines or following saved paths."""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.durable_state import atomic_json
from carr_library.operations import OperationFacade, ResourceBroker, ResourceRequest
from carr_library.operations.studio import read_studio_operation


def test_existing_import_and_assistant_stores_keep_workspace_and_control_authority(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected library')
    other = app.create_workspace('Other library')
    source = tmp_path / 'source.txt'
    source.write_text('Private source text stays out of the task view.', encoding='utf-8')
    first = app.plan_import_batch(workspace, [source])
    second = app.plan_import_batch(other, [source])
    assistant = AssistantService(app)
    identity = assistant.submit(assistant.preview('List books', Scope(workspace)))
    facade = OperationFacade(app, assistant=assistant)
    views = facade.list(workspace=workspace)
    assert {row.key for row in views} == {'import:' + first['batch_id'], 'assistant:' + identity}
    assert all('Private source text' not in repr(row) for row in views)
    with pytest.raises(PermissionError):
        facade.get('import', second['batch_id'], workspace=workspace)
    facade.control('assistant', identity, 'cancel')
    assert assistant.store.control(identity) == 'cancel'
    assert not (app.root / 'runtime').exists()


def test_studio_view_only_uses_an_explicitly_opened_render_context(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    external = tmp_path / 'externally-selected-output'
    external.mkdir()
    identity = uuid4().hex
    # A saved output suggestion alone grants no dashboard filesystem discovery.
    controller = SimpleNamespace(private_root=app.root, project_id=identity,
        output_dir=external, _operation_output=None)
    assert read_studio_operation(controller) is None
    assert OperationFacade(app, studio=controller).list() == ()
    controller._operation_output = external
    cancelled = []
    controller.cancel_render = cancelled.append
    atomic_json(external / 'render-journal.json', {'schema_version': 1, 'run_key': 'a' * 64,
        'state': 'running', 'completed_steps': 5, 'scene_count': 2, 'source_sha256': 'b' * 64,
        'message': 'Private narration text must not appear in the task dashboard.'})
    broker = ResourceBroker.for_app(app.root)
    facade = OperationFacade(app, broker=broker, studio=controller)
    with broker.reserve(ResourceRequest(), operation_id='studio:' + identity, timeout=0):
        value, = facade.list()
        assert (value.kind, value.completed, value.total, value.live) == ('studio', 5, None, True)
        assert value.controls == ('cancel',) and 'Private narration' not in repr(value)
        facade.control('studio', identity, 'cancel')
        assert cancelled == [identity]
        assert facade.get('studio', identity).status == 'running'
    assert not (app.root / 'state/studio').exists()


def test_corrupt_checkpoint_remains_attention_without_source_body(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    directory = app.root / 'state/assistant'
    directory.mkdir(parents=True)
    (directory / (uuid4().hex + '.json')).write_text('PRIVATE malformed source body', encoding='utf-8')
    value, = OperationFacade(app).list()
    assert value.status == 'attention' and value.controls == ()
    assert 'PRIVATE' not in repr(value)
    with pytest.raises(ValueError):
        OperationFacade(app).get('assistant', '../../source')
