"""Actual CLI, isolated synthetic protocol process and checked transcript exports."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from carr_library.__main__ import main
from carr_library.application import LibraryApp
from carr_library.operations import ResourceBroker, ResourceLimits


def runtime(root, **options):
    path = Path(__file__).parents[1] / 'transcription' / 'fixture_runtime.py'
    spec = importlib.util.spec_from_file_location('cli_synthetic_runtime', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_runtime(root, options=options)


def call(root, capsys, *arguments, expected=0):
    with sqlite3.connect(Path(root) / 'library.sqlite') as database:
        [(workspace,)] = database.execute('SELECT workspace FROM workspace_meta').fetchall()
    assert main(['transcription', '--data-dir', str(root), '--workspace', workspace,
                 *map(str, arguments)]) == expected
    captured = capsys.readouterr()
    return json.loads(captured.out) if captured.out else None, captured.err


def prepare(tmp_path, capsys, **options):
    engine = runtime(tmp_path / 'existing runtime', **options)
    app = LibraryApp(tmp_path / 'дані')
    app.create_workspace('Explicit CLI workspace')
    ResourceBroker.for_app(app.root).configure(ResourceLimits(min_free_disk_bytes=0))
    values = ['--python', engine.python, '--worker', engine.worker, '--model', engine.model]
    inspected, _ = call(app.root, capsys, 'inspect-runtime', *values)
    assert not list(engine.root.rglob('job_status.json'))
    profile, _ = call(app.root, capsys, 'register-runtime', '--name', 'Synthetic protocol fixture',
                      *values, '--trust-runtime', '--fingerprint', inspected['fingerprint'])
    return app, engine, profile


def test_cli_register_submit_resume_read_edit_export_and_no_clobber(tmp_path, capsys):
    app, engine, profile = prepare(tmp_path, capsys)
    source = tmp_path / 'Вхідний запис.wav'
    source.write_bytes(b'Synthetic transport fixture, not decoded speech.')
    job, _ = call(app.root, capsys, 'submit', source, '--profile', profile['id'],
                  '--language', 'uk', '--format', 'txt', '--format', 'srt', '--format', 'vtt', '--format', 'json')
    identity = job['id']
    assert job['status'] == 'pending'
    source.unlink()
    call(app.root, capsys, 'pause', identity)
    completed, _ = call(app.root, capsys, 'resume', identity)
    assert completed['status'] == 'complete' and completed['done'] == 1
    read, _ = call(app.root, capsys, 'read', identity)
    assert read['text'] == 'Синтетичний протокольний результат.'
    assert read['provenance']['citation_authority'] is False
    edited = tmp_path / 'edited.txt'
    edited.write_text('Окрема редагована копія.', encoding='utf-8')
    call(app.root, capsys, 'save-edit', identity, '--source', job['sources'][0]['id'], '--text-file', edited)
    separate, _ = call(app.root, capsys, 'read', identity, '--edited')
    assert separate['text'] == 'Окрема редагована копія.'
    assert call(app.root, capsys, 'read', identity)[0]['text'] == read['text']
    destination = tmp_path / 'exports'
    destination.mkdir()
    receipt, _ = call(app.root, capsys, 'export', identity, '--output', destination)
    assert receipt['status'] == 'complete' and len(receipt['files']) == 5
    srt = next(Path(row['path']) for row in receipt['files'] if row['format'] == 'srt')
    assert '00:01:00,000 --> 00:01:01,000' in srt.read_text(encoding='utf-8')
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    _, error = call(app.root, capsys, 'export', identity, '--output', destination, expected=1)
    assert 'exists' in error
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == before


def test_cli_failed_inference_is_nonzero_with_saved_job_and_no_success_export(tmp_path, capsys):
    app, engine, profile = prepare(tmp_path, capsys, wrong_language=True)
    source = tmp_path / 'synthetic.wav'
    source.write_bytes(b'Not actual speech')
    job, _ = call(app.root, capsys, 'submit', source, '--profile', profile['id'], '--run', expected=1)
    assert job['status'] == 'failed' and job['done'] == 0
    status, _ = call(app.root, capsys, 'status', job['id'])
    assert status['id'] == job['id'] and status['status'] == 'failed'
    _, error = call(app.root, capsys, 'read', job['id'], expected=1)
    assert error


def test_cli_requires_exact_review_fingerprint_before_trusting_runtime(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(['transcription','--data-dir',str(tmp_path / 'library'),'--workspace','not-created','register-runtime',
              '--name','Unreviewed','--python','python','--worker','worker.py','--model','model','--trust-runtime'])
    assert error.value.code == 2
    assert 'exact --fingerprint' in capsys.readouterr().err
    assert not (tmp_path / 'library').exists()


def test_cli_keeps_canonical_history_separate_and_requires_binding_for_historical_execution(tmp_path, capsys):
    app, engine, profile = prepare(tmp_path, capsys)
    source = tmp_path / 'retained.wav'
    source.write_bytes(b'Synthetic retained source, not speech.')
    job, _ = call(app.root, capsys, 'submit', source, '--profile', profile['id'])
    assert call(app.root, capsys, 'jobs')[0] == []
    assert [row['id'] for row in call(app.root, capsys, 'jobs', '--canonical')[0]] == [job['id']]
    (app.root / 'state/transcription/jobs' / job['id'] / 'workspace.json').unlink()
    before = call(app.root, capsys, 'status', job['id'])[0]
    assert before['execution_review_required'] is True
    _, error = call(app.root, capsys, 'resume', job['id'], expected=1)
    assert 'bind' in error.lower()
    _, error = call(app.root, capsys, 'bind-workspace', job['id'], expected=1)
    assert 'review' in error.lower()
    bound, _ = call(app.root, capsys, 'bind-workspace', job['id'], '--reviewed')
    assert bound['status'] == 'pending'
    assert not engine.worker.with_name('fixture-starts.log').exists()
    completed, _ = call(app.root, capsys, 'resume', job['id'])
    assert completed['status'] == 'complete'
