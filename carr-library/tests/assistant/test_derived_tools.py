"""Real derivative publication, source preservation and durable assistant recovery."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import derived_tools
from carr_library.assistant.state import atomic_json
from carr_library.assistant.validation import validate_job
from carr_library.audio.production import parse_script


RAW = ('[effect Narrator highpass frequency=80]\r\n'
       '[effect Narrator gain gain_db=-3]\r\n'
       '# Exact 😀 chapter\r\nNarrator: Ірина — точні слова.  \r\n')
GAIN = {'kind': 'gain', 'parameters': [['gain_db', -6]], 'enabled': True}


@pytest.fixture
def env(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Retained library')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / 'сценарій.txt'
    source.write_bytes(RAW.encode('utf-8'))
    return app, source, outputs, Scope(input_roots=(inputs,), output_root=outputs)


def command(source, *, tool='audio.effects', target='Narrator', changes=None, name='edited.txt'):
    args = {'path': str(source), 'source_sha256': sha256(source.read_bytes()).hexdigest(), 'name': name}
    if tool == 'audio.effects':
        args.update(target=target, changes=changes or [{'op': 'add', 'effect': deepcopy(GAIN)}])
    return Step(tool, args)


def submitted(env, **options):
    app, source, output, scope = env
    service = AssistantService(app)
    step = command(source, **options)
    identity = service.submit(Plan(scope, (step,)))
    return service, identity, step


def completed(env, **options):
    service, identity, step = submitted(env, **options)
    report = service.run(identity)
    assert report.status == 'complete', report
    return service, identity, step, report.results[0]


@pytest.mark.parametrize('language', ['en', 'uk'])
def test_guided_conversion_runs_actual_isolated_reader_and_preserves_unicode(env, language):
    app, source, output, scope = env
    original = 'Перша сторінка 😀.\r\nТочні слова.\fДруга сторінка.'
    source.write_bytes(original.encode('utf-8'))
    service = AssistantService(app)
    goal = ('Convert "' + str(source) + '" as "reading.txt"' if language == 'en'
            else 'Конвертуй "' + str(source) + '" як "reading.txt"')
    plan = service.preview(goal, scope)
    assert plan.steps[0].arguments['source_sha256'] == sha256(source.read_bytes()).hexdigest()
    result = service.run(service.submit(plan))
    assert result.status == 'complete', result
    assert (output / 'reading.txt').read_bytes() == original.encode('utf-8')
    evidence = json.loads((output / 'reading.txt.provenance.json').read_text(encoding='utf-8'))
    assert evidence['source_sha256'] == plan.steps[0].arguments['source_sha256']
    assert evidence['verified_for_quotation'] is False and evidence['original_modified'] is False
    assert len(evidence['segments']) == 2
    assert source.read_bytes() == original.encode('utf-8')
    assert len(result.results[0]['artifacts']) == 2


def test_guided_effects_export_preserves_original_comments_and_spoken_scenes(env):
    app, source, output, scope = env
    changes = [{'op': 'add', 'effect': GAIN}]
    goal = 'Ефекти "' + str(source) + '" як "new.txt" для "Narrator": ' + json.dumps(changes)
    service = AssistantService(app)
    plan = service.preview(goal, scope)
    result = service.run(service.submit(plan))
    assert result.status == 'complete', result
    changed = (output / 'new.txt').read_bytes().decode('utf-8')
    assert '# Exact 😀 chapter\r\n' in changed
    assert parse_script(changed).scenes == parse_script(RAW).scenes
    assert source.read_bytes() == RAW.encode('utf-8')
    report = json.loads((output / 'new.txt.provenance.json').read_text(encoding='utf-8'))
    assert report['spoken_text_modified'] is report['rendered_audio'] is False
    assert report['changes'] == changes


@pytest.mark.parametrize('operation', ['add', 'edit', 'move', 'bypass', 'remove', 'set-chain', 'master-mix'])
def test_effect_actions_use_existing_typed_role_and_master_editor(env, operation):
    changes = {
        'add': {'op': 'add', 'effect': GAIN},
        'edit': {'op': 'edit', 'index': 0, 'effect': GAIN},
        'move': {'op': 'move', 'index': 0, 'destination': 1},
        'bypass': {'op': 'bypass', 'index': 0},
        'remove': {'op': 'remove', 'index': 0},
        'set-chain': {'op': 'set-chain', 'effects': [GAIN]},
        'master-mix': {'op': 'master-mix', 'value': .4},
    }
    _, _, _, result = completed(env, changes=[changes[operation]],
                                target=None if operation == 'master-mix' else 'Narrator')
    after = parse_script(Path(result['artifacts'][0]['path']).read_bytes().decode('utf-8'))
    before = parse_script(RAW)
    assert after.scenes == before.scenes
    effects = next(role.effects for role in after.roles if role.name == 'Narrator')
    if operation == 'add':
        assert len(effects) == 3 and effects[-1].parameters == (('gain_db', -6),)
    elif operation == 'edit':
        assert effects[0].kind == 'gain'
    elif operation == 'move':
        assert [effect.kind for effect in effects] == ['gain', 'highpass']
    elif operation == 'bypass':
        assert effects[0].enabled is False
    elif operation == 'remove':
        assert [effect.kind for effect in effects] == ['gain']
    elif operation == 'set-chain':
        assert len(effects) == 1 and effects[0].kind == 'gain'
    else:
        assert after.master_mix == .4
    assert env[1].read_bytes() == RAW.encode('utf-8')


@pytest.mark.parametrize('tool', ['document.convert', 'audio.effects'])
def test_source_changed_after_approval_never_runs_new_bytes_under_old_plan(env, tool):
    service, identity, _ = submitted(env, tool=tool)
    env[1].write_text('Narrator: A different source.', encoding='utf-8')
    result = service.run(identity)
    assert result.status == 'attention' and result.completed == 0
    assert 'changed after review' in result.message
    assert not list(env[2].iterdir())


@pytest.mark.parametrize('tool', ['document.convert', 'audio.effects'])
def test_selected_source_cannot_escape_explicit_input_scope(env, tmp_path, tool):
    app, source, output, scope = env
    foreign = tmp_path / 'foreign.txt'
    foreign.write_bytes(source.read_bytes())
    service = AssistantService(app)
    with pytest.raises((PermissionError, ValueError), match='scope|selected|outside'):
        service.submit(Plan(scope, (command(foreign, tool=tool),)))
    assert not list(output.iterdir())


@pytest.mark.parametrize('name', ['../outside.txt', 'nested/out.txt', 'C:/outside.txt',
                                  r'C:\outside.txt', '.hidden.txt', 'NUL.txt',
                                  'output.wav', 'output.txt '])
def test_derivative_names_must_be_portable_new_text_children(env, name):
    with pytest.raises(ValueError):
        derived_tools.validate_arguments(command(env[1], name=name))


@pytest.mark.parametrize('tool', ['document.convert', 'audio.effects'])
def test_no_extra_authority_fields_are_allowed(env, tool):
    step = command(env[1], tool=tool)
    step.arguments['shell'] = 'data from a document'
    with pytest.raises(ValueError):
        derived_tools.validate_arguments(step)


@pytest.mark.parametrize('damage', ['operation', 'schema', 'tool', 'source', 'name', 'size', 'budget'])
def test_persisted_publication_manifest_is_exact_and_bounded(env, damage):
    service, identity, step, _ = completed(env)
    record = service.store.load(identity)
    intent = deepcopy(record['steps'][0]['intent'])
    value, scope = intent['derived_publication'], record['plan']['scope']
    if damage == 'operation':
        value['operation_id'] = 'invalid'
    elif damage == 'schema':
        value['schema'] = 'future'
    elif damage == 'tool':
        value['tool'] = 'document.convert'
    elif damage == 'source':
        value['source_sha256'] = '0' * 64
    elif damage == 'name':
        value['files'][0]['name'] = '../escape.txt'
    elif damage == 'size':
        value['files'][0]['size'] = True
    else:
        scope['max_bytes'] = 1
    with pytest.raises(ValueError):
        derived_tools.validate_intent(step, intent, scope)


@pytest.mark.parametrize('damage', ['path', 'digest', 'size', 'extra-file', 'tool', 'source', 'boolean', 'claim'])
def test_completion_receipt_cannot_invent_files_or_claims(env, damage):
    service, identity, step, result = completed(env)
    record = service.store.load(identity)
    result = deepcopy(result)
    if damage == 'path':
        result['artifacts'][0]['path'] = str(env[1])
    elif damage == 'digest':
        result['artifacts'][0]['sha256'] = '0' * 64
    elif damage == 'size':
        result['artifacts'][0]['size'] += 1
    elif damage == 'extra-file':
        result['artifacts'].append(deepcopy(result['artifacts'][0]))
    elif damage == 'tool':
        result['derived_receipt']['tool'] = 'audio.produce'
    elif damage == 'source':
        result['derived_receipt']['source_sha256'] = '0' * 64
    elif damage == 'boolean':
        result['derived_receipt']['original_modified'] = 0
    else:
        result['derived_receipt']['whole_book_narrated'] = True
    with pytest.raises(ValueError):
        derived_tools.validate_result(step, result, record['plan']['scope'], record['steps'][0]['intent'])


@pytest.mark.parametrize('budget', ['files', 'bytes'])
def test_approved_resource_limits_are_checked_before_preparation(env, budget):
    app, source, output, scope = env
    scope = replace(scope, **({'max_files': 1} if budget == 'files' else {'max_bytes': 8}))
    with pytest.raises(ValueError):
        AssistantService(app).submit(Plan(scope, (command(source),)))
    assert not list(output.iterdir())


def test_preparation_cancel_removes_only_owned_unpublished_files_then_resumes(env):
    service, identity, _ = submitted(env)
    stop = Event()
    def progress(index, total, message):
        if message.startswith('Preparing the reviewed'):
            stop.set()
    result = service.run(identity, cancelled=stop.is_set, progress=progress)
    assert result.status == 'paused' and result.completed == 0
    assert not list(env[2].iterdir())
    stop.clear()
    assert service.resume(identity, cancelled=stop.is_set).status == 'complete'


def pause_after_text(env, monkeypatch):
    from carr_library.audio import jobs
    service, identity, step = submitted(env)
    stop, real = Event(), jobs.publish_noreplace
    def publish(source, target, **options):
        result = real(source, target, **options)
        if Path(target).name == 'edited.txt':
            stop.set()
        return result
    monkeypatch.setattr(jobs, 'publish_noreplace', publish)
    report = service.run(identity, cancelled=stop.is_set)
    assert report.status == 'paused' and report.completed == 0
    assert (env[2] / 'edited.txt').exists()
    assert not (env[2] / 'edited.txt.provenance.json').exists()
    monkeypatch.setattr(jobs, 'publish_noreplace', real)
    return service, identity, step


def test_partial_publication_resumes_exact_prepared_bytes_without_repeating_edit(env, monkeypatch):
    service, identity, _ = pause_after_text(env, monkeypatch)
    previous = (env[2] / 'edited.txt').read_bytes()
    env[1].write_text('Narrator: User changed the original later.', encoding='utf-8')
    def forbidden(*args, **kwargs):
        pytest.fail('A persisted publication must not repeat conversion or effect editing.')
    monkeypatch.setattr(derived_tools, '_edit_script', forbidden)
    report = AssistantService(env[0]).resume(identity)
    assert report.status == 'complete', report
    assert (env[2] / 'edited.txt').read_bytes() == previous
    assert env[1].read_text(encoding='utf-8') == 'Narrator: User changed the original later.'
    assert len(report.results[0]['artifacts']) == 2
    assert not list(env[2].glob('.assistant-derived-*'))


def test_partial_publication_never_overwrites_a_conflicting_result(env, monkeypatch):
    service, identity, _ = pause_after_text(env, monkeypatch)
    report_path = env[2] / 'edited.txt.provenance.json'
    report_path.write_bytes(b'User-owned conflicting bytes.')
    result = service.resume(identity)
    assert result.status == 'attention' and result.completed == 0
    assert report_path.read_bytes() == b'User-owned conflicting bytes.'


def test_failed_intent_write_cannot_publish_or_adopt_uncommitted_staging(env, monkeypatch):
    service, identity, _ = submitted(env)
    save = service.store.save
    def fail_intent(record):
        if record['steps'][0]['intent']:
            raise OSError('Synthetic checkpoint storage failure')
        return save(record)
    monkeypatch.setattr(service.store, 'save', fail_intent)
    with pytest.raises(OSError, match='checkpoint storage'):
        service.run(identity)
    assert not (env[2] / 'edited.txt').exists()
    assert not service.store.load(identity)['steps'][0]['intent']
    result = AssistantService(env[0]).resume(identity)
    assert result.status == 'attention' and result.completed == 0
    assert not (env[2] / 'edited.txt').exists()


def test_completed_pair_reconciles_after_post_move_durability_error(env, monkeypatch):
    from carr_library.audio import jobs
    service, identity, _ = submitted(env)
    real = jobs.publish_noreplace
    def publish(source, target, **options):
        result = real(source, target, **options)
        if Path(target).name == 'edited.txt.provenance.json':
            raise OSError('Synthetic directory synchronization failure after atomic move')
        return result
    monkeypatch.setattr(jobs, 'publish_noreplace', publish)
    result = service.run(identity)
    assert result.status == 'failed'
    assert (env[2] / 'edited.txt.provenance.json').is_file()
    monkeypatch.setattr(jobs, 'publish_noreplace', real)
    assert AssistantService(env[0]).resume(identity).status == 'complete'


def test_stop_after_last_atomic_move_preserves_complete_pair(env, monkeypatch):
    from carr_library.audio import jobs
    service, identity, _ = submitted(env)
    real, stop = jobs.publish_noreplace, Event()
    def publish(source, target, **options):
        result = real(source, target, **options)
        if Path(target).name == 'edited.txt.provenance.json':
            stop.set()
        return result
    monkeypatch.setattr(jobs, 'publish_noreplace', publish)
    result = service.run(identity, cancelled=stop.is_set)
    assert stop.is_set() and result.status == 'complete' and result.completed == 1
    assert len(result.results[0]['artifacts']) == 2


@pytest.mark.parametrize('name', ['edited.txt', 'edited.txt.provenance.json'])
def test_completed_artifact_changes_are_detected_without_republication(env, name):
    service, identity, _, _ = completed(env)
    target = env[2] / name
    target.write_bytes(b'Changed after completion.')
    result = service.resume(identity)
    assert result.status == 'failed'
    assert target.read_bytes() == b'Changed after completion.'


def test_source_with_hardlink_alias_is_rejected_before_publication(env):
    alias = env[1].with_name('alias.txt')
    os.link(env[1], alias)
    with pytest.raises(ValueError, match='hard-link'):
        submitted(env)
    assert not list(env[2].iterdir())


def test_saved_intent_cannot_be_transferred_to_another_step_identity(env):
    service, identity, _, _ = completed(env)
    record = service.store.load(identity)
    record['steps'][0]['intent']['derived_publication']['operation_id'] = '0' * 32
    with pytest.raises(ValueError, match='different approved step'):
        validate_job(record)
    atomic_json(service.store.path(identity), record)
    with pytest.raises(ValueError):
        service.resume(identity)


def test_backup_restore_preserves_derived_receipts_but_never_grants_resume(env, tmp_path):
    from carr_library.library_backup import create_backup, restore_backup
    service, identity, _, _ = completed(env)
    backup = tmp_path / 'backup'
    checksum = create_backup(env[0], backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    saved = AssistantService(restored)
    assert saved.get(identity).status == 'attention'
    with pytest.raises(PermissionError, match='review'):
        saved.resume(identity)
    assert (env[2] / 'edited.txt').is_file()


@pytest.mark.parametrize('target', ['text', 'report'])
def test_preexisting_destination_is_never_adopted_even_if_source_is_unchanged(env, target):
    suffix = '' if target == 'text' else '.provenance.json'
    occupied = env[2] / ('edited.txt' + suffix)
    occupied.write_bytes(b'Existing user output.')
    with pytest.raises(FileExistsError):
        submitted(env)
    assert occupied.read_bytes() == b'Existing user output.'


@pytest.mark.parametrize('suffix', ['.rtf', '.doc', '.png'])
def test_optional_converter_selection_is_required_at_preview_before_a_job_exists(env, suffix):
    app, source, output, scope = env
    selected = source.with_suffix(suffix)
    selected.write_bytes(source.read_bytes())
    service = AssistantService(app)
    with pytest.raises(ValueError, match='Document conversion'):
        service.submit(Plan(scope, (command(selected, tool='document.convert'),)))
    assert not service.jobs() and not list(output.iterdir())


@pytest.mark.parametrize('damage', ['role', 'empty', 'operation', 'parameter', 'index', 'master-tempo'])
def test_invalid_effect_edit_is_rejected_before_any_output_or_job(env, damage):
    app, source, output, scope = env
    step = command(source)
    if damage == 'role':
        step.arguments['target'] = 'A role absent from the script'
    elif damage == 'empty':
        step.arguments['changes'] = []
    elif damage == 'operation':
        step.arguments['changes'] = [{'op': 'execute', 'command': 'book instructions'}]
    elif damage == 'parameter':
        step.arguments['changes'][0]['effect']['parameters'] = [['command', -3]]
    elif damage == 'index':
        step.arguments['changes'] = [{'op': 'remove', 'index': True}]
    else:
        step.arguments['target'] = None
        step.arguments['changes'] = [{'op': 'add', 'effect': {
            'kind': 'tempo', 'parameters': [['factor', 1.1]], 'enabled': True}}]
    service = AssistantService(app)
    with pytest.raises(ValueError):
        service.submit(Plan(scope, (step,)))
    assert not service.jobs() and not list(output.iterdir())
