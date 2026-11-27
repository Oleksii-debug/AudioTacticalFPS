"""Cooperative boundaries; fixtures below do not qualify audio or OCR engines."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from carr_library.operations import (ResourceBroker, ResourceLimits, ResourceRequest,
    ResourceUnavailable, ResourceConflict)
from carr_library.operations.admission import document_request, production_request
from carr_library.audio import production
from carr_library.audio.models import MusicCue
from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker

MIB = 1024 ** 2


def test_audio_estimate_keeps_stems_music_and_selected_encoding(tmp_path):
    plan = production.parse_script('Narrator: Test\n[pause 2]')
    base = production_request(plan, tmp_path, ResourceLimits())
    # Four source characters use the declared .5-second estimate, plus 2s pause.
    pcm = 4 * plan.sample_rate * 4
    assert base.disk_bytes == pcm * (8 + 3 * 2) + 32 * MIB
    assert base.cpu_slots == ResourceLimits().cpu_slots
    assert base.memory_bytes == ResourceLimits().memory_bytes
    dry = production_request(replace(plan, export_stems=False), tmp_path, ResourceLimits())
    assert dry.disk_bytes < base.disk_bytes
    encoded = production_request(replace(plan, output_format='flac'), tmp_path, ResourceLimits())
    assert encoded.disk_bytes == base.disk_bytes + pcm


def test_audio_sound_duration_must_be_explicit_before_admission(tmp_path):
    plan = production.parse_script('[sound effect.wav]')
    with pytest.raises(ResourceUnavailable, match='Measure'):
        production_request(plan, tmp_path, ResourceLimits())
    request = production_request(plan, tmp_path, ResourceLimits(), sound_seconds={'effect.wav': 2.5})
    assert request.disk_bytes > 32 * MIB


@pytest.mark.parametrize('device', ['auto', 'mps', 'cuda:1'])
def test_audio_unknown_device_never_becomes_cpu(tmp_path, device):
    with pytest.raises(ResourceUnavailable, match='explicit'):
        production_request(production.parse_script('[pause 1]'), tmp_path,
            ResourceLimits(), music_devices=(device,))


def test_document_reserves_both_library_and_actual_parser_scratch(tmp_path):
    root, scratch = tmp_path / 'library', tmp_path / 'scratch'
    request = document_request(root, scratch, ResourceLimits())
    assert request.memory_bytes == 3 * 1024 ** 3
    assert request.disk_path == root and request.disk_bytes == 1024 * MIB
    assert request.additional_disks == ((scratch, 512 * MIB),)
    assert request.total_disk_bytes == 1536 * MIB


@pytest.mark.parametrize('fail', [False, True])
def test_audio_lease_spans_renderer_and_releases_after_its_cleanup(tmp_path, monkeypatch, fail):
    broker = ResourceBroker.for_app(tmp_path)
    observed = []
    def rendered(plan, root, **options):
        snapshot = broker.snapshot()
        assert snapshot['leases'][0]['operation_id'] == 'studio:' + 'a' * 32
        assert snapshot['usage']['memory_bytes'] == broker.limits.memory_bytes
        assert not options['cancelled']()
        observed.append(True)
        if fail:
            raise RuntimeError('Synthetic renderer failure after owned cleanup.')
        return {'status': 'complete', 'synthetic': True}
    monkeypatch.setattr(production, '_render_locked', rendered)
    options = dict(engine=SimpleNamespace(synthesize=lambda *a, **k: None), ffmpeg='unexecuted',
                   broker=broker, operation_id='studio:' + 'a' * 32)
    plan = production.parse_script('[pause 1]')
    if fail:
        with pytest.raises(RuntimeError, match='Synthetic'):
            production.render_project(plan, tmp_path / 'output', **options)
    else:
        assert production.render_project(plan, tmp_path / 'output', **options)['synthetic']
    assert observed == [True] and broker.snapshot()['leases'] == []


def test_audio_cancelled_while_waiting_invokes_no_renderer(tmp_path, monkeypatch):
    broker = ResourceBroker.for_app(tmp_path)
    stop = threading.Event()
    monkeypatch.setattr(production, '_render_locked',
                        lambda *a, **k: pytest.fail('Cancelled admission ran a renderer.'))
    with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots), operation_id='occupier'):
        with pytest.raises(InterruptedError, match='waiting'):
            production.render_project(production.parse_script('[pause 1]'), tmp_path / 'output',
                engine=SimpleNamespace(synthesize=lambda *a, **k: None), ffmpeg='unexecuted',
                broker=broker, cancelled=stop.is_set, on_status=lambda _: stop.set())
        assert len(broker.snapshot()['leases']) == 1
    assert broker.snapshot()['leases'] == []


def test_music_gpu_is_checked_before_any_renderer(tmp_path, monkeypatch):
    broker = ResourceBroker.for_app(tmp_path)
    plan = production.parse_script('[music engine=musicgen model_manifest=profiles/selected.json]\n[pause 1]')
    monkeypatch.setattr(production, '_production_music_manifest',
        lambda *a, **k: SimpleNamespace(manifest_sha256='a' * 64, settings={'device': 'cuda'}))
    monkeypatch.setattr(production, '_render_locked',
                        lambda *a, **k: pytest.fail('Unadmitted GPU ran a renderer.'))
    with pytest.raises(ResourceUnavailable, match='CUDA'):
        production.render_project(plan, tmp_path / 'output',
            engine=SimpleNamespace(synthesize=lambda *a, **k: None), ffmpeg='unexecuted', broker=broker)
    assert broker.snapshot()['leases'] == []


def test_music_selection_is_bound_again_after_waiting(tmp_path, monkeypatch):
    broker = ResourceBroker.for_app(tmp_path)
    calls = []
    def model(*args, **options):
        calls.append(True)
        return SimpleNamespace(manifest_sha256=('a' if len(calls) == 1 else 'b') * 64,
                               settings={'device': 'cpu'})
    def rendered(plan, root, **options):
        return options['music_manifest_resolver']('profiles/selected.json', 'musicgen',
                                                  cancelled=options['cancelled'])
    monkeypatch.setattr(production, '_production_music_manifest', model)
    monkeypatch.setattr(production, '_render_locked', rendered)
    plan = production.parse_script('[music engine=musicgen model_manifest=profiles/selected.json]\n[pause 1]')
    with pytest.raises(ResourceConflict, match='profile changed'):
        production.render_project(plan, tmp_path / 'output',
            engine=SimpleNamespace(synthesize=lambda *a, **k: None), ffmpeg='unexecuted', broker=broker)
    assert len(calls) == 2 and broker.snapshot()['leases'] == []


def import_fixture(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Resource fixture')
    source = tmp_path / 'not-decoded.pdf'
    source.write_bytes(b'Synthetic admission fixture; no parser test is claimed.')
    metadata = dict(title='Synthetic input', authors=(), language='en', edition=None,
                    volume=None, collections=(), work_id=None, edition_id=None)
    return app, workspace, source, metadata, Config()


def test_document_cancelled_before_admission_keeps_resumable_identity(tmp_path, monkeypatch):
    app, workspace, source, metadata, config = import_fixture(tmp_path)
    monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('A cancelled import launched.'))
    result = worker.run_request(app.root, workspace, source, metadata, config, cancelled=lambda: True)
    assert result['state'] == 'cancelled'
    record = worker.load_request(app.root, result['request_id'])
    assert record['state'] == 'cancelled' and record['metadata']['title'] == metadata['title']
    assert 'attempt' not in record and source.exists()


@pytest.mark.parametrize('failure', [False, True])
def test_document_lease_covers_native_supervisor_and_is_released(tmp_path, monkeypatch, failure):
    app, workspace, source, metadata, config = import_fixture(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    seen = []
    def invoke(message, **options):
        snapshot = broker.snapshot()
        assert snapshot['leases'][0]['operation_id'] == 'document:' + message['request_id']
        assert snapshot['usage']['memory_bytes'] == 3 * 1024 ** 3
        seen.append(message['attempt_id'])
        if failure:
            raise RuntimeError('Synthetic supervisor fault after child cleanup.')
        return {'state': 'cancelled', 'message': 'Synthetic stopped worker.'}
    monkeypatch.setattr(worker, '_invoke', invoke)
    if failure:
        with pytest.raises(RuntimeError, match='Synthetic'):
            worker.run_request(app.root, workspace, source, metadata, config, broker=broker)
    else:
        result = worker.run_request(app.root, workspace, source, metadata, config, broker=broker)
        assert result['state'] == 'cancelled'
    assert len(seen) == 1 and broker.snapshot()['leases'] == []


def test_queued_document_rechecks_saved_request_before_native_launch(tmp_path, monkeypatch):
    from contextlib import contextmanager
    app, workspace, source, metadata, config = import_fixture(tmp_path)
    prepared = worker.prepare_request(app.root, workspace, source, metadata, config)
    identity = prepared['request_id']
    broker = ResourceBroker.for_app(app.root)
    entered, errors = threading.Event(), []
    reserve = broker.reserve
    @contextmanager
    def observed(request, **options):
        entered.set()
        with reserve(request, **options) as lease:
            yield lease
    monkeypatch.setattr(broker, 'reserve', observed)
    monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('Changed request launched.'))
    def run():
        try:
            worker.run_request(app.root, workspace, source, {}, None,
                               request_id=identity, broker=broker)
        except BaseException as exc:
            errors.append(exc)
    with reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots), operation_id='occupier'):
        thread = threading.Thread(target=run)
        thread.start()
        assert entered.wait(5)
        changed = worker.load_request(app.root, identity)
        changed['metadata']['title'] = 'Changed while queued'
        worker._store(app.root, identity, changed)
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1
    assert isinstance(errors[0], ValueError) and 'changed while waiting' in str(errors[0])
    assert broker.snapshot()['leases'] == [] and source.exists()
