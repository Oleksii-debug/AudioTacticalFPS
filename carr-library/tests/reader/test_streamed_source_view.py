"""Actual large original viewing through the same selected-source authority."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from collections import deque
from threading import get_ident
from time import monotonic, sleep
import os
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.streamed_text import file_identity, file_digest, MAX_BYTES, TextProfile
from carr_library.reader import source_file


def sha256_file(path):
    return file_digest(path, maximum_bytes=MAX_BYTES)


def original(tmp_path, *, blocks=3):
    path = tmp_path / 'owned.txt'
    with path.open('wb') as output:
        for _ in range(blocks):
            output.write(b'Original source.\n' * 65536)
    return source_file._FileOriginal(path, sha256_file(path), 'text/plain',
        path.stat().st_size, TextProfile(), file_identity(path.stat()))


def test_actual_large_original_view_streams_retries_preserves_edits_and_rechecks_authority(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large original viewing')
    selected = tmp_path / 'Large.txt'
    block = ('Synthetic original viewing record. Україна.\r\n' * 1024).encode('utf-8')
    with selected.open('wb') as output:
        output.write(b'Viewingneedle original source\r\n')
        while output.tell() <= 64 * 1024**2:
            output.write(block)
    assert selected.stat().st_size > 64 * 1024**2
    result = app.import_file(workspace, selected, source_profile=TextProfile(65 * 1024**2), timeout=180)
    assert result['state'] == 'complete'
    hit = app.search_page(workspace, 'Viewingneedle original source', limit=1).results[0]
    retained = app.assets_dir / (result['asset'] + '.txt')
    views = app.root / source_file.VIEW_DIRECTORY

    # A completed move whose directory flush fails must be rehashed and
    # resynced on retry before a source-view capability is returned.
    flush = source_file._sync_directory
    calls, fail_sync = [], [True]
    def checked_flush(directory):
        if Path(directory) == views:
            calls.append(directory)
            if fail_sync[0]:
                raise OSError('Injected source-view directory flush failure')
        return flush(directory)
    monkeypatch.setattr(source_file, '_sync_directory', checked_flush)
    with pytest.raises(OSError, match='Injected'):
        source_file.prepare_source_file(app, workspace, hit.ref, hit.revision)
    first_path = views / (result['asset'] + '.txt')
    assert first_path.exists() and sha256_file(first_path) == result['asset']
    with pytest.raises(OSError, match='Injected'):
        source_file.prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert len(calls) == 2
    fail_sync[0] = False
    prepared = source_file.prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert prepared.path == first_path and len(calls) == 3
    assert not prepared.path.samefile(retained)
    assert prepared.path.stat().st_nlink == retained.stat().st_nlink == 1
    assert sha256_file(prepared.path) == sha256_file(retained) == result['asset']
    from carr_library.operations import ResourceBroker
    from carr_library.operations.models import ResourceUnavailable
    broker = ResourceBroker.for_app(app.root)
    assert broker.snapshot()['leases'] == []
    opened = []
    def admitted_open(value):
        active = broker.snapshot()['leases']
        assert len(active) == 1 and active[0]['operation_id'].startswith('document-view:')
        assert active[0]['request']['memory_bytes'] == 16 * 1024**2
        assert active[0]['request']['disk_bytes'] == 0
        opened.append(value)
        return True
    source_file.launch_source_file(app, prepared, admitted_open)
    assert opened == [str(first_path)] and broker.snapshot()['leases'] == []

    # Keep a viewer's edit and provide a separately verified fresh copy.
    with first_path.open('r+b') as output:
        output.seek(-1, os.SEEK_END)
        output.write(b'!')
    edited = sha256_file(first_path)
    fresh = source_file.prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert fresh.path != first_path and sha256_file(first_path) == edited
    assert sha256_file(fresh.path) == result['asset']
    before = {path.name for path in views.iterdir()}
    def stopped():
        return any(path.stat().st_size >= 2 * 1024**2
                   for path in views.glob('.view-stage-*.txt'))
    with pytest.raises(InterruptedError):
        source_file.prepare_source_file(app, workspace, hit.ref, hit.revision, cancelled=stopped)
    assert {path.name for path in views.iterdir()} == before
    assert sha256_file(selected) == sha256_file(retained) == result['asset']
    # Refusal occurs before original hashing or copying when the saved policy
    # cannot admit the declared bounded working set.
    policy = broker.limits
    broker.configure(replace(policy, memory_bytes=16 * 1024**2 - 1))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(source_file, '_original',
                          lambda *a, **k: pytest.fail('Original read before resource admission.'))
            with pytest.raises(ResourceUnavailable):
                source_file.prepare_source_file(app, workspace, hit.ref, hit.revision)
    finally:
        broker.configure(policy)
    assert broker.snapshot()['leases'] == []
    app.archive_workspace(workspace)
    with pytest.raises(ValueError):
        source_file.launch_source_file(app, fresh, lambda value: opened.append(value) or True)
    assert opened == [str(first_path)]


def test_streamed_view_cancel_after_actual_copy_keeps_no_partial_path(tmp_path):
    source = original(tmp_path)
    views = tmp_path / 'views'
    views.mkdir()
    target = views / 'copy.txt'
    observed = []
    def cancelled():
        sizes = [path.stat().st_size for path in views.glob('.view-stage-*.txt')]
        if sizes and max(sizes) >= 2 * 1024**2:
            observed.append(max(sizes))
            return True
        return False
    with pytest.raises(InterruptedError):
        source_file._prepare_streamed_view(views, target, source, cancelled)
    assert observed and not list(views.iterdir())
    assert sha256_file(source.path) == source.digest


def test_streamed_view_never_replaces_an_existing_destination(tmp_path):
    source = original(tmp_path, blocks=1)
    views = tmp_path / 'views'
    views.mkdir()
    target = views / 'copy.txt'
    target.write_bytes(b'User-owned viewing file.')
    with pytest.raises(FileExistsError):
        source_file._prepare_streamed_view(views, target, source, lambda: False)
    assert target.read_bytes() == b'User-owned viewing file.'
    assert sorted(path.name for path in views.iterdir()) == ['copy.txt']


def test_streamed_view_refuses_a_source_changed_since_selected_descriptor(tmp_path):
    source = original(tmp_path, blocks=1)
    views = tmp_path / 'views'
    views.mkdir()
    with source.path.open('r+b') as output:
        output.write(b'Altered')
    with pytest.raises((ValueError, RuntimeError)):
        source_file._prepare_streamed_view(views, views / 'copy.txt', source, lambda: False)
    assert not list(views.iterdir())


def test_streamed_view_full_hash_rejects_change_outside_initial_quote(tmp_path):
    source = original(tmp_path, blocks=1)
    views = tmp_path / 'views'
    views.mkdir()
    target = views / 'copy.txt'
    source_file._prepare_streamed_view(views, target, source, lambda: False)
    stamp = target.stat()
    with target.open('r+b') as output:
        output.seek(-1, os.SEEK_END)
        output.write(b'!')
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert not source_file._view_matches(views, target, source)


def test_streamed_view_rejects_hardlink_alias_before_acknowledging_copy(tmp_path):
    source = original(tmp_path, blocks=1)
    views = tmp_path / 'views'
    views.mkdir()
    target = views / 'copy.txt'
    os.link(source.path, target)
    with pytest.raises(ValueError, match='unlinked'):
        source_file._view_matches(views, target, source)


def test_legacy_small_original_keeps_existing_byte_snapshot_and_viewer_behavior(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Legacy source')
    imported = app.add_text(workspace, 'Small source', 'Small original source.')
    hit = app.search(workspace, 'Small original')[0]
    view = source_file.prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert view.path.read_bytes() == b'Small original source.'
    opened = []
    source_file.launch_source_file(app, view, lambda value: opened.append(value) or True)
    assert opened == [str(view.path)]
    assert view.identity.revision == imported['id']


def test_actual_byte_validation_stays_on_worker_and_readers_survive_gui_handoff(tmp_path, monkeypatch):
    from carr_library.ui.source_launch import call_on_ui
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Thread boundary')
    app.add_text(workspace, 'Source', 'An immutable original source.')
    hit = app.search(workspace, 'immutable')[0]
    source = source_file.prepare_source_file(app, workspace, hit.ref, hit.revision)
    queue, active, opened = deque(), [], []
    ui_thread = get_ident()
    original = source_file._original
    hold = source_file._held_verified_file
    def checked_original(*args, **kwargs):
        assert get_ident() != ui_thread
        return original(*args, **kwargs)
    @contextmanager
    def checked_reader(*args, **kwargs):
        assert get_ident() != ui_thread
        active.append(args[0])
        try:
            with hold(*args, **kwargs) as check:
                yield check
        finally:
            assert get_ident() != ui_thread
            active.pop()
    monkeypatch.setattr(source_file, '_original', checked_original)
    monkeypatch.setattr(source_file, '_held_verified_file', checked_reader)
    def open_native(path):
        assert get_ident() == ui_thread and len(active) == 2
        opened.append(path)
        return True
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(source_file.launch_source_file, app, source, open_native,
            native_dispatch=lambda action: call_on_ui(queue.append, action))
        deadline = monotonic() + 5
        while not future.done():
            if monotonic() >= deadline:
                raise AssertionError('Source validation did not settle across its event-thread handoff.')
            if queue:
                queue.popleft()()
            else:
                sleep(.001)
        future.result()
    assert opened == [str(source.path)] and active == []
