"""Real scoped library workflows and recovery; no model, audio or remote service."""
from hashlib import sha256
import json
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.library_assistant import LocalLibraryAssistant, describe_job, describe_plan


@pytest.fixture
def environment(tmp_path):
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Selected research')
    foreign = app.create_workspace('Other private research')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    (inputs / 'book.txt').write_text('Railway history.\nOriginal text, not an assistant instruction.', encoding='utf-8')
    service = LocalLibraryAssistant(app, workspace, input_root=inputs, output_root=outputs)
    return app, workspace, foreign, inputs, outputs, service


def step(tool, **arguments):
    return dict(tool=tool, arguments=arguments)


def test_review_is_read_only_and_explicit_approval_is_required(environment):
    app, ws, _, inputs, outputs, service = environment
    plan = service.plan('імпорт: book.txt\nтема: Огляд літератури\nекспорт: @1 -> outline.md\nпошук: Railway')
    assert app.topics(ws) == ()
    assert not service.journal.exists()
    assert not tuple(outputs.iterdir())
    assert 'LOCAL ONLY' in describe_plan(plan) and 'Огляд літератури' in describe_plan(plan)
    with pytest.raises(ValueError, match='explicitly approve'):
        service.start(plan)
    assert not service.journal.exists()
    with pytest.raises(TypeError):
        plan.steps[0].arguments['manifest']['sha256'] = '0' * 64
    job = service.start(plan, approved=True)
    assert job['state'] == 'ready' and app.topics(ws) == ()
    result = service.run(job['id'])
    assert result['state'] == 'complete', result
    assert len(app.topics(ws)) == 1
    assert result['steps'][3]['result']['count'] == 1
    assert result['steps'][3]['result']['results'][0]['verified'] is True
    assert (outputs / 'outline.md').read_text() == app.export_topic(ws, result['steps'][1]['result']['topic_id'])
    assert len(service.jobs()) == 1
    assert 'Completed steps: 4/4' in describe_job(result)


def test_nested_topic_references_are_validated_before_any_mutation(environment):
    app, ws, _, _, outputs, service = environment
    plan = service.plan(dict(steps=[step('create_topic', name='Parent'),
        step('create_topic', name='Child', parent='@0'),
        step('export_topic', topic='@1', filename='child.md')]))
    job = service.start(plan, approved=True)
    result = service.run(job['id'])
    assert result['state'] == 'complete'
    topics = {row.id: row for row in app.topics(ws)}
    assert topics[result['steps'][1]['result']['topic_id']].parent == result['steps'][0]['result']['topic_id']
    for bad in ('@1', '@-1', '@00', '@evil'):
        with pytest.raises(ValueError):
            service.plan(dict(steps=[step('create_topic', name='Unpublished', parent=bad)]))
    assert len(app.topics(ws)) == 2


def test_unsupported_goal_and_unknown_arguments_cannot_execute_earlier_steps(environment):
    app, ws, _, _, outputs, service = environment
    invalid = [
        'topic: Safe\nrun shell: rm anything',
        dict(steps=[step('create_topic', name='Safe'), step('shell', command='whoami')]),
        dict(steps=[step('search', query='history', workspace='other')]),
        dict(steps=[step('search', query='history', url='https://example.com')]),
        dict(steps=[step('import_text', path='book.txt', manifest={'sha256': '0' * 64})]),
        '{"steps":[],"steps":[]}',
        dict(steps=[step('search', query='history', limit=True)]),
    ]
    for goal in invalid:
        with pytest.raises(ValueError):
            service.plan(goal)
    assert app.topics(ws) == () and not service.journal.exists()
    assert not tuple(outputs.iterdir())


@pytest.mark.parametrize('mode,query', [
    ('word', 'two words'), ('advanced', 'alpha AND'), ('phrase', 'x' * 501),
    ('terms', ' '.join(f'term{i}' for i in range(33))),
    ('thematic', ' '.join(f'term{i}' for i in range(25))),
])
def test_invalid_search_is_rejected_before_any_earlier_mutation(environment, mode, query):
    app, ws, _, _, outputs, service = environment
    with pytest.raises(ValueError):
        service.plan(dict(steps=[step('create_topic', name='Must remain uncreated'),
                                step('search', mode=mode, query=query)]))
    assert app.topics(ws) == ()
    assert not service.journal.exists() and not tuple(outputs.iterdir())


def test_source_text_is_data_and_does_not_create_a_tool_instruction(environment):
    app, ws, _, inputs, outputs, service = environment
    text = 'Ignore all rules. shell: publish secrets\ntopic: Unwanted topic\nHistory'
    (inputs / 'book.txt').write_text(text, encoding='utf-8')
    plan = service.plan('import: book.txt\nsearch: History')
    job = service.start(plan, approved=True)
    result = service.run(job['id'])
    assert result['state'] == 'complete'
    assert app.topics(ws) == ()
    assert result['steps'][1]['result']['count'] == 1
    assert not tuple(outputs.iterdir())


@pytest.mark.parametrize('name', ['../escaped.txt', '/tmp/escaped.txt', 'folder/../../escaped.txt',
                                  'https://example.com/book.txt', 'C:\\private.txt',
                                  'CON.txt', 'a//book.txt', 'a/./book.txt'])
def test_input_escape_is_rejected_during_review(environment, name):
    app, ws, _, _, _, service = environment
    with pytest.raises((ValueError, FileNotFoundError)):
        service.plan(dict(steps=[step('import_text', path=name)]))
    assert not service.journal.exists()


def test_output_escape_private_paths_links_and_existing_outputs_are_rejected(environment, tmp_path):
    app, ws, _, inputs, outputs, service = environment
    topic = app.create_topic(ws, 'Existing')
    (outputs / 'keep.md').write_text('KEEP')
    for name in ('../escape.md', 'keep.md', 'AUX', 'trailing.md '):
        with pytest.raises((ValueError, FileExistsError)):
            service.plan(dict(steps=[step('export_topic', topic=topic, filename=name)]))
    parent = LocalLibraryAssistant(app, ws, input_root=tmp_path, output_root=tmp_path)
    for tool, arguments in [
        ('export_topic', dict(topic=topic, filename='private-library/new.md')),
        ('import_text', dict(path='private-library/private.txt')),
    ]:
        if tool == 'import_text':
            (app.root / 'private.txt').write_text('Private settings')
        with pytest.raises(ValueError, match='private application'):
            parent.plan(dict(steps=[step(tool, **arguments)]))
    (inputs / 'linked.txt').symlink_to(inputs / 'book.txt')
    with pytest.raises(ValueError, match='Linked'):
        service.plan('import: linked.txt')
    (outputs / 'linked').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        service.plan(dict(steps=[step('export_topic', topic=topic, filename='linked/escape.md')]))
    assert (outputs / 'keep.md').read_text() == 'KEEP'


def test_input_hash_is_bound_at_review_and_rechecked_at_execution(environment):
    app, ws, _, inputs, _, service = environment
    plan = service.plan('import: book.txt')
    original = (inputs / 'book.txt').read_bytes()
    (inputs / 'book.txt').write_text('Changed before approval')
    with pytest.raises(ValueError, match='changed after preview'):
        service.start(plan, approved=True)
    assert not service.journal.exists()
    (inputs / 'book.txt').write_bytes(original)
    job = service.start(plan, approved=True)
    (inputs / 'book.txt').write_text('Changed after approval')
    result = service.run(job['id'])
    assert result['state'] == 'failed'
    assert 'changed after import planning' in result['error']
    with app.read(ws) as db:
        assert db.execute('SELECT COUNT(*) FROM catalog_edition WHERE workspace=?', (ws,)).fetchone()[0] == 0


def test_search_does_not_cross_scope_or_replace_notebook_handles(environment):
    app, ws, foreign, _, _, service = environment
    app.add_text(ws, 'Selected title', 'Railway history from selected scope.')
    app.add_text(foreign, 'PRIVATE_SENTINEL', 'Railway history from another scope.')
    old, = app.search(ws, 'selected')
    previous = dict(app._selections)
    plan = service.plan('search: Railway history')
    result = service.run(service.start(plan, approved=True)['id'])
    assert result['state'] == 'complete'
    assert result['steps'][0]['result']['count'] == 1
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)
    assert app._selections == previous
    assert app.copy(ws, old.ref, old.revision, 'quote') == 'selected'
    with pytest.raises(ValueError, match='another library'):
        LocalLibraryAssistant.open_job(app, foreign, result['id'])
    assert LocalLibraryAssistant(app, foreign).jobs() == ()


def test_pause_and_reopen_resumes_without_repeating_completed_steps(environment):
    app, ws, _, inputs, outputs, service = environment
    job = service.start(service.plan('import: book.txt\ntopic: New topic\nexport: @1 -> saved.md'), approved=True)
    cancel = Event()
    def progress(message):
        if message.startswith('Step 2'):
            cancel.set()
    first = service.run(job['id'], cancelled=cancel.is_set, progress=progress)
    assert first['state'] == 'paused'
    assert first['steps'][0]['state'] == 'complete'
    (inputs / 'book.txt').unlink()
    reopened = LocalLibraryAssistant.open_job(app, ws, job['id'])
    resumed = reopened.run(job['id'])
    assert resumed['state'] == 'complete', resumed
    assert len(app.topics(ws)) == 1
    with app.read(ws) as db:
        assert db.execute('SELECT COUNT(*) FROM catalog_edition WHERE workspace=?', (ws,)).fetchone()[0] == 1
    before = (outputs / 'saved.md').stat().st_mtime_ns
    assert reopened.run(job['id'])['state'] == 'complete'
    assert (outputs / 'saved.md').stat().st_mtime_ns == before


@pytest.mark.parametrize('operation', ['create_topic', 'import_text', 'export_topic'])
def test_crash_after_tool_commit_before_checkpoint_is_reconciled_without_duplicates(environment, monkeypatch, operation):
    app, ws, _, inputs, outputs, service = environment
    if operation == 'create_topic':
        plan = service.plan('topic: Once')
    elif operation == 'import_text':
        plan = service.plan('import: book.txt')
    else:
        topic = app.create_topic(ws, 'Exportable')
        plan = service.plan(dict(steps=[step('export_topic', topic=topic, filename='once.md')]))
    job = service.start(plan, approved=True)
    save = service._save
    def crash(state):
        if state['steps'][0]['state'] == 'complete':
            raise SystemExit('Simulated process termination after tool committed.')
        save(state)
    monkeypatch.setattr(service, '_save', crash)
    with pytest.raises(SystemExit):
        service.run(job['id'])
    resumed = LocalLibraryAssistant.open_job(app, ws, job['id']).run(job['id'])
    assert resumed['state'] == 'complete', resumed
    if operation == 'create_topic':
        assert len(app.topics(ws)) == 1
    elif operation == 'import_text':
        with app.read(ws) as db:
            assert db.execute('SELECT COUNT(*) FROM catalog_edition WHERE workspace=?', (ws,)).fetchone()[0] == 1
    else:
        assert (outputs / 'once.md').read_text() == app.export_topic(ws, topic)
        assert resumed['steps'][0]['result']['recovered'] is True


def test_changed_output_is_never_overwritten_after_approval(environment):
    app, ws, _, _, outputs, service = environment
    topic = app.create_topic(ws, 'Topic')
    plan = service.plan(dict(steps=[step('export_topic', topic=topic, filename='new.md')]))
    job = service.start(plan, approved=True)
    (outputs / 'new.md').write_text('Another process wrote this')
    result = service.run(job['id'])
    assert result['state'] == 'failed'
    assert 'not overwritten' in result['error']
    assert (outputs / 'new.md').read_text() == 'Another process wrote this'


def test_archived_workspace_and_modified_journal_fail_before_running(environment):
    app, ws, _, _, _, service = environment
    job = service.start(service.plan('topic: Not executed'), approved=True)
    path = service.journal / (job['id'] + '.json')
    original = path.read_bytes()
    value = json.loads(original)
    value['job']['plan']['steps'][0]['arguments']['name'] = 'Changed without approval'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='checksum'):
        service.run(job['id'])
    path.write_bytes(original)
    app.archive_workspace(ws)
    with pytest.raises(ValueError, match='archived'):
        service.run(job['id'])


@pytest.mark.parametrize('tool', [
    'search', 'list_topics', 'create_topic', 'export_topic', 'import_text', 'backup',
])
def test_completed_checkpoint_has_a_typed_tool_result_not_only_a_valid_checksum(environment, tool):
    app, ws, _, inputs, outputs, _ = environment
    topic = app.create_topic(ws, 'Existing topic')
    service = LocalLibraryAssistant(app, ws, input_root=inputs, output_root=outputs,
                                    allow_full_backup=True)
    arguments = {
        'search': dict(query='Railway'), 'list_topics': {},
        'create_topic': dict(name='Created topic'),
        'export_topic': dict(topic=topic, filename='exact.md'),
        'import_text': dict(path='book.txt'), 'backup': dict(folder='preserved'),
    }
    job = service.start(service.plan(dict(steps=[step(tool, **arguments[tool])])), approved=True)
    result = service.run(job['id'])
    assert result['state'] == 'complete'
    result['steps'][0]['result'] = {'unrecognized': 'Malformed typed checkpoint'}
    service._save(result)  # A checksum alone does not validate a supported schema.
    before = {str(path.relative_to(outputs)): path.read_bytes()
              for path in outputs.rglob('*') if path.is_file()}
    with pytest.raises(ValueError, match='result checkpoint'):
        LocalLibraryAssistant.open_job(app, ws, job['id'])
    assert {str(path.relative_to(outputs)): path.read_bytes()
            for path in outputs.rglob('*') if path.is_file()} == before


def test_topic_checkpoint_cannot_retarget_a_later_export(environment):
    app, ws, _, _, outputs, service = environment
    other = app.create_topic(ws, 'Unselected topic')
    job = service.start(service.plan('topic: Requested topic\nexport: @0 -> report.md'), approved=True)
    cancel = Event()
    paused = service.run(job['id'], cancelled=cancel.is_set,
        progress=lambda message: cancel.set() if message.startswith('Step 2') else None)
    assert paused['state'] == 'paused'
    paused['steps'][0]['result']['topic_id'] = other
    service._save(paused)
    with pytest.raises(ValueError, match='create_topic result checkpoint'):
        service.run(job['id'])
    assert not (outputs / 'report.md').exists()
    assert {topic.name for topic in app.topics(ws)} == {'Unselected topic', 'Requested topic'}


@pytest.mark.parametrize('damage', ['complete_job', 'result_on_pending', 'future_complete', 'foreign_intent'])
def test_invalid_checkpoint_progress_cannot_silently_skip_execution(environment, damage):
    app, ws, _, _, _, service = environment
    job = service.start(service.plan('topic: Must not execute\ntopics'), approved=True)
    if damage == 'complete_job':
        job['state'] = 'complete'
    elif damage == 'result_on_pending':
        job['steps'][0]['result'] = {'unrecognized': True}
    elif damage == 'future_complete':
        job['steps'][1].update(state='complete', result={'topics': []})
    else:
        job['steps'][0]['intent'] = {'kind': 'export', 'content': {'size': 0, 'sha256': '0' * 64}}
    service._save(job)
    with pytest.raises(ValueError):
        service.run(job['id'])
    assert app.topics(ws) == ()


def test_full_backup_requires_separate_permission_and_recovers_without_overwrite(environment, monkeypatch):
    app, ws, foreign, inputs, outputs, service = environment
    app.add_text(foreign, 'Other library source', 'Explicit all-library backup includes this.')
    with pytest.raises(ValueError, match='ALL libraries'):
        service.plan('backup: preserved')
    permitted = LocalLibraryAssistant(app, ws, output_root=outputs, allow_full_backup=True)
    plan = permitted.plan('backup: preserved')
    assert 'BACK UP ALL LIBRARIES' in describe_plan(plan)
    job = permitted.start(plan, approved=True)
    save = permitted._save
    def crash(state):
        if state['steps'][0]['state'] == 'complete':
            raise SystemExit('Crash after successful backup')
        save(state)
    monkeypatch.setattr(permitted, '_save', crash)
    with pytest.raises(SystemExit):
        permitted.run(job['id'])
    resumed = LocalLibraryAssistant.open_job(app, ws, job['id']).run(job['id'])
    assert resumed['state'] == 'complete', resumed
    target = outputs / 'preserved' / 'snapshot'
    assert target.is_dir()
    assert sha256((target / 'manifest.json').read_bytes()).hexdigest() == resumed['steps'][0]['result']['manifest_sha256']
    with sqlite3.connect(target / 'library.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM workspace').fetchone()[0] == 2


@pytest.mark.parametrize('checkpoint', ['complete', 'before_checkpoint'])
@pytest.mark.parametrize('damage', ['changed_bytes', 'missing'])
def test_backup_resume_verifies_saved_reading_state_without_republishing(
        environment, monkeypatch, checkpoint, damage):
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import FILENAME, ReadingState
    app, ws, _, _, outputs, _ = environment
    book = app.add_text(ws, 'Book with saved place', 'Original source page.')
    place = ReadingDesk(app, ws, book['edition']).page(1).location
    reading = ReadingState(app.root)
    reading.remember(place)
    reading.add_bookmark(place, 'Original bookmark')
    live_state = reading.path.read_bytes()
    service = LocalLibraryAssistant(app, ws, output_root=outputs, allow_full_backup=True)
    job = service.start(service.plan('backup: preserved'), approved=True)
    if checkpoint == 'before_checkpoint':
        save = service._save

        def crash(state):
            if state['steps'][0]['state'] == 'complete':
                raise SystemExit('Crash after snapshot publication, before job completion')
            save(state)

        monkeypatch.setattr(service, '_save', crash)
        with pytest.raises(SystemExit):
            service.run(job['id'])
    else:
        assert service.run(job['id'])['state'] == 'complete'
    target = outputs / 'preserved' / 'snapshot'
    path = target / FILENAME
    before = {str(item.relative_to(target)): item.read_bytes()
              for item in target.rglob('*') if item.is_file()}
    assert before[FILENAME] == live_state
    if damage == 'changed_bytes':
        path.write_bytes(live_state.replace(b'Original bookmark', b'Modified bookmark'))
    else:
        path.unlink()
    damaged = {str(item.relative_to(target)): item.read_bytes()
               for item in target.rglob('*') if item.is_file()}

    def no_republication(_):
        pytest.fail('An existing backup must be verified without publishing a replacement.')

    monkeypatch.setattr(app, 'backup', no_republication)
    resumed = LocalLibraryAssistant.open_job(app, ws, job['id']).run(job['id'])
    assert resumed['state'] == 'failed', resumed
    assert 'reading-state' in resumed['error']
    assert {str(item.relative_to(target)): item.read_bytes()
            for item in target.rglob('*') if item.is_file()} == damaged
    assert reading.path.read_bytes() == live_state


def test_new_backup_is_verified_before_a_step_can_be_completed(environment, monkeypatch):
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import FILENAME, ReadingState
    app, ws, _, _, outputs, _ = environment
    book = app.add_text(ws, 'Book with bookmark', 'Original source page.')
    ReadingState(app.root).add_bookmark(
        ReadingDesk(app, ws, book['edition']).page(1).location, 'Original bookmark')
    backup = app.backup

    def changed_after_publication(destination):
        digest = backup(destination)
        path = destination / FILENAME
        path.write_bytes(path.read_bytes().replace(b'Original bookmark', b'Modified bookmark'))
        return digest

    monkeypatch.setattr(app, 'backup', changed_after_publication)
    service = LocalLibraryAssistant(app, ws, output_root=outputs, allow_full_backup=True)
    job = service.start(service.plan('backup: preserved'), approved=True)
    result = service.run(job['id'])
    assert result['state'] == 'failed', result
    assert result['steps'][0]['state'] != 'complete'
    assert 'reading-state' in result['error']


def test_job_lock_rejects_concurrent_runner(environment):
    from carr_library.ingest.writer_guard import exclusive_writer, WriterBusy
    _, _, _, _, _, service = environment
    job = service.start(service.plan('topics'), approved=True)
    with exclusive_writer(service.journal):
        with pytest.raises(WriterBusy):
            service.run(job['id'])


def test_export_retains_actual_exact_evidence_and_separate_notes(environment):
    app, ws, _, _, outputs, service = environment
    app.add_text(ws, 'Research source', 'Original railway quotation.', authors=('Author',))
    topic = app.create_topic(ws, 'Research notes')
    hit, = app.search(ws, 'railway')
    app.save_hit(ws, topic, hit.ref, hit.revision, note='My own commentary')
    expected = app.export_topic(ws, topic)
    plan = service.plan(dict(steps=[step('export_topic', topic=topic, filename='evidence.md')]))
    result = service.run(service.start(plan, approved=True)['id'])
    assert result['state'] == 'complete'
    assert (outputs / 'evidence.md').read_text() == expected
    assert 'railway' in expected and 'My own commentary' in expected
    assert app.copy(ws, hit.ref, hit.revision, 'quote') == hit.quote


def test_completed_output_corruption_is_detected_when_resuming(environment):
    app, ws, _, _, outputs, service = environment
    topic = app.create_topic(ws, 'Exportable')
    job = service.start(service.plan(dict(steps=[step('export_topic', topic=topic, filename='result.md')])),
                        approved=True)
    assert service.run(job['id'])['state'] == 'complete'
    (outputs / 'result.md').write_text('External change')
    checked = service.run(job['id'])
    assert checked['state'] == 'failed' and 'completed export changed' in checked['error']
    assert (outputs / 'result.md').read_text() == 'External change'


def test_real_process_exit_after_topic_commit_recovers_single_topic(environment):
    import os
    import subprocess
    import sys
    from pathlib import Path
    import carr_library.library_assistant as assistant_module
    app, ws, _, _, _, service = environment
    job = service.start(service.plan('topic: Durable topic'), approved=True)
    code = '''
import os, sys
from carr_library.application import LibraryApp
from carr_library.library_assistant import LocalLibraryAssistant
service = LocalLibraryAssistant.open_job(LibraryApp(sys.argv[1]), sys.argv[2], sys.argv[3])
save = service._save
def stop(job):
    if job['steps'][0]['state'] == 'complete':
        os._exit(73)
    save(job)
service._save = stop
service.run(sys.argv[3])
'''
    env = dict(os.environ)
    source = str(Path(assistant_module.__file__).resolve().parents[1])
    project = str(Path(source).parent)
    env['PYTHONPATH'] = os.pathsep.join((source, project))
    process = subprocess.run([sys.executable, '-c', code, str(app.root), ws, job['id']],
                             env=env, capture_output=True, text=True, timeout=30)
    assert process.returncode == 73, (process.stdout, process.stderr)
    assert len(app.topics(ws)) == 1
    resumed = LocalLibraryAssistant.open_job(app, ws, job['id']).run(job['id'])
    assert resumed['state'] == 'complete'
    assert len(app.topics(ws)) == 1


@pytest.mark.parametrize('damage', ['none', 'extra_link', 'changed_staging', 'replaced_staging'])
def test_real_process_exit_after_export_link_recovers_only_its_recorded_twin(environment, damage):
    import os
    import subprocess
    import sys
    from pathlib import Path
    import carr_library.library_assistant as assistant_module
    app, ws, _, _, outputs, service = environment
    topic = app.create_topic(ws, 'Exportable topic')
    job = service.start(service.plan(dict(steps=[
        step('export_topic', topic=topic, filename='once.md')])), approved=True)
    code = '''
import os, sys
from carr_library.application import LibraryApp
from carr_library.library_assistant import LocalLibraryAssistant
service = LocalLibraryAssistant.open_job(LibraryApp(sys.argv[1]), sys.argv[2], sys.argv[3])
link = os.link
def stop(source, destination, *args, **kwargs):
    link(source, destination, *args, **kwargs)
    os._exit(73)
os.link = stop
service.run(sys.argv[3])
'''
    env = dict(os.environ)
    source = str(Path(assistant_module.__file__).resolve().parents[1])
    env['PYTHONPATH'] = os.pathsep.join((source, str(Path(source).parent)))
    process = subprocess.run([sys.executable, '-c', code, str(app.root), ws, job['id']],
                             env=env, capture_output=True, text=True, timeout=30)
    assert process.returncode == 73, (process.stdout, process.stderr)
    target = outputs / 'once.md'
    assert target.read_text() == app.export_topic(ws, topic)
    assert target.stat().st_nlink == 2
    staged, = outputs.glob('.assistant-export-*')
    if damage == 'extra_link':
        os.link(target, outputs / 'unaccounted.md')
    elif damage == 'changed_staging':
        staged.write_text('Changed through the surviving link')
    elif damage == 'replaced_staging':
        raw = staged.read_bytes()
        staged.unlink()
        staged.write_bytes(raw)
    before = {path.name: (path.read_bytes(), path.stat().st_nlink, path.stat().st_mtime_ns)
              for path in outputs.iterdir()}
    if damage != 'none':
        with pytest.raises(ValueError):
            LocalLibraryAssistant.open_job(app, ws, job['id'])
        assert {path.name: (path.read_bytes(), path.stat().st_nlink, path.stat().st_mtime_ns)
                for path in outputs.iterdir()} == before
        return
    reopened = LocalLibraryAssistant.open_job(app, ws, job['id'])
    # Preview/load is read-only; reconciliation occurs under the run lock.
    assert target.stat().st_nlink == 2 and staged.exists()
    result = reopened.run(job['id'])
    assert result['state'] == 'complete', result
    assert result['steps'][0]['result']['recovered'] is True
    assert target.read_text() == app.export_topic(ws, topic)
    assert target.stat().st_nlink == 1 and not staged.exists()
    assert target.stat().st_mtime_ns == before[target.name][2]
    assert reopened.run(job['id'])['state'] == 'complete'
