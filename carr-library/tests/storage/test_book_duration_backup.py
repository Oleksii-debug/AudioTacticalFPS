"""The current full-backup family retains measured PCM reviews passively."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import jobs
from carr_library.audio.batches import AudioBatchService, producer_guard
from carr_library.audio_batch_backup import state_paths, validate_state_tree
from carr_library.library_backup import create_backup, verify_backup, restore_backup, RestoreError
from tests.audio.test_audio_batches import ffmpeg
from tests.audio.test_book_duration import reviewed_book, root_for, publish


@pytest.mark.parametrize('published', [False, True])
def test_full_backup_keeps_accepted_pcm_and_parts_without_original_render_cache_or_resynthesis(tmp_path, ffmpeg, published):
    app, service, batch, studio, original, voice, review = reviewed_book(tmp_path, ffmpeg)
    root = root_for(service, batch, review)
    source = (root / 'source.wav').read_bytes()
    completed = publish(service, batch, review) if published else None
    # The retained accepted PCM is its own reviewed source; a generated Studio
    # cache is not an external authority required to reopen the duration plan.
    child_audio = app.root / 'audio-projects' / batch.items[0].id / 'audio'
    shutil.rmtree(child_audio / 'renders')
    assert validate_state_tree(app.root) == ((batch.id, batch.items[0].id),)
    snapshot = tmp_path / 'snapshot'
    proof = create_backup(app, snapshot)
    verify_backup(snapshot, proof)
    destination = tmp_path / 'restored'
    restore_backup(snapshot, destination, proof)
    reopened = AudioBatchService(destination, ffmpeg=ffmpeg)
    value = reopened.describe_duration(batch.id, batch.items[0].id, review['id'])
    assert value['review'] == review and value['receipt'] == completed
    restored_root = root_for(reopened, batch, review)
    assert (restored_root / 'source.wav').read_bytes() == source
    result = publish(reopened, batch, review)
    assert result['status'] == 'complete'
    assert (restored_root / 'source.wav').read_bytes() == source
    assert studio.project_path.read_bytes() == original
    assert validate_state_tree(destination) == ((batch.id, batch.items[0].id),)
    second = tmp_path / 'second-snapshot'
    verify_backup(second, create_backup(LibraryApp(destination), second))


@pytest.mark.parametrize('change', ['extra-member', 'source-bytes', 'source-alias', 'part-bytes', 'receipt-frames'])
def test_complete_backup_refuses_forged_or_unclaimed_duration_content(tmp_path, ffmpeg, change):
    app, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    receipt = publish(service, batch, review)
    root = root_for(service, batch, review)
    if change == 'extra-member':
        (root / 'unclaimed.dat').write_bytes(b'not typed')
    elif change == 'source-bytes':
        path = root / 'source.wav'
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes((data[-1] ^ 1,)))
    elif change == 'source-alias':
        import os
        os.link(root / 'source.wav', tmp_path / 'foreign-alias.wav')
    elif change == 'part-bytes':
        path = root / 'outputs' / receipt['files'][0]['name']
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes((data[-1] ^ 1,)))
    else:
        value = deepcopy(receipt)
        value['files'][0]['pcm_frames'] -= 1
        jobs.atomic_json(root / 'outputs' / 'receipt.json', value)
    with pytest.raises((ValueError, RestoreError, OSError)):
        create_backup(app, tmp_path / 'must-fail-snapshot')


def test_duration_review_shares_the_existing_batch_snapshot_lock(tmp_path, ffmpeg):
    app, service, batch, _, _, _, review = reviewed_book(tmp_path, ffmpeg)
    with producer_guard(app.root):
        with pytest.raises((RuntimeError, ValueError, RestoreError)):
            create_backup(app, tmp_path / 'active-snapshot')
    assert any(path.name == 'source.wav' and '/duration/' in path.as_posix()
               for path in state_paths(app.root))
    snapshot = tmp_path / 'settled-snapshot'
    verify_backup(snapshot, create_backup(app, snapshot))
