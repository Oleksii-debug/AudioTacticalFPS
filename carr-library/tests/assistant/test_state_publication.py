"""Actual assistant checkpoint snapshots and concurrent atomic publication.

These are real private files and real OS sharing/rename operations. Authored
records test the state envelope only; they do not stand in for tool execution.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading

import pytest

from carr_library.assistant import state as module


IDENTITY = '12' * 16


def _store(tmp_path):
    store = module.JobStore(tmp_path / 'Українська бібліотека' / 'assistant')
    store.save({'schema': 'carr-assistant-job/1', 'id': IDENTITY,
                'status': 'queued', 'payload': 'original snapshot'})
    return store


def _observe_reads(monkeypatch, operation):
    original = module._open_json_file

    @contextmanager
    def opened(path):
        with original(path) as stream:
            class Reader:
                def fileno(self):
                    return stream.fileno()

                def read(self, size=-1):
                    return operation(Path(path), stream, size)

            yield Reader()

    monkeypatch.setattr(module, '_open_json_file', opened)


@pytest.mark.parametrize('kind', ['job', 'control'])
def test_actual_open_snapshot_allows_worker_checkpoint_replacement(tmp_path, monkeypatch, kind):
    store = _store(tmp_path)
    if kind == 'control':
        store.control(IDENTITY, 'run')
        selected = store.path(IDENTITY).with_suffix('.control.json')
        before = 'run'
        read = lambda: store.control(IDENTITY)
        save = lambda: store.control(IDENTITY, 'pause')
        expected = 'pause'
    else:
        selected = store.path(IDENTITY)
        before = store.load(IDENTITY)
        read = lambda: store.load(IDENTITY)
        value = dict(before, status='complete', payload='new completed snapshot')
        save = lambda: store.save(value)
        expected = value

    entered, release = threading.Event(), threading.Event()
    held = []
    old_identity = module.regular(selected)

    def reading(path, stream, size):
        assert path == selected
        if not held:
            held.append(os.fstat(stream.fileno()))
            entered.set()
            assert release.wait(5), 'The actual opened snapshot was not released.'
        return stream.read(size)

    _observe_reads(monkeypatch, reading)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read)
        try:
            assert entered.wait(5)
            # The genuine reader remains open throughout this real publication.
            # On Windows this requires both delete sharing and the supported
            # native replacement semantics; no reader is closed or retried here.
            save()
            current = module.regular(selected)
            assert (current.st_dev, current.st_ino) != (old_identity.st_dev, old_identity.st_ino)
            assert not future.done()
        finally:
            release.set()
        assert future.result(timeout=5) == before
    assert read() == expected
    assert held and not tuple(store.directory.glob('.*.tmp'))


def test_default_64mib_budget_uses_bounded_actual_read_requests(tmp_path, monkeypatch):
    path = tmp_path / 'checkpoint.json'
    expected = {'payload': 'a' * (200 * 1024)}
    module.atomic_json(path, expected)
    requested = []

    def reading(_path, stream, size):
        requested.append(size)
        assert 0 < size <= 64 * 1024
        return stream.read(size)

    _observe_reads(monkeypatch, reading)
    assert module.MAX_CHECKPOINT_BYTES == 64 * 1024 * 1024
    assert module.read_json(path) == expected
    assert len(requested) >= 4 and max(requested) == 64 * 1024


@pytest.mark.parametrize('exceeds_budget', [False, True])
def test_actual_same_inode_growth_keeps_declared_budget(tmp_path, monkeypatch, exceeds_budget):
    path = tmp_path / 'checkpoint.json'
    module.atomic_json(path, {'payload': ''})
    before = module.regular(path)
    maximum = 128
    expected = {'payload': 'x' * (256 if exceeds_budget else 64)}
    replacement = (json.dumps(expected) + '\n').encode('utf-8')
    changed, requested = [], []

    def reading(selected, stream, size):
        requested.append(size)
        assert 0 < size <= maximum + 1
        if not changed:
            changed.append(True)
            selected.write_bytes(replacement)
            after = module.regular(selected)
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        return stream.read(size)

    _observe_reads(monkeypatch, reading)
    if exceeds_budget:
        with pytest.raises(ValueError, match='exceeds its budget'):
            module.read_json(path, maximum=maximum)
    else:
        assert module.read_json(path, maximum=maximum) == expected
    assert changed == [True]
    assert path.read_bytes() == replacement


@pytest.mark.parametrize('continuous', [False, True])
def test_actual_replacement_between_stat_and_open_has_finite_retry(tmp_path, monkeypatch, continuous):
    path = tmp_path / 'checkpoint.json'
    module.atomic_json(path, {'generation': 0})
    original = module._open_json_file
    opened, consumed = [], []

    @contextmanager
    def replace_before_open(selected):
        if continuous or not opened:
            module.atomic_json(selected, {'generation': len(opened) + 1})
        with original(selected) as stream:
            opened.append(os.fstat(stream.fileno()))

            class Reader:
                def fileno(self):
                    return stream.fileno()

                def read(self, size=-1):
                    consumed.append(size)
                    return stream.read(size)

            yield Reader()

    monkeypatch.setattr(module, '_open_json_file', replace_before_open)
    if continuous:
        with pytest.raises(ValueError, match='changed repeatedly while opening'):
            module.read_json(path)
        assert len(opened) == 8 and not consumed
    else:
        assert module.read_json(path) == {'generation': 1}
        assert len(opened) == 2 and consumed


def test_substituted_multiple_link_descriptor_is_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / 'checkpoint.json'
    foreign = tmp_path / 'foreign.json'
    module.atomic_json(path, {'selected': True})
    module.atomic_json(foreign, {'foreign': True})
    original = module._open_json_file
    held = []

    @contextmanager
    def linked_before_open(selected):
        selected.unlink()
        os.link(foreign, selected)
        with original(selected) as stream:
            held.append(os.fstat(stream.fileno()))
            assert held[-1].st_nlink == 2

            class Reader:
                def fileno(self):
                    return stream.fileno()

                def read(self, size=-1):
                    raise AssertionError('An inadmissible linked descriptor was read.')

            yield Reader()

    monkeypatch.setattr(module, '_open_json_file', linked_before_open)
    with pytest.raises(ValueError, match='private regular files'):
        module.read_json(path)
    assert len(held) == 1
    assert json.loads(foreign.read_bytes()) == {'foreign': True}


@pytest.mark.parametrize('maximum', [0, -1, True, 1.5, 64 * 1024 * 1024 + 1])
def test_invalid_limit_is_refused_before_opening(tmp_path, monkeypatch, maximum):
    path = tmp_path / 'checkpoint.json'
    module.atomic_json(path, {'value': 1})

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Invalid limits cannot admit a descriptor.')

    monkeypatch.setattr(module, '_open_json_file', forbidden)
    with pytest.raises(ValueError, match='byte budget'):
        module.read_json(path, maximum=maximum)


def test_initial_oversize_and_empty_file_are_refused_before_opening(tmp_path, monkeypatch):
    path = tmp_path / 'checkpoint.json'
    module.atomic_json(path, {'value': 'more than eight bytes'})

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Invalid initial size cannot admit a descriptor.')

    monkeypatch.setattr(module, '_open_json_file', forbidden)
    with pytest.raises(ValueError, match='checkpoint size'):
        module.read_json(path, maximum=8)
    path.write_bytes(b'')
    with pytest.raises(ValueError, match='checkpoint size'):
        module.read_json(path)


def test_unrelated_publication_failure_preserves_old_checkpoint_and_cleans_stage(tmp_path, monkeypatch):
    store = _store(tmp_path)
    path = store.path(IDENTITY)
    before = path.read_bytes()

    def denied(temporary, destination):
        assert Path(temporary).is_file() and Path(destination) == path
        assert path.read_bytes() == before
        raise PermissionError('Authored unrelated destination denial.')

    monkeypatch.setattr(module, '_replace_state', denied)
    with pytest.raises(PermissionError, match='unrelated destination denial'):
        store.save({'schema': 'carr-assistant-job/1', 'id': IDENTITY, 'status': 'complete'})
    assert path.read_bytes() == before
    assert not tuple(store.directory.glob('.*.tmp'))
