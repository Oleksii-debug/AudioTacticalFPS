"""Current-service CLI acceptance. Synthetic peer tests are not ASR qualification."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from carr_library import transcription_cli as cli
from carr_library.audio.jobs import read_json
from carr_library.runtime_settings import EngineProfileStore
from carr_library.transcription_service import TranscriptionService
from tests.test_transcription_service import prepared_environment, real_protocol_child


def invoke(service, capsys, *args):
    code = cli.main(['--data-dir', str(service.library_root), *map(str, args)])
    output = capsys.readouterr()
    return code, json.loads(output.out) if output.out else None, output.err


def pin(manifest):
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def selection(manifest):
    return ['--manifest', str(manifest), '--reviewed-sha256', pin(manifest)]


def test_inspection_registration_and_explicit_submit_do_not_launch(
        prepared_environment, capsys, monkeypatch):
    service, manifest, source, settings = prepared_environment()
    from carr_library.audio import capture_process
    monkeypatch.setattr(capture_process, 'start_owned_process',
                        lambda *a, **k: pytest.fail('Inspect/register/prepare must not execute code'))
    code, report, _ = invoke(service, capsys, 'inspect-runtime', '--manifest', manifest)
    assert code == 0 and report['manifest_sha256'] == pin(manifest)
    assert report['executed'] is False and report['quality'] == 'NOT_QUALIFIED'
    code, profile, _ = invoke(service, capsys, 'register-runtime', '--manifest', manifest,
                             '--reviewed-sha256', pin(manifest), '--name', 'Місцевий огляд ü')
    assert code == 0 and profile['enabled'] is True
    code, result, notices = invoke(service, capsys, 'submit', source, '--profile', profile['id'],
                                  '--language', 'uk', '--format', 'txt', '--format', 'srt')
    assert code == 0 and result['status'] == 'prepared' and not result['executed']
    definition = service.definition(result['id'])
    assert definition['settings']['language'] == 'uk'
    assert definition['settings']['output_srt'] is True
    assert definition['runtime_manifest_sha256'] == pin(manifest)
    assert result['id'] in notices
    assert not (service._job(result['id']) / 'execution.json').exists()


@pytest.mark.parametrize('arguments', [
    ['submit', 'input.mp3', '--manifest', 'manifest.json'],
    ['run', 'a' * 32, '--manifest', 'manifest.json', '--reviewed-sha256', 'G' * 64],
    ['resume', 'a' * 32, '--profile', 'b' * 32, '--timeout', 'nan'],
    ['submit', 'input.mp3', '--profile', 'b' * 32, '--beam-size', '0'],
    ['submit', 'input.mp3', '--profile', 'b' * 32, '--format', 'txt', '--format', 'txt'],
], ids=['missing-pin', 'invalid-pin', 'nonfinite-timeout', 'invalid-setting', 'duplicate-format'])
def test_invalid_flags_do_not_create_a_library(tmp_path, arguments):
    root = tmp_path / 'absent'
    with pytest.raises(SystemExit) as error:
        cli.main(['--data-dir', str(root), *arguments])
    assert error.value.code == 2
    assert not root.exists()


def test_manifest_replacement_after_cli_selection_is_not_implicitly_accepted(
        prepared_environment, capsys, monkeypatch):
    service, manifest, source, _ = prepared_environment()
    original = cli._selected_runtime
    def replace_after_selection(*args):
        chosen = original(*args)
        manifest.write_bytes(manifest.read_bytes() + b' ')
        return chosen
    monkeypatch.setattr(cli, '_selected_runtime', replace_after_selection)
    code, result, error = invoke(service, capsys, 'submit', source, *selection(manifest))
    assert code == 1 and result is None
    assert service.jobs() == () and 'error' in error
    assert source.exists()


def test_disabled_registered_profile_cannot_prepare(prepared_environment, capsys):
    service, manifest, source, _ = prepared_environment()
    store = EngineProfileStore(service.library_root)
    profile = store.register(manifest, expected_sha256=pin(manifest), name='Selected')
    store.set_enabled(profile.id, False)
    code, _, error = invoke(service, capsys, 'submit', source, '--profile', profile.id)
    assert code == 1 and service.jobs() == ()
    assert 'disabled' in error


def test_recursive_scan_is_explicit_and_frozen_at_prepare(prepared_environment, capsys):
    service, manifest, source, _ = prepared_environment()
    folder = source.parent / 'selected'
    folder.mkdir()
    shutil.copyfile(source, folder / 'one.mp3')
    code, _, error = invoke(service, capsys, 'submit', folder, *selection(manifest))
    assert code == 1 and service.jobs() == () and '--recursive' in error
    code, result, _ = invoke(service, capsys, 'submit', folder, '--recursive', *selection(manifest))
    assert code == 0
    (folder / 'later.mp3').write_bytes(b'Not part of the saved selection')
    assert len(service.definition(result['id'])['inputs']) == 1


def test_status_is_recorded_metadata_not_completion_authority(prepared_environment, capsys):
    service, manifest, source, settings = prepared_environment()
    identity = service.prepare([source], manifest, settings)
    service._state(service._job(identity), 'complete', 'a' * 32)
    code, status, _ = invoke(service, capsys, 'status', identity)
    assert code == 0 and status['recorded_state'] == 'complete'
    assert status['completion_verified'] is False
    code, _, _ = invoke(service, capsys, 'verify', identity)
    assert code == 1


def test_controls_request_existing_worker_flags_without_claiming_acknowledgement(
        prepared_environment, capsys):
    service, manifest, source, settings = prepared_environment()
    identity = service.prepare([source], manifest, settings)
    job = service._job(identity)
    original = (job / 'job.json').read_bytes()
    for command, present in (('pause', True), ('continue', False)):
        code, response, _ = invoke(service, capsys, command, identity)
        assert code == 0 and response['acknowledged'] is False
        assert (job / 'pause.flag').exists() is present
    code, response, _ = invoke(service, capsys, 'cancel', identity)
    assert code == 0 and response['acknowledged'] is False
    assert (job / 'stop.flag').read_bytes() == b''
    assert (job / 'job.json').read_bytes() == original


def test_real_protocol_cli_run_restart_and_read_after_original_runtime_removal(
        prepared_environment, real_protocol_child, capsys):
    service, manifest, source, _ = prepared_environment()
    code, first, notices = invoke(service, capsys, 'submit', source, *selection(manifest),
                                  '--run', '--timeout', '10')
    assert code == 0 and first['input_count'] == 1
    identity = service.jobs()[0]['id']
    assert identity in notices and real_protocol_child
    code, second, _ = invoke(service, capsys, 'resume', identity, *selection(manifest), '--timeout', '10')
    assert code == 0 and second['run_id'] != first['run_id']
    assert list((service._job(identity) / 'history').iterdir())
    source.unlink()
    shutil.rmtree(manifest.parent)
    code, listed, _ = invoke(service, capsys, 'outputs', identity)
    assert code == 0 and listed['historical'] and not listed['runtime_current_verified']
    code, read, _ = invoke(service, capsys, 'read', identity)
    assert code == 0 and read['text'].startswith('Synthetic protocol text;')
    assert read['citation_authority'] is False


def test_zero_exit_without_receipt_does_not_become_cli_success(
        prepared_environment, real_protocol_child, capsys):
    service, manifest, source, _ = prepared_environment('zero_only')
    code, result, notices = invoke(service, capsys, 'submit', source, *selection(manifest),
                                  '--run', '--timeout', '10')
    assert code == 1 and result is None
    identity = service.jobs()[0]['id']
    assert identity in notices and 'prepared_job' in notices
    assert service.definition(identity)['inputs']


def test_reprepare_uses_retained_inputs_and_keeps_old_job_immutable(
        prepared_environment, capsys):
    service, manifest, source, settings = prepared_environment()
    old = service.prepare([source], manifest, settings)
    old_bytes = (service._job(old) / 'job.json').read_bytes()
    source.unlink()
    manifest.write_bytes(manifest.read_bytes() + b' ')
    code, new, _ = invoke(service, capsys, 'reprepare', old, *selection(manifest))
    assert code == 0 and new['id'] != old and not new['executed']
    assert (service._job(old) / 'job.json').read_bytes() == old_bytes
    assert service.definition(new['id'])['runtime_manifest_sha256'] == pin(manifest)
    assert service.definition(new['id'])['inputs'][0]['sha256'] == service.definition(old)['inputs'][0]['sha256']


def test_separate_reading_edit_and_export_preserve_accepted_transcript(
        prepared_environment, real_protocol_child, capsys, tmp_path):
    service, manifest, source, settings = prepared_environment()
    identity = service.prepare([source], manifest, settings)
    service.run(identity, manifest, timeout=10)
    before = service.completed(identity)
    edit = tmp_path / 'edit.txt'
    edit.write_text('Мій окремий читальний текст ü.', encoding='utf-8')
    code, saved, _ = invoke(service, capsys, 'save-edit', identity, '--text-file', edit)
    assert code == 0 and saved['status'] == 'saved'
    from carr_library.reader.narration import NarrationStore
    project, checksum = NarrationStore(service.library_root).load(saved['project_id'])
    assert checksum == saved['sha256'] and project.current_text == edit.read_text(encoding='utf-8')
    assert project.original_text.startswith('Synthetic protocol text;')
    assert service.completed(identity)['outputs'] == before['outputs']
    destination = tmp_path / 'reading-export.txt'
    code, exported, _ = invoke(service, capsys, 'export-edit', project.id, '--output', destination)
    assert code == 0 and exported['status'] == 'complete'
    assert destination.read_text(encoding='utf-8') == project.current_text
    code, _, _ = invoke(service, capsys, 'export-edit', project.id, '--output', destination)
    assert code == 1 and destination.read_text(encoding='utf-8') == project.current_text


def test_top_level_route_and_isolated_installed_origin(prepared_environment, tmp_path):
    service, _, _, _ = prepared_environment()
    import carr_library
    import bootstrap
    package_root = str(Path(carr_library.__file__).resolve().parent.parent)
    bootstrap_root = str(Path(bootstrap.__file__).resolve().parent.parent)
    script = (
        'import pathlib,sys;'
        'sys.path[:0]=sys.argv[1:3];'
        'import carr_library,bootstrap;'
        'assert pathlib.Path(carr_library.__file__).resolve().parent.parent==pathlib.Path(sys.argv[1]);'
        'assert pathlib.Path(bootstrap.__file__).resolve().parent.parent==pathlib.Path(sys.argv[2]);'
        'from carr_library.__main__ import main;'
        'raise SystemExit(main(sys.argv[3:]))'
    )
    result = subprocess.run([sys.executable, '-I', '-c', script, package_root, bootstrap_root,
                             'transcription', '--data-dir', str(service.library_root), 'jobs'],
                            cwd=tmp_path, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr[-4000:]
    assert json.loads(result.stdout) == []
