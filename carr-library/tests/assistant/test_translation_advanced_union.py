"""Advanced UI/CLI boundaries on real retained-source jobs and the shared ledger.

Reuse the existing source/HTTPS and native-control fixtures. HTTPS responses and
widgets are synthetic; authorization, source reads, jobs and exports are real.
"""
from dataclasses import replace
from hashlib import sha256
import io
import json
from pathlib import Path
import runpy
import signal
import sys

import pytest

from carr_library import __main__ as entry
from carr_library.assistant import translation_cli as cli
from carr_library.assistant.profiles import ProfileStore
from carr_library.speech.reading import ReadingDocument


_tests = Path(__file__).parent
_source_cases = runpy.run_path(str(_tests / 'test_translation_advanced_source.py'))
advanced_source = _source_cases['advanced_source']
submit_source = _source_cases['submit']
GLOSSARY = _source_cases['GLOSSARY']
translation_window = runpy.run_path(str(_tests / 'test_translation.py'))['translation_window']


def _store(case):
    store = ProfileStore(case.app.root / 'state' / 'providers')
    store.save(case.profile)
    return store


def _change(store, profile, mutation):
    if mutation == 'removed':
        store.remove(profile.id)
    else:
        store.save(replace(profile, name='Changed after explicit review'))


def _cli_gateway(monkeypatch, case):
    calls = []
    def gateway(*args, **kwargs):
        calls.append(True)
        return case.service.gateway
    monkeypatch.setattr(cli, 'Gateway', gateway)
    return calls


def test_advanced_source_ui_preserves_glossary_review_saved_scope_and_offline_export(
        advanced_source, translation_window, tmp_path):
    case, window = advanced_source, translation_window
    frame = window.bind(case)
    original = case.app.assets.resolve(case.imported['asset'])
    original_digest = sha256(original.read_bytes()).hexdigest()

    def scenario():
        selected = window.find('Selected text')
        assert not selected.editable and selected.value == case.source.text
        assert case.profile.adapter in window.find('Google API account and edition').items[0]
        glossary = window.find('Advanced v3 glossary resource')
        window.find('Source language').ChangeValue('en')
        window.find('Maximum approved').ChangeValue('2')
        glossary.ChangeValue(GLOSSARY)
        window.click('Preview')
        assert GLOSSARY in window.find('Preview, supported').value
        glossary.ChangeValue(GLOSSARY + '-changed')
        window.click('Translate / resume')
        assert not case.service.list() and len(case.calls) == 1
        assert 'Preview this exact' in window.find('Preview, supported').value
        glossary.ChangeValue(GLOSSARY)
        window.click('Preview')
        window.click('Translate / resume')
        job, = case.service.list()
        assert job['status'] == 'complete' and job['kind'] == 'translation/2'
        assert job['authorization']['data_classes'] == ['selected-script']
        assert job['glossary'] == GLOSSARY
        assert job['source_binding']['anchor'] == case.document.source_anchor
        assert GLOSSARY in window.messages[-1][0] and case.profile.endpoint in window.messages[-1][0]
        assert not selected.editable and not glossary.enabled
        window.click('Saved jobs')
        assert glossary.value == GLOSSARY and not glossary.enabled
        before = len(case.calls)
        ProfileStore(case.app.root / 'state' / 'providers').remove(case.profile.id)
        case.vault.revoked = True
        glossary.ChangeValue(GLOSSARY + '-changed')
        window.click('Export')
        assert not tuple(tmp_path.glob('Translation-*'))
        assert 'Visible settings differ' in window.find('Preview, supported').value
        glossary.ChangeValue(GLOSSARY)
        window.click('Export')
        output, = tmp_path.glob('Translation-*')
        reading = ReadingDocument.load(output / 'translation.reading.json')
        assert reading.source_anchor == case.document.source_anchor
        assert reading.source_text == case.source.text and reading.text != reading.source_text
        assert json.loads((output / 'translation-provenance.json').read_text())['glossary'] == GLOSSARY
        assert len(case.calls) == before and sha256(original.read_bytes()).hexdigest() == original_digest
        window.click('New translation')
        assert selected.value == case.source.text and not selected.editable and glossary.value == ''
        window.click('Close')

    window.state['scenario'] = scenario
    window.ui.show_translation(frame, source_loader=lambda cancelled: case.source, source_topic=case.topic)


@pytest.mark.parametrize('stage', ['queued', 'vault'])
@pytest.mark.parametrize('mutation', ['removed', 'changed'])
def test_advanced_language_discovery_rechecks_account_through_queued_and_vault_boundaries(
        advanced_source, translation_window, monkeypatch, stage, mutation):
    case, window = advanced_source, translation_window
    frame = window.bind(case)
    store = ProfileStore(case.app.root / 'state' / 'providers')
    language_path = case.service._languages_path(case.profile)
    cached = language_path.read_bytes()
    reads = []
    get = case.vault.get
    def lookup(identity, reference):
        reads.append((identity, reference))
        value = get(identity, reference)
        _change(store, case.profile, mutation)
        return value

    def scenario():
        if stage == 'queued':
            window.state['deferred'] = True
        else:
            monkeypatch.setattr(case.vault, 'get', lookup)
        window.click('Fetch languages')
        if stage == 'queued':
            assert len(window.state['pending']) == 1
            _change(store, case.profile, mutation)
            window.state['pending'].pop(0)()
            assert not reads and not window.gateways
        else:
            assert len(reads) == len(window.gateways) == 1
        assert len(case.calls) == 1 and not case.service.list()
        assert language_path.read_bytes() == cached
        assert window.state['focused'] == 'Preview, supported languages and job status'
        assert 'unavailable' in window.find('Preview, supported').value or 'communication failed' in window.find('Preview, supported').value
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame)


def test_cli_dispatch_resumes_retained_glossary_job_and_exports_after_account_removal(
        advanced_source, monkeypatch, capsys, tmp_path):
    case = advanced_source
    store = _store(case)
    identity, _ = submit_source(case, GLOSSARY)
    gateways = _cli_gateway(monkeypatch, case)
    base = ['translation', '--data-dir', str(case.app.root)]
    old_signal = signal.getsignal(signal.SIGINT)
    assert entry.main(base + ['resume', identity]) == 0
    assert signal.getsignal(signal.SIGINT) is old_signal
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    assert gateways == [True]
    before = len(case.calls)
    store.remove(case.profile.id)
    case.vault.revoked = True
    monkeypatch.setattr(cli, 'Gateway', lambda *args, **kwargs: pytest.fail('Offline export constructed a remote Gateway'))
    destination = tmp_path / 'offline CLI source export'
    assert entry.main(base + ['export', identity, '--output', str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    reading = ReadingDocument.load(destination / 'translation.reading.json')
    assert reading.source_text == case.source.text and reading.source_anchor == case.document.source_anchor
    assert reading.text != reading.source_text and len(case.calls) == before
    assert json.loads((destination / 'translation-provenance.json').read_text())['glossary'] == GLOSSARY


@pytest.mark.parametrize('mutation', ['removed', 'changed'])
def test_cli_remote_resume_requires_current_saved_account_before_gateway(
        advanced_source, monkeypatch, capsys, mutation):
    case = advanced_source
    store = _store(case)
    identity, _ = submit_source(case, GLOSSARY)
    _change(store, case.profile, mutation)
    gateways = _cli_gateway(monkeypatch, case)
    assert cli.main(['--data-dir', str(case.app.root), 'resume', identity]) == 1
    assert 'account is unavailable' in capsys.readouterr().err
    assert not gateways and len(case.calls) == 1
    saved = case.service.get(identity)
    assert saved['status'] == 'queued' and not saved['receipts']


@pytest.mark.parametrize('operation', ['languages', 'resume'])
def test_cli_rechecks_current_account_after_vault_lookup_before_connection(
        advanced_source, monkeypatch, capsys, operation):
    case = advanced_source
    store = _store(case)
    identity, _ = submit_source(case, GLOSSARY)
    gateways = _cli_gateway(monkeypatch, case)
    get = case.vault.get
    def lookup(profile, reference):
        value = get(profile, reference)
        store.remove(case.profile.id)
        return value
    monkeypatch.setattr(case.vault, 'get', lookup)
    arguments = (['languages', '--profile', case.profile.id, '--approve-remote']
                 if operation == 'languages' else ['resume', identity])
    old_signal = signal.getsignal(signal.SIGINT)
    assert cli.main(['--data-dir', str(case.app.root)] + arguments) == 1
    assert signal.getsignal(signal.SIGINT) is old_signal
    assert capsys.readouterr().err.startswith('Translation: ')
    assert gateways == [True] and len(case.calls) == 1
    assert not case.service.get(identity)['receipts']


def test_cli_submission_rechecks_account_after_exact_file_review_before_job_publication(
        advanced_source, monkeypatch, capsys, tmp_path):
    case = advanced_source
    store = _store(case)
    source = tmp_path / 'selected.txt'
    source.write_text('Explicitly selected Unicode Україна.', encoding='utf-8')
    base = ['--data-dir', str(case.app.root)]
    scope = ['--profile', case.profile.id, '--input', str(source), '--source', 'en',
             '--target', 'uk', '--glossary', GLOSSARY, '--budget', '1']
    assert cli.main(base + ['preview'] + scope) == 0
    reviewed = json.loads(capsys.readouterr().out)
    review = cli._review
    def changed(*args):
        value = review(*args)
        store.remove(case.profile.id)
        return value
    monkeypatch.setattr(cli, '_review', changed)
    gateways = _cli_gateway(monkeypatch, case)
    assert cli.main(base + ['submit'] + scope + ['--approve-remote', '--review-sha256', reviewed['review_sha256']]) == 1
    assert 'account is unavailable' in capsys.readouterr().err
    assert not case.service.list() and not gateways and len(case.calls) == 1


def test_cli_unicode_metadata_survives_a_non_utf8_stdout_pipe(advanced_source, monkeypatch):
    case = advanced_source
    store = _store(case)
    name = 'Український обліковий запис'
    store.save(replace(case.profile, name=name))
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding='ascii', errors='strict')
    monkeypatch.setattr(sys, 'stdout', stream)
    assert cli.main(['--data-dir', str(case.app.root), 'profiles']) == 0
    stream.flush()
    rows = json.loads(output.getvalue().decode('ascii'))
    assert rows[0]['name'] == name and len(case.calls) == 1
