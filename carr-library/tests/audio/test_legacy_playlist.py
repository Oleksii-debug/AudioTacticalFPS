"""Actual current-owner migration; no alternate player, source path or device."""
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp, open_existing_library
from carr_library.audio import legacy_playlist as migration
from carr_library.audio.legacy_playlist_state import NAME, PROVENANCE_NAME, provenance_assets
from carr_library.audio.playlist import PlaylistStore
from carr_library.audio.playback_processing import PlaybackControls
from carr_library.audio.backup import backup_project, restore_project
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.ui.audio_studio import StudioController


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()


def _fixture(tmp_path, width=2):
    app = LibraryApp(tmp_path / 'current')
    source = tmp_path / 'selected canonical library'
    path = tmp_path / 'original.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, width, 8000, 64, 'NONE', 'PCM'))
        audio.writeframes(bytes(range(width)) * 64)
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    name = 'state/studio/inputs/' + digest + '.audio'
    selected = source / name
    selected.parent.mkdir(parents=True)
    selected.write_bytes(raw)
    row = dict(filename=name, label='Я' * 256, sha256=digest, size_bytes=len(raw),
               frames=64, sample_rate=8000, channels=1, sample_width=width)
    value = dict(schema='carr-playback-queue/1',
        items=[row, dict(row, label='Second occurrence')], index=1, frame=21,
        volume=17, muted=True, balance=.7, skips=[17, 51],
        output=dict(index=63, name='Former output', interface='not-a-current-device',
                    manufacturer=1, product=2, driver=3, channels=2))
    queue = source / NAME
    queue.parent.mkdir(parents=True)
    queue.write_bytes(_encode(value))
    return app, source, selected, value


def test_review_import_and_current_store_use_exact_originals_without_starting_audio(tmp_path, monkeypatch):
    app, source, original, old = _fixture(tmp_path)
    import carr_library.audio.player as player
    monkeypatch.setattr(player, 'WavePlayer', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('Migration must never construct a player.')))
    review = migration.review_root(app.root, source, voice='sapi5:default', language='uk')
    assert 'starts no playback' in review.description()
    original_bytes = original.read_bytes()
    result = migration.import_reviewed(review)
    assert result.durability_confirmed and result.items == 2
    controller = StudioController(private_root=app.root).open(result.project_path)
    assert controller.input_kind == 'narration' and controller.draft.source_text == ''
    store = PlaylistStore(app.root, result.project_id)
    current = store.review()
    assert [row.label for row in current.state.items] == [row['label'] for row in old['items']]
    assert (current.state.index, current.state.frame, current.state.accuracy) == (1, 21, 'historical_cursor')
    assert current.state.controls == PlaybackControls()
    provenance = json.loads((store.audio / PROVENANCE_NAME).read_bytes())
    assert provenance['queue_utf8'].encode() == (source / NAME).read_bytes()
    assert json.loads(provenance['queue_utf8'])['output'] == old['output']
    original.unlink()
    assert all(path.read_bytes() == original_bytes for path in store.paths(store.review()))


def test_current_broker_is_used_for_private_staging_not_a_second_policy(tmp_path, monkeypatch):
    app, source, _, _ = _fixture(tmp_path)
    from carr_library.operations import ResourceBroker
    real = ResourceBroker.for_app
    roots = []
    def capture(root, **kwargs):
        roots.append(Path(root))
        return real(root, **kwargs)
    monkeypatch.setattr(ResourceBroker, 'for_app', capture)
    review = migration.review_root(app.root, source)
    result = migration.import_reviewed(review)
    assert result.project_path.is_file()
    assert roots and all(root == app.root for root in roots)


@pytest.mark.parametrize('change', ['queue', 'pcm'], ids=['saved-queue', 'original-pcm'])
def test_changes_after_review_never_publish_a_new_project(tmp_path, change):
    app, source, original, _ = _fixture(tmp_path)
    review = migration.review_root(app.root, source)
    if change == 'queue':
        (source / NAME).write_bytes((source / NAME).read_bytes() + b' ')
    else:
        stamp = original.stat()
        with original.open('r+b') as stream:
            stream.seek(-2, os.SEEK_END)
            stream.write(b'\x22\x44')
        os.utime(original, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises((ValueError, RuntimeError)):
        migration.import_reviewed(review)
    base = app.root / 'audio-projects'
    assert not base.exists() or not tuple(base.iterdir())


def test_cancellation_at_final_publication_keeps_source_and_existing_projects(tmp_path, monkeypatch):
    app, source, original, _ = _fixture(tmp_path)
    review = migration.review_root(app.root, source)
    initial = original.read_bytes()
    stopped = [False]
    real = migration.publish_noreplace
    def at_boundary(staged, target, **kwargs):
        stopped[0] = True
        return real(staged, target, **kwargs)
    monkeypatch.setattr(migration, 'publish_noreplace', at_boundary)
    with pytest.raises(InterruptedError):
        migration.import_reviewed(review, cancelled=lambda: stopped[0])
    assert original.read_bytes() == initial
    assert not tuple((app.root / 'audio-projects').iterdir())


def test_postmove_failure_retains_one_exact_project_and_acknowledges_its_identity(tmp_path, monkeypatch):
    app, source, _, _ = _fixture(tmp_path)
    review = migration.review_root(app.root, source)
    real = migration.publish_noreplace
    def moved(staged, target, **kwargs):
        real(staged, target, **kwargs)
        raise OSError('Synthetic post-move directory flush failure.')
    monkeypatch.setattr(migration, 'publish_noreplace', moved)
    with pytest.raises(migration.QueueImportCommittedError) as failure:
        migration.import_reviewed(review)
    result = failure.value.result
    assert result.project_path.is_file() and not result.durability_confirmed
    assert len(tuple((app.root / 'audio-projects').iterdir())) == 1
    store = PlaylistStore(app.root, result.project_id)
    assert len(store.review().state.items) == 2
    provenance_assets(store.audio, result.project_id)


def test_new_project_collision_never_replaces_an_existing_directory(tmp_path, monkeypatch):
    app, source, _, _ = _fixture(tmp_path)
    review = migration.review_root(app.root, source)
    identity = 'a' * 32
    occupied = app.root / 'audio-projects' / identity
    occupied.mkdir(parents=True)
    (occupied / 'unrelated').write_bytes(b'Keep this existing project.')
    monkeypatch.setattr(migration, 'uuid4', lambda: SimpleNamespace(hex=identity))
    with pytest.raises(FileExistsError):
        migration.import_reviewed(review)
    assert (occupied / 'unrelated').read_bytes() == b'Keep this existing project.'
    assert tuple(path.name for path in occupied.iterdir()) == ('unrelated',)


def test_backups_preserve_raw_queue_and_originals_even_after_current_queue_is_cleared(tmp_path):
    app, source, _, old = _fixture(tmp_path)
    result = migration.import_reviewed(migration.review_root(app.root, source))
    store = PlaylistStore(app.root, result.project_id)
    current = store.review()
    store.checkpoint(current, clear=True)
    snapshot = tmp_path / 'project.zip'
    backup_project(result.project_path, snapshot, project_dir=store.audio,
                   include_audio=False, include_cache=False)
    separate = restore_project(snapshot, tmp_path / 'separate')
    provenance_assets(separate['project_dir'], result.project_id)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    reopened = PlaylistStore(restored, result.project_id)
    assert reopened.review().state.items == ()
    retained = provenance_assets(reopened.audio, result.project_id)
    assert 'assets/' + old['items'][0]['sha256'] + '.wav' in retained
    (reopened.audio / 'assets' / (old['items'][0]['sha256'] + '.wav')).unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        create_backup(open_existing_library(restored), tmp_path / 'incomplete')


@pytest.mark.parametrize('width', [1, 3, 4], ids=['pcm8', 'pcm24', 'pcm32'])
def test_unrepresentable_original_width_is_preserved_and_requires_explicit_conversion(tmp_path, width):
    app, source, original, _ = _fixture(tmp_path, width=width)
    before = original.read_bytes()
    with pytest.raises(ValueError, match='explicit media converter'):
        migration.review_root(app.root, source)
    assert original.read_bytes() == before
    assert not (app.root / 'audio-projects').exists()


def test_archived_queue_import_never_needs_old_absolute_root_or_source_device(tmp_path):
    app, source, original, old = _fixture(tmp_path)
    archive = tmp_path / 'canonical backup'
    create_backup(app, archive)
    metadata = json.loads((archive / 'manifest.json').read_bytes())
    entries = []
    for name in (NAME, old['items'][0]['filename']):
        path = archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (source / name).read_bytes()
        path.write_bytes(raw)
        entries.append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    metadata.update(version=4, generated_audio_included=False, sidecars=entries)
    raw = _encode(metadata)
    (archive / 'manifest.json').write_bytes(raw)
    pin = sha256(raw).hexdigest()
    restored = restore_backup(archive, tmp_path / 'restored legacy', pin)
    original.unlink()
    listed = migration.sources(restored)
    assert [(row.kind, row.identity) for row in listed] == [('archive', pin)]
    result = migration.import_reviewed(migration.review_archive(restored, pin))
    queue = PlaylistStore(restored, result.project_id).review()
    assert queue.state.frame == old['frame'] and queue.state.accuracy == 'historical_cursor'
    assert queue.state.controls == PlaybackControls()


def test_actual_original_change_after_copy_with_restored_mtime_is_refused(tmp_path, monkeypatch):
    app, source, original, _ = _fixture(tmp_path)
    review = migration.review_root(app.root, source)
    real = migration.publish_noreplace
    def alter_before_boundary(staged, target, **kwargs):
        stamp = original.stat()
        with original.open('r+b') as stream:
            stream.seek(-2, os.SEEK_END)
            stream.write(b'\x99\x33')
        os.utime(original, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert original.stat().st_size == stamp.st_size
        assert original.stat().st_mtime_ns == stamp.st_mtime_ns
        return real(staged, target, **kwargs)
    monkeypatch.setattr(migration, 'publish_noreplace', alter_before_boundary)
    with pytest.raises(ValueError, match='recording changed'):
        migration.import_reviewed(review)
    assert not tuple((app.root / 'audio-projects').iterdir())


@pytest.mark.parametrize('member', ['document', 'provenance', 'queue', 'pcm', 'extra', 'replacement'],
                         ids=['document', 'provenance', 'queue', 'pcm', 'extra-member', 'same-byte-replacement'])
def test_final_private_tree_pin_rejects_payload_mutation_inside_unchanged_directories(
        tmp_path, monkeypatch, member):
    app, source, _, old = _fixture(tmp_path)
    review = migration.review_root(app.root, source)
    real = migration.publish_noreplace
    def alter_before_boundary(staged, target, **kwargs):
        mapping = {'document': staged / 'studio/project.json',
                   'provenance': staged / ('audio/' + PROVENANCE_NAME),
                   'queue': staged / 'audio/playback-queue.json',
                   'pcm': staged / ('audio/assets/' + old['items'][0]['sha256'] + '.wav')}
        if member == 'extra':
            (staged / 'audio/unlisted.bin').write_bytes(b'Unknown private-stage payload.')
        elif member == 'replacement':
            path = mapping['pcm']
            data, stamp = path.read_bytes(), path.stat()
            other = path.with_name('replacement.part')
            other.write_bytes(data)
            os.replace(other, path)
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        else:
            path = mapping[member]
            stamp = path.stat()
            with path.open('r+b') as stream:
                stream.seek(-2, os.SEEK_END)
                original_byte = stream.read(1)
                stream.seek(-1, os.SEEK_CUR)
                stream.write(bytes([original_byte[0] ^ 1]))
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            assert path.stat().st_size == stamp.st_size
            assert path.stat().st_mtime_ns == stamp.st_mtime_ns
        return real(staged, target, **kwargs)
    monkeypatch.setattr(migration, 'publish_noreplace', alter_before_boundary)
    with pytest.raises(ValueError, match='private queue'):
        migration.import_reviewed(review)
    assert not tuple((app.root / 'audio-projects').iterdir())


def test_empty_imported_queue_keeps_provenance_through_exact_project_snapshot(tmp_path):
    app, source, original, old = _fixture(tmp_path)
    old.update(items=[], index=None, frame=0)
    (source / NAME).write_bytes(_encode(old))
    result = migration.import_reviewed(migration.review_root(app.root, source))
    store = PlaylistStore(app.root, result.project_id)
    assert store.review().state.items == ()
    assert not (store.audio / 'assets').exists()
    expected = (store.audio / PROVENANCE_NAME).read_bytes()
    backup = tmp_path / 'empty-project.zip'
    backup_project(result.project_path, backup, project_dir=store.audio,
                   include_audio=False, include_cache=False)
    restored = restore_project(backup, tmp_path / 'empty-restored')
    assert (restored['project_dir'] / PROVENANCE_NAME).read_bytes() == expected
    assert provenance_assets(restored['project_dir'], result.project_id) == {
        PROVENANCE_NAME: restored['project_dir'] / PROVENANCE_NAME}


def test_last_publication_boundary_has_no_external_callback_after_any_staged_pin(
        tmp_path, monkeypatch):
    app, source, _, _ = _fixture(tmp_path)
    review = migration.review_root(app.root, source)
    actual_publish, actual_native = migration.publish_noreplace, migration.native_identity
    state = dict(round=0, root=None, first=None, original=None, changed=False)
    def native(path):
        result = actual_native(path)
        if (state['round'] == 2 and state['root'] is not None
                and state['root'] in Path(path).parents and state['first'] is None
                and not Path(path).name.endswith('.lock')):
            state['first'], state['original'] = Path(path), Path(path).read_bytes()
        return result
    def cancelled():
        if state['round'] == 2 and state['first'] is not None and not state['changed']:
            path = state['first']
            stamp = path.stat()
            with path.open('r+b') as stream:
                stream.seek(-2, os.SEEK_END)
                byte = stream.read(1)
                stream.seek(-1, os.SEEK_CUR)
                stream.write(bytes([byte[0] ^ 1]))
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            state['changed'] = True
        return False
    def publish(staged, target, **options):
        state['root'] = staged
        boundary = options['cancelled']
        def counted():
            state['round'] += 1
            return boundary()
        options['cancelled'] = counted
        return actual_publish(staged, target, **options)
    monkeypatch.setattr(migration, 'native_identity', native)
    monkeypatch.setattr(migration, 'publish_noreplace', publish)
    result = migration.import_reviewed(review, cancelled=cancelled)
    assert state['round'] == 2 and state['first'] is not None
    assert not state['changed'], 'No observer may run after the last bounded native verification begins.'
    published = result.project_path.parent.parent / state['first'].relative_to(state['root'])
    assert published.read_bytes() == state['original']
