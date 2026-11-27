"""Signed admission remains attached to the actual new batch work boundary."""
import json

from carr_library.application import LibraryApp
from carr_library.audio.batches import AudioBatchService
from carr_library.audio.batch_models import Selection
from carr_library.research.export_file import capture_export_destination
from carr_library.security.admission import operation_context
from carr_library.security.controller import TrustController
from .test_domain_admission import authority, expire
from tests.audio.test_audio_batches import ffmpeg, pcm, preset, output_file


def test_actual_completed_batch_replay_and_export_survive_expiry_but_new_codec_work_does_not(
        authority, tmp_path, ffmpeg):
    app = LibraryApp(tmp_path / 'library', trust_controller=authority.controller)
    service = AudioBatchService(app.root, ffmpeg=ffmpeg, trust_controller=authority.controller)
    source = pcm(tmp_path / 'original.wav')
    state = service.create((Selection('audio', str(source), 'Allowed output', preset()),))
    first = service.run(state.id)
    assert first.status == 'complete'
    exact = output_file(service, first).read_bytes()
    expire(authority)
    assert service.run(state.id).status == 'complete'
    target = tmp_path / 'protected-data-export.wav'
    approval = capture_export_destination(target, private_root=app.root)
    service.export_file(state.id, first.items[0].id, first.items[0].outputs[0], target, approval=approval)
    assert target.read_bytes() == exact
    fresh = service.create((Selection('audio', str(source), 'New delivery work', preset('flac')),))
    refused = service.run(fresh.id)
    assert refused.status == refused.items[0].state == 'failed'
    assert 'Existing work' in refused.items[0].error
    assert not output_file(service, refused).exists()
    assert output_file(service, first).read_bytes() == exact


def test_one_actual_item_finishes_on_its_admitted_lease_and_the_next_item_requires_fresh_permission(
        authority, tmp_path, ffmpeg):
    app = LibraryApp(tmp_path / 'library', trust_controller=authority.controller)
    service = AudioBatchService(app.root, ffmpeg=ffmpeg, trust_controller=authority.controller)
    source = pcm(tmp_path / 'source.wav')
    state = service.create((Selection('audio', str(source), 'First', preset()),
                            Selection('audio', str(source), 'Second', preset('flac'))))
    expired = []
    def progress(message):
        if message.startswith('Converting item 1 ') and not expired:
            expired.append(True)
            expire(authority)
    result = service.run(state.id, on_status=progress)
    assert result.status == 'failed'
    assert [item.state for item in result.items] == ['complete', 'failed']
    assert service.verified_receipt(result, result.items[0])
    assert not output_file(service, result, 1).exists()
    assert service.broker.snapshot()['leases'] == []


def test_batch_cannot_replace_an_active_host_trust_controller(authority, tmp_path, ffmpeg):
    app = LibraryApp(tmp_path / 'library', trust_controller=authority.controller)
    other = TrustController()
    service = AudioBatchService(app.root, ffmpeg=ffmpeg, trust_controller=other)
    source = pcm(tmp_path / 'source.wav')
    state = service.create((Selection('audio', str(source), 'Source', preset()),))
    with operation_context(authority.controller):
        result = service.run(state.id)
    assert result.status == 'failed' and 'authority' in result.items[0].error
    assert not output_file(service, result).exists()
