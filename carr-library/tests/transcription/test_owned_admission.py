"""Real native locks and child lifetime; synthetic protocol, no inference."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library import transcription
from carr_library.transcription import service as local_files
from carr_library.transcription.ownership import operation_lock


def load_fixture(name, filename):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('protocol', ['receipts', 'local-file'])
@pytest.mark.parametrize('journal_failure', [False, True], ids=['saved-review', 'failed-review-save'])
def test_real_job_and_broker_locks_survive_pending_child_and_failed_status_save(
        tmp_path, monkeypatch, protocol, journal_failure):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Owned ASR')
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Synthetic retained media identity.')
    if protocol == 'receipts':
        fixture = load_fixture('owned_strict_fixture', 'test_supplied_worker_bridge.py')
        models = tmp_path / 'models'
        fixture.synthetic_models(models)
        worker = tmp_path / 'worker.py'
        worker.write_text(fixture.SYNTHETIC_WORKER.replace('__MODE__', repr('complete')), encoding='utf-8')
        service = transcription.TranscriptionService(app, workspace, broker=broker)
        service.configure(Path(sys.executable), worker, models)
        state = service.get(service.submit([source], options={'model': 'small'}))
        module, path, lock_name = transcription, service._path(state['id']), 'transcribe'
    else:
        fixture = load_fixture('owned_local_file_fixture', 'fixture_runtime.py')
        runtime = fixture.make_runtime(tmp_path / 'runtime')
        service = local_files.TranscriptionService(app.root / 'state/transcription', broker=broker)
        profile = fixture.register(service, runtime)
        state = service.create_job(profile['id'], [source])
        module, path, lock_name = local_files, service._job_root(state['id']), 'run'
    original_save = service._save
    started = False
    processes, owners = [], []
    def save(*args, **kwargs):
        if started and journal_failure:
            raise OSError('Synthetic status persistence failure')
        return original_save(*args, **kwargs)
    monkeypatch.setattr(service, '_save', save)
    def run_process(*args, **kwargs):
        nonlocal started
        assert kwargs['owned_tree'] is True
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c',
                                  'import sys; sys.stdin.buffer.read()'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        processes.append(child)
        owner = CleanupOwner(lambda: child.poll() is not None)
        owners.append(owner)
        started = True
        raise AudioCleanupPending('Synthetic retained real-child ownership', cleanup_owner=owner)
    monkeypatch.setattr(module, 'run_process', run_process)
    try:
        expected = OSError if journal_failure else AudioCleanupPending
        with pytest.raises(expected):
            service.run(state['id'])
        assert processes[0].poll() is None
        assert len(broker.snapshot()['leases']) == 1
        with pytest.raises(RuntimeError, match='already owned'):
            with operation_lock(path, lock_name):
                raise AssertionError('A second execution must not enter.')
        assert owners[0].close() is False
        processes[0].stdin.close()
        processes[0].wait(timeout=10)
        released = []
        thread = threading.Thread(target=lambda: released.append(owners[0].close()))
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and released == [True]
        assert broker.snapshot()['leases'] == []
        with operation_lock(path, lock_name):
            pass
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
        for owner in owners:
            owner.close()
