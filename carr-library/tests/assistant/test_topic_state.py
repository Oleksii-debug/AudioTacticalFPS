"""Typed topic checkpoints, passive backup, named drafts and existing CLI entry."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.models import canonical
from carr_library.assistant.state import atomic_json
from carr_library.assistant.templates import combine_instructions
from carr_library.assistant.validation import validate_job
from carr_library import library_backup
from carr_library.research import export_file


@pytest.fixture
def environment(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Typed source scope')
    other = app.create_workspace('Different workspace')
    outputs = tmp_path / 'exports'
    outputs.mkdir()
    service = AssistantService(app)
    plan = Plan(Scope(workspace, (), outputs), (
        Step('topic.create', {'name': 'Parent'}),
        Step('topic.export', {'topic': '@0', 'name': 'saved.md'}),
        Step('topic.list'),
    ))
    return app, workspace, other, outputs, service, plan


@pytest.mark.parametrize('damage', [
    'created-id', 'created-name', 'created-parent', 'created-workspace',
    'export-topic', 'export-path', 'export-artifact', 'export-size-boolean',
    'intent-topic', 'intent-stage-path', 'intent-file-identity', 'intent-extra-field',
    'listed-workspace', 'listed-duplicate', 'listed-archived',
])
def test_forged_topic_receipts_fail_pure_validation_without_opening_saved_paths(
        environment, monkeypatch, damage):
    app, workspace, other, outputs, service, plan = environment
    identity = service.submit(plan)
    assert service.run(identity).status == 'complete'
    value = deepcopy(service.store.load(identity))
    created, exported, listed = [row['result'] for row in value['steps']]
    prepared = value['steps'][1]['intent']['topic_publication']
    if damage == 'created-id':
        created['topic_id'] = '0' * 32
    elif damage == 'created-name':
        created['name'] = 'Unapproved name'
    elif damage == 'created-parent':
        created['parent'] = 'unapproved-parent'
    elif damage == 'created-workspace':
        created['workspace'] = other
    elif damage == 'export-topic':
        exported['topic_id'] = 'another-topic'
    elif damage == 'export-path':
        exported['output'] = str(outputs / 'unapproved.md')
    elif damage == 'export-artifact':
        exported['artifacts'][0]['sha256'] = '0' * 64
    elif damage == 'export-size-boolean':
        exported['size'] = True
    elif damage == 'intent-topic':
        prepared['topic'] = 'another-topic'
    elif damage == 'intent-stage-path':
        prepared['staging']['name'] = '../outside'
    elif damage == 'intent-file-identity':
        prepared['staging']['identity'][1] = True
    elif damage == 'intent-extra-field':
        prepared['resume_approved'] = True
    elif damage == 'listed-workspace':
        listed['workspace'] = other
    elif damage == 'listed-duplicate':
        listed['topics'].append(deepcopy(listed['topics'][0]))
    else:
        listed['topics'][0]['archived'] = True
    def forbidden(*args, **kwargs):
        pytest.fail('Pure backup/checkpoint validation cannot open any saved path.')
    monkeypatch.setattr(Path, 'open', forbidden)
    with pytest.raises(ValueError):
        validate_job(value)


def test_rehashed_plan_cannot_retarget_a_topic_reference_to_another_tool(environment):
    _, _, _, _, service, plan = environment
    identity = service.submit(plan)
    value = service.store.load(identity)
    value['plan']['steps'][0] = {'tool': 'topic.list', 'arguments': {}}
    value['plan_sha256'] = sha256(canonical(value['plan']).encode('utf-8')).hexdigest()
    with pytest.raises(ValueError, match='preceding topic.create'):
        validate_job(value)


def test_completed_topic_membership_is_checked_against_the_backup_database(environment, tmp_path):
    app, workspace, other, _, service, plan = environment
    identity = service.submit(plan)
    report = service.run(identity)
    assert report.status == 'complete'
    created = report.results[0]['topic_id']
    with app.write(workspace) as db:
        db.execute('DELETE FROM research_topic WHERE workspace=? AND id=?', (workspace, created))
        db.execute('INSERT INTO research_topic(workspace,id,parent,name,position,archived) VALUES(?,?,NULL,?,0,0)',
                   (other, created, 'Different workspace'))
    destination = tmp_path / 'refused-backup'
    with pytest.raises((ValueError, library_backup.RestoreError), match='topic|workspace'):
        library_backup.create_backup(app, destination)
    assert not (destination / library_backup.MANIFEST).exists()


def test_complete_backup_recovery_preserves_topic_jobs_templates_and_external_path_inertia(
        environment, tmp_path, monkeypatch):
    app, workspace, _, outputs, service, plan = environment
    template = service.workflow_templates().save('Reusable topic evidence export', plan)
    identity = service.submit(plan)
    assert service.run(identity).status == 'complete'
    original = service.store.path(identity).read_bytes()
    first = tmp_path / 'backup'
    checksum = library_backup.create_backup(app, first)
    library_backup.verify_backup(first, checksum)
    restored_root = library_backup.restore_backup(first, tmp_path / 'restored', checksum)
    restored = LibraryApp(restored_root)
    recovered = AssistantService(restored)
    assert recovered.store.path(identity).read_bytes() == original
    assert recovered.workflow_templates().get(template.id) == template
    assert recovered.get(identity).status == 'attention'
    before_output = (outputs / 'saved.md').read_bytes(), (outputs / 'saved.md').stat().st_mtime_ns
    with pytest.raises(PermissionError, match='review and submit'):
        recovered.resume(identity)
    assert ((outputs / 'saved.md').read_bytes(), (outputs / 'saved.md').stat().st_mtime_ns) == before_output
    second = tmp_path / 'second-backup'
    second_checksum = library_backup.create_backup(restored, second)
    library_backup.verify_backup(second, second_checksum)
    twice = library_backup.restore_backup(second, tmp_path / 'twice-restored', second_checksum)
    assert (twice / 'state' / 'assistant' / (identity + '.json')).read_bytes() == original
    fresh_output = tmp_path / 'fresh-output'
    fresh_output.mkdir()
    fresh = recovered.preview(template.instruction(), Scope(workspace, (), fresh_output))
    assert fresh.scope.output_root == fresh_output and len(recovered.jobs()) == 1
    assert not list(fresh_output.iterdir())
    report = recovered.run(recovered.submit(fresh))
    assert report.status == 'complete'
    assert (fresh_output / 'saved.md').exists() and len(restored.topics(workspace)) == 2
    assert len(app.topics(workspace)) == 1


def test_combined_templates_rebase_each_local_topic_reference_without_mutating_saved_drafts(environment):
    app, workspace, _, outputs, service, _ = environment
    first_plan = Plan(Scope(workspace), (
        Step('topic.create', {'name': 'First parent'}),
        Step('topic.create', {'name': 'First child', 'parent': '@0'}),
    ))
    second_plan = Plan(Scope(workspace), (
        Step('topic.create', {'name': 'Second parent'}),
        Step('topic.create', {'name': 'Second child', 'parent': '@0'}),
    ))
    store = service.workflow_templates()
    first = store.save('First template', first_plan)
    second = store.save('Second template', second_plan)
    first_before, second_before = first.to_dict(), second.to_dict()
    instruction = combine_instructions([first, second])
    draft = json.loads(instruction)
    assert draft['steps'][1]['arguments']['parent'] == '@0'
    assert draft['steps'][3]['arguments']['parent'] == '@2'
    assert first.to_dict() == first_before and second.to_dict() == second_before
    assert 'scope' not in draft and service.jobs() == ()
    plan = service.preview(instruction, Scope(workspace))
    report = service.run(service.submit(plan))
    assert report.status == 'complete'
    assert report.results[1]['parent'] == report.results[0]['topic_id']
    assert report.results[3]['parent'] == report.results[2]['topic_id']
    assert report.results[1]['parent'] != report.results[3]['parent']


@pytest.mark.parametrize('mode', ['guided', 'json'])
def test_existing_cli_previews_then_explicitly_runs_topic_steps(environment, tmp_path, capsys, mode):
    from carr_library.assistant.cli import main
    app, workspace, _, outputs, service, plan = environment
    instruction = tmp_path / 'reviewed.txt'
    instruction.write_text(('Create topic "Parent"\nExport topic "@0" as "saved.md"\nList topics'
                           if mode == 'guided' else json.dumps({
                               'schema': plan.schema, 'steps': [step.to_dict() for step in plan.steps],
                               'summary': 'Typed exact topic workflow'})), encoding='utf-8')
    args = ['--data-dir', str(app.root), '--workspace', workspace,
            '--output-root', str(outputs), '--instructions', str(instruction)]
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['steps'][0]['tool'] == 'topic.create'
    assert service.jobs() == () and app.topics(workspace) == ()
    assert main([*args, '--run']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'complete' and report['completed'] == 3
    assert (outputs / 'saved.md').exists() and len(app.topics(workspace)) == 1


@pytest.mark.parametrize('existing', [False, True])
def test_shared_export_prepared_failure_preserves_destination_and_cleans_owned_stage(tmp_path, existing):
    target = tmp_path / 'export.md'
    private = tmp_path / 'library'
    private.mkdir()
    if existing:
        target.write_bytes(b'Approved previous bytes')
    approval = export_file.capture_export_destination(target, private_root=private)
    observed = []
    def prepared(stage, destination):
        assert stage.read_bytes() == b'Exact\r\npayload'
        assert destination == approval
        observed.append(stage)
        raise OSError('Synthetic durable-checkpoint refusal')
    with pytest.raises(OSError, match='checkpoint refusal'):
        export_file.write_export(target, 'Exact\r\npayload', private_root=private,
                                 approval=approval, prepared=prepared)
    assert len(observed) == 1 and not observed[0].exists()
    assert target.read_bytes() == b'Approved previous bytes' if existing else not target.exists()
    assert not list(tmp_path.glob('.research-export-*'))


def test_prepared_hook_observes_fsynced_stage_before_atomic_publisher_runs(tmp_path, monkeypatch):
    import os
    target = tmp_path / 'new.md'
    private = tmp_path / 'library'
    private.mkdir()
    order = []
    fsync = export_file.os.fsync
    def synced(fd):
        result = fsync(fd)
        order.append('fsync')
        return result
    monkeypatch.setattr(export_file.os, 'fsync', synced)
    def prepared(stage, destination):
        assert order == ['fsync'] and not target.exists()
        assert stage.read_bytes() == b'Exact evidence'
        assert destination.require_absent
        order.append('prepared')
    operation = 'rename' if os.name == 'nt' else 'link'
    publish = getattr(export_file.os, operation)
    def published(source, destination, *args, **kwargs):
        assert order == ['fsync', 'prepared']
        order.append('publication')
        return publish(source, destination, *args, **kwargs)
    monkeypatch.setattr(export_file.os, operation, published)
    receipt = export_file.write_export(target, 'Exact evidence', private_root=private,
                                      require_absent=True, prepared=prepared)
    assert order == ['fsync', 'prepared', 'publication']
    assert receipt.path == target and target.read_bytes() == b'Exact evidence'
