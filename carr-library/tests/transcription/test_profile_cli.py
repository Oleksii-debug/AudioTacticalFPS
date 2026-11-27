"""One CLI dispatches existing workspace profiles and reviewed library-wide jobs.

The process fixture is the exact public supervisor peer. It cannot recognize
speech and does not contain or qualify any private worker, model or recording.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library import transcription_cli as cli
from carr_library.transcription_receipt import DEFAULT_SETTINGS, PROTOCOL
from carr_library.reader.narration import NarrationStore

_spec = importlib.util.spec_from_file_location('supplied_profile_cli_peer',
    Path(__file__).parents[1] / 'test_transcription_service.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
prepared_environment = _fixture.prepared_environment
real_protocol_child = _fixture.real_protocol_child


def arguments(root, *values):
    return ['--data-dir', str(root), '--profile', PROTOCOL, *map(str, values)]


def value(capsys):
    return json.loads(capsys.readouterr().out)


def test_prepare_command_captures_all_seventeen_independent_settings(prepared_environment, capsys):
    service, manifest, source, _ = prepared_environment()
    original = source.read_bytes()
    assert cli.main(arguments(service.library_root, 'prepare', source, '--manifest', manifest,
        '--title', 'Українські налаштування', '--output-mode', 'one_per_folder',
        '--task', 'translate', '--translation-target', 'en', '--language', 'uk',
        '--model', 'small', '--device', 'cpu', '--compute-type', 'int8',
        '--beam-size', '4', '--parallel-files', '3', '--no-vad-filter', '--include-headers',
        '--no-output-txt', '--output-srt', '--output-vtt', '--output-json',
        '--multilingual', '--no-condition-on-previous-text')) == 0
    result = value(capsys)
    assert result['scope'] == 'library' and result['protocol'] == PROTOCOL
    expected = dict(DEFAULT_SETTINGS)
    expected.update(output_mode='one_per_folder', task='translate', translation_target='en',
        language='uk', beam_size=4, parallel_files=3, vad_filter=False, include_headers=True,
        output_txt=False, output_srt=True, output_vtt=True, output_json=True,
        multilingual=True, condition_on_previous_text=False)
    definition = service.definition(result['id'])
    assert len(definition['settings']) == 17 and definition['settings'] == expected
    assert definition['title'] == 'Українські налаштування'
    assert 'workspace' not in definition and source.read_bytes() == original
    assert not (service._job(result['id']) / 'job_status.json').exists()


def test_same_cli_runs_real_protocol_child_and_saves_separate_reading_copy(
        prepared_environment, real_protocol_child, capsys):
    service, manifest, source, _ = prepared_environment()
    original = source.read_bytes()
    assert cli.main(arguments(service.library_root, 'prepare', source, '--manifest', manifest,
                              '--output-srt', '--output-vtt', '--output-json')) == 0
    identity = value(capsys)['id']
    assert cli.main(arguments(service.library_root, 'run', identity, '--manifest', manifest, '--timeout', 30)) == 0
    completed = value(capsys)
    assert completed['protocol'] == PROTOCOL and completed['input_count'] == 1
    assert completed['output_count'] == 4 and completed['runtime_current_verified'] is True
    assert 'status' not in completed
    assert cli.main(arguments(service.library_root, 'verify', identity)) == 0
    historical = value(capsys)
    assert historical['historical'] is True and historical['runtime_current_verified'] is False
    original_output = next(row for row in historical['outputs'] if row['format'] == 'txt')
    result_path = Path(historical['results_root']) / original_output['path']
    expected_output = result_path.read_bytes()
    assert cli.main(arguments(service.library_root, 'reading-copy', identity,
                              '--output', original_output['path'])) == 0
    saved = value(capsys)
    project, digest = NarrationStore(service.library_root).load(saved['id'])
    assert digest == saved['saved_sha256'] and project.original_sha256 == saved['original_sha256']
    assert project.original_path == str(result_path)
    assert project.original_text.encode('utf-8') == expected_output
    assert len(project.revisions) == 1 and project.revisions[0].number == 0
    assert result_path.read_bytes() == expected_output and source.read_bytes() == original
    assert real_protocol_child


def test_zero_exit_without_receipt_never_becomes_cli_success(prepared_environment, real_protocol_child, capsys):
    service, manifest, source, settings = prepared_environment('zero_only')
    identity = service.prepare([source], manifest, settings)
    assert cli.main(arguments(service.library_root, 'run', identity, '--manifest', manifest, '--timeout', 30)) == 1
    captured = capsys.readouterr()
    assert not captured.out and 'Transcription:' in captured.err
    assert cli.main(arguments(service.library_root, 'verify', identity)) == 1
    assert not capsys.readouterr().out


def test_pause_resume_and_stop_commands_report_request_only(prepared_environment, capsys):
    service, manifest, source, settings = prepared_environment()
    identity = service.prepare([source], manifest, settings)
    original = service.definition(identity)
    job = service._job(identity)
    for action, flag, exists in (('pause', 'pause.flag', True),
                                 ('resume', 'pause.flag', False), ('stop', 'stop.flag', True)):
        assert cli.main(arguments(service.library_root, action, identity)) == 0
        assert value(capsys) == {'id': identity, 'control_requested': action, 'scope': 'library'}
        assert (job / flag).exists() is exists
        if exists:
            assert (job / flag).read_bytes() == b''
        assert service.definition(identity) == original


@pytest.mark.parametrize('options', [
    ['jobs'],
    ['--profile', PROTOCOL, '--workspace', 'existing', 'jobs'],
    ['--workspace', 'existing', 'prepare', 'source.mp3', '--manifest', 'runtime.json'],
    ['--profile', PROTOCOL, 'run', 'a' * 32],
    ['--workspace', 'existing', 'run', 'a' * 32, '--manifest', 'runtime.json'],
    ['--workspace', 'existing', '--profile', 'whisper-v5-manifest-v1', 'configure',
     '--protocol', 'receipts-v1', '--python', 'python', '--worker', 'worker.py', '--models-dir', 'models'],
], ids=['scope-required', 'no-workspace-reclassification', 'prepare-profile-required',
        'explicit-runtime-required', 'legacy-config-unchanged', 'conflicting-profiles'])
def test_ambiguous_or_conflicting_scope_is_rejected_before_library_mutation(tmp_path, capsys, options):
    root = tmp_path / 'must-not-open'
    with pytest.raises(SystemExit) as raised:
        cli.main(['--data-dir', str(root), *options])
    assert raised.value.code == 2 and not root.exists()
    assert not capsys.readouterr().out


def test_workspace_job_remains_in_original_namespace_and_is_not_adopted_by_new_profile(
        prepared_environment, capsys):
    supplied, _, source, _ = prepared_environment()
    app = LibraryApp(supplied.library_root)
    workspace = app.create_workspace('Existing workspace')
    legacy = app.transcription(workspace)
    identity = legacy.submit([source], protocol='receipts-v1')
    original = (legacy._path(identity) / 'job.json').read_bytes()
    assert cli.main(['--data-dir', str(app.root), '--workspace', workspace, 'jobs']) == 0
    row, = value(capsys)
    assert row['id'] == identity and row['protocol'] == 'receipts-v1'
    assert cli.main(arguments(app.root, 'jobs')) == 0
    assert value(capsys) == []
    assert cli.main(arguments(app.root, 'show', identity)) == 1
    assert not capsys.readouterr().out
    assert (legacy._path(identity) / 'job.json').read_bytes() == original
    assert not (supplied.root / identity).exists()


@pytest.mark.parametrize('changed', ['output', 'input'])
def test_reading_copy_command_rechecks_saved_bytes_before_publishing(
        prepared_environment, real_protocol_child, capsys, changed):
    service, manifest, source, settings = prepared_environment()
    identity = service.prepare([source], manifest, settings)
    completed = service.run(identity, manifest, timeout=30)
    selected = completed['outputs'][0]
    if changed == 'output':
        target = Path(completed['results_root']) / selected['path']
    else:
        definition = service.definition(identity)
        target = service._job(identity) / definition['inputs'][0]['path']
    target.write_bytes(b'Changed after verified execution.')
    assert cli.main(arguments(service.library_root, 'reading-copy', identity, '--output', selected['path'])) == 1
    assert not capsys.readouterr().out
    assert not (service.library_root / 'narration-projects').exists()
