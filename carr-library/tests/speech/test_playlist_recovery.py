"""Retained queue recovery, actual full backup and passive authority boundaries."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.speech.player import WavePlayer
from carr_library.speech.playlist import (
    NAME, PlaylistStore, SavedPlaylist, prepare_playlist, retain_playlist,
)


def audio(path, value=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(bytes((value, 0)) * 8000)
    return path


@pytest.fixture
def queue(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Playlist recovery')
    sources = (audio(tmp_path / 'selected' / 'Перша.wav'),
               audio(tmp_path / 'selected' / 'Друга.wav', 2))
    items = retain_playlist(app.root, prepare_playlist(sources))
    player = WavePlayer()
    player.set_playlist((items[1], items[0], items[1]))
    player.select_queue(1)
    player.seek(.375, start_if_stopped=False)
    player.set_skip_intervals(2.5, 7.5)
    value = SavedPlaylist.capture(app.root, player)
    store = PlaylistStore(app.root)
    token = store.save(value, expected_token=None)
    yield app, sources, player, store, value, token
    player.close()


def test_retention_uses_exact_private_originals_and_keeps_duplicate_order(queue):
    app, sources, player, store, saved, token = queue
    assert saved.index == 1 and saved.frame == 3000 and saved.skips == (2.5, 7.5)
    assert [item.label for item in saved.items] == [sources[1].name, sources[0].name, sources[1].name]
    assert saved.items[0].filename == saved.items[2].filename
    for item in saved.items:
        retained = app.root / item.filename
        assert retained.parent == app.root / 'state' / 'studio' / 'inputs'
        assert retained.stat().st_nlink == 1
        assert sha256(retained.read_bytes()).hexdigest() == item.sha256
    assert store.load() == (saved, token)
    assert not player.queue_active and player.state == 'stopped'
    raw = store.path.read_text(encoding='utf-8')
    assert all(str(path) not in raw for path in sources)


def test_loading_queue_reads_only_own_json_without_opening_media_or_devices(queue, monkeypatch):
    app, sources, player, store, saved, token = queue
    for path in sources:
        path.unlink()
    real_open = Path.open
    def guarded(path, *args, **kwargs):
        if path.suffix in ('.wav', '.audio'):
            pytest.fail('Passive queue load opened media')
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    monkeypatch.setattr(WavePlayer, 'play', lambda *args, **kwargs: pytest.fail('load played audio'))
    assert PlaylistStore(app.root).load() == (saved, token)


def test_real_backup_restores_saved_queue_after_original_and_library_deletion(queue, tmp_path):
    app, sources, player, store, saved, token = queue
    player.close()
    destination = tmp_path / 'backup'
    digest = create_backup(app, destination)
    manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
    entries = {entry['filename']: entry for entry in manifest['sidecars']}
    assert NAME in entries
    for item in saved.items:
        assert entries[item.filename]['sha256'] == item.sha256
        assert entries[item.filename]['size_bytes'] == item.size_bytes
    assert not any(name.endswith('.lock') for name in entries)
    for path in sources:
        path.unlink()
    shutil.rmtree(app.root)
    restored = restore_backup(destination, tmp_path / 'new library', digest)
    reloaded, actual_token = PlaylistStore(restored).load()
    assert reloaded == saved and actual_token == token
    current = WavePlayer()
    try:
        assert current.playlist == () and current.path is None
        reviewed = reloaded.review(restored)
        reloaded.apply(current, reviewed)
        assert current.state == 'stopped' and not current.queue_active
        assert current._process is None and not current._open and current._wave is None
        assert current.playlist_index == 1 and current.position_seconds == .375
        assert current.skip_intervals == (2.5, 7.5)
        assert [item.display_name for item in current.playlist] == [item.label for item in saved.items]
        assert all(item.path.is_relative_to(restored) for item in current.playlist)
        assert SavedPlaylist.capture(restored, current) == saved
        # A second complete backup uses the same verified managed bytes.
        create_backup(LibraryApp(restored), tmp_path / 'second backup')
    finally:
        current.close()


@pytest.mark.parametrize('change', ['bytes', 'missing'])
def test_changed_or_missing_retained_media_cannot_be_reviewed_or_backed_up(queue, tmp_path, change):
    app, sources, player, store, saved, token = queue
    target = app.root / saved.items[0].filename
    if change == 'missing':
        target.unlink()
    else:
        with target.open('r+b') as output:
            output.seek(-2, 2)
            output.write(b'\x7f\x00')
    assert store.load() == (saved, token), 'Passive state remains readable for explicit repair.'
    with pytest.raises((ValueError, OSError)):
        saved.review(app.root)
    with pytest.raises((ValueError, OSError)):
        create_backup(app, tmp_path / 'rejected backup')
    assert not (tmp_path / 'rejected backup').exists()


def test_saved_row_cannot_retarget_external_file_even_with_resealed_json(queue, tmp_path, monkeypatch):
    app, sources, player, store, saved, token = queue
    unrelated = audio(tmp_path / 'unrelated.wav', 77)
    value = saved.to_dict()
    value['items'][0]['filename'] = str(unrelated)
    store.path.write_bytes(encode(value))
    original = Path.open
    def forbid(path, *args, **kwargs):
        if path == unrelated:
            pytest.fail('Invalid passive queue opened an unrelated local file')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', forbid)
    with pytest.raises(ValueError, match='retained audio identity'):
        store.load()
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected backup')


def test_saved_state_cas_preserves_concurrent_writer_and_unsaved_snapshot(queue):
    app, sources, player, store, saved, token = queue
    newer = replace(saved, frame=4000)
    new_token = PlaylistStore(app.root).save(newer, expected_token=token)
    with pytest.raises(ValueError, match='Another window'):
        store.save(replace(saved, frame=5000), expected_token=token)
    assert store.load() == (newer, new_token)
    assert saved.frame == 3000


@pytest.mark.parametrize('field,value', [
    ('index', True), ('index', 3), ('frame', 8001), ('frame', -1),
    ('volume', float('nan')), ('muted', 1), ('balance', float('inf')),
    ('skips', [0, 30]), ('skips', [10, True]), ('schema', 'other'),
])
def test_invalid_passive_fields_fail_without_native_admission(queue, field, value):
    _, _, _, _, saved, _ = queue
    raw = saved.to_dict()
    raw[field] = value
    with pytest.raises(ValueError):
        SavedPlaylist.from_dict(raw)


def test_review_cancellation_keeps_original_saved_state(queue):
    app, sources, player, store, saved, token = queue
    with pytest.raises(InterruptedError):
        saved.review(app.root, cancelled=lambda: True)
    assert store.load() == (saved, token)
    assert player.state == 'stopped' and not player.queue_active


def test_current_controls_override_is_explicit_and_never_applies_saved_device(queue, monkeypatch):
    app, _, _, _, saved, _ = queue
    from carr_library.speech.playback_devices import PlaybackDevice
    from dataclasses import asdict
    device = PlaybackDevice(7, 'Previously chosen device', 'old-interface', 1, 2, 3, 2)
    saved = replace(saved, output=asdict(device), volume=0, muted=True)
    current = WavePlayer()
    reviewed = saved.review(app.root)
    calls = []
    original = current.select_output
    def select(value):
        calls.append(value)
        if value is not None:
            raise RuntimeError('Saved output is unavailable; no fallback')
        return original(value)
    monkeypatch.setattr(current, 'select_output', select)
    try:
        with pytest.raises((RuntimeError, ValueError)):
            saved.apply(current, reviewed)
        assert current.playlist == () and current.path is None
        calls.clear()
        saved.apply(current, reviewed, use_current_controls=True)
        assert not calls and current.levels.volume == 100 and not current.levels.muted
        assert current.state == 'stopped' and current.position_seconds == .375
    finally:
        current.close()


def test_retention_rechecks_reviewed_source_before_copy_and_cancel_publishes_no_queue(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    source = audio(tmp_path / 'selected.wav')
    items = prepare_playlist((source,))
    with source.open('ab') as output:
        output.write(b'changed after exact review')
    with pytest.raises(ValueError, match='changed'):
        retain_playlist(root, items)
    assert not (root / NAME).exists()
    valid = prepare_playlist((audio(tmp_path / 'another.wav'),))
    with pytest.raises(InterruptedError):
        retain_playlist(root, valid, cancelled=lambda: True)
    assert not (root / NAME).exists()


@pytest.mark.parametrize('old_identity', ['legacy-adapter-interface', 'previous-endpoint-installation'])
def test_old_saved_output_stays_passive_and_requires_explicit_current_output(queue, monkeypatch, old_identity):
    from dataclasses import asdict
    from carr_library.speech import playback_devices as devices
    app, _, _, store, saved, token = queue
    current_output = devices.PlaybackDevice(0, 'Same device name', 'current-endpoint', 1, 2, 3, 2)
    old_output = replace(current_output, interface=old_identity)
    saved = replace(saved, output=asdict(old_output))
    token = store.save(saved, expected_token=token)
    original = store.path.read_bytes()
    native_calls = []
    def inventory(**options):
        native_calls.append(True)
        return (current_output,)
    monkeypatch.setattr(devices, 'output_devices', inventory)
    loaded, actual_token = store.load()
    assert loaded == saved and actual_token == token and native_calls == []
    assert loaded.to_dict()['output'] == asdict(old_output)
    reviewed = loaded.review(app.root)
    current = WavePlayer()
    try:
        with pytest.raises(RuntimeError, match='no fallback'):
            loaded.apply(current, reviewed)
        assert native_calls == [True]
        assert current.output_device is None and current.playlist == () and current.path is None
        assert not current._open and current._process is None and current._wave is None
        current.select_output(current_output)
        loaded.apply(current, reviewed, use_current_controls=True)
        assert native_calls == [True, True]
        assert current.output_device == current_output and current.playlist_index == 1
        assert current.state == 'stopped' and current.position_seconds == .375
        assert not current._open and current._process is None and current._wave is None
        assert store.path.read_bytes() == original and store.load() == (saved, token)
    finally:
        current.close()


def test_saved_endpoint_rebinds_a_changed_ordinal_only_after_explicit_review(queue, monkeypatch):
    from dataclasses import asdict
    from carr_library.speech import playback_devices as devices
    app, _, _, store, saved, token = queue
    old_output = devices.PlaybackDevice(0, 'Same device name', 'stable-endpoint', 1, 2, 3, 2)
    current_output = replace(old_output, index=3)
    saved = replace(saved, output=asdict(old_output))
    token = store.save(saved, expected_token=token)
    original = store.path.read_bytes()
    native_calls = []
    def inventory(**options):
        native_calls.append(True)
        return (devices.PlaybackDevice(0, old_output.name, 'other-endpoint', 1, 2, 3, 2), current_output)
    monkeypatch.setattr(devices, 'output_devices', inventory)
    loaded, actual_token = store.load()
    assert loaded == saved and actual_token == token and native_calls == []
    reviewed = loaded.review(app.root)
    current = WavePlayer()
    try:
        assert native_calls == []
        loaded.apply(current, reviewed)
        assert native_calls == [True]
        assert current.output_device == current_output and current.output_device.identity == old_output.identity
        assert current.playlist_index == 1 and current.position_seconds == .375
        assert current.state == 'stopped' and not current.queue_active
        assert not current._open and current._process is None and current._wave is None
        assert store.path.read_bytes() == original and store.load() == (saved, token)
    finally:
        current.close()
