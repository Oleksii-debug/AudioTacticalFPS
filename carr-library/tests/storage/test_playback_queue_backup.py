"""Common4 copies the exact retained queue without activating media or devices."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime
from carr_library.audio.output_devices import OutputDevice
from carr_library.durable_state import encode
from carr_library import library_backup as backups
from carr_library.speech import VoiceRegistry
from carr_library.speech.playlist import (
    LEGACY_SCHEMA, MAX_ITEMS, MAX_STATE_BYTES, NAME, SCHEMA,
    PlaylistStore, SavedPlaylist, SavedPlaylistItem, prepare_playlist, retain_playlist,
)
from carr_library.studio.clips import ClipEditor
from carr_library.studio.render import ProductionService


def audio(path, sample):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(bytes((sample, 0)) * 8000)
    return path


def inventory(root):
    return {str(path.relative_to(root)): (path.stat().st_size, sha256(path.read_bytes()).hexdigest())
            for path in root.rglob('*') if path.is_file()}


@pytest.fixture
def saved_queue(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual selected FFmpeg is required.'
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Retained queue backup')
    assert app.add_text(workspace, 'Original source', 'Independent library source.')['state'] == 'complete'
    voices = VoiceRegistry([])
    service = ProductionService(app.root / 'state' / 'studio', voices=voices, runtime_factory=AudioRuntime)
    editor = ClipEditor(app.root, service=service)
    sources = (audio(tmp_path / 'selected' / 'Перша.wav', 1),
               audio(tmp_path / 'selected' / 'Друга.wav', 2))
    retained = retain_playlist(app.root, prepare_playlist(sources), retain=editor.retain)
    rows = tuple(SavedPlaylistItem.reviewed(app.root, item)
                 for item in (retained[1], retained[0], retained[1]))
    value = SavedPlaylist(rows, 1, 3000, 37, True, -.25, (1000, 3600),
                          asdict(OutputDevice(7, 'authored-saved-endpoint', 'Saved output')))
    token = PlaylistStore(app.root).save(value, expected_token=None)
    assert token == sha256((app.root / NAME).read_bytes()).hexdigest()
    try:
        yield app, workspace, sources, editor, service, voices, value
    finally:
        assert editor.close() is not False
        assert service.close() is not False
        assert voices.close() is not False
        if hasattr(app, 'close_search'):
            assert app.close_search() is not False


def forbid_activation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Passive common4 queue recovery activated a media, device, or process owner.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(AudioRuntime, '__init__', forbidden)
    monkeypatch.setattr(ProductionService, '__init__', forbidden)
    monkeypatch.setattr('carr_library.speech.player.WavePlayer.__init__', forbidden)
    monkeypatch.setattr('carr_library.speech.playlist.prepare_playlist', forbidden)
    monkeypatch.setattr('carr_library.audio.output_devices.resolve_output_device', forbidden)


@pytest.mark.parametrize('schema', [SCHEMA, LEGACY_SCHEMA])
def test_real_common4_restores_exact_queue_order_and_controls_without_activation(
        saved_queue, tmp_path, monkeypatch, schema):
    app, workspace, sources, editor, service, voices, value = saved_queue
    if schema == LEGACY_SCHEMA:
        value = replace(value, schema=schema, skips=(.1, 600), output={
            'index': 7, 'name': 'Historical output', 'interface': 'old-output-endpoint',
            'manufacturer': 1, 'product': 2, 'driver': 3, 'channels': 2})
        store = PlaylistStore(app.root)
        store.save(value, expected_token=store.load()[1])
    raw = (app.root / NAME).read_bytes()
    media = {row.filename: (app.root / row.filename).read_bytes() for row in value.items}
    assert len(value.items) == 3 and len(media) == 2
    assert all(str(source).encode() not in raw for source in sources)
    assert editor.close() is not False
    assert service.close() is not False
    assert voices.close() is not False
    folder = tmp_path / 'backup'
    with monkeypatch.context() as passive:
        forbid_activation(passive)
        checksum = backups.create_backup(app, folder)
        manifest = backups.verify_backup(folder, checksum)
        assert manifest['version'] == 4
        queue_row, = [row for row in manifest['sidecars'] if row['filename'] == NAME]
        assert queue_row['sha256'] == sha256(raw).hexdigest()
        assert queue_row['size_bytes'] == len(raw)
        assert len([row for row in manifest['sidecars'] if row['filename'] in media]) == 2
        before = inventory(folder)
        shutil.rmtree(app.root)
        shutil.rmtree(sources[0].parent)
        restored = backups.restore_backup(folder, tmp_path / 'restored', checksum)
        loaded, token = PlaylistStore(restored).load()
        assert loaded == value and token == sha256(raw).hexdigest()
        assert (restored / NAME).read_bytes() == raw
        assert inventory(folder) == before
        assert {name: (restored / name).read_bytes() for name in media} == media
        assert all((restored / name).stat().st_nlink == 1 for name in media)
        assert loaded.index == 1 and loaded.frame == 3000
        assert [row.filename for row in loaded.items] == [value.items[i].filename for i in range(3)]
        reopened = LibraryApp(restored)
        try:
            assert reopened.search(workspace, 'Independent library source')
        finally:
            if hasattr(reopened, 'close_search'):
                assert reopened.close_search() is not False
    # Only an explicit subsequent review reads retained PCM again; it never needs
    # the now-deleted external sources or the saved output device.
    reviewed = value.review(restored)
    assert [item.sha256 for item in reviewed] == [row.sha256 for row in value.items]
    assert all(item.path.is_relative_to(restored / 'state' / 'studio' / 'inputs') for item in reviewed)


def reseal(folder, mutate):
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    mutate(folder, manifest)
    raw = encode(manifest)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def update_queue(folder, manifest, raw):
    (folder / NAME).write_bytes(raw)
    row, = [entry for entry in manifest['sidecars'] if entry['filename'] == NAME]
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize('damage', [
    'missing-media', 'changed-media', 'digest', 'count', 'external-path',
    'duplicate-key', 'oversized-state', 'unknown-sibling',
])
def test_resigned_queue_inconsistency_is_rejected_by_actual_full_restore(
        saved_queue, tmp_path, monkeypatch, damage):
    app, _, _, _, _, _, value = saved_queue
    folder = tmp_path / 'backup'
    backups.create_backup(app, folder)
    def mutate(folder, manifest):
        if damage == 'missing-media':
            name = value.items[0].filename
            (folder / name).unlink()
            manifest['sidecars'] = [entry for entry in manifest['sidecars'] if entry['filename'] != name]
        elif damage == 'changed-media':
            name = value.items[0].filename
            raw = bytearray((folder / name).read_bytes())
            raw[-1] ^= 1
            (folder / name).write_bytes(raw)
            row, = [entry for entry in manifest['sidecars'] if entry['filename'] == name]
            row['sha256'] = sha256(raw).hexdigest()
        elif damage == 'unknown-sibling':
            name = 'state/speech/playback-queue-extra.json'
            raw = encode(value.to_dict())
            (folder / name).write_bytes(raw)
            manifest['sidecars'].append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
        else:
            wire = value.to_dict()
            if damage == 'digest':
                wire['items'][0]['sha256'] = '0' * 64
            elif damage == 'count':
                wire['items'] = [wire['items'][0]] * (MAX_ITEMS + 1)
            elif damage == 'external-path':
                wire['items'][0]['filename'] = str(tmp_path / 'unselected.wav')
            raw = encode(wire)
            if damage == 'duplicate-key':
                raw = raw.replace(b'{', b'{"schema": "carr-playback-queue/2",', 1)
            elif damage == 'oversized-state':
                raw += b' ' * (MAX_STATE_BYTES + 1 - len(raw))
            update_queue(folder, manifest, raw)
    checksum = reseal(folder, mutate)
    before = inventory(folder)
    destination = tmp_path / 'refused'
    with monkeypatch.context() as passive:
        forbid_activation(passive)
        with pytest.raises((ValueError, RuntimeError, OSError)):
            backups.restore_backup(folder, destination, checksum)
    assert not destination.exists()
    assert inventory(folder) == before


@pytest.mark.parametrize('target', ['queue', 'media'])
def test_actual_hard_link_is_refused_before_common4_publication(saved_queue, tmp_path, target):
    app, _, _, _, _, _, value = saved_queue
    selected = app.root / (NAME if target == 'queue' else value.items[0].filename)
    original = selected.read_bytes()
    alias = tmp_path / 'alias'
    import os
    os.link(selected, alias)
    try:
        destination = tmp_path / 'refused'
        with pytest.raises((ValueError, RuntimeError, OSError)):
            backups.create_backup(app, destination)
        assert not destination.exists()
        assert selected.read_bytes() == original and alias.read_bytes() == original
    finally:
        alias.unlink()


@pytest.mark.parametrize('schema', [SCHEMA, LEGACY_SCHEMA])
def test_incremental_queue_recovery_is_passive_and_keeps_exact_saved_order(
        saved_queue, tmp_path, monkeypatch, schema):
    from carr_library.backup_incremental import (BackupReference,
        create_incremental_backup, restore_incremental_backup)
    from carr_library.backup_budget import BackupBudget, MIB, GIB
    app, _, sources, editor, service, voices, value = saved_queue
    if schema == LEGACY_SCHEMA:
        value = replace(value, schema=schema, skips=(.1, 600), output={
            'index': 7, 'name': 'Historical output', 'interface': 'old-output-endpoint',
            'manufacturer': 1, 'product': 2, 'driver': 3, 'channels': 2})
        store = PlaylistStore(app.root)
        store.save(value, expected_token=store.load()[1])
    base_path = tmp_path / 'full'
    base = BackupReference(base_path, backups.create_backup(app, base_path))
    store = PlaylistStore(app.root)
    value = replace(value, items=(value.items[1], value.items[0], value.items[1]), index=2, frame=4000)
    store.save(value, expected_token=store.load()[1])
    raw = (app.root / NAME).read_bytes()
    media = {row.filename: (app.root / row.filename).read_bytes() for row in value.items}
    assert editor.close() is not False and service.close() is not False and voices.close() is not False
    policy = BackupBudget(memory_bytes=GIB, staging_bytes=256*MIB, seconds=300)
    with monkeypatch.context() as passive:
        forbid_activation(passive)
        receipt = create_incremental_backup(app, tmp_path / 'delta', base=base, backup_budget=policy)
        layer = json.loads((receipt.path / 'manifest.json').read_bytes())
        assert NAME in layer['stored'] and not set(media) & set(layer['stored'])
        before = inventory(base.path), inventory(receipt.path)
        shutil.rmtree(app.root)
        shutil.rmtree(sources[0].parent)
        restored = restore_incremental_backup(receipt.path, tmp_path / 'restored-layer',
            receipt.manifest_sha256, bases=(base,), backup_budget=policy)
        loaded, token = PlaylistStore(restored).load()
        assert loaded == value and token == sha256(raw).hexdigest()
        assert (restored / NAME).read_bytes() == raw
        assert {name: (restored / name).read_bytes() for name in media} == media
        assert all((restored / name).stat().st_nlink == 1 for name in media)
        assert (inventory(base.path), inventory(receipt.path)) == before
        assert loaded.index == 2 and loaded.frame == 4000
        assert loaded.schema == schema and loaded.output == value.output
