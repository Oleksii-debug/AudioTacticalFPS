"""Actual assistant imports retain the shared scanner's bounds and receipts."""
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.ingest import batch_scan


def prepared(tmp_path, *, count=5, max_files=5000, max_bytes=1024 * 1024,
             identical=False):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit selected library')
    inputs = tmp_path / 'Українські джерела'
    inputs.mkdir()
    for index in range(count):
        (inputs / f'{index:04}.txt').write_text(
            'Exact source repeated.' if identical else f'Exact source {index}.', encoding='utf-8')
    service = AssistantService(app)
    plan = Plan(Scope(workspace, (inputs,), max_files=max_files, max_bytes=max_bytes),
                (Step('library.import_folder', {'path': str(inputs)}),))
    identity = service.submit(plan)
    return app, workspace, inputs, service, plan, identity


def test_257_files_are_one_selected_root_and_actual_import_is_reopenable(tmp_path):
    app, workspace, inputs, service, plan, identity = prepared(tmp_path, count=257, identical=True)
    other = app.create_workspace('Other private library')
    report = service.run(identity)
    assert report.status == 'complete', report
    result, = report.results
    assert result['scan_complete'] is True
    assert result['counts']['complete'] == 1 and result['counts']['duplicate'] == 256
    batch = app.import_batch(workspace, result['batch_id'])
    assert batch['scan']['roots'] == [str(inputs)] and batch['recursive'] is True
    assert batch['total'] == 257 and not app.browse(other)
    assert len(list(inputs.iterdir())) == 257
    reopened = LibraryApp(app.root)
    hit, = reopened.search(workspace, 'Exact source repeated.')
    assert reopened.copy(workspace, hit.ref, hit.revision, 'quote') == 'Exact source repeated.'
    resumed = AssistantService(reopened).resume(identity, approved_plan_sha256=plan.digest)
    assert resumed.status == 'complete' and resumed.results[0]['batch_id'] == batch['batch_id']


def test_five_sources_follow_three_real_durable_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 2)
    app, workspace, inputs, service, _, identity = prepared(tmp_path, max_files=5)
    report = service.run(identity)
    assert report.status == 'complete', report
    result, = report.results
    assert len(result['batch_ids']) == 3 and result['counts']['complete'] == 5
    pages = [app.import_batch(workspace, value) for value in result['batch_ids']]
    assert [page['total'] for page in pages] == [2, 2, 1]
    assert [page['scan']['offset'] for page in pages] == [0, 2, 4]
    assert pages[0]['scan']['next_batch_id'] == pages[1]['batch_id']
    assert pages[1]['scan']['next_batch_id'] == pages[2]['batch_id']
    assert pages[-1]['scan']['next_offset'] is None
    assert set(result['editions']) == {row.edition_id for row in app.browse(workspace)}
    assert {row['source'] for page in pages for row in page['rows']} == {
        str(path) for path in inputs.iterdir()}


def test_pause_between_pages_reuses_completed_ids_without_rerunning_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 2)
    app, workspace, _, service, plan, identity = prepared(tmp_path)
    stopped, executed = Event(), []
    original = app.run_import_batch

    def finish_first(*args, **kwargs):
        result = original(*args, **kwargs)
        executed.append(result['batch_id'])
        stopped.set()
        return result

    monkeypatch.setattr(app, 'run_import_batch', finish_first)
    paused = service.run(identity, cancelled=stopped.is_set)
    assert paused.status == 'paused' and not paused.results
    assert len(executed) == 1 and len(app.browse(workspace)) == 2
    first = app.import_batch(workspace, executed[0])
    assert first['state'] == 'complete'
    reopened = LibraryApp(app.root)
    original_resume = reopened.run_import_batch
    resumed_pages = []

    def remaining_only(*args, **kwargs):
        assert args[1] != first['batch_id']
        result = original_resume(*args, **kwargs)
        resumed_pages.append(result['batch_id'])
        return result

    monkeypatch.setattr(reopened, 'run_import_batch', remaining_only)
    resumed = AssistantService(reopened).resume(identity, approved_plan_sha256=plan.digest)
    assert resumed.status == 'complete', resumed
    assert resumed.results[0]['batch_ids'] == [first['batch_id'], *resumed_pages]
    assert len(resumed_pages) == 2
    assert resumed.results[0]['counts']['complete'] == 5
    assert resumed.results[0]['counts']['duplicate'] == 0
    assert len(reopened.browse(workspace)) == 5


@pytest.mark.parametrize('budget', ['files', 'bytes'])
def test_whole_folder_budget_is_checked_before_source_bytes_or_first_page(tmp_path, monkeypatch, budget):
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 2)
    options = {'max_files': 2} if budget == 'files' else {'max_bytes': 10}
    app, workspace, _, service, _, identity = prepared(tmp_path, count=3, **options)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Admission must finish before opening source bytes.')

    monkeypatch.setattr(batch_scan, 'planned_snapshot', forbidden)
    report = service.run(identity)
    assert report.status == 'attention' and 'budget' in report.message
    assert not app.browse(workspace)
    assert not list((app.root / 'import-batches').glob('*.json'))
    assert service.store.load(identity)['steps'][0]['intent'] == {}


def test_structural_scan_limit_reports_incomplete_without_importing_a_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_scan, 'MAX_SCAN_ENTRIES', 2)
    app, workspace, _, service, _, identity = prepared(tmp_path, count=3)
    report = service.run(identity)
    assert report.status == 'attention'
    assert 'not completely scanned' in report.message and not report.results
    assert not app.browse(workspace)
    assert not list((app.root / 'import-batches').glob('*.json'))


def test_source_inventory_change_after_first_page_keeps_receipt_and_requires_review(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 2)
    app, workspace, inputs, service, plan, identity = prepared(tmp_path, count=3, identical=True)
    original = app.run_import_batch
    completed = []

    def changed_after_page(*args, **kwargs):
        result = original(*args, **kwargs)
        completed.append(result['batch_id'])
        (inputs / 'newly-added.txt').write_text('Never approved by this inventory.', encoding='utf-8')
        return result

    monkeypatch.setattr(app, 'run_import_batch', changed_after_page)
    report = service.run(identity)
    assert report.status == 'attention' and 'inventory changed' in report.message.lower()
    assert not report.results and len(completed) == 1
    assert app.import_batch(workspace, completed[0])['state'] == 'complete'
    assert len(app.browse(workspace)) == 1
    resumed = AssistantService(app).resume(identity, approved_plan_sha256=plan.digest)
    assert resumed.status == 'attention' and 'inventory changed' in resumed.message.lower()
    assert len(list((app.root / 'import-batches').glob('*.json'))) == 1


def test_changed_inventory_between_admission_and_planning_never_publishes(tmp_path, monkeypatch):
    app, workspace, inputs, service, _, identity = prepared(tmp_path, count=1)
    original = app.plan_import_batch

    def changed_before_scan(*args, **kwargs):
        (inputs / '0000.txt').write_text('A changed source.', encoding='utf-8')
        return original(*args, **kwargs)

    monkeypatch.setattr(app, 'plan_import_batch', changed_before_scan)
    report = service.run(identity)
    assert report.status == 'attention' and 'inventory changed' in report.message.lower()
    assert not app.browse(workspace) and not report.results


def test_cancel_during_admission_opens_no_source_bytes(tmp_path, monkeypatch):
    app, workspace, _, service, _, identity = prepared(tmp_path)
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Cancelled metadata admission must not open source bytes.')

    monkeypatch.setattr(batch_scan, 'planned_snapshot', forbidden)
    report = service.run(identity, cancelled=cancelled)
    assert report.status == 'paused' and not report.results
    assert not app.browse(workspace) and service.store.load(identity)['steps'][0]['intent'] == {}


def test_completed_last_page_reconciles_lost_assistant_reply_without_new_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 2)
    app, workspace, _, service, plan, identity = prepared(tmp_path, count=3, identical=True)
    first = service.run(identity)
    assert first.status == 'complete'
    record = service.store.load(identity)
    record['status'] = 'running'
    record['steps'][0].update(status='running', result=None)
    service.store.save(record)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Already committed page must not run a second time.')

    monkeypatch.setattr(app, 'run_import_batch', forbidden)
    report = AssistantService(app).resume(identity, approved_plan_sha256=plan.digest)
    assert report.status == 'complete' and report.results == first.results
    assert len(list((app.root / 'import-batches').glob('*.json'))) == 2
    assert len(app.browse(workspace)) == 1


def test_legacy_single_queue_cannot_expand_the_original_selected_folder(tmp_path):
    app, workspace, inputs, service, _, identity = prepared(tmp_path, count=1)
    allowed = inputs / 'selected'
    allowed.mkdir()
    (allowed / 'inside.txt').write_text('Selected input.', encoding='utf-8')
    plan = Plan(Scope(workspace, (inputs,)),
                (Step('library.import_folder', {'path': str(allowed)}),))
    identity = service.submit(plan)
    foreign = app.plan_import_batch(workspace, (inputs / '0000.txt',), recursive=False)
    record = service.store.load(identity)
    record['steps'][0]['intent'] = {'batch': foreign['batch_id']}
    service.store.save(record)
    report = service.run(identity)
    assert report.status == 'attention' and 'outside the selected input' in report.message
    assert not app.browse(workspace)


def test_unsupported_entries_are_reported_as_attention_not_whole_folder_success(tmp_path):
    app, workspace, inputs, service, _, identity = prepared(tmp_path, count=1)
    (inputs / 'unhandled.bin').write_bytes(b'not an imported document')
    report = service.run(identity)
    assert report.status == 'attention' and 'Import Center' in report.message
    assert not report.results and len(app.browse(workspace)) == 1
    batch_id = service.store.load(identity)['steps'][0]['intent']['batch']
    batch = app.import_batch(workspace, batch_id)
    assert batch['counts']['complete'] == batch['counts']['rejected'] == 1


def test_approved_file_cannot_be_replaced_by_a_folder_before_execution(tmp_path):
    app, workspace, inputs, service, _, _ = prepared(tmp_path, count=1)
    source = inputs / '0000.txt'
    identity = service.submit(Plan(Scope(workspace, (inputs,)),
        (Step('library.import', {'path': str(source)}),)))
    source.unlink()
    source.mkdir()
    (source / 'unexpected.txt').write_text('Unapproved folder traversal.', encoding='utf-8')
    report = service.run(identity)
    assert report.status == 'attention' and 'file/folder operation' in report.message
    assert not app.browse(workspace)
