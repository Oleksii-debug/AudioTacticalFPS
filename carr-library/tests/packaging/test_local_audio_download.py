"""Explicit interactive/download consent reuses the current local preparation."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.audio import runtime
from tests.packaging.test_audio_acquisition import install_transport
from tests.packaging.test_local_audio_setup import selected, setup_tool


@pytest.fixture
def acquisition(selected, setup_tool, monkeypatch):
    archive, output, library = selected
    preparer = setup_tool._load_preparer()
    checks = []
    def synthetic_native(directory):
        checks.append(Path(directory))
        for name in runtime.ARCHIVE_MEMBERS:
            runtime.regular_file(Path(directory) / name)
        return {'status': 'PASS', 'platform': 'Windows', 'fixture': 'synthetic-only'}
    monkeypatch.setattr(preparer, '_native_check', synthetic_native)
    monkeypatch.setattr(setup_tool, '_load_preparer', lambda: preparer)
    monkeypatch.setattr(setup_tool, '_require_windows', lambda: None)
    transport = install_transport(monkeypatch, [{'body': archive.read_bytes()}])
    return SimpleNamespace(archive=archive, output=output, library=library,
        setup=setup_tool, preparer=preparer, checks=checks, transport=transport)


def arguments(e):
    return ['--destination', str(e.output), '--library-root', str(e.library)]


def temporary_inputs(e):
    return tuple(e.output.parent.glob('.carr-audio-acquire-*'))


def test_download_plan_accounts_for_archive_copy_and_does_not_authorize_execution(selected, setup_tool, monkeypatch):
    archive, output, library = selected
    monkeypatch.setattr(setup_tool, '_load_preparer', lambda: pytest.fail('Plans cannot load the network helper.'))
    offline = setup_tool.plan(destination=output, library_root=library)
    download = setup_tool.plan(destination=output, library_root=library, download=True)
    assert offline['downloads'] is False and download['downloads'] is True
    assert download['archive'] is None and download['archive_identity'] is None
    assert download['required_free_bytes'] == offline['required_free_bytes'] + 2 * runtime.FFMPEG_ARCHIVE_BYTES
    assert download['archive_sha256'] == runtime.FFMPEG_ARCHIVE_SHA256
    assert download['engine_settings_changed'] is False
    assert not output.exists()


def test_default_cli_remains_offline_plan_only(acquisition, monkeypatch, capsys):
    e = acquisition
    monkeypatch.setattr(e.setup, 'download_local', lambda plan: pytest.fail('No download consent.'))
    monkeypatch.setattr(e.setup, 'prepare_local', lambda plan: pytest.fail('No execution consent.'))
    assert e.setup.main(arguments(e)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'plan' and report['downloads'] is False
    assert not e.transport.requests and not e.output.exists() and not temporary_inputs(e)


@pytest.mark.parametrize('answer', ['NO', 'no', 'yes', '', 'EOF'])
def test_interactive_download_refusal_is_read_only(acquisition, monkeypatch, capsys, answer):
    e = acquisition
    prompts = []
    def respond(prompt):
        prompts.append(prompt)
        if answer == 'EOF':
            raise EOFError
        return answer
    monkeypatch.setattr('builtins.input', respond)
    monkeypatch.setattr(e.setup, 'download_local', lambda plan: pytest.fail('YES was not granted.'))
    assert e.setup.main([*arguments(e), '--interactive']) == 0
    captured = capsys.readouterr()
    assert prompts and 'Download this exact archive' in prompts[0]
    assert '"downloads": true' in captured.out and 'Cancelled.' in captured.out
    assert not e.transport.requests and not e.output.exists() and not temporary_inputs(e)


@pytest.mark.parametrize('consent', ['interactive', 'download'])
def test_explicit_download_uses_current_preparer_and_cleans_temporary_archive(acquisition, monkeypatch, capsys, consent):
    e = acquisition
    original = e.archive.read_bytes()
    prompts = []
    def approve(prompt):
        prompts.append(prompt)
        return 'YES'
    monkeypatch.setattr('builtins.input', approve)
    assert e.setup.main([*arguments(e), '--' + consent]) == 0
    captured = capsys.readouterr()
    assert '"status": "prepared"' in captured.out and '"status": "downloaded"' in captured.out
    assert len(prompts) == (1 if consent == 'interactive' else 0)
    assert len(e.transport.requests) == 1 and e.transport.bodies[0].closed
    assert len(e.checks) == 1
    result = runtime.verify_runtime(e.output / 'runtime')
    assert result['native_validation']['fixture'] == 'synthetic-only'
    assert (e.output / 'runtime' / 'ffmpeg.exe').read_bytes() == b'synthetic ffmpeg bytes'
    assert not temporary_inputs(e) and e.archive.read_bytes() == original
    assert (e.library / 'keep.txt').read_bytes() == b'Original library data'
    assert '"engine_settings_changed": false' in captured.out


def test_manual_archive_preparation_does_not_enter_network_path(acquisition, monkeypatch):
    e = acquisition
    monkeypatch.setattr(e.preparer, '_download_opener', lambda: pytest.fail('Manual archive stays offline.'))
    report = e.setup.prepare_local(e.setup.plan(e.archive, e.output, library_root=e.library))
    assert report['downloads'] is False and len(e.checks) == 1
    assert not e.transport.requests and not temporary_inputs(e)


@pytest.mark.parametrize('extra', [
    ['--download', '--prepare'], ['--download', '--interactive'], ['--download', '--archive'],
])
def test_conflicting_authority_options_refuse_before_any_write(acquisition, extra):
    e = acquisition
    args = [*arguments(e), *extra]
    if extra[-1] == '--archive':
        args.append(str(e.archive))
    with pytest.raises(SystemExit) as error:
        e.setup.main(args)
    assert error.value.code == 2
    assert not e.transport.requests and not e.output.exists() and not temporary_inputs(e)


@pytest.mark.parametrize('field', ['archive_sha256', 'archive_bytes', 'runtime_files', 'downloads'])
def test_changed_reviewed_download_plan_refuses_before_network(acquisition, field):
    e = acquisition
    reviewed = deepcopy(e.setup.plan(destination=e.output, library_root=e.library, download=True))
    if field == 'archive_sha256':
        reviewed[field] = '0' * 64
    elif field == 'archive_bytes':
        reviewed[field] += 1
    elif field == 'runtime_files':
        reviewed[field] = ['foreign.exe']
    else:
        reviewed[field] = False
    with pytest.raises(ValueError):
        e.setup.download_local(reviewed)
    assert not e.transport.requests and not e.output.exists() and not temporary_inputs(e)


def test_destination_created_during_confirmation_is_preserved_without_download(acquisition, monkeypatch):
    e = acquisition
    def approve(prompt):
        e.output.mkdir()
        (e.output / 'foreign').write_bytes(b'Keep the new independent folder.')
        return 'YES'
    monkeypatch.setattr('builtins.input', approve)
    assert e.setup.main([*arguments(e), '--interactive']) == 1
    assert (e.output / 'foreign').read_bytes() == b'Keep the new independent folder.'
    assert not e.transport.requests and not temporary_inputs(e)


def test_download_disk_budget_refuses_before_temporary_creation(acquisition, monkeypatch):
    e = acquisition
    reviewed = e.setup.plan(destination=e.output, library_root=e.library, download=True)
    monkeypatch.setattr(e.setup.shutil, 'disk_usage',
                        lambda path: SimpleNamespace(free=reviewed['required_free_bytes'] - 1))
    with pytest.raises(OSError, match='disk space'):
        e.setup.download_local(reviewed)
    assert not e.transport.requests and not e.output.exists() and not temporary_inputs(e)


def test_unsupported_platform_cannot_start_download_or_create_work(acquisition, monkeypatch):
    e = acquisition
    def refuse():
        raise RuntimeError('Actual preparation and verification require Windows x64.')
    monkeypatch.setattr(e.setup, '_require_windows', refuse)
    assert e.setup.main([*arguments(e), '--download']) == 1
    assert not e.transport.requests and not e.output.exists() and not temporary_inputs(e)


def test_wrong_download_digest_cleans_inputs_without_publishing_runtime(acquisition, monkeypatch):
    e = acquisition
    original = e.archive.read_bytes()
    e.transport = install_transport(monkeypatch, [{'body': bytes([original[0] ^ 1]) + original[1:]}])
    assert e.setup.main([*arguments(e), '--download']) == 1
    assert e.transport.bodies[0].closed and not temporary_inputs(e)
    assert not e.output.exists() and e.archive.read_bytes() == original
    assert (e.library / 'keep.txt').read_bytes() == b'Original library data'


def test_native_failure_is_not_reported_as_prepared_and_does_not_keep_network_input(acquisition, monkeypatch, capsys):
    e = acquisition
    def fail(directory):
        raise RuntimeError('Synthetic native validation failure.')
    monkeypatch.setattr(e.preparer, '_native_check', fail)
    assert e.setup.main([*arguments(e), '--download']) == 1
    captured = capsys.readouterr()
    assert 'Synthetic native validation failure.' in captured.err
    assert '"status": "prepared"' not in captured.out
    assert e.output.is_dir() and not (e.output / 'runtime').exists()
    assert not temporary_inputs(e) and e.transport.bodies[0].closed


def test_temporary_cleanup_failure_cannot_leave_a_success_receipt(acquisition, monkeypatch, capsys):
    e = acquisition
    original = e.setup.tempfile.TemporaryDirectory
    @contextmanager
    def failing_cleanup(*args, **kwargs):
        with original(*args, **kwargs) as directory:
            yield directory
        if kwargs.get('prefix') == '.carr-audio-acquire-':
            raise OSError('Synthetic temporary cleanup failure.')
    monkeypatch.setattr(e.setup.tempfile, 'TemporaryDirectory', failing_cleanup)
    assert e.setup.main([*arguments(e), '--download']) == 1
    captured = capsys.readouterr()
    assert 'Synthetic temporary cleanup failure.' in captured.err
    assert '"status": "prepared"' not in captured.out
    assert not temporary_inputs(e)


def test_keyboard_stop_during_transfer_keeps_existing_library_and_archive(acquisition, monkeypatch, capsys):
    e = acquisition
    original = e.archive.read_bytes()
    def stop(body, value):
        if value:
            raise KeyboardInterrupt
    e.transport = install_transport(monkeypatch, [{'body': original, 'action': stop}])
    assert e.setup.main([*arguments(e), '--download']) == 130
    assert not e.output.exists() and not temporary_inputs(e)
    assert e.transport.bodies[0].closed and e.archive.read_bytes() == original
    assert (e.library / 'keep.txt').read_bytes() == b'Original library data'
    assert '"status": "prepared"' not in capsys.readouterr().out
