"""Real typed plan/job reuse; templates never become execution authority."""
from contextlib import contextmanager
from dataclasses import replace
import json
import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import templates as module
from carr_library.assistant.templates import (TemplateStore, WorkflowTemplate, definition_from_plan,
    definition_from_project, instantiate_plan, review_request, batch_settings)
from carr_library.studio.project import parse_script, Project


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    inputs = tmp_path / 'inputs'; inputs.mkdir()
    outputs = tmp_path / 'outputs'; outputs.mkdir()
    workspace = app.create_workspace('Current explicit scope')
    return app, inputs, outputs, workspace


def recipe(store, *, tool='library.search', argument='query'):
    return store.save('Named query', 'assistant',
        {'steps': [{'tool': tool, 'arguments': {argument: {'$parameter': 'value'}}}]},
        {'value': {'kind': 'text', 'label': 'Current value', 'example': 'railway'}})


def test_real_per_item_plan_preview_submit_run_and_restart_are_separate(library):
    app, inputs, outputs, workspace = library
    app.add_text(workspace, 'Retained source', 'railway harbours')
    service, store = AssistantService(app), TemplateStore(app.root)
    saved = recipe(store)
    raw = (store.root / (saved.id + '.json')).read_bytes()
    request = review_request(saved, {}, items=[{'value': 'railway'}, {'value': 'harbours'}])
    assert service.jobs() == ()
    plan = service.preview(json.dumps(request), Scope(workspace, (inputs,), outputs))
    assert [step.arguments['query'] for step in plan.steps] == ['railway', 'harbours']
    assert service.jobs() == () and not list(outputs.iterdir())
    job = service.submit(plan)
    assert service.get(job).status == 'queued'
    result = service.run(job)
    assert result.status == 'complete' and result.completed == 2
    assert len(result.results[0]['results']) == len(result.results[1]['results']) == 1
    reopened = AssistantService(LibraryApp(app.root))
    newer = reopened.submit(reopened.preview(json.dumps(request), Scope(workspace, (), outputs)))
    assert newer != job and reopened.get(newer).status == 'queued'
    assert (store.root / (saved.id + '.json')).read_bytes() == raw


def test_saved_plan_scope_approval_and_completed_receipts_are_not_template_data(library):
    app, inputs, outputs, workspace = library
    service = AssistantService(app)
    original = Plan(Scope(workspace, (inputs,), outputs, allow_full_backup=True),
                    (Step('library.backup', {'name': 'old-backup'}),))
    body = definition_from_plan(original)
    assert set(body) == {'steps'} and 'scope' not in body
    saved = TemplateStore(app.root).save('Full backup recipe', 'assistant', body)
    request = json.dumps(review_request(saved, {}))
    with pytest.raises(PermissionError):
        service.preview(request, Scope(workspace, (inputs,), outputs))
    assert service.jobs() == () and not (outputs / 'old-backup').exists()


def test_template_cannot_expand_new_input_scope_or_escape_output(library):
    app, inputs, outputs, workspace = library
    private = outputs / 'outside.txt'; private.write_text('not selected', encoding='utf-8')
    store, service = TemplateStore(app.root), AssistantService(app)
    saved = recipe(store, tool='library.import', argument='path')
    request = json.dumps(review_request(saved, {'value': str(private)}))
    with pytest.raises(PermissionError):
        service.preview(request, Scope(workspace, (inputs,), outputs))
    raw = {'steps': [{'tool': 'library.extract', 'arguments': {'edition': 'not-read', 'name': '../escape'}}]}
    other = store.save('Escaping output', 'assistant', raw)
    with pytest.raises(PermissionError):
        service.preview(json.dumps(review_request(other, {})), Scope(workspace, (), outputs))
    assert private.read_text(encoding='utf-8') == 'not selected' and not service.jobs()


@pytest.mark.parametrize('payload', [
    {'steps': [{'tool': 'shell', 'arguments': {'command': 'whoami'}}]},
    {'steps': [{'tool': 'library.list', 'arguments': {'code': 'print(1)'}}]},
    {'steps': [{'tool': {'$parameter': 'operation'}, 'arguments': {}}]},
    {'steps': [{'tool': 'library.list', 'arguments': {}}], 'scope': {'allow_create': True}},
])
def test_only_fixed_allowlisted_typed_operations_can_be_saved(library, payload):
    store = TemplateStore(library[0].root)
    parameters = ({'operation': {'kind': 'text', 'label': 'Operation', 'example': 'library.list'}}
                  if isinstance(payload['steps'][0]['tool'], dict) else {})
    with pytest.raises(ValueError):
        store.save('Rejected macro', 'assistant', payload, parameters)
    assert store.list() == ()


@pytest.mark.parametrize('change', ['scope', 'approval', 'status', 'job_id', 'callback', 'schema'])
def test_template_record_rejects_authority_or_future_fields(library, change):
    saved = recipe(TemplateStore(library[0].root))
    raw = saved.to_dict(); raw[change] = 'untrusted'
    with pytest.raises(ValueError):
        WorkflowTemplate.from_dict(raw)


@pytest.mark.parametrize('values', [None, [], {'wrong': 'x'}, {'value': 1}, {'value': 'x', 'extra': 'y'}])
def test_parameters_are_explicit_exact_and_typed(library, values):
    saved = recipe(TemplateStore(library[0].root))
    with pytest.raises(ValueError):
        instantiate_plan(saved, Scope(), values)


def test_parameter_text_is_literal_data_not_a_second_instruction(library):
    app, _, _, workspace = library
    saved = recipe(TemplateStore(app.root))
    supplied = 'railway"\nCreate library "Injected"\n__import__("os")'
    plan = instantiate_plan(saved, Scope(workspace), {'value': supplied})
    assert len(plan.steps) == 1 and plan.steps[0].tool == 'library.search'
    assert plan.steps[0].arguments == {'query': supplied}
    assert len(app.workspaces()) == 1


def test_expansion_obeys_current_budget_and_rejects_arbitrary_iterators(library):
    saved = recipe(TemplateStore(library[0].root))
    with pytest.raises(ValueError, match='step budget'):
        instantiate_plan(saved, Scope(max_steps=1), {}, items=[{'value': 'a'}, {'value': 'b'}])
    consumed = []
    def rows():
        consumed.append(True); yield {'value': 'a'}
    with pytest.raises(ValueError, match='plain JSON'):
        instantiate_plan(saved, Scope(), {}, items=rows())
    assert consumed == []


def test_project_template_has_fresh_ids_and_no_reused_source_authority(library):
    app, inputs, outputs, _ = library
    original = parse_script('Narrator: Original words.', voice_ids=('selected:voice',))
    scene = original.scenes[0]
    old_line = replace(scene.lines[0], source_ref='old-workspace:old-revision:0')
    original = replace(original, scenes=(replace(scene, lines=(old_line,)),))
    old = original.to_dict()
    saved = TemplateStore(app.root).save('Narration layout', 'project', definition_from_project(original))
    first = instantiate_plan(saved, Scope(input_roots=(inputs,), output_root=outputs), {})
    second = instantiate_plan(saved, Scope(input_roots=(inputs,), output_root=outputs), {})
    p1, p2 = [Project.from_dict(plan.steps[0].arguments['script']) for plan in (first, second)]
    assert len({original.id, p1.id, p2.id}) == 3 and p1.revision == p2.revision == 1
    assert p1.roles == original.roles and p1.scenes[0].lines[0].text == old_line.text
    assert p1.scenes[0].lines[0].source_ref == p2.scenes[0].lines[0].source_ref == ''
    assert original.to_dict() == old


def test_project_cue_paths_need_the_new_scope(library):
    from carr_library.studio.project import Cue
    app, inputs, outputs, _ = library
    cue = inputs / 'local.wav'; cue.write_bytes(b'only a path-admission fixture')
    project = parse_script('Narrator: data', voice_ids=('local:voice',))
    project = replace(project, scenes=(replace(project.scenes[0], cues=(Cue('local', str(cue)),)),))
    saved = TemplateStore(app.root).save('Clip template', 'project', definition_from_project(project))
    with pytest.raises(PermissionError):
        instantiate_plan(saved, Scope(output_root=outputs), {})
    actual = instantiate_plan(saved, Scope(input_roots=(inputs,), output_root=outputs), {})
    assert actual.steps[0].arguments['script']['scenes'][0]['cues'][0]['path'] == str(cue)


def test_store_revision_cas_and_defensive_json_preserve_exact_existing_bytes(library):
    store = TemplateStore(library[0].root); first = recipe(store)
    changed = first.definition; changed['steps'][0]['tool'] = 'shell'
    assert first.definition['steps'][0]['tool'] == 'library.search'
    second = store.save('Updated query', first.kind, first.definition, first.parameters, previous=first)
    raw = (store.root / (first.id + '.json')).read_bytes()
    assert second.id == first.id and second.revision == 2
    with pytest.raises(ValueError, match='changed after review'):
        store.save('Stale update', first.kind, first.definition, first.parameters, previous=first)
    assert (store.root / (first.id + '.json')).read_bytes() == raw


@pytest.mark.parametrize('where', ['publication', 'lock-exit'])
@pytest.mark.parametrize('error', [OSError, KeyboardInterrupt])
def test_late_save_error_reports_checked_presence_without_false_execution(library, monkeypatch, where, error):
    store = TemplateStore(library[0].root)
    original_write, original_lock = module.atomic_json, module.state_lock
    if where == 'publication':
        def publish(path, value, **kwargs):
            original_write(path, value, **kwargs); raise error('after publication')
        monkeypatch.setattr(module, 'atomic_json', publish)
    else:
        @contextmanager
        def lock(*args, **kwargs):
            with original_lock(*args, **kwargs):
                yield
            raise error('after actual release')
        monkeypatch.setattr(module, 'state_lock', lock)
    saved = recipe(store)
    assert store.get(saved.id).fingerprint == saved.fingerprint
    assert 'present' in store.warning and 'could not be confirmed' in store.warning
    assert AssistantService(library[0]).jobs() == ()


@pytest.mark.parametrize('foreign', [False, True])
def test_prepublication_or_foreign_bytes_cannot_be_reported_as_saved(library, monkeypatch, foreign):
    store = TemplateStore(library[0].root); actual = module.atomic_json
    def fail(path, value, **kwargs):
        if foreign:
            actual(path, {**value, 'name': 'Foreign current value'}, **kwargs)
        raise OSError('publication failed')
    monkeypatch.setattr(module, 'atomic_json', fail)
    with pytest.raises(OSError):
        recipe(store)
    assert store.warning == ''


def test_bounded_reader_uses_the_actual_record_allowance(library, monkeypatch):
    store = TemplateStore(library[0].root); saved = recipe(store)
    actual, budgets = module.read_json, []
    def read(path, *, maximum):
        budgets.append(maximum); return actual(path, maximum=maximum)
    monkeypatch.setattr(module, 'read_json', read)
    assert store.get(saved.id) == saved and budgets == [module.MAX_TEMPLATE_BYTES]


def test_batch_preset_reuses_actual_split_and_name_validators():
    from dataclasses import asdict
    from carr_library.conversion.planning import SplitOptions
    settings = dict(options=asdict(SplitOptions(mode='blank-lines')), template='{source}-{part:03d}',
                    combined=False, action='text', voice_id='', language='', format='wav')
    assert batch_settings(settings) == settings
    for changed in ({**settings, 'destination': '/old/output'}, {**settings, 'job_id': 'old'},
                    {**settings, 'template': '../escape'}, {**settings, 'format': 'arbitrary-filter'}):
        with pytest.raises(ValueError):
            batch_settings(changed)


def test_real_document_preset_still_requires_capture_prepare_preview_approval_and_run(library):
    from dataclasses import asdict
    from carr_library.conversion.batches import DocumentBatches
    from carr_library.conversion.planning import SplitOptions
    app, inputs, outputs, _ = library
    source = inputs / 'retained.txt'
    original = 'Перший рядок.\n\nSecond paragraph.'.encode('utf-8')
    source.write_bytes(original)
    settings = dict(options=asdict(SplitOptions(mode='blank-lines')), template='{source}-{part:03d}',
        combined=False, action='text', voice_id='', language='', format='wav')
    saved = TemplateStore(app.root).save('Split text batch', 'document-batch', settings)
    batch = DocumentBatches(app.root)
    identity = batch.create([source]); batch.prepare(identity)
    selected = saved.resolve({})
    preview = batch.preview(identity, outputs, SplitOptions(**selected['options']),
        **{key: value for key, value in selected.items() if key != 'options'})
    assert batch.get(identity)['review'] is None and not list(outputs.iterdir())
    batch.approve(preview)
    assert not list(outputs.iterdir())
    result = batch.run(identity)
    assert result['status'] == 'complete'
    assert b''.join((outputs / row['name']).read_bytes() for row in result['outputs']) == original
    assert source.read_bytes() == original
    assert batch.close() is not False


def test_project_item_overrides_preserve_typed_voice_and_format_choices(library):
    app, inputs, outputs, _ = library
    project = parse_script('Narrator: Explicit per-item text', voice_ids=('example:voice',))
    body = definition_from_project(project)
    body['project']['roles'][0]['voice_id'] = {'$parameter': 'voice'}
    body['format'] = {'$parameter': 'format'}
    parameters = {
        'voice': {'kind': 'voice', 'label': 'Current voice', 'example': 'example:voice'},
        'format': {'kind': 'text', 'label': 'Current format', 'example': 'wav'},
    }
    saved = TemplateStore(app.root).save('Per-item narration', 'project', body, parameters)
    plan = instantiate_plan(saved, Scope(input_roots=(inputs,), output_root=outputs), {},
        items=[{'voice': 'selected:first', 'format': 'wav'},
               {'voice': 'selected:second', 'format': 'flac'}])
    assert [step.arguments['format'] for step in plan.steps] == ['wav', 'flac']
    assert [step.arguments['script']['roles'][0]['voice_id'] for step in plan.steps] == [
        'selected:first', 'selected:second']
    with pytest.raises(ValueError):
        instantiate_plan(saved, plan.scope, {'voice': 'selected:first', 'format': 'custom-shell'})
    assert AssistantService(app).jobs() == ()
