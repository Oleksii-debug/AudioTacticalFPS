"""Real canonical library/revision/backup checks with synthetic provider replies."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import runpy
import threading
from time import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import translation_cli
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Authorization, Gateway, ProviderError
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library.assistant.translation_source import source_options
from carr_library.library_backup import restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.source import read_source_range

from test_translation_v3 import profile, Vault, transport, translation_handlers, prepare_handler


@pytest.fixture
def source_library(tmp_path, transport):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit translation source')
    topic = app.create_topic(workspace, 'Selected topic')
    original = 'Original first paragraph. ' * 240 + '\fSecond source segment.\r\nУкраїна.'
    imported = app.add_text(workspace, 'Exact source title', original)
    p, vault = profile(), Vault()
    ProfileStore(app.root / 'state' / 'providers').save(p)
    service = TranslationService(app.root / 'state' / 'translation', library=app,
        gateway=Gateway(app.root / 'state' / 'translation' / 'provider', vault=vault, mode='APPROVED_REMOTE'))
    approval = Authorization('source_discovery', 'source_discovery', ((p.id, p.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(p, authorization=approval)
    return SimpleNamespace(app=app, workspace=workspace, topic=topic, original=original,
        imported=imported, p=p, vault=vault, service=service)


def select(e, **kwargs):
    return e.service.select_source(e.workspace, e.imported['edition'], topic=e.topic, **kwargs)


def arguments(selected):
    return {key: selected[key] for key in ('source_anchor', 'source_metadata', 'source_topic')}


def submit(e, selected):
    approval, cost = translation_approval(e.p, selected['text'], 'en', 'uk', '1', **arguments(selected))
    identity = e.service.submit(e.p, selected['text'], 'en', 'uk', title=selected['title'],
        authorization=approval, per_call_cost=cost, **arguments(selected))
    return identity, approval


def test_document_and_range_use_exact_canonical_source_and_export_lineage(source_library, tmp_path, transport):
    e = source_library
    selected = select(e)
    desk = ReadingDesk(e.app, e.workspace, e.imported['edition'], topic=e.topic)
    original = read_source_range(desk, 1, len(desk.pages)).reading_document()
    assert selected['source_anchor'] == original.source_anchor
    assert selected['text'] == original.source_text
    assert selected['source_metadata'] == asdict(desk.metadata)
    second = select(e, first=2, last=2)
    assert second['text'] == e.original.split('\f')[1]
    assert second['source_anchor']['positions'][0]['ordinal'] == 2
    identity, approval = submit(e, selected)
    assert approval.task_id.startswith(approval.id + ':') and len(approval.task_id) == 97
    assert e.service.resume(identity)['status'] == 'complete'
    before = e.service.jobs.path(identity).read_bytes()
    count = len(transport['calls'])
    destination = tmp_path / 'bound-export'
    e.service.export(identity, destination)
    saved = ReadingDocument.load(destination / 'translation.reading.json')
    assert saved.source_anchor == original.source_anchor
    assert saved.source_text == original.source_text and not saved.source_span(0, len(saved.text)).exact
    report = json.loads((destination / 'translation-provenance.json').read_bytes())
    assert report['source_anchor'] == original.source_anchor
    assert report['source_metadata'] == asdict(desk.metadata) and report['source_topic'] == e.topic
    assert report['quotation_authority'] is False
    assert e.app.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode('utf-8')
    assert e.service.jobs.path(identity).read_bytes() == before
    assert len(transport['calls']) == count


def test_actual_epub_chapter_is_selected_in_spine_order(source_library, tmp_path):
    e = source_library
    source = tmp_path / 'chapters.epub'
    factory = runpy.run_path(str(Path(__file__).parents[1] / 'reader' / 'test_ebook_reader.py'))['book']
    factory(source)
    imported = e.app.import_file(e.workspace, source, title='Reviewed EPUB', authors=('Source author',))
    assert imported['state'] == 'complete'
    selected = e.service.select_source(e.workspace, imported['edition'], topic=e.topic, chapter=2)
    assert 'Archive context.' in selected['text'] and 'Railway research.' not in selected['text']
    assert selected['source_anchor']['positions'][0]['ordinal'] == 2
    assert selected['source_anchor']['quotation_authority'] is False
    labels = source_options(e.app, e.workspace, imported['edition'], topic=e.topic)
    assert labels['positions'][1].startswith('EPUB chapter 2:')
    with pytest.raises(ValueError):
        e.service.select_source(e.workspace, imported['edition'], chapter=3)
    identity, _ = submit(e, selected)
    assert e.service.resume(identity)['status'] == 'complete'
    assert e.app.assets.resolve(selected['source_anchor']['asset_sha256']).read_bytes() == source.read_bytes()


@pytest.mark.parametrize('change', ['revision', 'workspace', 'topic', 'original'])
def test_changed_source_cannot_start_or_mutate_queued_job(source_library, transport, change):
    e = source_library
    identity, _ = submit(e, select(e))
    before, count = e.service.jobs.path(identity).read_bytes(), len(transport['calls'])
    if change == 'revision':
        e.app.update_metadata(e.workspace, e.imported['edition'], title='New revision')
    elif change == 'workspace':
        e.app.archive_workspace(e.workspace)
    elif change == 'topic':
        with e.app.topic_transaction(e.workspace) as tree:
            tree.archive(e.topic)
    else:
        e.app.assets.resolve(e.imported['asset']).write_bytes(b'Changed retained original')
    with pytest.raises((ValueError, PermissionError)):
        e.service.resume(identity)
    assert e.service.jobs.path(identity).read_bytes() == before
    assert len(transport['calls']) == count


def test_source_change_during_response_is_uncertain_and_cannot_publish_or_retry(source_library, transport):
    e = source_library
    identity, approval = submit(e, select(e, first=2, last=2))
    def revoke(document):
        e.app.archive_workspace(e.workspace)
        return document
    transport['response'] = revoke
    with pytest.raises(PermissionError, match='source or scope changed'):
        e.service.resume(identity)
    job = e.service.jobs.load(identity)
    assert job['status'] == 'attention' and job['receipts'] == []
    recorded = e.service.gateway.ledger.get(approval.id)['operations'][identity + '_0']
    assert recorded['status'] == 'uncertain' and recorded['output_sha256'] is None
    e.app.restore_workspace(e.workspace)
    transport['response'] = None
    before = len(transport['calls'])
    with pytest.raises(ProviderError, match='already been attempted'):
        e.service.resume(identity)
    assert len(transport['calls']) == before
    assert not e.service.jobs.load(identity)['receipts']


def test_source_revocation_while_serializing_never_publishes_export(source_library, tmp_path, monkeypatch):
    e = source_library
    identity, _ = submit(e, select(e, first=2, last=2))
    assert e.service.resume(identity)['status'] == 'complete'
    export = ReadingDocument.export
    def revoke(document, path, **kwargs):
        result = export(document, path, **kwargs)
        if not kwargs.get('original'):
            e.app.archive_workspace(e.workspace)
        return result
    monkeypatch.setattr(ReadingDocument, 'export', revoke)
    destination = tmp_path / 'revoked-export'
    with pytest.raises(PermissionError):
        e.service.export(identity, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob('.pending-translation-export-*'))


@pytest.mark.parametrize('change', ['metadata', 'topic', 'anchor', 'remove-binding', 'task'])
def test_source_binding_cannot_be_relabelled_after_completion(source_library, transport, tmp_path, change):
    e = source_library
    identity, _ = submit(e, select(e, first=2, last=2))
    e.service.resume(identity)
    job = e.service.jobs.load(identity)
    if change == 'metadata':
        job['source_metadata']['title'] = 'Different source'
    elif change == 'topic':
        job['source_topic'] = None
    elif change == 'anchor':
        job['source_anchor']['edition'] = 'another-edition'
    elif change == 'remove-binding':
        for key in ('source_anchor', 'source_metadata', 'source_topic'):
            del job[key]
    else:
        job['authorization']['task_id'] = job['authorization']['id']
    e.service.jobs.save(job)
    before, count = e.service.jobs.path(identity).read_bytes(), len(transport['calls'])
    with pytest.raises(ValueError):
        e.service.export(identity, tmp_path / 'relabelled')
    assert e.service.jobs.path(identity).read_bytes() == before
    assert len(transport['calls']) == count and not (tmp_path / 'relabelled').exists()


def test_paused_source_job_restores_to_new_root_and_reuses_accepted_chunks(source_library, tmp_path, transport):
    e = source_library
    selected = select(e)
    identity, approval = submit(e, selected)
    stop = []
    result = e.service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *unused: stop.append(True))
    assert result['status'] == 'paused' and result['completed'] == 1 and result['total'] > 1
    saved = e.service.jobs.load(identity)
    folder = tmp_path / 'backup'
    receipt = e.app.backup(folder)
    destination = restore_backup(folder, tmp_path / 'restored', receipt)
    app = LibraryApp(destination)
    reopened = TranslationService(app.root / 'state' / 'translation', library=app,
        gateway=Gateway(app.root / 'state' / 'translation' / 'provider', vault=e.vault, mode='APPROVED_REMOTE'))
    assert reopened.jobs.load(identity) == saved
    before = len(transport['calls'])
    complete = reopened.resume(identity)
    assert complete['status'] == 'complete'
    assert len(transport['calls']) - before == complete['total'] - 1
    assert reopened.jobs.load(identity)['receipts'][0] == saved['receipts'][0]
    assert reopened.jobs.load(identity)['authorization']['task_id'] == approval.task_id
    reopened.export(identity, tmp_path / 'restored-export')
    document = ReadingDocument.load(tmp_path / 'restored-export/translation.reading.json')
    assert document.source_anchor == selected['source_anchor']
    assert app.assets.resolve(e.imported['asset']).read_bytes() == e.original.encode('utf-8')


def test_cli_library_review_binds_exact_source_revision_before_submit(source_library, monkeypatch, capsys):
    e = source_library
    monkeypatch.setattr(translation_cli, 'Gateway',
        lambda directory, **kwargs: Gateway(directory, vault=e.vault, **kwargs))
    args = ['--data-dir', str(e.app.root)]
    selected = ['--profile', e.p.id, '--edition', e.imported['edition'], '--workspace', e.workspace,
        '--topic', e.topic, '--first', '2', '--last', '2', '--source', 'en', '--target', 'uk', '--budget', '1']
    assert translation_cli.main(args + ['preview'] + selected) == 0
    review = json.loads(capsys.readouterr().out)
    assert review['source_anchor']['edition'] == e.imported['edition']
    assert review['source_metadata']['title'] == 'Exact source title'
    e.app.update_metadata(e.workspace, e.imported['edition'], title='Changed metadata revision')
    assert translation_cli.main(args + ['submit'] + selected +
        ['--approve-remote', '--review-sha256', review['review_sha256']]) == 1
    assert 'current preview hash' in capsys.readouterr().err
    assert not e.service.list()


def native_source(e, monkeypatch):
    import sys
    app = e.frame.library
    workspace = app.create_workspace('Native source choice')
    source = app.add_text(workspace, 'Selected book', 'Exact selected source.\fOther source position.')
    e.frame.active = (workspace, None)
    panel = SimpleNamespace(workspace=workspace, ready=True, rows={source['edition']: object()},
                            _selection=lambda: (None, None, source['edition']))
    e.frame.book = SimpleNamespace(GetPage=lambda index: panel)
    wx = sys.modules['wx']
    class Picker:
        def __init__(self, *args, **kwargs): pass
        def ShowModal(self): return wx.ID_OK
        def GetSelection(self): return 2  # The actual second source position.
        def Destroy(self): pass
    monkeypatch.setattr(wx, 'SingleChoiceDialog', Picker, raising=False)
    return source


def load_native_source(e):
    prepare_handler(e)
    e.press('Load library source')
    assert len(e.state['queue']) == 1
    e.complete()
    assert len(e.state['queue']) == 1
    e.complete()


def test_native_source_choice_uses_library_snapshot_and_explicit_transfer(translation_handlers, transport, monkeypatch):
    e = translation_handlers
    source = native_source(e, monkeypatch)
    def scenario():
        load_native_source(e)
        assert e.find('Selected text; originals remain unchanged').value == 'Other source position.'
        assert not e.service.list() and len(transport['calls']) == 1
        e.press('Preview')
        e.complete()
        assert source['edition'] in e.find('Preview, supported languages and job status').value
        e.press('Translate / resume')
        assert len(transport['calls']) == 1
        e.complete()
        queued, = e.service.list()
        assert queued['status'] == 'queued' and len(transport['calls']) == 1
        e.complete()
        job, = e.service.list()
        assert job['status'] == 'complete'
        assert job['source_anchor']['positions'][0]['ordinal'] == 2
        assert job['source_metadata']['title'] == 'Selected book'
        assert len(transport['calls']) == 2
    e.scenario(scenario)


def test_source_preview_and_submission_validate_on_worker_using_frozen_fields(
        translation_handlers, transport, monkeypatch):
    e = translation_handlers
    source = native_source(e, monkeypatch)
    ui_thread = threading.get_ident()
    validations = []
    original = TranslationService._validate_source
    def validate(service, job, **kwargs):
        if job.get('source_anchor') is not None:
            validations.append(threading.get_ident())
            assert validations[-1] != ui_thread, 'Source verification blocked the UI thread.'
        return original(service, job, **kwargs)
    monkeypatch.setattr(TranslationService, '_validate_source', validate)
    def complete_on_worker():
        callback, done, failed = e.state['queue'].pop(0)
        outcome = []
        def work():
            try: outcome.append((done, callback()))
            except Exception as error: outcome.append((failed, error))
        worker = threading.Thread(target=work)
        worker.start(); worker.join(10)
        assert not worker.is_alive() and len(outcome) == 1
        handler, result = outcome[0]
        handler(result)
    def scenario():
        load_native_source(e)
        widget_type = type(e.find('Selected text; originals remain unchanged'))
        get_value = widget_type.GetValue
        def read_on_ui(widget):
            assert threading.get_ident() == ui_thread, 'The worker read a live wx control.'
            return get_value(widget)
        monkeypatch.setattr(widget_type, 'GetValue', read_on_ui)
        e.press('Preview')
        assert validations == [] and len(e.state['queue']) == 1
        assert not e.find('Translate / resume').enabled
        complete_on_worker()
        assert len(validations) == 1 and e.find('Translate / resume').enabled
        e.press('Translate / resume')
        assert len(validations) == 1 and len(e.state['queue']) == 1
        assert len(e.state['approvals']) == 2 and not e.service.list()
        # Even a programmatic widget change cannot alter the captured approval.
        e.find('Target language code').ChangeValue('de')
        e.find('Selected text; originals remain unchanged').ChangeValue('Unreviewed replacement')
        complete_on_worker()
        queued, = e.service.list()
        assert queued['status'] == 'queued' and len(transport['calls']) == 1
        assert queued['source_text'] == 'Other source position.' and queued['target_language'] == 'uk'
        assert queued['source_anchor']['edition'] == source['edition']
        assert len(e.state['queue']) == 1
        complete_on_worker()
        assert e.service.list()[0]['status'] == 'complete' and len(transport['calls']) == 2
        assert all(identity != ui_thread for identity in validations)
    e.scenario(scenario)


@pytest.mark.parametrize('phase', ['preview', 'submit'])
def test_source_review_and_submission_can_cancel_during_real_validation(
        translation_handlers, transport, monkeypatch, phase):
    e = translation_handlers
    native_source(e, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = TranslationService._validate_source
    def scenario():
        load_native_source(e)
        if phase == 'submit':
            e.press('Preview'); e.complete()
        def validate(service, job, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(service, job, **kwargs)
        monkeypatch.setattr(TranslationService, '_validate_source', validate)
        e.press('Preview' if phase == 'preview' else 'Translate / resume')
        assert not entered.is_set() and len(e.state['queue']) == 1
        callback, done, failed = e.state['queue'].pop(0)
        outcome = []
        def work():
            try: outcome.append((done, callback()))
            except Exception as error: outcome.append((failed, error))
        worker = threading.Thread(target=work)
        worker.start()
        try:
            assert entered.wait(5)
            assert e.find('Pause').enabled
            e.press('Pause')
        finally:
            release.set()
            worker.join(10)
        assert not worker.is_alive() and len(outcome) == 1
        handler, result = outcome[0]
        assert isinstance(result, InterruptedError)
        handler(result)
        assert not e.service.list() and not e.state['queue'] and len(transport['calls']) == 1
        assert e.find('Preview').enabled
    e.scenario(scenario)


def test_pause_after_source_submission_before_ui_completion_does_not_resume(
        translation_handlers, transport, monkeypatch):
    e = translation_handlers
    native_source(e, monkeypatch)
    def scenario():
        load_native_source(e)
        e.press('Preview'); e.complete()
        e.press('Translate / resume')
        callback, done, failed = e.state['queue'].pop(0)
        identity = callback()
        e.press('Pause')
        done(identity)
        queued, = e.service.list()
        assert queued['id'] == identity and queued['status'] == 'queued'
        assert not e.state['queue'] and len(transport['calls']) == 1
        assert 'Resume explicitly' in e.find('Preview, supported languages and job status').value
    e.scenario(scenario)


def test_source_revocation_after_connection_before_http_is_not_sent_and_exactly_resumable(
        source_library, transport, monkeypatch):
    from carr_library.assistant import providers
    e = source_library
    identity, approval = submit(e, select(e, first=2, last=2))
    factory = providers._Connection
    once = [True]
    def opened(*args, **kwargs):
        connection = factory(*args, **kwargs)
        if once.pop() if once else False:
            e.app.archive_workspace(e.workspace)
        return connection
    monkeypatch.setattr(providers, '_Connection', opened)
    with pytest.raises(PermissionError):
        e.service.resume(identity)
    assert len(transport['calls']) == 1  # Only the preceding language discovery.
    row = e.service.gateway.ledger.get(approval.id)['operations'][identity + '_0']
    assert row['status'] == 'not_sent' and row['output_sha256'] is None
    assert not e.service.jobs.load(identity)['receipts']
    e.app.restore_workspace(e.workspace)
    assert e.service.resume(identity)['status'] == 'complete'
    assert len(transport['calls']) == 2
    assert e.service.gateway.ledger.get(approval.id)['operations'][identity + '_0']['status'] == 'complete'


def test_direct_translation_module_obeys_the_existing_installation_lease(
        source_library, tmp_path, monkeypatch, capsys, transport):
    import sys
    from carr_library.installation_lease import installation_lease
    e = source_library
    installation = tmp_path / 'managed install'
    environment = installation / 'app' / '.venv'
    environment.mkdir(parents=True)
    (installation / 'managed-install.ini').write_text('synthetic managed installation', encoding='utf-8')
    (environment / '.installation-complete').write_text('complete', encoding='utf-8')
    monkeypatch.setattr(sys, 'prefix', str(environment))
    count = len(transport['calls'])
    args = ['--data-dir', str(e.app.root), 'profiles']
    with installation_lease(installation, exclusive=True):
        assert translation_cli.main(args) == 1
        assert 'Close Resonance' in capsys.readouterr().err
    assert len(transport['calls']) == count
    assert translation_cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)[0]['id'] == e.p.id
