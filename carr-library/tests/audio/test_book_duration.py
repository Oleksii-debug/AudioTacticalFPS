"""Actual accepted-PCM splitting, replay and publication-integrity regressions."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from carr_library.audio import book_duration as duration, jobs, media_conversion as media
from carr_library.audio.batch_models import Selection, digest
from carr_library.audio.batches import AudioBatchService
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.audio.ffmpeg import wav_info
from carr_library.research.export_file import capture_export_destination
from tests.audio.test_audio_batches import ffmpeg, preset, service_at, save_book, SyntheticVoice


def completed_book(tmp_path, ffmpeg, *, split='whole'):
    voice = SyntheticVoice()
    app, service = service_at(tmp_path, ffmpeg, voice=voice)
    studio = save_book(app, ffmpeg, voice,
        '[output sample_rate=48000]\n# First\nNarrator: First retained sentence.\n'
        '# Second\nNarrator: Second retained sentence.', 'Measured synthetic book')
    original = studio.project_path.read_bytes()
    batch = service.create((Selection('book', studio.project_id, 'Measured delivery',
                                     preset('wav', split=split)),))
    batch = service.run(batch.id)
    assert batch.status == 'complete', [item.error for item in batch.items]
    return app, service, batch, studio, original, voice


def reviewed_book(tmp_path, ffmpeg, *, format='wav', split='whole'):
    app, service, batch, studio, original, voice = completed_book(tmp_path, ffmpeg, split=split)
    item = batch.items[0]
    review = service.review_duration(batch.id, item.id, '0.137', output_format=format)
    return app, service, batch, studio, original, voice, review


def root_for(service, batch, review):
    return service.path(batch.id) / 'duration' / review['id']


def publish(service, batch, review, **options):
    return service.publish_duration(batch.id, batch.items[0].id, review['id'],
                                    approved_sha256=review['sha256'], **options)


def pcm_bytes(path):
    info = wav_info(path)
    with Path(path).open('rb') as source:
        source.seek(info.data_offset)
        return source.read(info.data_size)


@pytest.mark.parametrize('frames,part,expected', [
    (1, 1, ((0, 1),)), (3, 2, ((0, 2), (2, 3))),
    (8, 4, ((0, 4), (4, 8))), (7, 99, ((0, 7),)),
    (48001, 48000, ((0, 48000), (48000, 48001))),
])
def test_integer_intervals_cover_every_sample_once_including_last_remainder(frames, part, expected):
    assert duration.intervals(frames, part) == expected
    assert sum(last - first for first, last in expected) == frames


@pytest.mark.parametrize('text,rate,frames', [
    ('0.05', 8000, 400), ('0.333333', 48000, 15999), ('001.500000', 44100, 66150),
])
def test_decimal_duration_uses_complete_source_frames_without_binary_float_drift(text, rate, frames):
    canonical, actual = duration.requested_duration(text, rate)
    assert actual == frames and 'E' not in canonical
    assert duration.requested_duration(canonical, rate) == (canonical, frames)


@pytest.mark.parametrize('value', [True, -1, float('nan'), '1e3', '0.0001', '86400.1', '1.0000001'])
def test_unreviewable_decimal_duration_is_rejected(value):
    with pytest.raises(ValueError):
        duration.requested_duration(value, 48000)


def test_excessive_part_count_is_refused_before_any_file_can_be_planned():
    with pytest.raises(ValueError, match='10000-part'):
        duration.intervals(10001, 1)


def test_actual_wav_parts_rejoin_to_exact_accepted_pcm_without_any_resynthesis(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, studio, original, voice, review = reviewed_book(tmp_path, ffmpeg)
    calls = list(voice.calls)
    root = root_for(service, batch, review)
    accepted = (root / 'source.wav').read_bytes()
    assert sha256(accepted).hexdigest() == review['source']['sha256']
    assert len(review['parts']) > 1
    assert review['parts'][-1]['end_frame'] == review['source']['frames']
    assert review['parts'][-1]['end_frame'] - review['parts'][-1]['first_frame'] < review['part_frames']
    monkeypatch.setattr(voice, 'synthesize', lambda *a, **k: pytest.fail('Duration publication resynthesised speech.'))
    receipt = publish(service, batch, review)
    joined = b''.join(pcm_bytes(root / 'outputs' / row['name']) for row in receipt['files'])
    assert joined == pcm_bytes(root / 'source.wav')
    assert sum(row['pcm_frames'] for row in receipt['files']) == review['source']['frames']
    assert all(row['frames'] == row['pcm_frames'] for row in receipt['files'])
    assert studio.project_path.read_bytes() == original and voice.calls == calls
    assert (root / 'source.wav').read_bytes() == accepted
    assert service.load(batch.id).items == batch.items and service.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('split', ['whole', 'chapters'])
def test_original_whole_and_chapter_outputs_and_schema_remain_unchanged(tmp_path, ffmpeg, split):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg, split=split)
    item = batch.items[0]
    output = service.path(batch.id) / 'outputs' / item.id
    originals = {path.name: path.read_bytes() for path in output.iterdir()}
    publish(service, batch, review)
    assert batch.schema == 'carr-audio-batch/1'
    assert service.load(batch.id).plan_sha256 == batch.plan_sha256
    assert service.load(batch.id).items[0].preset.split == split
    assert {path.name: path.read_bytes() for path in output.iterdir()} == originals


@pytest.mark.parametrize('format', ['wav', 'flac', 'mp3', 'opus', 'm4a', 'm4b', 'aiff', 'ogg', 'aac', 'wma'])
def test_each_existing_format_is_really_encoded_and_decoded_from_reviewed_intervals(tmp_path, ffmpeg, format):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg, format=format)
    receipt = publish(service, batch, review)
    root = root_for(service, batch, review)
    assert [row['name'] for row in receipt['files']] == [row['name'] for row in review['parts']]
    for row, part in zip(receipt['files'], review['parts']):
        assert row['sha256'] == sha256((root / 'outputs' / row['name']).read_bytes()).hexdigest()
        assert row['pcm_frames'] == part['end_frame'] - part['first_frame']
        assert row['sample_rate'] == review['source']['sample_rate']
        assert row['channels'] == review['source']['channels']
        assert row['frames'] > 0
        if format in ('wav', 'flac', 'aiff'):
            assert row['frames'] == row['pcm_frames']


@pytest.mark.parametrize('change', ['gap', 'overlap', 'boolean', 'tail', 'name', 'rate'])
def test_forged_duration_plan_cannot_redefine_names_or_sample_authority(tmp_path, ffmpeg, change):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    value = deepcopy(review)
    if change == 'gap':
        value['parts'][1]['first_frame'] += 1
    elif change == 'overlap':
        value['parts'][1]['first_frame'] -= 1
    elif change == 'boolean':
        value['parts'][0]['first_frame'] = False
    elif change == 'tail':
        value['parts'][-1]['end_frame'] -= 1
    elif change == 'name':
        value['parts'][0]['name'] = '../unselected.wav'
    else:
        value['source']['sample_rate'] = 44100
    body = {key: entry for key, entry in value.items() if key not in ('id', 'sha256')}
    value['sha256'] = digest(body)
    value['id'] = value['sha256'][:32]
    with pytest.raises(ValueError):
        duration.validate_review(value, batch, batch.items[0])


def test_same_complete_review_reuses_verified_outputs_without_encoder_or_voice(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, voice, review = reviewed_book(tmp_path, ffmpeg)
    complete = publish(service, batch, review)
    root = root_for(service, batch, review)
    before = {path.name: (path.stat().st_mtime_ns, path.read_bytes())
              for path in (root / 'outputs').iterdir()}
    monkeypatch.setattr(media, 'decode', lambda *a, **k: pytest.fail('A completed duration review decoded again.'))
    monkeypatch.setattr(voice, 'synthesize', lambda *a, **k: pytest.fail('A completed duration review spoke again.'))
    assert publish(service, batch, review) == complete
    assert before == {path.name: (path.stat().st_mtime_ns, path.read_bytes())
                      for path in (root / 'outputs').iterdir()}


def test_wrong_approval_never_enters_native_work_or_publishes_parts(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    monkeypatch.setattr(media, 'decode', lambda *a, **k: pytest.fail('Wrong review approval executed native work.'))
    with pytest.raises(ValueError, match='Approve the exact'):
        service.publish_duration(batch.id, batch.items[0].id, review['id'], approved_sha256='0' * 64)
    assert not (root_for(service, batch, review) / 'outputs').exists()


def test_changed_retained_pcm_is_refused_before_native_processing(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    root = root_for(service, batch, review)
    path = root / 'source.wav'
    data_offset = wav_info(path).data_offset
    with path.open('r+b') as stream:
        stream.seek(data_offset)
        stream.write(b'\x01\x02')
    monkeypatch.setattr(media, 'decode', lambda *a, **k: pytest.fail('Changed accepted PCM reached native work.'))
    with pytest.raises(ValueError):
        publish(service, batch, review)
    assert not (root / 'outputs').exists()


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_current_durable_control_stops_parts_and_same_explicit_review_resumes(tmp_path, ffmpeg, action):
    _, service, batch, _, _, voice, review = reviewed_book(tmp_path, ffmpeg)
    calls = list(voice.calls)
    issued = []
    def progress(message):
        if not issued:
            issued.append(service.control(batch.id, action))
    with pytest.raises(jobs.RenderCancelled):
        publish(service, batch, review, on_status=progress)
    root = root_for(service, batch, review)
    assert issued and not (root / 'outputs').exists()
    assert not list(service.path(batch.id).glob('.pending-duration-*'))
    assert service.load(batch.id).status == 'complete' and voice.calls == calls
    assert publish(service, batch, review)['status'] == 'complete'
    assert voice.calls == calls and service.broker.snapshot()['leases'] == []


def test_pending_native_owner_keeps_source_stage_and_broker_until_quiescent(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    root = root_for(service, batch, review)
    source = root / 'source.wav'
    actual, active, attempts = duration.verified_reader, [], []
    @contextmanager
    def held(path, **options):
        with actual(path, **options) as stream:
            if Path(path) == source:
                active.append(stream)
            try:
                yield stream
            finally:
                if Path(path) == source:
                    active.remove(stream)
    owner = None
    def fail_native(original, decoded, **options):
        nonlocal owner
        stage = Path(decoded).parent.parent
        def settle():
            attempts.append(True)
            assert active and not active[0].closed
            assert stage.is_dir() and stage.name.startswith('.pending-duration-')
            assert len(service.broker.snapshot()['leases']) == 1
            assert not (root / 'outputs').exists()
            return len(attempts) >= 2
        owner = CleanupOwner(settle)
        raise AudioCleanupPending('Synthetic duration owner retained', cleanup_owner=owner)
    monkeypatch.setattr(duration, 'verified_reader', held)
    monkeypatch.setattr(media, 'decode', fail_native)
    with pytest.raises(AudioCleanupPending, match='duration owner retained'):
        publish(service, batch, review)
    assert owner.quiescent and len(attempts) == 2 and not active
    assert not (root / 'outputs').exists()
    assert not list(service.path(batch.id).glob('.pending-duration-*'))
    assert service.broker.snapshot()['leases'] == []


def test_interrupted_typed_duration_scratch_is_recovered_without_touching_originals(tmp_path, ffmpeg):
    _, service, batch, studio, original, _, review = reviewed_book(tmp_path, ffmpeg)
    stage = service.path(batch.id) / ('.pending-duration-' + review['id'] + '-crashed')
    (stage / 'work').mkdir(parents=True)
    (stage / 'publish').mkdir()
    jobs.atomic_json(stage / 'owner.json', {'schema': duration.REVIEW_SCHEMA, 'action': 'publish',
        'batch_id': batch.id, 'item_id': batch.items[0].id, 'review_id': review['id']})
    (stage / 'work' / 'decoded.wav').write_bytes(b'partial derived scratch')
    assert publish(service, batch, review)['status'] == 'complete' and not stage.exists()
    assert studio.project_path.read_bytes() == original


def test_unclaimed_duration_scratch_is_refused_instead_of_deleted(tmp_path, ffmpeg):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    stage = service.path(batch.id) / ('.pending-duration-' + review['id'] + '-unknown')
    stage.mkdir()
    outside = stage / 'unclaimed-private-file'
    outside.write_bytes(b'UNCLAIMED DATA')
    with pytest.raises(ValueError, match='silently omitted'):
        publish(service, batch, review)
    assert outside.read_bytes() == b'UNCLAIMED DATA'
    assert not (root_for(service, batch, review) / 'outputs').exists()


def test_post_move_cancel_keeps_the_exact_already_committed_duration_receipt(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    root = root_for(service, batch, review)
    actual = duration.publish_noreplace
    def committed(source, target, **options):
        result = actual(source, target, **options)
        if Path(target) == root / 'outputs':
            service.control(batch.id, 'cancel')
        return result
    monkeypatch.setattr(duration, 'publish_noreplace', committed)
    assert publish(service, batch, review)['status'] == 'complete'
    assert service.describe_duration(batch.id, batch.items[0].id, review['id'])['status'] == 'complete'


def test_duration_source_hash_race_never_publishes_an_export_destination(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    receipt = publish(service, batch, review)
    filename = receipt['files'][0]['name']
    source = root_for(service, batch, review) / 'outputs' / filename
    actual = duration._completed
    changed = []
    def checked(*args, **options):
        value = actual(*args, **options)
        if not changed:
            raw = source.read_bytes()
            source.write_bytes(raw[:-1] + bytes((raw[-1] ^ 1,)))
            changed.append(True)
        return value
    monkeypatch.setattr(duration, '_completed', checked)
    target = tmp_path / 'must-not-be-published.wav'
    approval = capture_export_destination(target, private_root=service.library)
    with pytest.raises(ValueError, match='publication digest'):
        service.export_duration(batch.id, batch.items[0].id, review['id'], filename,
                                target, approval=approval)
    assert changed and not target.exists() and not list(tmp_path.glob('.publish-*'))


def test_existing_batch_export_hash_race_is_closed_by_the_same_copy_contract(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _ = completed_book(tmp_path, ffmpeg)
    item = batch.items[0]
    source = service.path(batch.id) / 'outputs' / item.id / item.outputs[0]
    actual = service.verified_receipt
    changed = []
    def checked(*args, **options):
        value = actual(*args, **options)
        if not changed:
            raw = source.read_bytes()
            source.write_bytes(raw[:-1] + bytes((raw[-1] ^ 1,)))
            changed.append(True)
        return value
    monkeypatch.setattr(service, 'verified_receipt', checked)
    target = tmp_path / 'must-not-be-exported.wav'
    with pytest.raises(ValueError, match='publication digest'):
        service.export_file(batch.id, item.id, item.outputs[0], target,
            approval=capture_export_destination(target, private_root=service.library))
    assert changed and not target.exists() and not list(tmp_path.glob('.publish-*'))


def test_duration_export_is_independent_and_refuses_an_existing_or_changed_destination(tmp_path, ffmpeg):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    receipt = publish(service, batch, review)
    filename = receipt['files'][0]['name']
    source = root_for(service, batch, review) / 'outputs' / filename
    target = tmp_path / 'approved.wav'
    value = service.export_duration(batch.id, batch.items[0].id, review['id'], filename, target,
        approval=capture_export_destination(target, private_root=service.library))
    assert value['sha256'] == sha256(source.read_bytes()).hexdigest()
    assert source.read_bytes() == target.read_bytes() and target.stat().st_nlink == 1
    other = tmp_path / 'changed.wav'
    approval = capture_export_destination(other, private_root=service.library)
    other.write_bytes(b'FOREIGN DESTINATION')
    with pytest.raises((ValueError, FileExistsError)):
        service.export_duration(batch.id, batch.items[0].id, review['id'], filename, other, approval=approval)
    assert other.read_bytes() == b'FOREIGN DESTINATION'


def test_changed_selected_native_runtime_cannot_publish_reviewed_parts(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    actual = media.runtime_identity
    monkeypatch.setattr(media, 'runtime_identity', lambda *a, **k: replace(
        actual(*a, **k), sha256='0' * 64))
    with pytest.raises(ValueError):
        publish(service, batch, review)
    assert not (root_for(service, batch, review) / 'outputs').exists()
    assert service.broker.snapshot()['leases'] == []


def test_resource_wait_rechecks_accepted_pcm_before_native_input_processing(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    root = root_for(service, batch, review)
    source = root / 'source.wav'
    actual = service.broker.reserve
    @contextmanager
    def changed_while_waiting(*args, **options):
        with actual(*args, **options) as lease:
            raw = source.read_bytes()
            source.write_bytes(raw[:-1] + bytes((raw[-1] ^ 1,)))
            yield lease
    monkeypatch.setattr(service.broker, 'reserve', changed_while_waiting)
    monkeypatch.setattr(media, 'decode', lambda *a, **k: pytest.fail('Unreviewed PCM reached a native decoder.'))
    with pytest.raises(ValueError):
        publish(service, batch, review)
    assert not (root / 'outputs').exists() and service.broker.snapshot()['leases'] == []


def test_configured_trust_denies_new_parts_but_verified_complete_receipts_remain_readable(tmp_path, ffmpeg, monkeypatch):
    from carr_library.security.controller import TrustController
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    complete = publish(service, batch, review)
    other = service.review_duration(batch.id, batch.items[0].id, '0.2', output_format='wav')
    controller, calls = TrustController(), []
    def deny(action):
        calls.append(action)
        raise PermissionError('Synthetic trust refusal')
    monkeypatch.setattr(controller, 'begin_operation', deny)
    service.trust_controller = controller
    monkeypatch.setattr(media, 'decode', lambda *a, **k: pytest.fail('Denied duration work reached native input.'))
    assert publish(service, batch, review) == complete
    with pytest.raises(PermissionError, match='trust refusal'):
        publish(service, batch, other)
    assert calls == ['audio.render']
    assert not (root_for(service, batch, other) / 'outputs').exists()


def test_cumulative_derived_parts_cannot_overrun_the_reserved_disk_budget(tmp_path, ffmpeg, monkeypatch):
    _, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    limit = review['source']['bytes'] * 4 + len(review['parts']) * 1024**2 + 2 * duration.MAX_STATE_BYTES
    real = media.encode
    def oversized(source, target, **options):
        real(source, target, **options)
        with Path(target).open('r+b') as output:
            output.seek(limit)
            output.write(b'\0')
    monkeypatch.setattr(media, 'encode', oversized)
    with pytest.raises(ValueError, match='disk allowance'):
        publish(service, batch, review)
    assert not (root_for(service, batch, review) / 'outputs').exists()
    assert not list(service.path(batch.id).glob('.pending-duration-*'))
    assert service.broker.snapshot()['leases'] == []
