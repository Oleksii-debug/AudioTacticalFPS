"""Actual library/jobs/ledger/backup batches with synthetic provider transport."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
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

def _fixture_module(name, filename):
    # Load only this selected sibling fixture file. Installed-package tests must
    # not add either the tests directory or the checkout to sys.path.
    path = Path(__file__).with_name(filename).resolve()
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(existing.__file__).resolve() != path:
            raise ImportError('Fixture module name is already bound to another file.')
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError('Could not load the selected sibling fixture file.')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(name) is module:
            del sys.modules[name]
        raise
    return module


_source_fixtures = _fixture_module(
    '_carr_fixture_translation_source', 'test_translation_source.py')
_google_fixtures = _fixture_module(
    '_carr_fixture_translation_v3', 'test_translation_v3.py')
source_library = _source_fixtures.source_library
select = _source_fixtures.select
transport = _google_fixtures.transport
GLOSSARY = _google_fixtures.GLOSSARY
TOKEN = _google_fixtures.TOKEN


def item(e, selected, **changes):
    return {'profile': e.p.to_dict(), 'text': selected['text'], 'title': selected['title'],
        'source': 'en', 'target': 'uk', 'budget': '1',
        **{key: selected[key] for key in ('source_anchor', 'source_metadata', 'source_topic')},
        **changes}


@pytest.fixture
def batch_env(source_library):
    e = source_library
    e.second = e.app.add_text(e.workspace, 'Second independently selected source',
                              'Other original text. Україна.\n')
    e.selected = [select(e), e.service.select_source(e.workspace, e.second['edition'], topic=e.topic)]
    e.items = [item(e, e.selected[0]),
               item(e, e.selected[1], target='fr', glossary=GLOSSARY)]
    e.batches = TranslationBatchService(e.service)
    return e


def prepared(e):
    review = e.batches.preview(e.items, title='Reviewed document order')
    identity = e.batches.submit(e.items, title='Reviewed document order',
                               review_sha256=review['review_sha256'], approve_remote=True)
    return identity, e.service.jobs.load(identity)


def reseal(value):
    value['manifest_sha256'] = sha256(canonical({key: item for key, item in value.items()
        if key not in ('updated', 'manifest_sha256')}).encode('utf-8')).hexdigest()


def test_preview_preparation_reuses_one_store_and_makes_no_remote_call(batch_env, transport):
    e = batch_env
    before = len(transport['calls']), len(e.vault.calls)
    identity, manifest = prepared(e)
    assert manifest['kind'] == KIND and validate_batch(manifest, identity) is manifest
    assert manifest['review']['documents'] == 2
    assert manifest['review']['characters'] == sum(len(value['text']) for value in e.selected)
    assert Decimal(manifest['review']['budget']) == Decimal('2')
    assert manifest['review']['items'][0]['selected_positions'] == 2
    assert manifest['review']['items'][1]['glossary'] == GLOSSARY
    assert e.batches.jobs is e.service.jobs
    assert len(e.service.jobs.identities()) == 3 and len(e.service.list()) == 2
    for index, member in enumerate(manifest['members']):
        job = e.service._load(member['job']['id'])
        assert job['id'] == member_id(identity, manifest['review_sha256'], index)
        assert job['status'] == 'queued' and job['receipts'] == []
        assert job['source_text'] == e.selected[index]['text']
        assert job['authorization']['task_id'].startswith(job['authorization']['id'] + ':')
        assert 'batch' not in job and job['kind'] == 'translation/1'
    assert (len(transport['calls']), len(e.vault.calls)) == before
    assert e.batches.get(identity)['status'] == 'queued'
    with pytest.raises(ValueError):
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
        if value.get('kind') == 'translation/1':
            stop.set()

    monkeypatch.setattr(e.service.jobs, 'save', save_then_stop)
    identity = e.batches.submit(e.items, review_sha256=review['review_sha256'],
                               approve_remote=True, cancelled=stop.is_set)
    manifest = e.service.jobs.load(identity)
    assert e.batches.get(identity)['status'] == 'preparing'
    assert len(e.service.jobs.identities()) == 2
    retained = e.service.jobs.path(identity).read_bytes()
    child = manifest['members'][0]['job']['id']
    accepted_template = e.service.jobs.path(child).read_bytes()
    stop.clear()
    monkeypatch.setattr(e.service.jobs, 'save', save)
    assert e.batches.prepare(identity)['status'] == 'queued'
    assert len(e.service.jobs.identities()) == 3
    assert e.service.jobs.path(identity).read_bytes() == retained
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
        e.service.jobs.save(manifest)
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
    with pytest.raises(PermissionError, match='source or scope changed'):
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
    destination = tmp_path / 'reviewed-batch-export'
    result = e.batches.export(identity, destination)
    assert result['status'] == 'complete' and len(result['items']) == 2
    published = json.loads((destination / 'batch-provenance.json').read_bytes())
    assert published['review_sha256'] == manifest['review_sha256']
    assert published['quotation_authority'] is False
    for selected, row in zip(e.selected, published['items']):
        output = destination / row['directory']
        reading = ReadingDocument.load(output / 'translation.reading.json')
        assert reading.source_text == selected['text']
        assert reading.source_anchor == selected['source_anchor']
        assert (output / 'original.txt').read_bytes() == selected['text'].encode('utf-8')
        assert sha256((output / 'translation-provenance.json').read_bytes()).hexdigest() == row['provenance_sha256']
        assert e.app.assets.resolve(selected['source_anchor']['asset_sha256']).is_file()
    with pytest.raises(FileExistsError):
        e.batches.export(identity, destination)
    assert len(transport['calls']) == before


def test_export_source_revocation_before_publication_leaves_no_visible_batch(batch_env, tmp_path, monkeypatch):
    e = batch_env
    identity, _ = prepared(e)
    e.batches.resume(identity)
    serialize = e.service._export_into
    def revoke_after_serializing(job, target):
        result = serialize(job, target)
        e.app.archive_workspace(e.workspace)
        return result
    monkeypatch.setattr(e.service, '_export_into', revoke_after_serializing)
    destination = tmp_path / 'revoked-batch-export'
    with pytest.raises(PermissionError):
        e.batches.export(identity, destination)
    assert not destination.exists() and not list(tmp_path.glob('.pending-translation-batch-*'))


@pytest.mark.parametrize('phase', ['preparing', 'paused'])
def test_actual_backup_delete_original_restore_reuses_fixed_jobs_and_receipts(
        batch_env, tmp_path, monkeypatch, transport, phase):
    e = batch_env
    identity, manifest = prepared(e)
    if phase == 'preparing':
        e.service.jobs.path(manifest['members'][1]['job']['id']).unlink()
    else:
        stop = threading.Event()
        assert e.batches.resume(identity, cancelled=stop.is_set,
            progress=lambda *unused: stop.set())['status'] == 'paused'
    before = e.batches.get(identity)
    templates = deepcopy(manifest)
    folder = tmp_path / 'batch-backup'
    def forbidden(*args, **kwargs):
        pytest.fail('Passive backup must not activate a provider, parser or worker.')
    with monkeypatch.context() as guarded:
        guarded.setattr(Gateway, 'request', forbidden)
        guarded.setattr(subprocess, 'Popen', forbidden)
        checksum = e.app.backup(folder)
        shutil.rmtree(e.app.root)
        restored = restore_backup(folder, tmp_path / 'restored-batch-library', checksum)
    app = LibraryApp(restored)
    service = TranslationService(app.root / 'state' / 'translation', library=app,
        gateway=Gateway(app.root / 'state' / 'translation' / 'provider', vault=e.vault, mode='APPROVED_REMOTE'))
    batch = TranslationBatchService(service)
    assert service.jobs.load(identity) == templates
    assert batch.get(identity) == before
    calls = len(transport['calls'])
    assert batch.resume(identity)['status'] == 'complete'
    assert len(transport['calls']) - calls == manifest['review']['calls'] - before['completed_chunks']
    assert len(service.jobs.identities()) == 3
    assert all(TOKEN not in path.read_text(encoding='utf-8') for path in
               (app.root / 'state' / 'translation').rglob('*.json'))
    batch.export(identity, tmp_path / 'restored-batch-output')


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


def test_dashboard_reads_batch_without_new_execution_controls(batch_env, transport):
    e = batch_env
    identity, manifest = prepared(e)
    before = len(transport['calls']), len(e.vault.calls)
    rows = OperationFacade(e.app).list(workspace=e.workspace)
    selected = next(row for row in rows if row.kind == 'translation' and row.id == identity)
    assert selected.status == 'queued' and selected.completed == 0 and selected.total == 2
    assert selected.controls == () and selected.workspace == e.workspace
    assert 'child document jobs' in selected.phase
    assert (len(transport['calls']), len(e.vault.calls)) == before
    assert len([row for row in rows if row.kind == 'translation']) == 3


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
    assert e.service.jobs.load(identity) == manifest and len(transport['calls']) == before
