"""Queue originals, checkpoints and outputs survive the common full backup."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.batch_models import Selection
from carr_library.audio.batches import AudioBatchService, producer_guard
from carr_library.audio import jobs
from carr_library.audio_batch_backup import state_paths, validate_state_tree
from carr_library.library_backup import create_backup, verify_backup, restore_backup, RestoreError
from carr_library.reader.reading_state import ReadingLocation
from carr_library.ui.audio_studio import StudioController
from tests.audio.test_audio_batches import ffmpeg, local_queue, output_file, preset, pcm, SyntheticVoice, save_book, service_at


def roundtrip(app, tmp_path, *, label='copy'):
    snapshot = tmp_path / (label + '-snapshot')
    proof = create_backup(app, snapshot)
    verify_backup(snapshot, proof)
    destination = tmp_path / (label + '-library')
    restore_backup(snapshot, destination, proof)
    return destination


@pytest.mark.parametrize('run_first', [False, True])
def test_queued_and_completed_media_restore_without_external_sources_or_empty_directory_assumptions(tmp_path, ffmpeg, run_first):
    app, service, state, source, original = local_queue(tmp_path, ffmpeg)
    if run_first:
        state = service.run(state.id)
        assert state.status == 'complete'
    before = service.load(state.id)
    source.unlink()
    restored = roundtrip(app, tmp_path)
    reopened = AudioBatchService(restored, ffmpeg=ffmpeg)
    assert reopened.load(state.id) == before
    assert (reopened.path(state.id) / 'sources' / state.items[0].source.file).read_bytes() == original
    if not run_first:
        # The manifest stores files; the empty outputs directory need not exist.
        empty = reopened.path(state.id) / 'outputs'
        if empty.exists():
            empty.rmdir()
        assert not empty.exists()
    result = reopened.run(state.id, action='resume')
    assert result.status == 'complete'
    assert result.items[0].attempt == 1
    assert reopened.verified_receipt(result, result.items[0])
    second = tmp_path / 'second-snapshot'
    proof = create_backup(LibraryApp(restored), second)
    verify_backup(second, proof)


def test_paused_queue_keeps_completed_output_and_durable_control_across_restore(tmp_path, ffmpeg):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg, formats=('wav', 'flac'))
    def progress(message):
        if message.startswith('Converting item 2 '):
            service.control(state.id, 'pause')
    paused = service.run(state.id, on_status=progress)
    assert paused.status == 'paused' and paused.items[0].state == 'complete'
    exact = output_file(service, paused).read_bytes()
    restored = roundtrip(app, tmp_path)
    reopened = AudioBatchService(restored, ffmpeg=ffmpeg)
    assert reopened.describe(paused.id)['control']['action'] == 'pause'
    result = reopened.run(paused.id, action='resume')
    assert result.status == 'complete'
    assert output_file(reopened, result).read_bytes() == exact
    assert result.items[0].attempt == 1 and result.items[1].attempt == 2


def test_full_backup_keeps_batch_book_child_original_draft_and_retained_clip_pcm(tmp_path, ffmpeg):
    voice = SyntheticVoice()
    app, service = service_at(tmp_path, ffmpeg, voice=voice)
    studio = save_book(app, ffmpeg, voice,
                       '[output sample_rate=8000]\n[clip original.wav id=c1 duration=.2]\n[duration .3]', 'Clip book')
    assets = studio.output_dir / 'assets'
    assets.mkdir()
    source = pcm(assets / 'original.wav')
    original = source.read_bytes()
    state = service.create((Selection('book', studio.project_id, 'Delivered clip book', preset()),))
    result = service.run(state.id)
    assert result.status == 'complete', result.items[0].error
    assert voice.calls == []
    child_id = result.items[0].id
    child_document = app.root / 'audio-projects' / child_id / 'studio' / 'project.json'
    exact_document = child_document.read_bytes()
    restored = roundtrip(app, tmp_path)
    assert (restored / child_document.relative_to(app.root)).read_bytes() == exact_document
    assert (restored / 'audio-projects' / child_id / 'audio' / 'assets' / 'original.wav').read_bytes() == original
    reopened = AudioBatchService(restored, ffmpeg=ffmpeg, registry=voice)
    assert reopened.run(result.id).status == 'complete' and voice.calls == []
    assert validate_state_tree(restored) == ((result.id, child_id),)
    proof = create_backup(LibraryApp(restored), tmp_path / 'rebackup')
    verify_backup(tmp_path / 'rebackup', proof)


def test_active_queue_namespace_prevents_inconsistent_complete_backup(tmp_path, ffmpeg):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    with producer_guard(app.root):
        with pytest.raises((RuntimeError, ValueError, RestoreError)):
            create_backup(app, tmp_path / 'busy-snapshot')
    proof = create_backup(app, tmp_path / 'idle-snapshot')
    verify_backup(tmp_path / 'idle-snapshot', proof)


@pytest.mark.parametrize('damage', ['unknown-file', 'source-changed', 'missing-complete-output', 'receipt-source'])
def test_unknown_or_changed_queue_state_is_never_silently_omitted_from_backup(tmp_path, ffmpeg, damage):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    result = service.run(state.id) if damage in ('missing-complete-output', 'receipt-source') else state
    root, item = service.path(state.id), result.items[0]
    if damage == 'unknown-file':
        (root / 'undeclared.json').write_text('{}', encoding='utf-8')
    elif damage == 'source-changed':
        source = root / 'sources' / item.source.file
        raw = bytearray(source.read_bytes())
        raw[-1] ^= 1
        source.write_bytes(raw)
    elif damage == 'missing-complete-output':
        shutil.rmtree(root / 'outputs' / item.id)
    else:
        path = root / 'outputs' / item.id / 'receipt.json'
        value = jobs.read_json(path)
        value['source_sha256'] = '0' * 64
        jobs.atomic_json(path, value)
    with pytest.raises((RuntimeError, ValueError, RestoreError)):
        create_backup(app, tmp_path / 'invalid-snapshot')


def test_pending_book_snapshot_must_retain_a_real_catalog_binding_even_without_a_child(tmp_path, ffmpeg):
    app, service = service_at(tmp_path, ffmpeg)
    words = 'Exact source words.'
    location = ReadingLocation('absent-workspace', 'absent-edition', 'absent-revision',
                               'logical_segment', 1, sha256(words.encode('utf-8')).hexdigest(), '1' * 64)
    reference = {'kind': 'library-source-reading-copy', 'workspace': location.workspace,
                 'edition': location.edition, 'revision': location.revision,
                 'pages': [{'location': asdict(location), 'source_start': 0, 'source_end': len(words), 'verified': True}],
                 'separator': 'form-feed between exact source pages or text segments'}
    studio = StudioController(private_root=app.root, ffmpeg=ffmpeg)
    studio.new(words, title='Unbound source', source_id='selected-synthetic-source', source_reference=reference)
    studio.save_in_library()
    state = service.create((Selection('book', studio.project_id, 'Pending bound book', preset()),))
    assert state.items[0].state == 'pending'
    # Remove the independent original fixture project, so the only remaining
    # catalog assertion is the newly retained pending queue snapshot itself.
    shutil.rmtree(studio.project_path.parent.parent)
    assert not (app.root / 'audio-projects' / state.items[0].id).exists()
    with pytest.raises((RuntimeError, ValueError, RestoreError)):
        create_backup(app, tmp_path / 'unbound-snapshot')


def test_known_interrupted_scratch_is_inert_but_unknown_state_is_not_hidden(tmp_path, ffmpeg):
    app, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    item = state.items[0]
    stage = service.path(state.id) / ('.pending-item-' + item.id + '-interrupted')
    stage.mkdir()
    jobs.atomic_json(stage / 'owner.json', {'batch_id': state.id, 'item_id': item.id,
                                           'plan_sha256': state.plan_sha256})
    (stage / 'work').mkdir()
    (stage / 'work' / 'decoded.wav').write_bytes(b'partial uncommitted PCM')
    files = state_paths(app.root)
    assert all('.pending-item-' not in str(path) for path in files)
    proof = create_backup(app, tmp_path / 'inert-scratch')
    verify_backup(tmp_path / 'inert-scratch', proof)
    (stage / 'work' / 'foreign-original.txt').write_text('do not silently omit me', encoding='utf-8')
    with pytest.raises(ValueError):
        state_paths(app.root)
