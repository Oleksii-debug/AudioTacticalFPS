"""Actual canonical supplied-worker journals and shared admission; no model quality claim."""
from dataclasses import replace
from pathlib import Path
import runpy
from types import SimpleNamespace
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import transcription as asr
from carr_library.audio.models import AudioBudget, AudioCancelled
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations import (OperationFacade, ResourceBroker, ResourceLimits,
                                     ResourceRequest, ResourceStateError, ResourceUnavailable)
from carr_library.ui.operations_dialog import OperationsController


@pytest.fixture
def installation(tmp_path, monkeypatch):
    helpers = runpy.run_path(str(Path(__file__).parents[1] / 'audio' / 'test_supplied_transcription.py'))
    return helpers['installation'].__wrapped__(tmp_path, monkeypatch)


def setup_service(tmp_path, installation, name='data'):
    app = LibraryApp(tmp_path / name)
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    service = asr.TranscriptionService(app.root / 'state', asr.SuppliedWhisperRuntime(installation),
        budget=AudioBudget(timeout=10, reserve_disk_bytes=0, max_output_bytes=4 * 1024**2), broker=broker)
    return app, broker, service


def source_file(tmp_path, name, text):
    path = tmp_path / (name + '.wav')
    path.write_text(text, encoding='utf-8')
    return path


def test_actual_asr_pause_view_handoff_resume_and_completed_reuse(tmp_path, installation, monkeypatch):
    app, broker, service = setup_service(tmp_path, installation)
    first = source_file(tmp_path, 'first', 'First synthetic transcript')
    second = source_file(tmp_path, 'second', 'Second synthetic transcript')
    identity = service.create([first, second], settings=asr.TranscriptionSettings(language='uk'))
    facade = OperationFacade(app, broker=broker, transcription=service)
    launches, live, waiting = [], [], []
    original = asr.run_process

    def run(*args, **kwargs):
        launches.append(True)
        live.append(broker.snapshot())
        return original(*args, **kwargs)

    monkeypatch.setattr(asr, 'run_process', run)

    def status(message):
        if message == 'Waiting for shared local transcription resources.':
            waiting.append(message)
            if len(waiting) == 2:
                row = facade.get('asr', identity)
                assert (row.completed, row.total) == (1, 2)
                facade.control('asr', identity, 'pause')

    with pytest.raises(AudioCancelled):
        service.run(identity, on_status=status)
    view = facade.get('asr', identity)
    assert (view.status, view.completed, view.total) == ('paused', 1, 2)
    assert set(view.controls) == {'pause', 'cancel'}
    assert view.checkpoint == f'state/transcription/{identity}/job.json'
    assert read_json(service.root / identity / 'control.json') == {'command': 'pause'}
    accepted = service.load(identity)['completed'][0]
    output = service.root / identity / 'outputs' / accepted / 'transcript.txt'
    before = output.read_bytes(), output.stat().st_mtime_ns
    assert len(launches) == 1
    assert live[0]['leases'][0]['operation_id'] == 'asr:' + identity
    assert live[0]['usage']['cpu_slots'] == broker.limits.cpu_slots
    assert live[0]['usage']['memory_bytes'] == broker.limits.memory_bytes
    assert broker.snapshot()['leases'] == []

    frame = SimpleNamespace(library=app, cancel_event=threading.Event(), working=False, _closing=False)
    controller = OperationsController(frame)
    controller.jobs = (view,)
    opened = []
    assert controller.open_transcription(view.key, opened.append)
    assert opened == [identity]
    assert service.load(identity)['status'] == 'paused'  # handoff never starts inference
    assert len(service.jobs()) == 1 and len(launches) == 1

    service.control(identity, 'run')
    complete = service.run(identity)
    assert complete['status'] == 'complete' and len(launches) == 2
    assert (output.read_bytes(), output.stat().st_mtime_ns) == before
    assert first.read_text(encoding='utf-8') == 'First synthetic transcript'
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='other-heavy-operation', timeout=0):
        assert service.run(identity)['status'] == 'complete'
        assert len(launches) == 2
        assert len(broker.snapshot()['leases']) == 1
    assert broker.snapshot()['leases'] == []


def test_asr_resource_wait_cancel_never_starts_worker_and_uses_existing_control(tmp_path, installation, monkeypatch):
    app, broker, service = setup_service(tmp_path, installation)
    source = source_file(tmp_path, 'pending', 'Retained input')
    identity = service.create([source])
    facade = OperationFacade(app, broker=broker, transcription=service)
    monkeypatch.setattr(asr, 'run_process', lambda *_args, **_kwargs: pytest.fail('Worker started before admission'))
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0):
        def status(message):
            if message == 'Waiting for shared local transcription resources.':
                facade.control('asr', identity, 'cancel')
        with pytest.raises(AudioCancelled):
            service.run(identity, on_status=status)
        assert len(broker.snapshot()['leases']) == 1
    row = facade.get('asr', identity)
    assert (row.status, row.completed, row.total) == ('cancelled', 0, 1)
    assert read_json(service.root / identity / 'control.json') == {'command': 'cancel'}
    assert not (service.root / identity / 'outputs').exists()
    assert source.read_text(encoding='utf-8') == 'Retained input'


def test_same_content_addressed_id_in_another_data_folder_grants_no_control(tmp_path, installation):
    app_a, broker_a, service_a = setup_service(tmp_path, installation, 'a')
    app_b, broker_b, service_b = setup_service(tmp_path, installation, 'b')
    source = source_file(tmp_path, 'shared', 'Same lawful synthetic audio')
    first, second = service_a.create([source]), service_b.create([source])
    assert first == second
    a, b = service_a.root / first / 'control.json', service_b.root / second / 'control.json'
    before = a.read_bytes(), b.read_bytes()
    with pytest.raises(ResourceStateError, match='another data folder'):
        OperationFacade(app_a, broker=broker_a, transcription=service_b).control('asr', first, 'cancel')
    with pytest.raises(ResourceStateError, match='another data folder'):
        OperationFacade(app_a, broker=broker_b, transcription=service_a)
    bound = OperationFacade(app_a, broker=broker_a, transcription=service_a)
    bound.services['asr'] = service_b
    with pytest.raises(ResourceStateError, match='another data folder'):
        bound.control('asr', first, 'pause')
    assert (a.read_bytes(), b.read_bytes()) == before


def test_impossible_complete_inventory_is_attention_without_receipt_scan(tmp_path, installation, monkeypatch):
    app, broker, service = setup_service(tmp_path, installation)
    identity = service.create([source_file(tmp_path, 'source', 'Incomplete journal')])
    path = service.root / identity / 'job.json'
    record = read_json(path)
    record['status'] = 'complete'
    atomic_json(path, record, replace=True)
    monkeypatch.setattr(service, '_receipt', lambda *_args, **_kwargs: pytest.fail('Dashboard scanned output bytes'))
    with pytest.raises(ValueError, match='completion records'):
        service.recorded_status(identity)
    rows = OperationFacade(app, broker=broker, transcription=service).list()
    assert len(rows) == 1 and rows[0].status == 'attention'


def test_unconfigured_or_remapped_cuda_never_starts_a_synthetic_worker(tmp_path, installation, monkeypatch):
    app, broker, service = setup_service(tmp_path, installation)
    identity = service.create([source_file(tmp_path, 'cuda', 'GPU admission fixture')],
                              settings=asr.TranscriptionSettings(device='cuda', compute_type='float16'))
    monkeypatch.setattr(asr, 'run_process', lambda *_args, **_kwargs: pytest.fail('Unadmitted GPU worker started'))
    with pytest.raises(ResourceUnavailable, match='explicit resource capacity'):
        service.run(identity)
    broker.configure(replace(broker.limits, gpu_devices=(('cuda:0', 1024**3),)))
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '1')
    with pytest.raises(ResourceUnavailable, match='remapping'):
        service.run(identity)
    assert broker.snapshot()['leases'] == []
