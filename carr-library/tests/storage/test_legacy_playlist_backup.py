"""Real small PCM/history roundtrips; the separate large gate measures >512 MiB."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.legacy_playlist_state import (
    NAME, MAX_STATE_BYTES, decode_queue, validate_queue, validate_historical)
from carr_library.backup_historical import (
    DIRECTORY, validate_manifest, validate_state_tree, _validate_queue)
from carr_library.library_backup import create_backup, verify_backup, restore_backup


def _raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode()


def _queue(item):
    return dict(schema='carr-playback-queue/1', items=[item, dict(item, label='Repeated original')],
                index=1, frame=7, volume=41, muted=True, balance=-.25, skips=[7, 43],
                output=dict(index=127, name='Previous output', interface='old-device-interface',
                            manufacturer=1, product=2, driver=3, channels=2))


def _seal(folder, value):
    raw = _raw(value)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _archive(tmp_path, *, width=2):
    app = LibraryApp(tmp_path / 'source library')
    folder = tmp_path / 'legacy backup'
    create_backup(app, folder)
    metadata = json.loads((folder / 'manifest.json').read_bytes())
    assert metadata['version'] == 2
    selected = tmp_path / 'source.wav'
    with wave.open(str(selected), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(width)
        out.setframerate(8000)
        out.writeframes(bytes(range(width)) * 32)
    raw = selected.read_bytes()
    digest = sha256(raw).hexdigest()
    filename = 'state/studio/inputs/' + digest + '.audio'
    target = folder / filename
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    row = dict(filename=filename, label='Український запис', sha256=digest,
               size_bytes=len(raw), frames=32, sample_rate=8000, channels=1,
               sample_width=width)
    queue = _queue(row)
    path = folder / NAME
    path.parent.mkdir(parents=True)
    path.write_bytes(_raw(queue))
    metadata.update(version=4, generated_audio_included=False, sidecars=[
        dict(filename=filename, size_bytes=len(raw), sha256=digest),
        dict(filename=NAME, size_bytes=path.stat().st_size, sha256=sha256(path.read_bytes()).hexdigest())])
    return app, folder, metadata, queue, _seal(folder, metadata)


@pytest.mark.parametrize('width', [1, 2, 3, 4], ids=['pcm8', 'pcm16', 'pcm24', 'pcm32'])
def test_historical_queue_exact_pcm_and_raw_settings_survive_restore_and_rebackup(tmp_path, width):
    app, source, manifest, queue, pin = _archive(tmp_path, width=width)
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    verify_backup(source, pin)
    assert before == {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    restored = restore_backup(source, tmp_path / 'restored', pin)
    assert not (restored / NAME).exists()
    archive = restored / DIRECTORY / pin
    original = archive / 'files' / sha256(NAME.encode()).hexdigest()
    assert original.read_bytes() == _raw(queue)
    item = queue['items'][0]
    stored = archive / 'files' / sha256(item['filename'].encode()).hexdigest()
    assert sha256(stored.read_bytes()).hexdigest() == item['sha256']
    validate_state_tree(restored)
    again = tmp_path / 'again'
    digest = create_backup(LibraryApp(restored), again)
    verify_backup(again, digest)
    final = restore_backup(again, tmp_path / 'final', digest)
    assert (final / DIRECTORY / pin / 'files' / original.name).read_bytes() == original.read_bytes()
    assert not (final / NAME).exists()


@pytest.mark.parametrize('damage', ['missing', 'wrong-sha', 'wrong-size', 'wrong-pcm'],
                         ids=['missing', 'wrong-sha', 'wrong-size', 'wrong-pcm'])
def test_historical_queue_rejects_missing_or_changed_media(tmp_path, damage):
    _app, source, metadata, queue, _pin = _archive(tmp_path)
    item = queue['items'][0]
    if damage == 'missing':
        metadata['sidecars'] = [row for row in metadata['sidecars'] if row['filename'] == NAME]
        (source / item['filename']).unlink()
    elif damage == 'wrong-sha':
        for row in queue['items']:
            row['sha256'] = 'a' * 64
            row['filename'] = 'state/studio/inputs/' + 'a' * 64 + '.audio'
    elif damage == 'wrong-size':
        for row in queue['items']:
            row['size_bytes'] += 1
    else:
        for row in queue['items']:
            row['frames'] -= 1
    raw = _raw(queue)
    (source / NAME).write_bytes(raw)
    entry = next(row for row in metadata['sidecars'] if row['filename'] == NAME)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    with pytest.raises(ValueError):
        restore_backup(source, tmp_path / 'refused', _seal(source, metadata))
    assert not (tmp_path / 'refused').exists()


def test_queue_schema_limits_precede_item_construction(tmp_path):
    _, _, _, queue, _ = _archive(tmp_path)
    queue['items'] = [object()] * 257
    with pytest.raises(ValueError, match='Unsupported'):
        validate_queue(queue)
    with pytest.raises(ValueError, match='byte allowance'):
        decode_queue(b' ' * (MAX_STATE_BYTES + 1))


@pytest.mark.parametrize('field,value', [
    ('frame', True), ('index', -1), ('volume', float('nan')),
    ('balance', float('inf')), ('skips', [1]), ('output', {'index': 0}),
], ids=['bool-frame', 'negative-index', 'nan-volume', 'infinite-balance', 'skips', 'output'])
def test_queue_bad_passive_fields_never_become_device_or_cursor_authority(tmp_path, field, value):
    _, _, _, queue, _ = _archive(tmp_path)
    queue[field] = value
    with pytest.raises(ValueError):
        validate_queue(queue)


def test_queue_duplicate_json_fields_and_traversal_fail_before_path_use(tmp_path):
    _, source, metadata, queue, _ = _archive(tmp_path)
    raw = _raw(queue).replace(b'"frame": 7', b'"frame": 7, "frame": 8')
    with pytest.raises(ValueError):
        decode_queue(raw)
    queue['items'][0]['filename'] = '../external.wav'
    with pytest.raises(ValueError):
        validate_queue(queue)
    called = []
    without_queue = [row for row in metadata['sidecars'] if row['filename'] != NAME]
    assert validate_historical(without_queue, lambda name: called.append(name)) == frozenset()
    assert not called


def test_declared_large_input_allowance_is_conditional_on_exact_queue_references(tmp_path, monkeypatch):
    # Envelope-only budget test: no sparse file or fabricated execution claim.
    _, source, metadata, queue, _ = _archive(tmp_path)
    extra = dict(filename='state/studio/inputs/' + 'b' * 64 + '.audio',
                 size_bytes=513 * 1024**2, sha256='b' * 64)
    metadata['sidecars'].append(extra)
    validate_manifest(metadata)  # The subsequent content pass must settle the allowance.
    import carr_library.audio.legacy_playlist_state as schema
    monkeypatch.setattr(schema, 'validate_historical',
                        lambda entries, resolve, cancelled: frozenset([queue['items'][0]['filename']]))
    with pytest.raises(ValueError, match='Unreferenced'):
        _validate_queue(source, metadata, stored=False, cancelled=lambda: False)
    metadata['sidecars'] = [row for row in metadata['sidecars'] if row['filename'] != NAME]
    with pytest.raises(ValueError, match='byte budgets'):
        validate_manifest(metadata)


def test_cancelled_queue_hash_never_acknowledges_or_rewrites_original(tmp_path):
    _, source, metadata, _, _ = _archive(tmp_path)
    before = {str(p): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    count = 0
    def cancelled():
        nonlocal count
        count += 1
        return count >= 5
    with pytest.raises(InterruptedError):
        validate_historical(metadata['sidecars'], lambda name: source / name, cancelled=cancelled)
    assert before == {str(p): p.read_bytes() for p in source.rglob('*') if p.is_file()}


def test_standalone_pcm_verification_refuses_actual_close_boundary_rewrite(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import os
    from carr_library.audio import legacy_playlist_state as schema
    _, source, _, queue, _ = _archive(tmp_path)
    row = queue['items'][0]
    path = source / row['filename']
    real = schema.verified_reader
    changed = []
    @contextmanager
    def after_verified_close(selected, **options):
        with real(selected, **options) as stream:
            yield stream
        if Path(selected) == path and not changed:
            before = path.stat()
            with path.open('r+b') as stream:
                stream.seek(-2, os.SEEK_END)
                stream.write(b'\x15\x71')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            changed.append(path)
    monkeypatch.setattr(schema, 'verified_reader', after_verified_close)
    with pytest.raises(ValueError, match='before verification was acknowledged'):
        schema.verify_pcm(path, row)
    assert changed == [path]
