"""Real scoped topic operations through the current assistant and research store."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import topic_tools
from carr_library.assistant.models import TOOLS
from carr_library.assistant.validation import validate_job
from carr_library.research import export_file


@pytest.fixture
def environment(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected research')
    other = app.create_workspace('Other private research')
    app.add_text(workspace, 'Незмінне джерело', 'Exact railway quotation.\r\nДругий рядок.',
                 authors=('Source Author',))
    app.add_text(other, 'Private evidence', 'railway outside the selected workspace')
    topic = app.create_topic(workspace, 'Saved source evidence')
    foreign = app.create_topic(other, 'Private topic')
    hit, = app.search(workspace, 'railway')
    app.save_hit(workspace, topic, hit.ref, hit.revision, note='My separate note. Мій коментар.')
    outputs = tmp_path / 'exports'
    outputs.mkdir()
    service = AssistantService(app)
    return app, workspace, other, topic, foreign, outputs, service


def plan_for(environment, *steps, **limits):
    _, workspace, _, _, _, outputs, _ = environment
    return Plan(Scope(workspace, (), outputs, **limits), tuple(steps))


@pytest.mark.parametrize('language', ['english', 'ukrainian'])
def test_native_guided_topics_preview_is_readonly_and_nested_run_is_scoped(environment, language):
    app, workspace, other, topic, foreign, outputs, service = environment
    goal = ('Create topic "Parent"\nCreate topic "Child" under "@0"\nList topics'
            if language == 'english' else
            'Створи тему "Parent"\nСтвори тему "Child" у "@0"\nПокажи теми')
    before = app.topics(workspace)
    plan = service.preview(goal, Scope(workspace))
    assert app.topics(workspace) == before and service.jobs() == ()
    assert [step.tool for step in plan.steps] == ['topic.create', 'topic.create', 'topic.list']
    identity = service.submit(plan)
    assert app.topics(workspace) == before
    report = service.run(identity)
    assert report.status == 'complete', report
    parent, child, listed = report.results
    assert parent['workspace'] == child['workspace'] == listed['workspace'] == workspace
    assert parent['topic_id'] == topic_tools.topic_identity(identity, 0)
    assert child['parent'] == parent['topic_id']
    assert {row['id'] for row in listed['topics']} == {topic, parent['topic_id'], child['topic_id']}
    assert foreign not in {row['id'] for row in listed['topics']}
    assert len(app.topics(other)) == 1 and not list(outputs.iterdir())
    before = app.topics(workspace)
    assert AssistantService(LibraryApp(app.root)).resume(identity).status == 'complete'
    assert app.topics(workspace) == before


@pytest.mark.parametrize('reference', ['@0', '@1', '@01', '@-1', '@', '@999999', 'foreign', 'archived'])
def test_invalid_topic_references_refuse_the_whole_plan_before_earlier_creation(environment, reference):
    app, workspace, _, topic, foreign, _, service = environment
    if reference == 'foreign':
        reference = foreign
    if reference == 'archived':
        with app.topic_transaction(workspace) as tree:
            tree.archive(topic)
        reference = topic
    # A first create cannot refer to itself, a future result or another workspace.
    before = app.topics(workspace, include_archived=True)
    plan = plan_for(environment, Step('topic.create', {'name': 'Must not be created', 'parent': reference}))
    with pytest.raises((ValueError, PermissionError)):
        service.submit(plan)
    assert app.topics(workspace, include_archived=True) == before and service.jobs() == ()


def test_reference_to_an_earlier_noncreation_step_is_rejected(environment):
    app, workspace, _, _, _, _, service = environment
    before = app.topics(workspace)
    with pytest.raises(ValueError, match='preceding topic.create'):
        service.submit(plan_for(environment, Step('topic.list'),
            Step('topic.create', {'name': 'Invalid child', 'parent': '@0'})))
    assert app.topics(workspace) == before and service.jobs() == ()


@pytest.mark.parametrize('tool,args', [
    ('topic.list', {'workspace': 'outside'}),
    ('topic.list', {'include_archived': True}),
    ('topic.create', {'name': ''}),
    ('topic.create', {'name': 'x' * 513}),
    ('topic.create', {'name': 'Bad\x00name'}),
    ('topic.create', {'name': 'Valid', 'id': 'unreviewed'}),
    ('topic.create', {'name': 'Valid', 'parent': True}),
    ('topic.export', {'topic': '@0', 'name': '../escape.md'}),
    ('topic.export', {'topic': '@0', 'name': 'CON.md'}),
    ('topic.export', {'topic': '@0', 'name': 'folder//name.md'}),
    ('topic.export', {'topic': '@0', 'name': 'name.md '}),
    ('topic.export', {'topic': '@0', 'name': 'stream:secret'}),
    ('topic.export', {'topic': '@0', 'name': 'name.md', 'format': 'shell'}),
    ('topic.export', {'topic': '@0', 'name': 'name.md', 'format': True}),
    ('topic.export', {'topic': '@0', 'name': 'name.md', 'replace': True}),
])
def test_unreviewed_topic_arguments_cannot_run_an_earlier_valid_step(environment, tool, args):
    app, workspace, _, _, _, outputs, service = environment
    before = app.topics(workspace)
    plan = plan_for(environment, Step('topic.create', {'name': 'Earlier safe step'}), Step(tool, args))
    with pytest.raises((ValueError, PermissionError)):
        service.submit(plan)
    assert app.topics(workspace) == before and service.jobs() == () and not list(outputs.iterdir())


def test_current_library_creation_can_feed_nested_topics_without_an_alternate_store(environment):
    app, _, _, _, _, _, service = environment
    plan = service.preview('Create library "New scope"\nCreate topic "Parent"\n'
        'Create topic "Child" under "@1"\nList topics', Scope(allow_create=True))
    report = service.run(service.submit(plan))
    assert report.status == 'complete', report
    workspace = report.results[0]['workspace']
    rows = app.topics(workspace)
    assert len(rows) == 2
    assert report.results[2]['parent'] == report.results[1]['topic_id']
    assert all(row['id'] in {topic.id for topic in rows} for row in report.results[3]['topics'])


@pytest.mark.parametrize('format', topic_tools.FORMATS)
def test_topic_export_preserves_exact_verified_source_notes_and_renderer_format(environment, format):
    app, workspace, _, topic, _, outputs, service = environment
    before_catalog = app.browse(workspace)
    before_evidence = app.evidence(workspace, topic)
    expected = app.export_topic(workspace, topic, format=format).encode('utf-8')
    name = 'exact-evidence.' + format
    report = service.run(service.submit(plan_for(environment,
        Step('topic.export', {'topic': topic, 'name': name, 'format': format}))))
    assert report.status == 'complete', report
    result, = report.results
    assert (outputs / name).read_bytes() == expected
    assert result['sha256'] == sha256(expected).hexdigest() and result['size'] == len(expected)
    assert result['workspace'] == workspace and result['topic_id'] == topic
    assert result['artifacts'] == [{'path': str(outputs / name), 'size': len(expected),
                                    'sha256': sha256(expected).hexdigest()}]
    assert app.evidence(workspace, topic) == before_evidence
    assert app.browse(workspace) == before_catalog
    if format in ('markdown', 'text', 'csv'):
        assert b'railway' in expected and b'My separate note.' in expected


@pytest.mark.parametrize('tool', ['topic.list', 'topic.create', 'topic.export'])
def test_each_topic_tool_requires_current_library_scope(environment, tool):
    _, _, _, topic, _, outputs, service = environment
    args = {} if tool == 'topic.list' else {'name': 'Scoped'} if tool == 'topic.create' else {
        'topic': topic, 'name': 'evidence.md'}
    with pytest.raises(PermissionError, match='Choose a library'):
        service.submit(Plan(Scope(output_root=outputs), (Step(tool, args),)))


@pytest.mark.parametrize('kind', ['count', 'bytes'])
def test_topic_budgets_fail_visibly_before_publication(environment, kind):
    app, workspace, _, topic, _, outputs, service = environment
    if kind == 'count':
        app.create_topic(workspace, 'Second topic')
        plan = plan_for(environment, Step('topic.list'), max_files=1)
    else:
        plan = plan_for(environment, Step('topic.export', {'topic': topic, 'name': 'too-large.md'}), max_bytes=1)
    result = service.run(service.submit(plan))
    assert result.status == 'failed' and result.completed == 0
    assert not list(outputs.iterdir())


@pytest.mark.parametrize('change', ['rename', 'archive', 'move', 'archived-parent'])
def test_changed_completed_topic_is_refused_without_recreating_or_continuing(environment, change):
    app, workspace, _, existing, _, _, service = environment
    first = Step('topic.create', {'name': 'Original', 'parent': existing})
    identity = service.submit(plan_for(environment, first))
    assert service.run(identity).status == 'complete'
    created = topic_tools.topic_identity(identity, 0)
    with app.topic_transaction(workspace) as tree:
        if change == 'rename':
            tree.rename(created, 'User changed name')
        elif change == 'archive':
            tree.archive(created)
        elif change == 'move':
            tree.move(created, None)
        else:
            tree.archive(existing)
    before = app.topics(workspace, include_archived=True)
    report = service.resume(identity)
    assert report.status == 'failed' and report.completed == 0
    assert app.topics(workspace, include_archived=True) == before


def test_existing_new_file_conflict_is_refused_before_inspecting_its_bytes(environment, monkeypatch):
    _, _, _, topic, _, outputs, service = environment
    target = outputs / 'existing.md'
    target.write_bytes(b'Unapproved user data')
    def forbidden(*args, **kwargs):
        pytest.fail('Create-only refusal must not read an unapproved existing file.')
    monkeypatch.setattr(export_file, '_content_fingerprint', forbidden)
    with pytest.raises(FileExistsError):
        service.submit(plan_for(environment, Step('topic.export', {'topic': topic, 'name': target.name})))
    assert target.read_bytes() == b'Unapproved user data'


@pytest.mark.parametrize('foreign_content', ['different', 'same'])
def test_concurrent_output_after_review_is_never_claimed_or_overwritten(environment, foreign_content):
    app, workspace, _, topic, _, outputs, service = environment
    target = outputs / 'concurrent.md'
    identity = service.submit(plan_for(environment, Step('topic.export', {'topic': topic, 'name': target.name})))
    raw = app.export_topic(workspace, topic).encode('utf-8') if foreign_content == 'same' else b'Independent user work'
    target.write_bytes(raw)
    before = target.stat()
    report = service.run(identity)
    assert report.status == 'attention' and report.completed == 0
    assert target.read_bytes() == raw and target.stat().st_mtime_ns == before.st_mtime_ns


def test_concurrent_creation_after_staging_keeps_the_foreign_file_and_removes_only_own_stage(
        environment, monkeypatch):
    _, _, _, topic, _, outputs, service = environment
    target = outputs / 'race.md'
    identity = service.submit(plan_for(environment, Step('topic.export', {'topic': topic, 'name': target.name})))
    original = service.store.save
    def compete(record):
        original(record)
        prepared = record['steps'][0]['intent'].get('topic_publication')
        if prepared and prepared['staging'] and not target.exists():
            target.write_bytes(b'Concurrent independent destination')
    monkeypatch.setattr(service.store, 'save', compete)
    report = service.run(identity)
    assert report.status in ('failed', 'attention') and report.completed == 0
    assert target.read_bytes() == b'Concurrent independent destination'
    assert not list(outputs.glob('.research-export-*'))


@pytest.mark.parametrize('phase', ['before-stage', 'after-stage', 'after-publication'])
def test_stop_preserves_the_actual_topic_export_commit_boundary(environment, monkeypatch, phase):
    _, _, _, topic, _, outputs, service = environment
    stop = Event()
    target = outputs / 'cancel.md'
    identity = service.submit(plan_for(environment, Step('topic.export', {'topic': topic, 'name': target.name})))
    original = service.store.save
    def observe(record):
        original(record)
        prepared = record['steps'][0]['intent'].get('topic_publication')
        if (phase == 'before-stage' and prepared and not prepared['staging']
                or phase == 'after-stage' and prepared and prepared['staging']):
            stop.set()
    monkeypatch.setattr(service.store, 'save', observe)
    if phase == 'after-publication':
        operation = 'rename' if os.name == 'nt' else 'link'
        publish = getattr(export_file.os, operation)
        def committed(source, destination, *args, **kwargs):
            result = publish(source, destination, *args, **kwargs)
            if Path(destination) == target:
                stop.set()
            return result
        monkeypatch.setattr(export_file.os, operation, committed)
    report = service.run(identity, cancelled=stop.is_set)
    if phase == 'after-publication':
        assert report.status == 'complete' and report.completed == 1 and target.exists()
    else:
        assert report.status == 'paused' and report.completed == 0 and not target.exists()
    assert not list(outputs.glob('.research-export-*'))


def test_postpublication_directory_sync_failure_is_a_completed_receipt_not_a_retry(environment, monkeypatch):
    from carr_library import durable_state
    _, _, _, topic, _, outputs, service = environment
    target = outputs / 'committed.md'
    identity = service.submit(plan_for(environment, Step('topic.export', {'topic': topic, 'name': target.name})))
    original = durable_state.sync_directory
    def refuse(path):
        if Path(path) == outputs and target.exists():
            raise OSError('Synthetic post-commit directory synchronization failure')
        return original(path)
    monkeypatch.setattr(durable_state, 'sync_directory', refuse)
    result = service.run(identity)
    assert result.status == 'complete' and target.exists()
    assert 'saved' in result.results[0]['cleanup_warning']
    before = target.read_bytes(), target.stat().st_mtime_ns
    assert service.resume(identity).status == 'complete'
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


@pytest.mark.parametrize('earlier', ['topic.export', 'library.extract', 'library.backup', 'document.convert'])
def test_topic_output_aliases_are_refused_before_any_creation(environment, earlier):
    app, workspace, _, topic, _, outputs, service = environment
    before = app.topics(workspace)
    if earlier == 'topic.export':
        first = Step(earlier, {'topic': topic, 'name': 'Shared.txt'})
    elif earlier == 'library.extract':
        first = Step(earlier, {'edition': app.browse(workspace)[0].edition_id, 'name': 'Shared.txt'})
    elif earlier == 'library.backup':
        first = Step(earlier, {'name': 'Shared.txt'})
    else:
        source = outputs.parent / 'convert.txt'
        source.write_text('Selected input only', encoding='utf-8')
        first = Step(earlier, {'path': str(source), 'source_sha256': sha256(source.read_bytes()).hexdigest(),
                               'name': 'Shared.txt'})
    scope = Scope(workspace, (outputs.parent,), outputs, allow_full_backup=True)
    with pytest.raises(ValueError, match='distinct filename'):
        service.submit(Plan(scope, (Step('topic.create', {'name': 'Must not run'}), first,
            Step('topic.export', {'topic': topic, 'name': 'shared.txt'}))))
    assert app.topics(workspace) == before and service.jobs() == ()


def test_current_thirteen_tools_remain_allowed_alongside_exact_three_topic_tools():
    assert TOOLS == frozenset((
        'library.create', 'library.list', 'library.catalog', 'library.import', 'library.import_folder',
        'library.search', 'library.extract', 'library.backup', 'audio.produce',
        'document.convert', 'audio.effects', 'reader.control', 'player.control',
        'topic.list', 'topic.create', 'topic.export'))
