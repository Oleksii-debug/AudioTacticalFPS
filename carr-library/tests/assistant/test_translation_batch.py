"""Actual library/jobs/ledger/backup batches with synthetic provider transport."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import runpy
from contextlib import contextmanager
from uuid import UUID
from zipfile import ZipFile
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.providers import Gateway, ProviderError
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library.assistant.translation_batch import (
    KIND, TranslationBatchService, member_id, validate_batch,
)
from carr_library.library_backup import restore_backup
from carr_library.operations.facade import OperationFacade
from carr_library.speech.reading import ReadingDocument


_fixture = runpy.run_path(str(Path(__file__).with_name('translation_batch_fixtures.py')))
transport, source_library, batch_env = (_fixture[name] for name in ('transport', 'source_library', 'batch_env'))
GLOSSARY, TOKEN = _fixture['GLOSSARY'], _fixture['TOKEN']

def prepared(e):
    review = e.batches.preview(e.items, title='Reviewed document order')
    identity = e.batches.submit(e.items, title='Reviewed document order',
                               review_sha256=review['review_sha256'], approve_remote=True)
    return identity, e.batches.jobs.load(identity)


def reseal(value):
    value['manifest_sha256'] = sha256(canonical({key: item for key, item in value.items()
        if key not in ('updated', 'manifest_sha256')}).encode('utf-8')).hexdigest()


def test_preview_preparation_reuses_current_children_and_makes_no_remote_call(batch_env, transport):
    e = batch_env
    before = len(transport['calls']), len(e.vault.calls)
    identity, manifest = prepared(e)
    assert manifest['kind'] == KIND and validate_batch(manifest, identity) is manifest
    assert manifest['review']['documents'] == 2
    assert manifest['review']['characters'] == sum(len(value['text']) for value in e.selected)
    assert Decimal(manifest['review']['budget']) == Decimal('2')
    assert manifest['review']['items'][0]['selected_positions'] == 2
    assert manifest['review']['items'][1]['glossary'] == GLOSSARY
    assert e.batches.service.jobs is e.service.jobs
    assert e.batches.jobs.directory == e.service.directory / 'batches'
    assert len(e.service.jobs.identities()) == 2 and len(e.service.list()) == 2
    for index, member in enumerate(manifest['members']):
        job = e.service._load(member['job']['id'])
        assert UUID(job['id']).version == 5
        assert job['id'] == __import__('carr_library.assistant.translation', fromlist=['_source_job_id'])._source_job_id(job)
        assert job['status'] == 'queued' and job['receipts'] == []
        assert job['source_text'] == e.selected[index]['text']
        assert job['authorization']['task_id'] == job['authorization']['id']
        assert job['authorization']['data_classes'] == ['selected-script']
        assert 'batch' not in job and job['kind'] == 'translation/2'
    assert (len(transport['calls']), len(e.vault.calls)) == before
    assert e.batches.get(identity)['status'] == 'queued'
    with pytest.raises(FileNotFoundError):
        e.service._load(identity)


@pytest.mark.parametrize('change', ['order', 'target', 'budget', 'title', 'glossary', 'source'])
def test_submit_rejects_a_changed_review_without_new_jobs_or_requests(batch_env, transport, change):
    e = batch_env
    review = e.batches.preview(e.items)
    altered = deepcopy(e.items)
    if change == 'order':
        altered.reverse()
    elif change == 'source':
        altered[0]['text'] += 'Unreviewed bytes.'
    elif change == 'title':
        altered[0]['title'] = 'Different document label'
    elif change == 'glossary':
        altered[0]['glossary'] = GLOSSARY
    elif change == 'target':
        altered[0]['target'] = 'fr'
    else:
        altered[0]['budget'] = '2'
    before = len(transport['calls'])
    with pytest.raises((ValueError, PermissionError)):
        e.batches.submit(altered, review_sha256=review['review_sha256'], approve_remote=True)
    assert not e.service.jobs.identities() and len(transport['calls']) == before


def test_batch_needs_explicit_approval_and_rejects_duplicate_or_oversized_selection(batch_env, transport):
    e = batch_env
    review = e.batches.preview(e.items)
    with pytest.raises(PermissionError, match='Explicitly'):
        e.batches.submit(e.items, review_sha256=review['review_sha256'])
    with pytest.raises(ValueError, match='twice'):
        e.batches.preview([e.items[0], deepcopy(e.items[0])])
    with pytest.raises(ValueError):
        e.batches.preview(e.items * 17)
    plain = {'profile': e.p.to_dict(), 'text': 'x' * 1_100_000, 'source': 'en', 'target': 'uk', 'budget': '1'}
    with pytest.raises(ValueError, match='bounded'):
        e.batches.preview([plain, {**plain, 'text': 'y' * 1_100_000}])
    assert len(transport['calls']) == 1 and not e.service.jobs.identities()


def test_interrupted_preparation_recovers_exact_child_ids_and_approvals(batch_env, monkeypatch, transport):
    e = batch_env
    review = e.batches.preview(e.items)
    stop = threading.Event()
    save = e.service.jobs.save

    def save_then_stop(value):
        save(value)
        if value.get('kind') in ('translation/1', 'translation/2'):
            stop.set()

    monkeypatch.setattr(e.service.jobs, 'save', save_then_stop)
    identity = e.batches.submit(e.items, review_sha256=review['review_sha256'],
                               approve_remote=True, cancelled=stop.is_set)
    manifest = e.batches.jobs.load(identity)
    assert e.batches.get(identity)['status'] == 'preparing'
    assert len(e.service.jobs.identities()) == 1
    retained = e.batches.jobs.path(identity).read_bytes()
    child = manifest['members'][0]['job']['id']
    accepted_template = e.service.jobs.path(child).read_bytes()
    stop.clear()
    monkeypatch.setattr(e.service.jobs, 'save', save)
    assert e.batches.prepare(identity)['status'] == 'queued'
    assert len(e.service.jobs.identities()) == 2
    assert e.batches.jobs.path(identity).read_bytes() == retained
    assert e.service.jobs.path(child).read_bytes() == accepted_template
    assert len(transport['calls']) == 1


def test_pause_restart_reuses_verified_chunks_and_never_starts_next_document_early(batch_env, transport):
    e = batch_env
    identity, manifest = prepared(e)
    stop = threading.Event()
    first, second = [member['job']['id'] for member in manifest['members']]
    result = e.batches.resume(identity, cancelled=stop.is_set,
                              progress=lambda *unused: stop.set())
    assert result['status'] == 'paused' and result['completed_chunks'] == 1
    saved = e.service._load(first)['receipts'][0]
    assert e.service.jobs.load(second)['status'] == 'queued'
    assert e.service.jobs.load(second)['receipts'] == []
    before = len(transport['calls'])
    stop.clear()
    assert e.batches.resume(identity)['status'] == 'complete'
    assert e.service._load(first)['receipts'][0] == saved
    assert len(transport['calls']) - before == manifest['review']['calls'] - 1
    files = {key: e.service.jobs.path(key).read_bytes() for key in (first, second)}
    before = len(transport['calls'])
    assert e.batches.resume(identity)['status'] == 'complete'
    assert {key: e.service.jobs.path(key).read_bytes() for key in files} == files
    assert len(transport['calls']) == before


def test_stop_between_documents_retains_complete_first_job_without_touching_second(batch_env, transport):
    e = batch_env
    identity, manifest = prepared(e)
    first, second = [member['job']['id'] for member in manifest['members']]
    stop = threading.Event()
    first_total = len(e.service.jobs.load(first)['chunks'])
    before_second = e.service.jobs.path(second).read_bytes()
    def stopped_after_first(done, total, phase):
        if done == first_total:
            stop.set()
    result = e.batches.resume(identity, cancelled=stop.is_set, progress=stopped_after_first)
    assert result['status'] == 'paused' and result['completed'] == 1
    assert e.service.jobs.path(second).read_bytes() == before_second
    assert len(transport['calls']) == first_total + 1


@pytest.mark.parametrize('change', ['order', 'review', 'output', 'template', 'child'])
def test_tampered_batch_or_child_cannot_make_a_request(batch_env, transport, change):
    e = batch_env
    identity, manifest = prepared(e)
    if change == 'child':
        child = e.service.jobs.load(manifest['members'][0]['job']['id'])
        child['title'] = 'A different individually valid child'
        e.service.jobs.save(child)
    else:
        if change == 'order':
            manifest['members'].reverse()
        elif change == 'review':
            manifest['review']['items'][0]['budget'] = '8'
            manifest['review_sha256'] = sha256(canonical(manifest['review']).encode()).hexdigest()
        elif change == 'output':
            manifest['members'][0]['output_directory'] = '../escape'
        else:
            manifest['members'][0]['job']['title'] = 'A changed template'
        reseal(manifest)
        e.batches.jobs.save(manifest)
    before = len(transport['calls'])
    with pytest.raises((ValueError, PermissionError)):
        e.batches.resume(identity)
    assert len(transport['calls']) == before


def test_member_check_runs_under_child_lock_before_any_request(batch_env, monkeypatch, transport):
    e = batch_env
    identity, manifest = prepared(e)
    child_id = manifest['members'][0]['job']['id']
    resume = e.service.resume

    def change_after_batch_snapshot(identity, **kwargs):
        child = e.service.jobs.load(identity)
        child['title'] = 'Changed between batch snapshot and child lock'
        e.service.jobs.save(child)
        return resume(identity, **kwargs)

    monkeypatch.setattr(e.service, 'resume', change_after_batch_snapshot)
    before = len(transport['calls'])
    with pytest.raises(ValueError, match='template'):
        e.batches.resume(identity)
    assert e.service.jobs.load(child_id)['status'] == 'queued'
    assert not e.service.jobs.load(child_id)['receipts']
    assert len(transport['calls']) == before


def test_stale_later_source_prevents_any_document_from_being_sent(batch_env, transport):
    e = batch_env
    identity, manifest = prepared(e)
    retained = {member['job']['id']: e.service.jobs.path(member['job']['id']).read_bytes()
                for member in manifest['members']}
    e.app.update_metadata(e.workspace, e.second['edition'], title='New revision after review')
    before = len(transport['calls'])
    with pytest.raises((ValueError, PermissionError)):
        e.batches.resume(identity)
    assert len(transport['calls']) == before
    assert {key: e.service.jobs.path(key).read_bytes() for key in retained} == retained


def test_revocation_during_response_retains_uncertain_ledger_and_stops_batch(batch_env, transport):
    e = batch_env
    identity, manifest = prepared(e)
    first, second = [member['job']['id'] for member in manifest['members']]
    def revoke(document):
        e.app.archive_workspace(e.workspace)
        return document
    transport['response'] = revoke
    before = len(transport['calls'])
    with pytest.raises(PermissionError):
        e.batches.resume(identity)
    assert len(transport['calls']) == before + 1
    assert e.service.jobs.load(first)['status'] == 'attention'
    assert e.service.jobs.load(first)['receipts'] == []
    assert e.service.jobs.load(second)['status'] == 'queued'
    approval = manifest['members'][0]['job']['authorization']
    attempt = e.service.gateway.ledger.get(approval['id'])['operations'][first + '_0']
    assert attempt['status'] == 'uncertain' and attempt['output_sha256'] is None
    e.app.restore_workspace(e.workspace)
    transport['response'] = None
    before = len(transport['calls'])
    with pytest.raises(ProviderError, match='already been attempted'):
        e.batches.resume(identity)
    assert len(transport['calls']) == before
    assert not e.service.jobs.load(second)['receipts']


def test_optional_progress_observer_cannot_rewrite_durable_results(batch_env):
    e = batch_env
    identity, _ = prepared(e)
    def broken(*args):
        raise RuntimeError('Optional display closed')
    assert e.batches.resume(identity, progress=broken)['status'] == 'complete'
    assert all(job['status'] == 'complete' for job in e.service.list())


def test_completed_batch_export_preserves_every_original_and_exact_member_lineage(batch_env, tmp_path, transport):
    e = batch_env
    identity, manifest = prepared(e)
    assert e.batches.resume(identity)['status'] == 'complete'
    before = len(transport['calls'])
    destination = tmp_path / 'reviewed-batch-export.zip'
    result = e.batches.export(identity, destination)
    assert result['status'] == 'complete' and len(result['items']) == 2
    assert result['sha256'] == sha256(destination.read_bytes()).hexdigest()
    with ZipFile(destination) as archive:
        published = json.loads(archive.read('batch-provenance.json'))
        assert published['review_sha256'] == manifest['review_sha256']
        assert published['quotation_authority'] is False
        assert len(archive.namelist()) == 11
        for selected, row in zip(e.selected, published['items']):
            prefix = row['directory'] + '/'
            reading = ReadingDocument.from_dict(json.loads(archive.read(prefix + 'translation.reading.json')))
            assert reading.source_text == selected['text'] and reading.source_anchor == selected['source_anchor']
            assert not reading.source_span(0, len(reading.text)).exact
            assert archive.read(prefix + 'original.txt') == selected['text'].encode('utf-8')
            for filename, digest in row['files'].items():
                assert sha256(archive.read(prefix + filename)).hexdigest() == digest
            report = json.loads(archive.read(prefix + 'translation-provenance.json'))
            assert report['decoded_text_binding'] == 'usage-ledger-sha256'
            assert report['quotation_authority'] is False
            assert e.app.assets.resolve(selected['source_anchor']['asset_sha256']).is_file()
    with pytest.raises(FileExistsError):
        e.batches.export(identity, destination)
    assert len(transport['calls']) == before


def test_export_source_revocation_before_publication_leaves_no_visible_package(batch_env, tmp_path, monkeypatch):
    e = batch_env
    identity, _ = prepared(e)
    e.batches.resume(identity)
    export = e.service.export
    def revoke_after_serializing(job, target, **kwargs):
        result = export(job, target, **kwargs)
        e.app.archive_workspace(e.workspace)
        return result
    monkeypatch.setattr(e.service, 'export', revoke_after_serializing)
    destination = tmp_path / 'revoked-batch-export.zip'
    with pytest.raises((ValueError, PermissionError)):
        e.batches.export(identity, destination)
    assert not destination.exists() and not list(tmp_path.glob('.pending-translation-batch-*'))


def test_resigned_backup_rejects_a_child_that_no_longer_matches_parent(batch_env, tmp_path):
    e = batch_env
    identity, batch = prepared(e)
    folder = tmp_path / 'batch-backup'
    e.app.backup(folder)
    name = 'state/translation/jobs/' + batch['members'][0]['job']['id'] + '.json'
    path = folder / name
    job = json.loads(path.read_bytes())
    job['title'] = 'Individually valid but no longer approved by this batch'
    raw = (canonical(job) + '\n').encode('utf-8')
    path.write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry = next(row for row in manifest['sidecars'] if row['filename'] == name)
    entry.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    destination = tmp_path / 'rejected-batch-restore'
    with pytest.raises(ValueError, match='template'):
        restore_backup(folder, destination, sha256(raw).hexdigest())
    assert not destination.exists()


def test_dashboard_keeps_existing_child_jobs_without_a_second_execution_queue(batch_env, transport):
    e = batch_env
    identity, manifest = prepared(e)
    before = len(transport['calls']), len(e.vault.calls)
    rows = OperationFacade(e.app).list(workspace=e.workspace)
    children = {member['job']['id'] for member in manifest['members']}
    selected = [row for row in rows if row.kind == 'translation' and row.id in children]
    assert len(selected) == 2
    assert all(row.controls == () and row.workspace == e.workspace for row in selected)
    assert not any(row.id == identity for row in rows)
    assert (len(transport['calls']), len(e.vault.calls)) == before
    assert e.batches.get(identity)['control'] == 'pause'

def test_plain_single_document_jobs_keep_their_existing_wire_and_execution(batch_env, transport):
    e = batch_env
    value = 'Independent pasted text, unchanged by batches.'
    approval, cost = translation_approval(e.p, value, 'en', 'uk', '1')
    single = e.service.submit(e.p, value, 'en', 'uk', authorization=approval, per_call_cost=cost)
    before = e.service.jobs.path(single).read_bytes()
    identity, manifest = prepared(e)
    assert e.service.jobs.path(single).read_bytes() == before
    assert e.service.resume(single)['status'] == 'complete'
    assert e.service._load(single)['authorization'] == json.loads(canonical(asdict(approval)))
    assert e.batches.get(identity)['status'] == 'queued'
    assert len(e.service.list()) == 3


def test_saved_batch_review_is_an_independent_copy_and_currency_cannot_be_relabelled(batch_env, transport):
    e = batch_env
    identity, manifest = prepared(e)
    before = len(transport['calls'])
    review = e.batches.get_review(identity)
    assert review['review_sha256'] == manifest['review_sha256'] and review['currency'] == 'USD'
    review['items'][0]['title'] = 'Caller-only copy'
    assert e.batches.get_review(identity)['items'][0]['title'] == e.items[0]['title']
    changed = deepcopy(manifest)
    changed['members'][0]['job']['authorization']['currency'] = 'EUR'
    reseal(changed)
    with pytest.raises(ValueError, match='currency'):
        validate_batch(changed, identity)
    assert e.batches.jobs.load(identity) == manifest and len(transport['calls']) == before


def test_pasted_batch_children_keep_uuid4_and_exact_individual_approvals(batch_env, transport):
    e = batch_env
    rows = [{key: value for key, value in row.items()
             if key not in ('source_anchor', 'source_metadata', 'source_topic')} for row in e.items]
    preview = e.batches.preview(rows)
    identity = e.batches.submit(rows, review_sha256=preview['review_sha256'], approve_remote=True)
    manifest = e.batches._load(identity)
    for index, member in enumerate(manifest['members']):
        child = e.service._load(member['job']['id'])
        assert child['kind'] == 'translation/1' and UUID(child['id']).version == 4
        assert child['id'] == member_id(identity, preview['review_sha256'], index)
        assert child['authorization']['data_classes'] == ['selected-excerpt']
        assert 'source_binding' not in child
    assert e.batches.resume(identity)['status'] == 'complete'


@pytest.mark.parametrize('observer', ['error', 'interrupt', 'late-cancel'])
def test_last_accepted_child_remains_complete_after_late_observer_event(batch_env, transport, observer):
    e = batch_env
    identity, manifest = prepared(e)
    stopped = threading.Event()
    def report(done, total, phase):
        if done != total:
            return
        if observer == 'error':
            raise RuntimeError('Observer failed after accepted receipts.')
        if observer == 'interrupt':
            raise KeyboardInterrupt()
        stopped.set()
    result = e.batches.resume(identity, cancelled=stopped.is_set, progress=report)
    assert result['status'] == 'complete'
    count = len(transport['calls'])
    assert e.batches.get(identity)['status'] == 'complete'
    assert e.batches.resume(identity, cancelled=lambda: True)['status'] == 'complete'
    assert len(transport['calls']) == count


@pytest.mark.parametrize('operation', ['resume', 'export'])
def test_confirmed_completion_survives_batch_lock_exit_error(batch_env, tmp_path, monkeypatch, operation):
    e = batch_env
    identity, _ = prepared(e)
    if operation == 'export':
        assert e.batches.resume(identity)['status'] == 'complete'
    original_lock = e.batches.jobs.lock
    entered = []
    @contextmanager
    def release_error(value):
        with original_lock(value):
            entered.append(value)
            yield
        raise OSError('Actual lock was released; optional close report failed.')
    monkeypatch.setattr(e.batches.jobs, 'lock', release_error)
    if operation == 'resume':
        result = e.batches.resume(identity)
    else:
        output = tmp_path / 'acknowledged-package.zip'
        result = e.batches.export(identity, output)
        assert result['sha256'] == sha256(output.read_bytes()).hexdigest()
        with ZipFile(output) as archive:
            assert len(archive.namelist()) == 11
    assert entered == [identity] and result['status'] == 'complete'
    assert 'owner' in result['cleanup_warning']
    assert e.batches.get(identity)['status'] == 'complete'


def test_cancel_after_real_binary_writer_acknowledgement_does_not_revoke_zip(batch_env, tmp_path, monkeypatch):
    from carr_library.assistant import translation_batch as module
    e = batch_env
    identity, _ = prepared(e)
    e.batches.resume(identity)
    original = module.write_binary_export
    stopped = threading.Event()
    def publish(*args, **kwargs):
        result = original(*args, **kwargs)
        stopped.set()
        return result
    monkeypatch.setattr(module, 'write_binary_export', publish)
    output = tmp_path / 'late-stop.zip'
    result = e.batches.export(identity, output, cancelled=stopped.is_set)
    assert stopped.is_set() and result['status'] == 'complete'
    assert result['sha256'] == sha256(output.read_bytes()).hexdigest()


def test_stage_cleanup_failure_precedes_any_package_publication(batch_env, tmp_path, monkeypatch):
    from carr_library.assistant import translation_batch as module
    e = batch_env
    identity, _ = prepared(e)
    e.batches.resume(identity)
    original = module.TemporaryDirectory
    class FailingCleanup(original):
        def cleanup(self):
            super().cleanup()
            raise OSError('Actual temporary children were removed; cleanup could not be confirmed.')
    monkeypatch.setattr(module, 'TemporaryDirectory', FailingCleanup)
    output = tmp_path / 'not-published.zip'
    with pytest.raises(OSError, match='cleanup'):
        e.batches.export(identity, output)
    assert not output.exists()
    assert not list(tmp_path.glob('.pending-translation-batch-*'))
    assert e.batches.get(identity)['status'] == 'complete'


def test_queued_export_rejects_replaced_destination_directory(batch_env, tmp_path):
    from carr_library.research.export_file import capture_export_destination
    e = batch_env
    identity, _ = prepared(e)
    e.batches.resume(identity)
    chosen = tmp_path / 'chosen'
    chosen.mkdir()
    output = chosen / 'approved.zip'
    approval = capture_export_destination(output, private_root=e.app.root)
    moved = tmp_path / 'moved-original'
    chosen.rename(moved)
    chosen.mkdir()
    with pytest.raises(ValueError, match='changed'):
        e.batches.export(identity, output, approval=approval)
    assert not output.exists() and not (moved / output.name).exists()


def test_actual_child_death_during_preparation_preserves_exact_templates_without_requests(
        batch_env, tmp_path, transport):
    import os
    import sys
    from carr_library.assistant import translation_batch as selected_module
    e = batch_env
    spec = tmp_path / 'explicit-child-input.json'
    spec.write_text(json.dumps(e.items, ensure_ascii=False), encoding='utf-8')
    script = r'''
import json, os, sys
from pathlib import Path
from carr_library.application import LibraryApp
from carr_library.assistant import translation_batch
from carr_library.assistant.translation import TranslationService
assert Path(translation_batch.__file__).resolve() == Path(sys.argv[3]).resolve()
app = LibraryApp(Path(sys.argv[1]))
service = TranslationService(app.root / 'state' / 'translation', library=app)
batches = translation_batch.TranslationBatchService(service)
items = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
review = batches.preview(items)
save = service.jobs.save
def stop_after_actual_child(value):
    save(value)
    os._exit(73)
service.jobs.save = stop_after_actual_child
batches.submit(items, review_sha256=review['review_sha256'], approve_remote=True)
raise AssertionError('The owned child must exit at the real saved-job boundary.')
'''
    result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script, str(e.app.root),
                             str(spec), str(selected_module.__file__)],
        capture_output=True, timeout=45, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    assert result.returncode == 73, result.stderr.decode('utf-8', 'replace')
    saved, = e.batches.list()
    assert saved['status'] == 'preparing'
    identity = saved['id']
    manifest_before = e.batches.jobs.path(identity).read_bytes()
    first = e.batches._load(identity)['members'][0]['job']['id']
    child_before = e.service.jobs.path(first).read_bytes()
    calls = len(transport['calls'])
    assert e.batches.prepare(identity)['status'] == 'queued'
    assert e.batches.jobs.path(identity).read_bytes() == manifest_before
    assert e.service.jobs.path(first).read_bytes() == child_before
    assert len(transport['calls']) == calls
