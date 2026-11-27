"""Shared task views forward audio queue controls without activating codecs."""
from dataclasses import replace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.batches import AudioBatchService
from carr_library.audio import jobs, media_conversion
from carr_library.operations import ResourceRequest, ResourceStateError
from carr_library.operations.facade import OperationFacade
from tests.audio.test_audio_batches import ffmpeg, local_queue, output_file


def test_shared_metadata_view_and_pause_use_the_existing_typed_queue_producer(tmp_path, ffmpeg, monkeypatch):
    app, service, state, source, _ = local_queue(tmp_path, ffmpeg)
    source.unlink()
    monkeypatch.setattr(media_conversion, 'runtime_identity',
                        lambda *args, **kwargs: pytest.fail('Task listing/control activated a native runtime'))
    facade = OperationFacade(app, broker=service.broker, audio_batches=service)
    rows = facade.list()
    row = next(row for row in rows if row.kind == 'audio-batch')
    assert (row.id, row.status, row.completed, row.total, row.unit) == (state.id, 'queued', 0, 1, 'items')
    assert row.verification == 'not_performed' and row.controls == ('pause', 'cancel')
    result = facade.control('audio-batch', state.id, 'pause')
    assert result['requested'] is True and result['acknowledged'] is False
    assert service.describe(state.id)['control']['action'] == 'pause'
    pending = facade.get('audio-batch', state.id)
    assert 'acknowledgement is pending' in pending.phase and pending.status == 'queued'


def test_shared_task_control_constructs_only_the_same_library_queue_service(tmp_path, ffmpeg):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    facade = OperationFacade(app, broker=service.broker)
    receipt = facade.control('audio-batch', state.id, 'cancel')
    assert receipt['operation_id'] == 'audio-batch:' + state.id
    assert facade.services['audio-batch'].library == service.library
    assert service.describe(state.id)['control']['action'] == 'cancel'


def test_foreign_queue_service_cannot_enter_the_current_shared_dashboard(tmp_path, ffmpeg):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    other = LibraryApp(tmp_path / 'other-library')
    with pytest.raises(ResourceStateError, match='another'):
        OperationFacade(other, audio_batches=service)


@pytest.mark.parametrize('damage', ['plan', 'control'])
def test_invalid_queue_metadata_is_attention_and_has_no_shared_controls(tmp_path, ffmpeg, damage):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    path = service.path(state.id) / ('batch.json' if damage == 'plan' else 'control.json')
    value = jobs.read_json(path)
    value['plan_sha256' if damage == 'plan' else 'batch_id'] = '0' * (64 if damage == 'plan' else 32)
    jobs.atomic_json(path, value)
    row = next(row for row in OperationFacade(app).list() if row.kind == 'audio-batch')
    assert row.status == 'attention' and row.controls == ()
    assert row.checkpoint.endswith('/batch.json')


def test_shared_resource_view_keeps_the_live_audio_queue_identity(tmp_path, ffmpeg):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    facade = OperationFacade(app, broker=service.broker)
    with service.broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=1024),
                                operation_id='audio-batch:' + state.id):
        row = facade.get('audio-batch', state.id)
        assert row.live is True
        assert facade.resources()['leases'][0]['operation_id'] == row.key


def test_dashboard_checkpoint_is_not_a_false_output_verification_claim(tmp_path, ffmpeg):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    completed = service.run(state.id)
    assert completed.status == 'complete'
    output_file(service, completed).unlink()
    row = OperationFacade(app).get('audio-batch', state.id)
    assert row.status == 'complete' and row.verification == 'not_performed' and row.controls == ()
    assert 'verify outputs' in row.phase.lower()
