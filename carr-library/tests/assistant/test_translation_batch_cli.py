"""Actual headless batch commands, library sources, jobs and synthetic Gateway."""
from copy import deepcopy
import json
from pathlib import Path
import signal
import runpy
import os
import subprocess
import sys
from zipfile import ZipFile

import pytest

from carr_library.assistant import translation_batch_cli as translation_cli
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Gateway
from carr_library.assistant.translation_batch import TranslationBatchService
_fixture = runpy.run_path(str(Path(__file__).with_name('translation_batch_fixtures.py')))
transport, source_library, batch_env = (_fixture[name] for name in ('transport', 'source_library', 'batch_env'))
GLOSSARY = _fixture['GLOSSARY']


def description(e):
    return {'schema': 'carr-translation-batch-input/1', 'title': 'CLI exact batch', 'items': [
        {'profile': e.p.id, 'workspace': e.workspace, 'edition': selected['source_anchor']['edition'],
         'topic': e.topic, 'title': selected['title'], 'source': 'en', 'target': target,
         'glossary': glossary, 'budget': '1'}
        for selected, target, glossary in zip(e.selected, ('uk', 'fr'), (None, GLOSSARY))]}


@pytest.fixture
def cli(batch_env, monkeypatch, tmp_path, capsys):
    e = batch_env
    monkeypatch.setattr(translation_cli, 'Gateway',
        lambda directory, **options: Gateway(directory, vault=e.vault, **options))
    file = tmp_path / 'explicit-batch.json'
    spec = description(e)
    def save():
        file.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
    save()
    def run(*args):
        before = signal.getsignal(signal.SIGINT)
        code = translation_cli.main(['--data-dir', str(e.app.root), *args])
        assert signal.getsignal(signal.SIGINT) is before
        output = capsys.readouterr()
        return code, json.loads(output.out) if output.out.strip() else None, output.err
    e.spec, e.file, e.save, e.cli = spec, file, save, run
    return e


def test_cli_review_prepare_run_reuse_and_explicit_export_use_existing_children(cli, transport, tmp_path):
    e = cli
    before = len(transport['calls'])
    code, review, error = e.cli('preview', '--batch-file', str(e.file))
    assert code == 0 and not error and review['documents'] == 2
    assert review['items'][1]['glossary'] == GLOSSARY
    args = ['submit', '--batch-file', str(e.file), '--review-sha256', review['review_sha256']]
    code, _, error = e.cli(*args)
    assert code == 1 and 'Explicitly' in error and not e.service.jobs.identities()
    code, saved, error = e.cli(*args, '--approve-remote')
    assert code == 0 and not error and saved['status'] == 'queued' and not saved['network_access']
    assert len(transport['calls']) == before
    assert len(e.service.list()) == 2 and len(e.service.jobs.identities()) == 2
    identity = saved['id']
    code, reread, error = e.cli('review', identity)
    assert code == 0 and not error and reread['review'] == review
    assert e.cli('prepare', identity)[1]['status'] == 'queued'
    assert e.cli('jobs')[1] == [saved_item for saved_item in e.batches.list()]
    assert len(transport['calls']) == before
    code, result, error = e.cli('resume', identity)
    assert code == 0 and result['status'] == 'complete' and result['completed'] == 2
    after = len(transport['calls'])
    assert after > before
    assert e.cli('resume', identity)[1]['status'] == 'complete'
    assert len(transport['calls']) == after
    output = tmp_path / 'explicit new export.zip'
    code, result, error = e.cli('export', identity, '--output', str(output))
    assert code == 0 and not error and result['path'] == str(output)
    with ZipFile(output) as archive:
        for index, selected in enumerate(e.selected, 1):
            assert archive.read(f'{index:04d}-translation/original.txt') == selected['text'].encode('utf-8')
    assert e.cli('export', identity, '--output', str(output))[0] == 1
    assert len(transport['calls']) == after


@pytest.mark.parametrize('change', ['order', 'title', 'budget', 'target', 'source_revision'])
def test_cli_rereads_exact_inputs_before_accepting_review_hash(cli, transport, change):
    e = cli
    _, review, _ = e.cli('preview', '--batch-file', str(e.file))
    before = len(transport['calls'])
    if change == 'order':
        e.spec['items'].reverse()
    elif change == 'title':
        e.spec['title'] = 'Different batch'
    elif change == 'source_revision':
        row = e.spec['items'][1]
        e.app.update_metadata(e.workspace, row['edition'], title='Changed library metadata revision')
    else:
        e.spec['items'][0][change] = {'budget': '2', 'target': 'fr'}[change]
    e.save()
    code, _, error = e.cli('submit', '--batch-file', str(e.file), '--approve-remote',
                          '--review-sha256', review['review_sha256'])
    assert code == 1 and error and not e.service.jobs.identities()
    assert len(transport['calls']) == before


def test_cli_explicit_relative_file_is_frozen_without_retaining_external_path(cli, transport):
    e = cli
    original = e.file.parent / 'selected exact.txt'
    raw = 'Original CRLF\r\nУкраїнський рядок.\r\n'.encode('utf-8')
    original.write_bytes(raw)
    e.spec['items'] = [{'profile': e.p.id, 'input': original.name, 'source': 'en',
                        'target': 'uk', 'budget': '1'}]
    e.save()
    _, review, _ = e.cli('preview', '--batch-file', str(e.file))
    args = ['submit', '--batch-file', str(e.file), '--approve-remote',
            '--review-sha256', review['review_sha256']]
    original.write_bytes(raw + b'Changed after review.')
    assert e.cli(*args)[0] == 1 and not e.service.jobs.identities()
    original.write_bytes(raw)
    code, saved, _ = e.cli(*args)
    assert code == 0
    job, = e.service.list()
    assert job['source_text'].encode('utf-8') == raw and 'source_binding' not in job and job['kind'] == 'translation/1'
    assert str(original) not in e.batches.jobs.path(saved['id']).read_text(encoding='utf-8')
    original.unlink()
    assert e.cli('resume', saved['id'])[1]['status'] == 'complete'


@pytest.mark.parametrize('change', ['unknown', 'both_selectors', 'mixed_scope', 'duplicates', 'too_many'])
def test_cli_strict_bounded_input_cannot_scan_or_silently_infer_selectors(cli, transport, change):
    e = cli
    if change == 'unknown':
        e.spec['items'][0]['upload_folder'] = str(e.app.root)
    elif change == 'both_selectors':
        e.spec['items'][0]['input'] = 'must-not-read.txt'
    elif change == 'mixed_scope':
        e.spec['items'][0].pop('edition')
        e.spec['items'][0]['input'] = 'must-not-read.txt'
    elif change == 'too_many':
        e.spec['items'] *= 17
    else:
        e.file.write_text('{"schema":"carr-translation-batch-input/1","title":"A","title":"B","items":[]}', encoding='utf-8')
    if change != 'duplicates':
        e.save()
    before = len(transport['calls'])
    code, _, error = e.cli('preview', '--batch-file', str(e.file))
    assert code == 1 and error and not e.service.jobs.identities()
    assert len(transport['calls']) == before


def test_cli_input_cancellation_stops_before_reading_the_next_document(cli, monkeypatch):
    e = cli
    stop, calls = [], []
    actual = translation_cli.capture_source
    def selected(*args, **kwargs):
        calls.append(args[2])
        value = actual(*args, **kwargs)
        stop.append(True)
        return value
    monkeypatch.setattr(translation_cli, 'capture_source', selected)
    with pytest.raises(InterruptedError):
        translation_cli._batch_input(e.service, ProfileStore(e.app.root / 'state' / 'providers'),
                                     e.file, cancelled=lambda: bool(stop))
    assert calls == [e.spec['items'][0]['edition']] and not e.service.jobs.identities()


@pytest.mark.parametrize('field,value', [('profile', {}), ('target', []), ('budget', 0.1),
                                          ('chapter', True), ('glossary', 5)])
def test_cli_invalid_json_setting_types_fail_before_any_job(cli, transport, field, value):
    e = cli
    e.spec['items'][0][field] = value
    e.save()
    before = len(transport['calls'])
    code, _, error = e.cli('preview', '--batch-file', str(e.file))
    assert code == 1 and 'explicit text settings' in error
    assert not e.service.jobs.identities() and len(transport['calls']) == before


def test_cli_actual_subprocess_review_uses_selected_package_and_sends_no_text(cli, transport):
    e = cli
    script = r'''
import json, sys
from pathlib import Path
from carr_library.assistant import translation_batch_cli
assert Path(translation_batch_cli.__file__).resolve() == Path(sys.argv[1]).resolve()
raise SystemExit(translation_batch_cli.main(['--data-dir', sys.argv[2], 'preview',
                                            '--batch-file', sys.argv[3]]))
'''
    before = len(transport['calls'])
    result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script,
        str(translation_cli.__file__), str(e.app.root), str(e.file)],
        capture_output=True, timeout=45, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    review = json.loads(result.stdout)
    assert review['documents'] == 2 and review['calls'] >= 2
    assert review['items'][0]['source_anchor'] == e.selected[0]['source_anchor']
    assert not e.service.jobs.identities() and len(transport['calls']) == before


def test_cli_revoked_account_after_gateway_construction_stops_before_credentials(cli, monkeypatch, transport):
    from dataclasses import replace
    e = cli
    _, review, _ = e.cli('preview', '--batch-file', str(e.file))
    code, saved, error = e.cli('submit', '--batch-file', str(e.file), '--approve-remote',
                              '--review-sha256', review['review_sha256'])
    assert code == 0 and not error
    before = len(transport['calls']), len(e.vault.calls)
    def changed(directory, **options):
        result = Gateway(directory, vault=e.vault, **options)
        ProfileStore(e.app.root / 'state' / 'providers').save(replace(e.p, enabled=False))
        return result
    monkeypatch.setattr(translation_cli, 'Gateway', changed)
    code, _, error = e.cli('resume', saved['id'])
    assert code == 1 and 'disabled' in error
    assert (len(transport['calls']), len(e.vault.calls)) == before
    assert e.batches.get(saved['id'])['completed_chunks'] == 0
