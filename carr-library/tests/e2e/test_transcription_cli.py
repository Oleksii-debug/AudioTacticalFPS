"""Actual flat-service CLI, isolated synthetic worker and receipted output exports.

No inference package/model is loaded; these tests do not establish acoustic or
language quality. They reuse the existing admitted worker protocol fixture.
"""
import json
from pathlib import Path
import runpy
import sys

import pytest

from carr_library.__main__ import main
from carr_library.application import LibraryApp
from carr_library.durable_state import read_json


def call(app, workspace, capsys, *arguments, expected=0):
    assert main(['transcribe', '--data-dir', str(app.root), '--workspace', workspace,
                 *map(str, arguments)]) == expected
    captured = capsys.readouterr()
    return json.loads(captured.out) if captured.out else None, captured.err


def prepare(tmp_path, capsys, *, mode='complete'):
    fixture = runpy.run_path(str(Path(__file__).parents[1] / 'transcription' /
                                 'test_supplied_worker_bridge.py'))
    worker = tmp_path / ('synthetic-' + mode + '.py')
    worker.write_text(fixture['SYNTHETIC_WORKER'].replace('__MODE__', repr(mode)), encoding='utf-8')
    models = tmp_path / 'models'
    models.mkdir()
    app = LibraryApp(tmp_path / 'дані')
    workspace = app.create_workspace('CLI synthetic protocol')
    values = ['--python', Path(sys.executable), '--worker', worker, '--models-dir', models]
    inspected, _ = call(app, workspace, capsys, 'inspect-runtime', *values)
    assert inspected['inference_qualified'] is False
    assert not list(app.root.rglob('job_status.json'))
    profile, _ = call(app, workspace, capsys, 'register-runtime', '--name', 'Synthetic protocol fixture',
                      *values, '--trust-runtime', '--fingerprint', inspected['fingerprint'])
    assert profile['qualification'] == 'unqualified' and profile['trusted']
    return app, workspace, worker, profile


def test_cli_register_submit_resume_read_edit_export_and_no_clobber(tmp_path, capsys):
    app, workspace, worker, profile = prepare(tmp_path, capsys)
    source = tmp_path / 'Вхідний запис.wav'
    source.write_bytes(b'Synthetic transport fixture, not decoded speech.')
    intake, _ = call(app, workspace, capsys, 'submit', source, '--profile', profile['id'], '--language', 'uk')
    identity = intake['id']
    job, _ = call(app, workspace, capsys, 'show', identity)
    assert job['status'] == 'ready' and intake['files'] == 1
    assert job['runtime_profile_id'] == profile['id'] and job['artifacts'] == []
    source.unlink()  # Resume uses the admitted retained source, not the deleted original.
    paused, _ = call(app, workspace, capsys, 'pause', identity)
    assert paused['status'] == 'paused'
    completed, _ = call(app, workspace, capsys, 'resume', identity, '--timeout', 30)
    assert completed['status'] == 'completed' and completed['done'] == 1
    assert len(completed['artifacts']) == 6
    read, _ = call(app, workspace, capsys, 'read', identity)
    assert read['text'].startswith('Synthetic transcript.') and not read['edited']
    assert 'Unverified' in read['notice']
    edited = tmp_path / 'edited.txt'
    edited.write_text('Окрема редагована копія.', encoding='utf-8')
    revision, _ = call(app, workspace, capsys, 'save-edit', identity,
                       '--source', job['sources'][0]['filename'], '--text-file', edited)
    separate, _ = call(app, workspace, capsys, 'read', identity, '--edited')
    assert separate['text'] == 'Окрема редагована копія.' and separate['revision'] == revision['revision']
    assert call(app, workspace, capsys, 'read', identity)[0]['text'] == read['text']
    destination = tmp_path / 'new-exports'
    receipt, _ = call(app, workspace, capsys, 'export', identity, '--output', destination,
                      '--format', 'txt', '--format', 'srt', '--format', 'srt')
    assert receipt == {'exported_to': str(destination)}
    manifest = read_json(destination / 'transcription-manifest.json')
    assert manifest['status'] == 'completed' and manifest['settings'] == completed['settings']
    suffixes = {Path(row['filename']).suffix for row in manifest['artifacts']}
    assert suffixes == {'.txt', '.srt'} and not list(destination.rglob('*.vtt'))
    assert (destination / 'edited' / (revision['revision'] + '.txt')).read_text(encoding='utf-8') == separate['text']
    srt = next(path for path in destination.rglob('*.srt') if not path.name.endswith('.words.srt'))
    assert '00:00:00,000 --> 00:00:01,000' in srt.read_text(encoding='utf-8')
    before = {path.relative_to(destination): path.read_bytes()
              for path in destination.rglob('*') if path.is_file()}
    _, error = call(app, workspace, capsys, 'export', identity, destination, expected=1)
    assert error
    assert {path.relative_to(destination): path.read_bytes()
            for path in destination.rglob('*') if path.is_file()} == before
    assert len(app.transcription(workspace).verify(identity)['artifacts']) == 6


def test_cli_failed_inference_is_nonzero_with_saved_job_and_no_success_text(tmp_path, capsys):
    app, workspace, worker, profile = prepare(tmp_path, capsys, mode='metadata-mismatch')
    source = tmp_path / 'synthetic.wav'
    source.write_bytes(b'Not actual speech')
    job, error = call(app, workspace, capsys, 'submit', source, '--profile', profile['id'],
                      '--run', '--timeout', 30, expected=1)
    assert job['status'] == 'failed' and job['done'] == 0
    assert 'Saved transcription job: ' + job['id'] in error
    status, _ = call(app, workspace, capsys, 'status', job['id'])
    assert status['id'] == job['id'] and status['status'] == 'failed'
    _, error = call(app, workspace, capsys, 'read', job['id'], expected=1)
    assert error


def test_cli_requires_exact_review_fingerprint_before_creating_library(tmp_path, capsys):
    target = tmp_path / 'uncreated-library'
    with pytest.raises(SystemExit) as error:
        main(['transcribe', '--data-dir', str(target), '--workspace', 'unselected',
              'register-runtime', '--name', 'Unreviewed', '--python', 'python',
              '--worker', 'worker.py', '--models-dir', 'model', '--trust-runtime'])
    assert error.value.code == 2
    assert 'exact --fingerprint' in capsys.readouterr().err
    assert not target.exists()


def test_cli_keeps_explicit_configure_create_folder_and_edit_entrypoints(tmp_path, capsys):
    app, workspace, worker, profile = prepare(tmp_path, capsys)
    configured, _ = call(app, workspace, capsys, 'configure', '--python', Path(sys.executable),
                         '--worker', worker, '--models-dir', tmp_path / 'models')
    assert configured['protocol'] == 'receipts-v1'
    folder = tmp_path / 'recordings'
    (folder / 'nested').mkdir(parents=True)
    media = folder / 'nested' / 'Запис.wav'
    media.write_bytes(b'Synthetic protocol media')
    (folder / 'notes.txt').write_text('Excluded private notes.', encoding='utf-8')
    intake, _ = call(app, workspace, capsys, 'create-folder', folder)
    assert intake['files'] == 1 and intake['excluded_files'] == 1
    job, _ = call(app, workspace, capsys, 'show', intake['id'])
    assert job['sources'][0]['original_name'] == 'nested/Запис.wav'
    assert job['status'] == 'ready'
    direct, _ = call(app, workspace, capsys, 'create', media, '--profile', profile['id'])
    assert direct['id'] != intake['id']
    done, _ = call(app, workspace, capsys, 'run', direct['id'], '--timeout', 30)
    edited = tmp_path / 'derivative.txt'
    edited.write_text('A separate legacy-command derivative.', encoding='utf-8-sig')
    saved, _ = call(app, workspace, capsys, 'edit', direct['id'],
                    '--source', done['sources'][0]['filename'], '--text-file', edited)
    assert saved['revision']
    assert app.transcription(workspace).edits(direct['id'])[-1]['text'] == 'A separate legacy-command derivative.'


def test_cli_named_profile_control_and_workspace_are_not_global_selection(tmp_path, capsys):
    app, workspace, worker, profile = prepare(tmp_path, capsys)
    source = tmp_path / 'synthetic.wav'
    source.write_bytes(b'Synthetic retained media')
    intake, _ = call(app, workspace, capsys, 'submit', source, '--profile', profile['id'])
    identity = intake['id']
    cancelled, _ = call(app, workspace, capsys, 'cancel', identity)
    assert cancelled['status'] == 'cancelled'
    ready, _ = call(app, workspace, capsys, 'continue', identity)
    assert ready['status'] == 'ready'
    runtime_rows, _ = call(app, workspace, capsys, 'runtimes')
    assert any(row['id'] == profile['id'] for row in runtime_rows)
    other = app.create_workspace('Different workspace')
    result, error = call(app, other, capsys, 'show', identity, expected=1)
    assert result is None and error
    assert app.transcription(workspace).get(identity)['status'] == 'ready'
    copied, _ = call(app, workspace, capsys, 'copy-job', identity, '--profile', profile['id'])
    assert copied['id'] != identity and copied['status'] == 'ready' and copied['artifacts'] == []
    assert copied['settings'] == ready['settings'] and copied['protocol'] == ready['protocol']
    assert [row['sha256'] for row in copied['sources']] == [row['sha256'] for row in ready['sources']]


@pytest.mark.parametrize('arguments', [
    ['--task', 'translate'],
    ['--device', 'network'],
])
def test_cli_rejects_unsupported_recognition_choices_before_mutation(tmp_path, capsys, arguments):
    target = tmp_path / 'uncreated-library'
    with pytest.raises(SystemExit) as error:
        main(['transcribe', '--data-dir', str(target), '--workspace', 'unselected',
              'create', str(tmp_path / 'source.wav'), *arguments])
    assert error.value.code == 2
    assert not target.exists()
    capsys.readouterr()


def test_cli_edited_text_read_is_actually_bounded_when_file_grows(tmp_path, capsys, monkeypatch):
    from carr_library import durable_state, transcription
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bounded edit admission')
    text = tmp_path / 'edited.txt'
    text.write_text('small', encoding='utf-8')
    original = durable_state._open_json_read
    def grow(path):
        text.write_text('x' * 65, encoding='utf-8')
        return original(path)
    monkeypatch.setattr(transcription, 'MAX_TEXT_BYTES', 64)
    monkeypatch.setattr(durable_state, '_open_json_read', grow)
    saved = []
    monkeypatch.setattr(transcription.TranscriptionService, 'save_edit',
                        lambda *args, **kwargs: saved.append(args))
    result, error = call(app, workspace, capsys, 'edit', 'a' * 32,
                         '--source', 'chosen.wav', '--text-file', text, expected=1)
    assert result is None and not saved
    assert 'changed' in error or 'allowance' in error


def test_cli_empty_or_ambiguous_export_destination_fails_before_library_creation(tmp_path, capsys):
    target = tmp_path / 'uncreated-library'
    for arguments in ([], ['one', '--output', 'two']):
        with pytest.raises(SystemExit) as error:
            main(['transcribe', '--data-dir', str(target), '--workspace', 'unselected',
                  'export', 'a' * 32, *arguments])
        assert error.value.code == 2
        assert not target.exists()
        capsys.readouterr()
