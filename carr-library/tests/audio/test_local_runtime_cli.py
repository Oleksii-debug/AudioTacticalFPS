"""Headless binding is scoped to media execution, never saved text inspection."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from carr_library.audio import local_runtime
from carr_library import application, transcription_cli
from carr_library.studio import cli


@pytest.mark.parametrize('operation', ['projects', 'jobs'])
def test_scene_data_inspection_does_not_require_selected_audio(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(application, 'LibraryApp', lambda root: SimpleNamespace(root=Path(root)))
    service = create_autospec(cli.ProductionService, instance=True)
    service.projects = SimpleNamespace(list=lambda: [])
    service.jobs.return_value = []
    monkeypatch.setattr(cli, 'ProductionService', lambda *a, **k: service)
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime',
                        lambda *a, **k: pytest.fail('saved project inspection attempted audio activation'))
    assert cli.main(['--data-dir', str(tmp_path), operation]) == 0
    service.close.assert_called_once_with()
    service.run.assert_not_called()


def test_scene_resume_binds_and_releases_the_actual_execution_lifetime(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(application, 'LibraryApp', lambda root: SimpleNamespace(root=Path(root)))
    class Binding:
        def close(self): calls.append('close')
    def bind(root, *, strict):
        assert root == tmp_path and strict is True
        calls.append('bind')
        return Binding()
    def run(identity, **kwargs):
        assert calls == ['bind'] and identity == 'saved-job'
        calls.append('run')
        return {'id': identity, 'status': 'completed', 'destination': str(tmp_path / 'out'),
                'artifacts': [], 'result': {}}
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', bind)
    service = create_autospec(cli.ProductionService, instance=True, spec_set=True)
    service.run.side_effect = run
    service.close.side_effect = lambda: calls.append('production.close')
    monkeypatch.setattr(cli, 'ProductionService', lambda *a, **k: service)
    assert cli.main(['--data-dir', str(tmp_path), 'resume', 'saved-job']) == 0
    assert calls == ['bind', 'run', 'production.close', 'close']
    service.run.assert_called_once()
    service.close.assert_called_once_with()


@pytest.mark.parametrize('operation, extra', [('jobs', []), ('show', ['id']),
                                             ('verify', ['id']), ('export', ['id', 'output'])])
def test_transcript_inspection_and_export_survive_unavailable_audio(tmp_path, monkeypatch, operation, extra):
    service = SimpleNamespace(list=lambda: [], get=lambda job: {'id': job},
                              verify=lambda job: {'status': 'verified'},
                              export=lambda job, destination: destination)
    monkeypatch.setattr(application, 'LibraryApp',
                        lambda root: SimpleNamespace(root=Path(root), transcription=lambda workspace: service))
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime',
                        lambda *a, **k: pytest.fail('saved transcript used an audio runtime'))
    assert transcription_cli.main(['--data-dir', str(tmp_path), '--workspace', 'workspace', operation, *extra]) == 0


def test_transcription_run_holds_selected_runtime_until_worker_returns(tmp_path, monkeypatch):
    calls = []
    @contextmanager
    def bind(root, *, strict):
        assert strict is True and root == tmp_path
        calls.append('enter')
        try:
            yield object()
        finally:
            calls.append('exit')
    def run(job, **kwargs):
        assert calls == ['enter']
        calls.append('run')
        return {'status': 'completed'}
    service = SimpleNamespace(run=run)
    monkeypatch.setattr(application, 'LibraryApp',
                        lambda root: SimpleNamespace(root=Path(root), transcription=lambda workspace: service))
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', bind)
    assert transcription_cli.main(['--data-dir', str(tmp_path), '--workspace', 'workspace', 'run', 'id']) == 0
    assert calls == ['enter', 'run', 'exit']


def test_assistant_preview_does_not_activate_media_or_inspect_a_runtime(tmp_path, monkeypatch):
    from carr_library import assistant
    source = tmp_path / 'instructions.txt'
    source.write_text('Search only.', encoding='utf-8')
    calls = []
    class Service:
        def __init__(self, app, *, production):
            calls.append('created')
            assert callable(production)
        def preview(self, instructions, scope):
            assert instructions == 'Search only.'
            return SimpleNamespace(to_dict=lambda: {'steps': []})
    monkeypatch.setattr(application, 'LibraryApp', lambda root: SimpleNamespace(root=Path(root)))
    production = create_autospec(cli.ProductionService, instance=True, spec_set=True)
    monkeypatch.setattr(cli, 'ProductionService', lambda *a, **k: production)
    monkeypatch.setattr(assistant, 'AssistantService', Service)
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime',
                        lambda *a, **k: pytest.fail('assistant preview activated audio'))
    assert cli.assistant_main(['--data-dir', str(tmp_path), '--instructions', str(source)]) == 0
    assert calls == ['created']
    production.close.assert_called_once_with()


def test_silence_cli_requires_explicit_criteria_and_keeps_selected_runtime_through_inspection(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(application, 'LibraryApp', lambda root: SimpleNamespace(root=Path(root)))
    @contextmanager
    def binding(root, *, strict, cancelled):
        assert root == tmp_path and strict is True
        assert cancelled() is False
        calls.append('bound')
        try:
            yield object()
        finally:
            calls.append('released')
    service = create_autospec(cli.ProductionService, instance=True, spec_set=True)
    def inspect(identity, *, threshold_dbfs, minimum_seconds, progress, runtime_binding):
        assert identity == 'saved-job' and calls == []
        runtime_binding(cancelled=lambda: False)
        assert calls == ['bound']
        assert threshold_dbfs == -50 and minimum_seconds == 2
        progress('Measuring at or below the explicit threshold.')
        calls.append('inspected')
        return {'schema': 'carr-silence-inspection/1', 'status': 'complete',
                'intervals': [], 'interval_count': 0, 'omitted_count': 0}
    service.inspect_silence.side_effect = inspect
    service.close.side_effect = lambda: calls.append('producer-closed')
    monkeypatch.setattr(cli, 'ProductionService', lambda *a, **k: service)
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', binding)
    assert cli.main(['--data-dir', str(tmp_path), 'inspect-silence', 'saved-job',
                     '--threshold-dbfs', '-50', '--minimum-seconds', '2']) == 0
    assert calls == ['bound', 'inspected', 'producer-closed', 'released']
    service.submit.assert_not_called()
    service.run.assert_not_called()
    service.close.assert_called_once_with()
    assert '"interval_count": 0' in capsys.readouterr().out


@pytest.mark.parametrize('extra', [
    ['--threshold-dbfs', '-50'],
    ['--minimum-seconds', '2'],
])
def test_silence_cli_does_not_guess_missing_criteria(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(application, 'LibraryApp', lambda *a: pytest.fail('Incomplete inspection created a library.'))
    with pytest.raises(SystemExit) as caught:
        cli.main(['--data-dir', str(tmp_path), 'inspect-silence', 'saved-job', *extra])
    assert caught.value.code == 2


def test_silence_cli_cancellation_does_not_offer_production_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(application, 'LibraryApp', lambda root: SimpleNamespace(root=Path(root)))
    @contextmanager
    def binding(*args, **kwargs):
        yield object()
    service = create_autospec(cli.ProductionService, instance=True, spec_set=True)
    service.inspect_silence.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(cli, 'ProductionService', lambda *a, **k: service)
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', binding)
    assert cli.main(['--data-dir', str(tmp_path), 'inspect-silence', 'saved-job',
                     '--threshold-dbfs', '-50', '--minimum-seconds', '2']) == 130
    text = capsys.readouterr().err
    assert 'Silence inspection interrupted' in text and 'preserved' in text
    assert 'Resume its saved job' not in text
    service.run.assert_not_called()
    service.close.assert_called_once_with()


@pytest.mark.parametrize('masked', [False, True], ids=['direct-pending', 'masked-pending'])
def test_silence_cli_waits_for_both_existing_owners_before_releasing_runtime(tmp_path, monkeypatch, masked):
    import threading
    import time
    from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
    calls, entered, finished = [], threading.Event(), threading.Event()
    permits = [threading.Event(), threading.Event()]
    owners = [CleanupOwner(lambda index=index: permits[index].is_set()) for index in range(2)]
    first = AudioCleanupPending('First inspection owner.', cleanup_owner=owners[0])
    second = AudioCleanupPending('Second inspection owner.', cleanup_owner=owners[1])
    second.__cause__ = first
    failure = OSError('Masked inspection cleanup') if masked else second
    if masked:
        failure.__cause__ = second
    monkeypatch.setattr(application, 'LibraryApp', lambda root: SimpleNamespace(root=Path(root)))
    @contextmanager
    def binding(*args, **kwargs):
        calls.append('bound')
        try:
            yield object()
        finally:
            assert all(owner.quiescent for owner in owners)
            calls.append('released')
    service = create_autospec(cli.ProductionService, instance=True, spec_set=True)
    def inspect(*args, **kwargs):
        assert calls == []
        kwargs['runtime_binding'](cancelled=lambda: False)
        entered.set()
        raise failure
    service.inspect_silence.side_effect = inspect
    monkeypatch.setattr(cli, 'ProductionService', lambda *a, **k: service)
    monkeypatch.setattr(local_runtime, 'open_local_audio_runtime', binding)
    result = []
    def run():
        try:
            result.append(cli.main(['--data-dir', str(tmp_path), 'inspect-silence', 'saved-job',
                '--threshold-dbfs', '-50', '--minimum-seconds', '2']))
        finally:
            finished.set()
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5) and calls == ['bound']
        permits[0].set()
        owners[0].close()
        assert not finished.wait(.05) and calls == ['bound']
        permits[1].set()
        owners[1].close()
        thread.join(5)
        assert not thread.is_alive() and result == [1]
        assert calls == ['bound', 'released']
        service.run.assert_not_called()
        service.close.assert_called_once_with()
    finally:
        for permit in permits:
            permit.set()
        for owner in owners:
            owner.close()
        thread.join(5)
