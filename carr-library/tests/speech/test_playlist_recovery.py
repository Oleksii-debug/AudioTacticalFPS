"""Real Studio retention and passive queue recovery; no native device authority."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime
from carr_library.audio.output_devices import OutputDevice
from carr_library.durable_state import encode
from carr_library.speech import VoiceRegistry
from carr_library.speech.player import WavePlayer
from carr_library.speech.playlist import (
    LEGACY_SCHEMA, NAME, SCHEMA, PlaylistStore, SavedPlaylist,
    prepare_playlist, retain_playlist, validate_collection,
)
from carr_library.studio.clips import ClipEditor
from carr_library.studio.render import ProductionService


def audio(path, value=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(bytes((value, 0)) * 8000)
    return path


@pytest.fixture
def queue(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual selected FFmpeg is required.'
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Playlist recovery')
    voices = VoiceRegistry([])
    service = ProductionService(app.root / 'state' / 'studio', voices=voices, runtime_factory=AudioRuntime)
    editor = ClipEditor(app.root, service=service)
    sources = (audio(tmp_path / 'selected' / 'Перша.wav'),
               audio(tmp_path / 'selected' / 'Друга.wav', 2))
    items = retain_playlist(app.root, prepare_playlist(sources), retain=editor.retain)
    player = WavePlayer()
    player.set_playlist((items[1], items[0], items[1]))
    player.select_queue(1)
    player.seek(.375, start_if_stopped=False)
    value = SavedPlaylist.capture(app.root, player, skips=(2.5, 7.5))
    store = PlaylistStore(app.root)
    token = store.save(value, expected_token=None)
    try:
        yield app, sources, player, store, value, token
    finally:
        assert player.close() is not False
        assert editor.close() is not False
        assert service.close() is not False
        assert voices.close() is not False
        if hasattr(app, 'close_search'):
            assert app.close_search() is not False


def test_retention_reuses_exact_private_originals_and_duplicate_order(queue):
    app, sources, player, store, saved, token = queue
    assert saved.index == 1 and saved.frame == 3000 and saved.skips == (2.5, 7.5)
    assert [row.label for row in saved.items] == [sources[1].name, sources[0].name, sources[1].name]
    assert saved.items[0].filename == saved.items[2].filename
    for row in saved.items:
        retained = app.root / row.filename
        assert retained.parent == app.root / 'state' / 'studio' / 'inputs'
        assert retained.stat().st_nlink == 1
        assert sha256(retained.read_bytes()).hexdigest() == row.sha256
    assert store.load() == (saved, token)
    assert not player.queue_active and player.state == 'stopped'
    assert not any(path.name.startswith('.pending-') for path in (app.root / 'state/studio/inputs').iterdir())
    raw = store.path.read_text(encoding='utf-8')
    assert all(str(path) not in raw for path in sources)
    entries = [{'filename': NAME, 'sha256': token, 'size_bytes': store.path.stat().st_size}]
    entries += [{'filename': row.filename, 'sha256': row.sha256, 'size_bytes': row.size_bytes}
                for row in saved.items]
    validate_collection(app.root, entries)


def test_load_is_only_passive_json_and_review_uses_retained_bytes_after_original_deletion(queue, monkeypatch):
    app, sources, player, store, saved, token = queue
    for path in sources:
        path.unlink()
    monkeypatch.setattr(WavePlayer, 'play', lambda *a, **k: pytest.fail('passive state played audio'))
    monkeypatch.setattr('carr_library.audio.output_devices.resolve_output_device',
                        lambda *a, **k: pytest.fail('passive state resolved a device'))
    assert PlaylistStore(app.root).load() == (saved, token)
    reviewed = saved.review(app.root)
    current = WavePlayer()
    try:
        saved.apply(current, reviewed, use_current_controls=True)
        assert current.state == 'stopped' and not current.queue_active
        assert current._process is None and not current._open and current._wave is None
        assert current.playlist_index == 1 and current.position_seconds == .375
        assert all(row.path.is_relative_to(app.root) for row in current.playlist)
        assert SavedPlaylist.capture(app.root, current, skips=saved.skips) == saved
    finally:
        assert current.close() is True


@pytest.mark.parametrize('change', ['bytes', 'missing'])
def test_changed_retained_media_never_gains_review_authority(queue, change):
    app, _, player, store, saved, token = queue
    target = app.root / saved.items[0].filename
    if change == 'missing':
        target.unlink()
    else:
        with target.open('r+b') as output:
            output.seek(-2, 2)
            output.write(b'\x7f\0')
    assert store.load() == (saved, token), 'Damaged media cannot erase the saved repairable document.'
    with pytest.raises((ValueError, OSError)):
        saved.review(app.root)
    assert player.state == 'stopped' and not player.queue_active


def test_saved_row_cannot_retarget_external_file_with_resealed_json(queue, tmp_path, monkeypatch):
    _, _, _, store, saved, _ = queue
    external = audio(tmp_path / 'unrelated.wav', 77)
    value = saved.to_dict()
    value['items'][0]['filename'] = str(external)
    store.path.write_bytes(encode(value))
    original = Path.open
    def forbid(path, *args, **kwargs):
        if path == external:
            pytest.fail('An invalid saved queue opened unrelated external media.')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', forbid)
    with pytest.raises(ValueError, match='retained audio identity'):
        store.load()


def test_exact_cas_keeps_concurrent_writer_and_uncommitted_snapshot(queue):
    app, _, _, store, saved, token = queue
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
    saved = queue[4].to_dict()
    saved[field] = value
    with pytest.raises(ValueError):
        SavedPlaylist.from_dict(saved)


def test_current_navigation_allowance_roundtrips_without_reinterpreting_legacy(queue):
    saved = queue[4]
    for interval in (1, 600, 1000, 3600):
        current = replace(saved, skips=(interval, interval))
        assert SavedPlaylist.from_dict(current.to_dict()) == current
    with pytest.raises(ValueError):
        replace(saved, skips=(3601, 10))
    legacy = replace(saved, schema=LEGACY_SCHEMA, skips=(.1, 600))
    assert SavedPlaylist.from_dict(legacy.to_dict()).to_dict() == legacy.to_dict()
    with pytest.raises(ValueError):
        replace(legacy, skips=(1000, 1000))


@pytest.mark.parametrize('dialect', [SCHEMA, LEGACY_SCHEMA])
def test_saved_device_is_never_substituted_by_an_ordinal_or_applied_implicitly(queue, monkeypatch, dialect):
    app, _, _, _, saved, _ = queue
    device = OutputDevice(7, 'authored-old-endpoint', 'Previously selected output')
    output = asdict(device) if dialect == SCHEMA else {
        'index': 7, 'name': 'Old output', 'interface': 'old-endpoint',
        'manufacturer': 1, 'product': 2, 'driver': 3, 'channels': 2}
    saved = replace(saved, schema=dialect, output=output)
    calls = []
    def unavailable(selected):
        calls.append(selected)
        raise RuntimeError('The exact saved output is unavailable.')
    monkeypatch.setattr('carr_library.audio.output_devices.resolve_output_device', unavailable)
    reviewed = saved.review(app.root)
    current = WavePlayer()
    try:
        with pytest.raises((RuntimeError, ValueError)):
            saved.apply(current, reviewed)
        assert current.playlist == () and current.path is None
        assert len(calls) == (1 if dialect == SCHEMA else 0)
        calls.clear()
        saved.apply(current, reviewed, use_current_controls=True)
        assert calls == [] and current.output_device is None
        assert current.state == 'stopped' and current.position_seconds == .375
        assert current.levels.volume == 100 and not current.levels.muted
    finally:
        assert current.close() is True


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_late_lock_exit_reports_exact_present_bytes_without_claiming_commit(queue, monkeypatch, failure):
    import carr_library.speech.playlist as module
    _, _, _, store, saved, token = queue
    actual = module.state_lock
    @contextmanager
    def release_failure(*args, **kwargs):
        with actual(*args, **kwargs):
            yield
        raise failure('authored release failure')
    monkeypatch.setattr(module, 'state_lock', release_failure)
    changed = replace(saved, frame=4000)
    new_token = store.save(changed, expected_token=token)
    assert store.load() == (changed, new_token)
    assert 'exact saved queue bytes are present' in store.notice
    assert 'completion remains unconfirmed' in store.notice


@pytest.mark.parametrize('replacement', ['foreign', 'removed'])
def test_late_lock_exit_never_hides_absent_or_foreign_state(queue, monkeypatch, replacement):
    import carr_library.speech.playlist as module
    _, _, _, store, saved, token = queue
    actual = module.state_lock
    @contextmanager
    def release_failure(*args, **kwargs):
        with actual(*args, **kwargs):
            yield
        if replacement == 'removed':
            store.path.unlink()
        else:
            store.path.write_bytes(encode(replace(saved, frame=2000).to_dict()))
        raise OSError('authored release failure')
    monkeypatch.setattr(module, 'state_lock', release_failure)
    with pytest.raises(OSError, match='release failure'):
        store.save(replace(saved, frame=4000), expected_token=token)
    assert not store.notice
    loaded, _ = store.load()
    assert loaded is None if replacement == 'removed' else loaded.frame == 2000


def test_retention_rechecks_source_and_requires_borrowed_actual_service(queue):
    app, sources, _, store, saved, token = queue
    source = audio(sources[0])
    reviewed = prepare_playlist((source,))
    with source.open('ab') as stream:
        stream.write(b'changed after review')
    admitted = []
    def must_not_retain(*args, **kwargs):
        admitted.append(True)
        raise AssertionError('changed source reached retention')
    with pytest.raises(ValueError, match='changed'):
        retain_playlist(app.root, reviewed, retain=must_not_retain)
    assert admitted == [] and store.load() == (saved, token)
    with pytest.raises(InterruptedError):
        saved.review(app.root, cancelled=lambda: True)
    assert store.load() == (saved, token)


def test_saved_state_rejects_duplicate_nonfinite_and_oversized_capture(queue):
    _, _, _, store, saved, _ = queue
    for raw in (b'{"schema":"carr-playback-queue/2","schema":"other"}',
                b'{"schema":NaN}', b'x' * (2 * 1024**2 + 1)):
        store.path.write_bytes(raw)
        with pytest.raises((ValueError, UnicodeError)):
            store.load()
    assert saved.frame == 3000
