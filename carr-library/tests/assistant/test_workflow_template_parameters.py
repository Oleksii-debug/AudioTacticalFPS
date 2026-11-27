"""Typed recipe expansion uses the current Plan and one passive catalogue."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import templates
from carr_library.assistant.templates import (WorkflowTemplate, TemplateStore,
    instantiate_plan, review_request)
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.project import Project, Role, Line, Scene, Cue


def definition():
    return [{'tool': 'library.search', 'arguments': {'query': {'$parameter': 'query'}, 'limit': {'$parameter': 'limit'}}}]


def parameters():
    return {'query': {'kind': 'text', 'label': 'Search text', 'example': 'railway'},
            'limit': {'kind': 'integer', 'label': 'Results per item', 'example': 3}}


def recipe():
    return WorkflowTemplate.define('Reusable exact query', definition(), parameters(), kind='batch-preset')


def project(path=None):
    return Project('old-project', 'Reviewed source project', (Role('narrator', 'Reader', 'chosen-voice'),),
        (Scene('scene', 'One', (Line('line', 'narrator', 'Original selected words', source_ref='old-edition:page-7'),),
               cues=() if path is None else (Cue('cue', str(path)),)),),
        original_script='Original selected words', revision=7)


def test_whole_value_parameters_and_ordered_per_item_overrides_create_only_a_new_plan(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recipe review')
    service = AssistantService(app)
    record = recipe()
    requested = review_request(record, {'query': 'default', 'limit': 2},
        items=[{'query': 'First exact query'}, {'query': 'Second exact query', 'limit': 5}])
    plan = service.preview(json.dumps(requested), Scope(workspace))
    assert [(step.arguments['query'], step.arguments['limit']) for step in plan.steps] == [
        ('First exact query', 2), ('Second exact query', 5)]
    assert not service.jobs()
    assert not plan.scope.input_roots and plan.scope.output_root is None
    assert 'scope' not in requested and 'scope' not in requested['template']
    assert record.parameters == parameters() and record.steps[0].arguments == definition()[0]['arguments']


@pytest.mark.parametrize('values,items', [
    ({'query': 'missing'}, None),
    ({'query': 'q', 'limit': True}, None),
    ({'query': 'q', 'limit': 1, 'approved': True}, None),
    ({'query': 'q', 'limit': 1}, []),
    ({'query': 'q', 'limit': 1}, [{}] * 129),
    ({'query': 'q', 'limit': 1}, [{'scope': {'allow_full_backup': True}}]),
])
def test_missing_wrong_extra_or_oversized_bindings_do_not_create_a_plan(values, items):
    with pytest.raises(ValueError):
        instantiate_plan(recipe(), Scope('workspace'), values, items=items)


def test_per_item_expansion_checks_live_step_budget_before_creating_steps(monkeypatch):
    record = recipe()
    def forbidden(*args, **kwargs):
        pytest.fail('Rejected expansion must not construct any Step.')
    monkeypatch.setattr(templates, 'Step', forbidden)
    with pytest.raises(ValueError, match='step budget'):
        instantiate_plan(record, Scope('workspace', max_steps=1),
            {'query': 'q', 'limit': 1}, items=[{}, {}])


def test_oversized_saved_step_array_is_rejected_before_step_construction(monkeypatch):
    raw = recipe().to_dict()
    raw['steps'] *= 2
    monkeypatch.setattr(templates, 'MAX_STEPS', 1)
    monkeypatch.setattr(templates, 'Step',
        lambda *args, **kwargs: pytest.fail('Passive oversized recipe allocated a Step.'))
    with pytest.raises(ValueError, match='bounded'):
        WorkflowTemplate.from_dict(raw)


@pytest.mark.parametrize('tool', ['audio.produce', 'audio.render'])
def test_project_capture_and_each_reuse_mint_fresh_ids_and_remove_all_old_source_refs(tmp_path, tool):
    selected = project()
    captured = WorkflowTemplate.from_project('Fresh performance', selected, tool=tool)
    assert selected.id == 'old-project' and selected.revision == 7
    assert selected.scenes[0].lines[0].source_ref == 'old-edition:page-7'
    assert captured.steps[0].arguments['script']['id'] == 'template'
    assert captured.steps[0].arguments['script']['scenes'][0]['lines'][0]['source_ref'] == ''
    scope = Scope(output_root=tmp_path)
    first = instantiate_plan(captured, scope)
    second = instantiate_plan(captured, scope)
    identities = [plan.steps[0].arguments['script']['id'] for plan in (first, second)]
    assert len(set(identities + ['old-project', 'template'])) == 4
    for plan in (first, second):
        script = plan.steps[0].arguments['script']
        assert script['revision'] == 1
        assert script['scenes'][0]['lines'][0]['source_ref'] == ''
        assert script['roles'][0]['voice_id'] == 'chosen-voice'
        assert script['original_script'] == selected.original_script


@pytest.mark.parametrize('tool', ['audio.produce', 'audio.render'])
def test_legacy_saved_audio_recipe_is_passive_but_does_not_replay_project_lineage(tmp_path, tool):
    raw = {'id': 'a' * 32, 'name': 'Legacy recipe', 'kind': 'workflow', 'summary': '',
           'steps': [{'tool': tool, 'arguments': {'script': project().to_dict(), 'format': 'wav'}}]}
    saved = WorkflowTemplate.from_dict(raw)
    assert encode(saved.to_dict()) == encode(raw)  # No silent rewrite of saved JSON during backup/read.
    proposed = instantiate_plan(saved, Scope(output_root=tmp_path))
    script = proposed.steps[0].arguments['script']
    assert script['id'] != 'old-project' and script['revision'] == 1
    assert script['scenes'][0]['lines'][0]['source_ref'] == ''


def test_new_project_clip_paths_require_current_input_selection(tmp_path):
    cue = tmp_path / 'selected.wav'; cue.write_bytes(b'not opened by passive template')
    record = WorkflowTemplate.from_project('Cue recipe', project(cue))
    before = cue.read_bytes()
    with pytest.raises(PermissionError, match='outside'):
        instantiate_plan(record, Scope(output_root=tmp_path))
    plan = instantiate_plan(record, Scope(input_roots=(cue,), output_root=tmp_path))
    assert plan.steps[0].arguments['script']['scenes'][0]['cues'][0]['path'] == str(cue)
    assert cue.read_bytes() == before
    cue.unlink()
    with pytest.raises((ValueError, OSError)):
        instantiate_plan(record, Scope(output_root=tmp_path))


def test_tool_names_and_expressions_cannot_be_parameterized():
    with pytest.raises((ValueError, TypeError)):
        WorkflowTemplate.define('Untrusted dispatch', [
            {'tool': {'$parameter': 'query'}, 'arguments': {}}], parameters())
    with pytest.raises(ValueError):
        WorkflowTemplate.define('Unknown field', [
            {'tool': 'library.list', 'arguments': {'scope': {'allow_full_backup': True}}}])
    with pytest.raises(ValueError):
        WorkflowTemplate.define('Unused parameter', [{'tool': 'library.list', 'arguments': {}}], parameters())
    literal = WorkflowTemplate.define('Literal data', [
        {'tool': 'library.search', 'arguments': {'query': '$(arbitrary code) {query}'}}])
    assert literal.proposal()['steps'][0]['arguments']['query'] == '$(arbitrary code) {query}'


def test_big_numeric_binding_is_a_bounded_validation_error():
    record = WorkflowTemplate.define('Number', [
        {'tool': 'library.search', 'arguments': {'query': 'q', 'limit': {'$parameter': 'n'}}}],
        {'n': {'kind': 'number', 'label': 'Bounded number', 'example': 1}})
    with pytest.raises(ValueError):
        record.proposal({'n': 10**400})


def test_parameter_types_are_checked_against_actual_argument_policy():
    with pytest.raises(ValueError):
        WorkflowTemplate.define('Wrong argument example', definition(),
            {**parameters(), 'limit': {'kind': 'text', 'label': 'Wrong type', 'example': 'three'}})
    record = recipe()
    with pytest.raises(ValueError):
        review_request(record, {'query': 'q', 'limit': 101})


def test_restored_typed_catalogue_is_passive_and_nfc_cas_rules_are_preserved(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Typed passive templates')
    store = TemplateStore(app.root)
    record = recipe()
    token = store.save(record, expected_token=None)
    before = store.path.read_bytes()
    from carr_library.assistant import models
    monkeypatch.setattr(models, 'local_path',
        lambda *a, **k: pytest.fail('Passive template restore tried to authorize a path.'))
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    assert (restored / templates.NAME).read_bytes() == before
    assert TemplateStore(restored).get(record.id)[0] == record
    assert not (restored / 'state/workflows').exists()
    with pytest.raises(ValueError, match='changed'):
        store.save(replace(record, name='Other'), expected_token='0' * 64)
    assert store.path.read_bytes() == before
