"""Actual retained PCM, durable queue conflicts and portable backup contracts."""
from dataclasses import replace
import json
import os
import wave

import pytest

from carr_library.application import LibraryApp, open_existing_library
from carr_library.audio import playlist as module
from carr_library.audio.backup import backup_project, restore_project
from carr_library.audio.playback import PlaybackStatus
from carr_library.audio.playback_processing import PlaybackControls
from carr_library.audio.player import WavePlayer
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.ui.audio_studio import StudioController


def wav(path, sample=b'\x10\x00'):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 8000, 'NONE', 'PCM'))
        stream.writeframes(sample * 8000)
    return path


@pytest.fixture
def project(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    controller = StudioController(private_root=app.root)
    controller.new('Immutable narration', title='Retained playlist')
    controller.save_in_library()
    store = module.PlaylistStore(app.root, controller.project_id)
    paths = (wav(tmp_path / 'Україна.wav'), wav(tmp_path / 'second.wav', b'\x20\x00'))
    return app, controller, store, paths


def test_actual_owned_copies_cursor_controls_restart_and_inert_review(project):
    app, controller, store, paths = project
    originals = tuple(path.read_bytes() for path in paths)
    first = store.append(paths)
    controls = PlaybackControls(1.25, 2, .5, -.25)
    saved = store.checkpoint(first, index=1, frame=4000, accuracy='driver_reported', controls=controls)
    paths[0].unlink()
    paths[1].write_bytes(b'External file changed after its verified copy.')
    other = module.PlaylistStore(app.root, controller.project_id)
    reviewed = other.review()
    assert reviewed.state == saved.state
    assert tuple(path.read_bytes() for path in other.paths(reviewed)) == originals
    value = json.loads(store.path.read_text('utf-8'))
    assert value['cursor'] == {'index': 1, 'frame': 4000, 'accuracy': 'driver_reported'}
    assert 'device' not in value and 'sleep' not in value
    assert controller.draft.source_text == 'Immutable narration'


def test_explicit_resume_uses_current_player_and_preserves_entire_queue(project):
    _, _, store, paths = project
    reviewed = store.append(paths)
    player = WavePlayer()
    opened = []
    class Device:
        def stop(self):
            pass
        def status(self):
            return PlaybackStatus()
        def play(self, path, **kwargs):
            opened.append((path, kwargs))
    player.native = Device()
    player.set_playlist(store.paths(reviewed))
    assert opened == []
    player.play_index(1, device_id=3, start_seconds=.5)
    assert player.playlist == store.paths(reviewed) and player.playlist_index == 1
    assert opened == [(store.paths(reviewed)[1], {'start_seconds': .5,
                                                'end_seconds': None, 'device_id': 3})]


def test_changed_owned_bytes_refuse_review_and_complete_backup(project, tmp_path):
    app, _, store, paths = project
    reviewed = store.append(paths)
    selected = store.paths(reviewed)[0]
    before = selected.stat()
    with selected.open('r+b') as stream:
        stream.seek(-2, os.SEEK_END)
        stream.write(b'\x34\x12')
    os.utime(selected, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match='receipt'):
        store.review()
    with pytest.raises(ValueError, match='receipt'):
        create_backup(app, tmp_path / 'invalid-backup')


def test_conflicting_windows_never_replace_another_committed_cursor(project):
    app, controller, store, paths = project
    first = store.append(paths)
    other = module.PlaylistStore(app.root, controller.project_id)
    stale = other.review()
    current = store.checkpoint(first, index=0, frame=2000, accuracy='completed_buffers')
    with pytest.raises(ValueError, match='another window'):
        other.checkpoint(stale, index=1, frame=3000, accuracy='driver_reported')
    assert store.review().state == current.state


@pytest.mark.parametrize('cursor', [
    {'index': True, 'frame': 0, 'accuracy': 'driver_reported'},
    {'index': 0, 'frame': True, 'accuracy': 'driver_reported'},
    {'index': 2, 'frame': 0, 'accuracy': 'driver_reported'},
    {'index': 0, 'frame': 8001, 'accuracy': 'driver_reported'},
    {'index': 0, 'frame': 20, 'accuracy': 'external_player'},
    {'index': 0, 'frame': None, 'accuracy': 'driver_reported'},
    {'index': None, 'frame': 0, 'accuracy': 'completed_buffers'},
])
def test_forged_cursor_evidence_is_rejected_before_playback(project, cursor):
    _, _, store, paths = project
    value = store.append(paths).state.payload()
    value['cursor'] = cursor
    with pytest.raises(ValueError):
        module.parse_queue(value, store.project_id)


def test_unknown_external_position_stays_unknown_across_restart(project):
    _, _, store, paths = project
    saved = store.checkpoint(store.append(paths), index=0, frame=None, accuracy='external_player')
    assert store.review().state == saved.state and saved.state.frame is None


def test_cancellation_after_final_flush_preserves_previous_queue(project, monkeypatch):
    _, _, store, paths = project
    reviewed = store.append(paths)
    before = store.path.read_bytes()
    stop = [False]
    fsync = module.os.fsync
    def flushed(fd):
        fsync(fd)
        stop[0] = True
    monkeypatch.setattr(module.os, 'fsync', flushed)
    with pytest.raises(RuntimeError):
        store.checkpoint(reviewed, index=0, frame=200, accuracy='completed_buffers',
                         cancelled=lambda: stop[0])
    assert store.path.read_bytes() == before
    assert not tuple(store.audio.glob('.queue-*'))


def test_failed_replacement_preserves_prior_state_and_never_reports_success(project, monkeypatch):
    _, _, store, paths = project
    reviewed = store.append(paths)
    before = store.path.read_bytes()
    real_replace = module.replace_json_file
    def refuse(source, destination):
        if destination == store.path:
            raise OSError('Synthetic disk commit failure')
        return real_replace(source, destination)
    monkeypatch.setattr(module, 'replace_json_file', refuse)
    with pytest.raises(OSError, match='commit failure'):
        store.checkpoint(reviewed, index=0, frame=900, accuracy='completed_buffers')
    assert store.path.read_bytes() == before


def test_complete_backup_different_root_rebackup_and_inert_copies(project, tmp_path):
    app, controller, store, paths = project
    saved = store.checkpoint(store.append(paths), index=1, frame=6000,
                             accuracy='completed_buffers', controls=PlaybackControls(.75, -1, .8, .2))
    source = tuple(path.read_bytes() for path in store.paths(saved))
    digest = create_backup(app, tmp_path / 'backup')
    verify_backup(tmp_path / 'backup', digest)
    target = restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    restored = open_existing_library(target)
    other = module.PlaylistStore(restored.root, controller.project_id)
    recovered = other.review()
    assert recovered.state == saved.state
    assert tuple(path.read_bytes() for path in other.paths(recovered)) == source
    assert all(path.is_relative_to(restored.root) for path in other.paths(recovered))
    again = create_backup(restored, tmp_path / 'again')
    verify_backup(tmp_path / 'again', again)


def test_project_snapshot_includes_playlist_even_without_rendered_audio(project, tmp_path):
    _, controller, store, paths = project
    saved = store.append(paths)
    snapshot = tmp_path / 'audio.zip'
    backup_project(controller.project_path, snapshot, project_dir=store.audio,
                   include_cache=False, include_audio=False)
    result = restore_project(snapshot, tmp_path / 'restored-project')
    recovered = module.inspect_queue(result['project_dir'], controller.project_id)
    assert recovered == saved.state


def test_clear_keeps_original_owned_pcm_but_commits_empty_inert_queue(project):
    _, _, store, paths = project
    saved = store.append(paths)
    retained = store.paths(saved)
    cleared = store.checkpoint(saved, clear=True)
    assert cleared.state.items == () and cleared.state.index is None
    assert all(path.exists() for path in retained)


def test_cross_store_review_and_project_identity_do_not_grant_write_authority(project):
    app, controller, store, paths = project
    saved = store.append(paths)
    other = module.PlaylistStore(app.root, controller.project_id)
    with pytest.raises(ValueError, match='exact project'):
        other.checkpoint(saved)
    value = saved.state.payload()
    with pytest.raises(ValueError, match='another project'):
        module.parse_queue(value, 'f' * 32)


def test_changed_header_or_foreign_relative_name_never_becomes_an_asset(project):
    _, _, store, paths = project
    value = store.append(paths).state.payload()
    value['items'][0]['asset'] = '../foreign.wav'
    with pytest.raises(ValueError, match='PCM'):
        module.parse_queue(value, store.project_id)


def test_cancellation_before_copy_keeps_existing_queue_and_external_sources(project):
    _, _, store, paths = project
    saved = store.append([paths[0]])
    before = store.path.read_bytes()
    original = paths[1].read_bytes()
    with pytest.raises(RuntimeError):
        store.append([paths[1]], review=saved, cancelled=lambda: True)
    assert store.path.read_bytes() == before and paths[1].read_bytes() == original



def test_native_change_time_is_part_of_review_and_each_player_restart(project, monkeypatch):
    from carr_library import native_file_metadata
    from carr_library.audio import player as player_module
    _, _, store, paths = project
    change = [101]
    monkeypatch.setattr(native_file_metadata, 'stream_change_time', lambda stream: change[0])
    monkeypatch.setattr(module, 'stream_change_time', lambda stream: change[0])
    reviewed = store.append(paths)
    selected = store.paths(reviewed)[0]
    ordinary = module.file_identity(selected.stat())
    player = WavePlayer()
    class Device:
        def __init__(self):
            self.snapshot = PlaybackStatus()
            self.plays = []
            self.resumes = 0
        def stop(self):
            pass
        def status(self):
            return self.snapshot
        def play(self, path, **kwargs):
            self.plays.append((path, kwargs))
            self.snapshot = PlaybackStatus(state='playing', path=str(path),
                position_frames=0, sample_rate=8000, total_frames=8000,
                channels=1, position_accuracy='completed_buffers')
        def resume(self):
            self.resumes += 1
    player.native = Device()
    player.set_playlist(store.paths(reviewed))
    player.play_index(0)
    player.set_ab_loop(.1, .5)
    before = len(player.native.plays)
    change[0] = 102
    assert module.file_identity(selected.stat()) == ordinary
    for action in (
        lambda: store.paths(reviewed),
        lambda: store.checkpoint(reviewed),
        lambda: player.play_index(0),
        lambda: player.seek(.2),
        lambda: player.set_ab_loop(.2, .6),
        player.clear_ab_loop,
    ):
        with pytest.raises(ValueError, match='changed'):
            action()
    player.native.snapshot = replace(player.native.snapshot, state='paused')
    with pytest.raises(ValueError, match='changed'):
        player.resume()
    player.native.snapshot = replace(player.native.snapshot, state='completed')
    with pytest.raises(ValueError, match='changed'):
        player.poll()
    assert len(player.native.plays) == before and player.native.resumes == 0


@pytest.mark.parametrize('operation', ['paths', 'checkpoint', 'play'], ids=str)
def test_actual_same_size_rewrite_with_restored_mtime_revokes_cached_queue(project, operation):
    _, _, store, paths = project
    reviewed = store.append(paths)
    retained = store.paths(reviewed)
    player = WavePlayer()
    player.set_playlist(retained)
    before = retained[0].stat()
    with retained[0].open('r+b') as stream:
        stream.seek(-2, os.SEEK_END)
        stream.write(b'\x99\x01')
    os.utime(retained[0], ns=(before.st_atime_ns, before.st_mtime_ns))
    if operation == 'paths':
        action = lambda: store.paths(reviewed)
    elif operation == 'checkpoint':
        action = lambda: store.checkpoint(reviewed)
    else:
        action = lambda: player.play_index(0)
    with pytest.raises(ValueError, match='changed'):
        action()


def test_optional_queue_lookup_permission_is_not_an_empty_queue(project, monkeypatch):
    _, _, store, paths = project
    real_lstat = type(store.path).lstat
    def denied(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError('Synthetic denied queue lookup')
        return real_lstat(path, *args, **kwargs)
    monkeypatch.setattr(type(store.path), 'lstat', denied)
    with pytest.raises(PermissionError, match='denied'):
        module.inspect_queue(store.audio, store.project_id)


def test_optional_queue_lookup_under_regular_file_is_not_absence(tmp_path):
    parent = tmp_path / 'not-a-directory'
    parent.write_bytes(b'original')
    with pytest.raises((OSError, ValueError)):
        module.inspect_queue(parent, 'a' * 32)
    assert parent.read_bytes() == b'original'


def test_replacement_sync_failure_returns_the_exact_committed_receipt(project, monkeypatch):
    _, _, store, paths = project
    reviewed = store.append(paths)
    def fail(directory):
        raise OSError('Synthetic post-replace directory flush failure')
    monkeypatch.setattr(module, '_sync_directory', fail)
    with pytest.raises(module.QueueChangeCommittedError) as caught:
        store.checkpoint(reviewed, index=1, frame=3456, accuracy='completed_buffers')
    receipt = caught.value.review
    assert caught.value.durability_confirmed is False
    assert receipt.owner is store and receipt.state.frame == 3456
    assert module.sha256(store.path.read_bytes()).hexdigest() == receipt.digest
    assert module.parse_queue(json.loads(store.path.read_bytes()), store.project_id) == receipt.state
    assert store.paths(receipt)
    assert not tuple(store.audio.glob('.queue-*'))


def test_create_move_then_error_is_acknowledged_by_owned_inode_not_equal_bytes(project, monkeypatch):
    _, _, store, _ = project
    reviewed = store.review()
    publish = module.publish_noreplace
    def moved(source, destination, **kwargs):
        publish(source, destination, **kwargs)
        raise OSError('Synthetic publication acknowledgement lost')
    monkeypatch.setattr(module, 'publish_noreplace', moved)
    with pytest.raises(module.QueueChangeCommittedError) as caught:
        store.checkpoint(reviewed)
    assert caught.value.review.state == reviewed.state
    assert caught.value.review.digest == module.sha256(store.path.read_bytes()).hexdigest()


def test_create_only_equal_byte_competitor_is_not_our_committed_receipt(project, monkeypatch):
    _, _, store, _ = project
    reviewed = store.review()
    def competitor(source, destination, **kwargs):
        destination.write_bytes(source.read_bytes())
        raise FileExistsError('Synthetic competitor won publication')
    monkeypatch.setattr(module, 'publish_noreplace', competitor)
    with pytest.raises(FileExistsError, match='competitor') as caught:
        store.checkpoint(reviewed)
    assert not isinstance(caught.value, module.QueueChangeCommittedError)
    assert module.parse_queue(json.loads(store.path.read_bytes()), store.project_id) == reviewed.state


@pytest.mark.parametrize('operation', ['checkpoint', 'append'], ids=str)
def test_outer_guard_release_failure_retains_exact_committed_queue(project, monkeypatch, operation):
    from contextlib import contextmanager
    _, _, store, paths = project
    reviewed = store.append([paths[0]])
    original = store.managed.writer
    @contextmanager
    def writer(identity):
        with original(identity):
            yield
        raise OSError('Synthetic cleanup error after actual native guard release')
    monkeypatch.setattr(store.managed, 'writer', writer)
    with pytest.raises(module.QueueChangeCommittedError) as caught:
        if operation == 'checkpoint':
            store.checkpoint(reviewed, index=0, frame=1234, accuracy='driver_reported')
        else:
            store.append([paths[1]], review=reviewed)
    receipt = caught.value.review
    assert receipt.owner is store
    assert module.sha256(store.path.read_bytes()).hexdigest() == receipt.digest
    assert module.parse_queue(json.loads(store.path.read_bytes()), store.project_id) == receipt.state
    assert (receipt.state.frame == 1234 if operation == 'checkpoint' else len(receipt.state.items) == 2)
    # Native ownership was actually released despite the observer's error.
    with original(store.project_id):
        pass


def test_explicit_legacy_labels_hashes_and_historical_cursor_keep_distinct_evidence(project):
    _, _, store, paths = project
    labels = ['Історична назва ' + 'я' * 240, 'Second']
    hashes = [module.sha256(path.read_bytes()).hexdigest() for path in paths]
    retained = store.append(paths, labels=labels, expected_sha256s=hashes)
    assert [item.label for item in retained.state.items] == labels
    saved = store.checkpoint(retained, index=1, frame=4321, accuracy='historical_cursor')
    assert store.review().state == saved.state
    assert saved.state.accuracy == 'historical_cursor'
    assert saved.state.controls == PlaybackControls()
    forged = saved.state.payload()
    forged['cursor']['frame'] = None
    with pytest.raises(ValueError, match='evidence'):
        module.parse_queue(forged, store.project_id)


@pytest.mark.parametrize('argument', ['count', 'type', 'surrogate', 'hash', 'changed'], ids=str)
def test_import_bindings_fail_before_queue_publication(project, argument):
    _, _, store, paths = project
    options = {
        'count': {'labels': ['one']},
        'type': {'labels': [None, 'two']},
        'surrogate': {'labels': ['\ud800', 'two']},
        'hash': {'expected_sha256s': ['Z' * 64] * 2},
        'changed': {'expected_sha256s': ['0' * 64] * 2},
    }[argument]
    with pytest.raises(ValueError):
        store.append(paths, **options)
    assert not store.path.exists()



def test_staged_queue_copy_uses_the_explicit_current_library_resource_broker(project, monkeypatch):
    from contextlib import contextmanager
    from carr_library.operations import ResourceBroker
    app, controller, _, paths = project
    broker = ResourceBroker.for_app(app.root)
    reserve = broker.reserve
    seen = []
    @contextmanager
    def admitted(request, **kwargs):
        seen.append((request, kwargs['operation_id']))
        with reserve(request, **kwargs) as lease:
            yield lease
    monkeypatch.setattr(broker, 'reserve', admitted)
    def forbidden(*args, **kwargs):
        raise AssertionError('A second policy must not replace the supplied current broker')
    monkeypatch.setattr(ResourceBroker, 'for_app', forbidden)
    store = module.PlaylistStore(app.root, controller.project_id, resource_broker=broker)
    review = store.append(paths)
    assert len(review.state.items) == 2 and len(seen) == 1
    request, operation = seen[0]
    assert request.cpu_slots == 1
    assert request.disk_bytes == sum(path.stat().st_size for path in paths)
    assert operation == 'playback-queue:' + controller.project_id
    assert 'resource_broker' not in json.loads(store.path.read_bytes())



def test_owned_asset_sync_failure_requires_successful_directory_sync_on_retry(project, monkeypatch):
    from carr_library.audio import jobs
    _, _, store, paths = project
    assets = store.audio / 'assets'
    actual_sync = jobs._sync_directory
    attempts = []
    def refuse_assets(path):
        if path == assets:
            attempts.append(path)
            raise OSError('Synthetic retained asset directory sync failure')
        return actual_sync(path)
    # The initial publisher and the exact verified-reuse path must both fail
    # without acknowledging a queue while this directory cannot be synced.
    monkeypatch.setattr(jobs, '_sync_directory', refuse_assets)
    monkeypatch.setattr(module, '_sync_directory', refuse_assets)
    expected = module.sha256(paths[0].read_bytes()).hexdigest() + '.wav'
    for iteration in range(2):
        with pytest.raises(OSError, match='asset directory sync failure') as caught:
            store.append([paths[0]])
        assert not isinstance(caught.value, module.QueueChangeCommittedError)
        assert (assets / expected).read_bytes() == paths[0].read_bytes()
        assert not store.path.exists()
        assert len(attempts) == iteration + 1
    synced = []
    def restored_sync(path):
        actual_sync(path)
        if path == assets:
            synced.append(path)
    monkeypatch.setattr(jobs, '_sync_directory', restored_sync)
    monkeypatch.setattr(module, '_sync_directory', restored_sync)
    review = store.append([paths[0]])
    assert synced == [assets]
    assert store.review().state == review.state
    assert tuple(path.name for path in store.paths(review)) == (expected,)
