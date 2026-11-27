"""Retained-source composition with Advanced receipts, Tasks and explicit CLI.

These use real library/reader/ledger/backup services. Only provider HTTPS and
the named native-handler controls are synthetic; no live account is contacted.
"""
from dataclasses import asdict, replace
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
from time import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import translation_cli
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Authorization, Gateway
from carr_library.assistant.state import read_json
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library.library_backup import restore_backup
from carr_library.operations import OperationFacade, ResourceStateError
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.source import read_source_range


def _fixture_file(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Explicit known test files work with the installed importlib gate; no source
# package path is injected and no alternate production module is loaded.
basic = _fixture_file('source_translation_basic_fixtures', 'test_translation.py')
advanced = _fixture_file('source_translation_advanced_fixtures', 'test_translation_v3.py')
connection, translation_window = basic.connection, basic.translation_window
transport = advanced.transport


def advanced_source(tmp_path, transport):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Advanced retained source')
    topic = app.create_topic(workspace, 'Selected source topic')
    raw = ('Original Україна e\u0301 🙂 &amp; line.\n' * 190
           + '\fSecond retained section. ' * 1 + 'Original second paragraph. ' * 180)
    imported = app.add_text(workspace, 'Exact Advanced source', raw)
    desk = ReadingDesk(app, workspace, imported['edition'], topic=topic)
    source = read_source_range(desk, 1, 2)
    vault, profile = advanced.Vault(), advanced.profile()
    directory = app.root / 'state' / 'translation'
    service = TranslationService(directory, library=app,
        gateway=Gateway(directory / 'provider', vault=vault, mode='APPROVED_REMOTE'))
    approval = Authorization('source_languages', 'source_languages', ((profile.id, profile.endpoint),),
                             ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(profile, authorization=approval)
    return SimpleNamespace(app=app, workspace=workspace, topic=topic, imported=imported,
                           source=source, document=source.reading_document(), service=service,
                           profile=profile, vault=vault)


def submit_source(case, *, glossary=None):
    approval, cost = translation_approval(case.profile, case.source.text, 'en', 'uk', '2',
                                          glossary=glossary, source_bound=True)
    identity = case.service.submit(case.profile, case.source.text, 'en', 'uk',
        title=case.source.title, source_document=case.document, topic=case.topic,
        glossary=glossary, authorization=approval, per_call_cost=cost)
    return identity, approval


@pytest.mark.parametrize('glossary', [None, advanced.GLOSSARY], ids=['ordinary', 'exact-glossary'])
def test_advanced_source_pause_resume_and_export_keep_lineage_and_decoded_receipts(tmp_path, transport, glossary):
    case = advanced_source(tmp_path, transport)
    original = case.app.assets.resolve(case.imported['asset'])
    before = original.read_bytes()
    identity, approval = submit_source(case, glossary=glossary)
    assert case.service.get(identity)['kind'] == 'translation/2'
    stop = []
    paused = case.service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))
    assert paused['status'] == 'paused' and paused['completed'] == 1 < paused['total']
    accepted = case.service.get(identity)['receipts'][:]
    assert case.service.resume(identity)['status'] == 'complete'
    job = case.service.get(identity)
    assert job['receipts'][:1] == accepted and len(transport['calls']) == 1 + len(job['chunks'])
    usage = case.service.gateway.ledger.get(approval.id)['operations']
    for index, receipt in enumerate(job['receipts']):
        assert receipt['reported_model'] == advanced.PARENT + '/models/general/nmt'
        assert receipt['glossary_config'] == ({'glossary': glossary, 'ignoreCase': False} if glossary else None)
        assert usage[identity + '_' + str(index)]['usage']['translation_sha256'] == receipt['translation_sha256']
        assert receipt['translation_sha256'] == sha256(receipt['text'].encode('utf-8')).hexdigest()
        assert '&amp;' in receipt['text']  # Advanced text/plain is never HTML-unescaped.
    target = tmp_path / 'derived'
    result = case.service.export(identity, target)
    reading = ReadingDocument.load(target / 'translation.reading.json')
    report = read_json(target / 'translation-provenance.json')
    assert result['status'] == 'complete' and reading.source_anchor == case.document.source_anchor
    assert reading.source_text == case.source.text and reading.text != reading.source_text
    assert report['decoded_text_binding'] == 'usage-ledger-sha256'
    assert report['provider_endpoint'] == advanced.ENDPOINT and report['glossary'] == glossary
    assert original.read_bytes() == before and not report['quotation_authority']
    reading.undo()
    assert reading.text == case.source.text


def test_advanced_source_self_hash_cannot_replace_the_accepted_decoded_output(tmp_path, transport):
    case = advanced_source(tmp_path, transport)
    identity, _ = submit_source(case, glossary=advanced.GLOSSARY)
    case.service.resume(identity)
    job = case.service.jobs.load(identity)
    job['receipts'][0]['text'] += ' Forged derivative.'
    job['receipts'][0]['translation_sha256'] = sha256(job['receipts'][0]['text'].encode()).hexdigest()
    case.service.jobs.save(job)
    calls = len(transport['calls'])
    with pytest.raises(ValueError, match='decoded-output receipt'):
        case.service.export(identity, tmp_path / 'forged')
    assert not (tmp_path / 'forged').exists() and len(transport['calls']) == calls
    view = OperationFacade(case.app).get('translation', identity)
    assert view.status == 'complete' and 'Stored translation checkpoint' in view.phase
    assert view.controls == ()  # A stored status never upgrades damaged output to verified text.


@pytest.mark.parametrize('status', ['queued', 'paused', 'complete'])
def test_tasks_reads_source_job_metadata_without_provider_or_resume_authority(tmp_path, connection, status):
    case = basic.bound_ready(tmp_path, connection)
    identity, _ = submit_source(case)
    if status != 'queued':
        stop = []
        case.service.resume(identity, cancelled=lambda: bool(stop),
            progress=(lambda *args: stop.append(True)) if status == 'paused' else lambda *args: None)
    job = case.service.get(identity)
    calls, secrets = len(connection['calls']), len(case.vault.calls)
    facade = OperationFacade(case.app, translation=case.service)
    view = facade.get('translation', identity, workspace=case.workspace)
    assert view.status == status and view.workspace == case.workspace
    assert (view.completed, view.total) == (len(job['receipts']), len(job['chunks']))
    assert view.controls == () and view.active_attempt_id is None
    assert facade.list(workspace=case.workspace) == (view,)
    other = case.app.create_workspace('Unrelated source')
    assert facade.list(workspace=other) == ()
    with pytest.raises(PermissionError, match='another selected library'):
        facade.get('translation', identity, workspace=other)
    for action in ('resume', 'pause', 'cancel'):
        with pytest.raises(ValueError):
            facade.control('translation', identity, action, workspace=case.workspace)
    assert len(connection['calls']) == calls and len(case.vault.calls) == secrets
    serialized = json.dumps(asdict(view))
    assert case.source.text not in serialized and case.profile.credential_ref not in serialized


@pytest.mark.parametrize('mutation', ['unknown-kind', 'missing-anchor'])
def test_tasks_keeps_malformed_source_jobs_as_bounded_attention_rows(tmp_path, connection, mutation):
    case = basic.bound_ready(tmp_path, connection)
    identity, _ = submit_source(case)
    job = case.service.jobs.load(identity)
    if mutation == 'unknown-kind': job['kind'] = 'translation/unknown'
    else: del job['source_binding']['anchor']
    case.service.jobs.save(job)
    facade = OperationFacade(case.app)
    with pytest.raises(ResourceStateError):
        facade.get('translation', identity)
    view, = facade.list()
    assert view.status == 'attention' and view.completed is view.total is None and view.controls == ()
    assert len(connection['calls']) == 1
    assert case.source.text not in json.dumps(asdict(view))


@pytest.mark.parametrize('change', ['unchanged', 'missing', 'disabled', 'changed', 'after-gateway'])
def test_cli_source_resume_uses_explicit_library_and_fresh_exact_account(tmp_path, connection, monkeypatch, capsys, change):
    case = basic.bound_ready(tmp_path, connection)
    identity, _ = submit_source(case)
    store = ProfileStore(case.app.root / 'state' / 'providers')
    store.save(case.profile)
    if change == 'missing': store.remove(case.profile.id)
    elif change == 'disabled': store.save(replace(case.profile, enabled=False))
    elif change == 'changed': store.save(replace(case.profile, name='Changed account'))
    gateways = []
    def gateway(directory, **kwargs):
        gateways.append(directory)
        if change == 'after-gateway': store.save(replace(case.profile, enabled=False))
        return Gateway(directory, vault=case.vault, **kwargs)
    monkeypatch.setattr(translation_cli, 'Gateway', gateway)
    status = translation_cli.main(['--data-dir', str(case.app.root), 'resume', identity])
    output = capsys.readouterr()
    job = case.service.jobs.load(identity)
    if change == 'unchanged':
        assert status == 0 and json.loads(output.out)['status'] == 'complete'
        assert len(job['receipts']) == len(job['chunks']) and len(connection['calls']) == len(job['chunks']) + 1
    else:
        assert status == 1 and 'account is unavailable, disabled or changed' in output.err
        assert not job['receipts'] and len(connection['calls']) == 1
        assert job['status'] == ('attention' if change == 'after-gateway' else 'queued')
    assert len(gateways) == int(change in ('unchanged', 'after-gateway'))


@pytest.mark.parametrize('adapter', ['basic', 'advanced'])
def test_completed_source_cli_replay_and_export_after_real_backup_delete_restore_are_offline(
        tmp_path, request, monkeypatch, capsys, adapter):
    provider = request.getfixturevalue('connection' if adapter == 'basic' else 'transport')
    case = basic.bound_ready(tmp_path, provider) if adapter == 'basic' else advanced_source(tmp_path, provider)
    identity, _ = submit_source(case, glossary=advanced.GLOSSARY if adapter == 'advanced' else None)
    case.service.resume(identity)
    saved = case.service.get(identity)
    backup = tmp_path / 'backup'
    digest = case.app.backup(backup)
    old_root = case.app.root
    shutil.rmtree(old_root)
    restored_root = restore_backup(backup, tmp_path / 'restored', digest)
    calls = len(provider['calls'])
    def no_remote_gateway(*args, **kwargs):
        raise AssertionError('Completed source work must not construct a remote gateway.')
    monkeypatch.setattr(translation_cli, 'Gateway', no_remote_gateway)
    base = ['--data-dir', str(restored_root)]
    assert translation_cli.main(base + ['resume', identity]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    output = tmp_path / 'restored-export'
    assert translation_cli.main(base + ['export', identity, '--output', str(output)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    reading = ReadingDocument.load(output / 'translation.reading.json')
    assert reading.source_text == case.source.text and reading.source_anchor == case.document.source_anchor
    assert reading.text != reading.source_text and not old_root.exists()
    restored = TranslationService(restored_root / 'state' / 'translation', library=LibraryApp(restored_root))
    assert restored.get(identity)['receipts'] == saved['receipts']
    assert len(provider['calls']) == calls


def test_advanced_source_native_handlers_preserve_glossary_when_reopening_saved_work(
        tmp_path, transport, translation_window):
    case = advanced_source(tmp_path, transport)
    window = translation_window
    frame = window.bind(case)
    def scenario():
        selected = window.find('Selected text')
        assert selected.GetValue() == case.source.text and not selected.editable
        window.find('Source language').ChangeValue('en')
        window.find('Target language').ChangeValue('uk')
        window.find('Advanced v3 glossary').ChangeValue(advanced.GLOSSARY)
        window.find('Maximum approved').ChangeValue('2')
        window.click('Preview'); window.click('Translate / resume')
        job, = case.service.list()
        assert job['kind'] == 'translation/2' and job['status'] == 'complete'
        assert job['glossary'] == advanced.GLOSSARY
        window.click('Saved jobs')
        glossary = window.find('Advanced v3 glossary')
        assert glossary.GetValue() == advanced.GLOSSARY and not glossary.enabled
        calls = len(transport['calls'])
        glossary.ChangeValue(advanced.GLOSSARY + '-changed')  # Deliberate stale/forged UI snapshot.
        window.click('Translate / resume')
        assert 'Visible settings differ' in window.find('Preview, supported').GetValue()
        assert len(transport['calls']) == calls and case.service.get(job['id']) == job
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame, source_loader=lambda cancelled: case.source,
                               source_topic=case.topic)
    assert not window.errors
