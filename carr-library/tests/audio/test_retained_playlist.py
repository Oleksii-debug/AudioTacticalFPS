"""Actual independent copies, typed state, CAS and commit-window recovery."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.audio import playlist as module
from carr_library.audio.playlist import (PlaylistStore, SavedPlaylist, SavedPlaylistItem,
    decode_playlist, prepare_playlist, retain_playlist, state_paths, validate_state_tree)
from carr_library.durable_state import encode
from tests.audio.playlist_fixture import pcm, retained


@pytest.mark.parametrize('rf64', [False, True])
def test_retained_pcm_is_independent_exact_and_survives_original_changes(tmp_path, rf64):
    app, sources, rows, value, receipt = retained(tmp_path, rf64=rf64)
    original = sources[0].read_bytes()
    assert rows[0].path != sources[0]
    assert rows[0].path.stat().st_nlink == sources[0].stat().st_nlink == 1
    assert rows[0].path.read_bytes() == original
    assert rows[0].path.stem == sha256(original).hexdigest()
    sources[0].write_bytes(b'An independent later source edit.')
    assert value.review(app.root)[0].path.read_bytes() == original
    assert validate_state_tree(app.root) == value
    assert PlaylistStore(app.root).load() == (value, receipt.token)


def test_loading_saved_queue_is_passive_and_does_not_open_media(tmp_path, monkeypatch):
    app, sources, rows, value, receipt = retained(tmp_path)
    monkeypatch.setattr(module, 'prepare_playlist', lambda *a, **k: pytest.fail('Passive loading opened PCM.'))
    assert PlaylistStore(app.root).load() == (value, receipt.token)
    assert not any(key in value.to_dict() for key in ('runtime', 'device', 'autoplay', 'source_path'))


@pytest.mark.parametrize('mutation', [
    'schema', 'extra-field', 'missing-field', 'external-path', 'escape-path', 'hash',
    'frames', 'rate', 'channels', 'size', 'label', 'index', 'index-bool', 'frame',
    'frame-bool', 'accuracy', 'skip-nan', 'skip-bool', 'skips'])
def test_invalid_saved_fields_never_gain_playback_authority(tmp_path, mutation):
    _, _, _, saved, _ = retained(tmp_path, count=1)
    value = saved.to_dict()
    if mutation == 'schema':
        value['schema'] = 'foreign/1'
    elif mutation == 'extra-field':
        value['autoplay'] = True
    elif mutation == 'missing-field':
        value.pop('accuracy')
    elif mutation == 'external-path':
        value['items'][0]['filename'] = 'C:/old/computer/audio.wav'
    elif mutation == 'escape-path':
        value['items'][0]['filename'] = '../outside.wav'
    elif mutation == 'hash':
        value['items'][0]['sha256'] = '0' * 64
    elif mutation == 'frames':
        value['items'][0]['frames'] = 1000000
    elif mutation == 'rate':
        value['items'][0]['sample_rate'] = 0
    elif mutation == 'channels':
        value['items'][0]['channels'] = 3
    elif mutation == 'size':
        value['items'][0]['size_bytes'] = module.MAX_FILE_BYTES + 1
    elif mutation == 'label':
        value['items'][0]['label'] = 'bad\x00label'
    elif mutation == 'index':
        value['index'] = 1
    elif mutation == 'index-bool':
        value['index'] = True
    elif mutation == 'frame':
        value['frame'] = 8001
    elif mutation == 'frame-bool':
        value['frame'] = False
    elif mutation == 'accuracy':
        value['accuracy'] = 'guessed_from_wall_clock'
    elif mutation == 'skip-nan':
        value['skips'][0] = float('nan')
    elif mutation == 'skip-bool':
        value['skips'][0] = True
    else:
        value['skips'] = [1]
    with pytest.raises(ValueError):
        SavedPlaylist.from_dict(value)


@pytest.mark.parametrize('raw', [b'{}', b'{"schema":1,"schema":1}', b'[]', b'null'])
def test_malformed_and_duplicate_json_are_rejected(raw):
    with pytest.raises(ValueError):
        decode_playlist(raw)


def test_queue_count_and_total_byte_budgets_are_independent():
    digest = 'a' * 64
    row = SavedPlaylistItem('playback-queue/media/' + digest + '.wav', 'Large PCM', digest,
                            module.MAX_FILE_BYTES, 8000, 8000, 1)
    with pytest.raises(ValueError):
        SavedPlaylist((row,) * 257, 0)
    with pytest.raises(ValueError):
        SavedPlaylist((row,) * 9, 0)


def test_cas_rejects_a_second_window_without_overwriting_current_queue(tmp_path):
    app, _, _, value, receipt = retained(tmp_path)
    store = PlaylistStore(app.root)
    newer = replace(value, index=1, frame=2000)
    current = store.save(newer, expected_token=receipt.token)
    with pytest.raises(ValueError, match='Another window'):
        store.save(replace(value, frame=100), expected_token=receipt.token)
    assert store.load() == (newer, current.token)


def test_changed_pcm_with_restored_mtime_is_not_accepted_for_save(tmp_path):
    app, _, rows, value, receipt = retained(tmp_path)
    path, before = rows[0].path, rows[0].path.stat()
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError):
        PlaylistStore(app.root).save(replace(value, frame=1), expected_token=receipt.token)
    assert PlaylistStore(app.root).load() == (value, receipt.token)


@pytest.mark.parametrize('kind', ['source', 'retained'])
def test_hardlinked_input_or_retained_media_is_refused(tmp_path, kind):
    app, sources, rows, _, _ = retained(tmp_path, count=1)
    path = sources[0] if kind == 'source' else rows[0].path
    alias = tmp_path / 'alias.wav'
    os.link(path, alias)
    with pytest.raises(ValueError):
        prepare_playlist((path,)) if kind == 'source' else state_paths(app.root)
    assert alias.read_bytes() == path.read_bytes()


def test_unknown_state_is_not_hidden_or_deleted_by_recovery(tmp_path):
    app, _, _, value, receipt = retained(tmp_path, count=1)
    foreign = app.root / module.DIRECTORY / 'unreviewed.json'
    foreign.write_bytes(b'{"keep":true}')
    with pytest.raises(ValueError, match='Unknown'):
        PlaylistStore(app.root).save(value, expected_token=receipt.token)
    assert foreign.read_bytes() == b'{"keep":true}'


def test_owned_interrupted_stages_are_inert_and_recovered_under_lock(tmp_path):
    app, _, rows, value, receipt = retained(tmp_path, count=1)
    pending = app.root / module.DIRECTORY / '.pending-abcdefgh'
    capture = rows[0].path.parent / ('.capture-' + 'b' * 32)
    publish = rows[0].path.parent / '.publish-abcdefgh'
    for path in (pending, capture, publish):
        path.write_bytes(b'private incomplete bytes')
    assert all(path not in state_paths(app.root) for path in (pending, capture, publish))
    assert validate_state_tree(app.root) == value
    result = PlaylistStore(app.root).save(value, expected_token=receipt.token)
    assert result.confirmed and all(not path.exists() for path in (pending, capture, publish))


def test_cancelled_capture_preserves_completed_copy_and_originals(tmp_path):
    app, sources, rows, _, _ = retained(tmp_path, count=2, save=False)
    # Start a genuinely new owned namespace so cancellation observes the first
    # completed atomic publication, not a fabricated successful return.
    from carr_library.application import LibraryApp
    second = LibraryApp(tmp_path / 'second-library')
    reviewed = prepare_playlist(sources)
    first = second.root / module.DIRECTORY / 'media' / (reviewed[0].sha256 + '.wav')
    original = tuple(path.read_bytes() for path in sources)
    with pytest.raises(InterruptedError):
        retain_playlist(second.root, reviewed, cancelled=first.exists)
    assert first.read_bytes() == original[0]
    assert tuple(path.read_bytes() for path in sources) == original
    assert not tuple(first.parent.glob('.capture-*'))
    assert not tuple(first.parent.glob('.publish-*'))
    assert validate_state_tree(second.root) is None
    resumed = retain_playlist(second.root, reviewed)
    assert [row.sha256 for row in resumed] == [row.sha256 for row in reviewed]


def test_pre_cancelled_review_and_retention_do_not_publish(tmp_path):
    app, sources, _, _, _ = retained(tmp_path, count=1)
    with pytest.raises(InterruptedError):
        prepare_playlist(sources, cancelled=lambda: True)
    reviewed = prepare_playlist(sources)
    from carr_library.application import LibraryApp
    other = LibraryApp(tmp_path / 'other-library')
    with pytest.raises(InterruptedError):
        retain_playlist(other.root, reviewed, cancelled=lambda: True)
    assert not (other.root / module.DIRECTORY).exists()


def test_review_timeout_is_explicit_without_guessing_a_success(tmp_path, monkeypatch):
    path = pcm(tmp_path / 'input.wav')
    clock = iter((0, 121))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(TimeoutError, match='two minutes'):
        prepare_playlist((path,))


def test_completed_write_with_failed_directory_flush_keeps_an_unconfirmed_receipt(tmp_path, monkeypatch):
    app, _, _, value, receipt = retained(tmp_path, count=1)
    original = module.atomic_json
    def committed_then_failed(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('Directory flush refused after publication.')
    monkeypatch.setattr(module, 'atomic_json', committed_then_failed)
    newer = replace(value, frame=800)
    store = PlaylistStore(app.root)
    result = store.save(newer, expected_token=receipt.token)
    assert not result.confirmed and result.saved == newer and 'retry Save' in result.warning
    assert store.load() == (newer, result.token)
    monkeypatch.setattr(module, 'atomic_json', original)
    assert store.save(newer, expected_token=result.token).confirmed


def test_unknown_or_unreadable_namespace_never_becomes_an_empty_playlist(tmp_path, monkeypatch):
    app, _, _, _, _ = retained(tmp_path, count=1)
    original = module.optional_present
    def denied(path):
        if Path(path) == app.root / module.DIRECTORY:
            raise PermissionError('Denied namespace inspection.')
        return original(path)
    monkeypatch.setattr(module, 'optional_present', denied)
    with pytest.raises(PermissionError):
        state_paths(app.root)


def test_clear_saved_queue_does_not_erase_completed_retained_originals(tmp_path):
    app, _, rows, _, receipt = retained(tmp_path)
    store = PlaylistStore(app.root)
    result = store.save(SavedPlaylist(), expected_token=receipt.token)
    assert result.confirmed and store.load()[0] == SavedPlaylist()
    assert all(row.path.exists() for row in rows)
    assert len(state_paths(app.root)) == 3


def test_empty_queue_roundtrip_needs_no_media_directory(tmp_path):
    app, _, _, value, receipt = retained(tmp_path, count=0)
    assert not (app.root / module.DIRECTORY / 'media').exists()
    assert validate_state_tree(app.root) == value == SavedPlaylist()
    assert PlaylistStore(app.root).load() == (value, receipt.token)
