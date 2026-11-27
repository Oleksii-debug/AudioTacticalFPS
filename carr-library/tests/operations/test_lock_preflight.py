"""Real native owner release during unowned-path validation, without PID reaping."""
import os
from pathlib import Path

import pytest

import carr_library.durable_state as state
import carr_library.operations._locks as locks


@pytest.mark.parametrize('stage', [
    'constructor-local-path', 'constructor-regular',
    'acquire-local-path', 'acquire-regular',
])
def test_normal_owner_release_during_preflight_allows_new_native_acquisition(tmp_path, monkeypatch, stage):
    path = tmp_path / '.admission.lock'
    owner = locks.NativeLock(path)
    assert owner.acquire()
    contender = locks.NativeLock(path) if stage.startswith('acquire') else None
    module = state if stage.endswith('local-path') else locks
    original, released = module.regular, []
    def release_before_stat(value, **kwargs):
        if Path(value) == path and not released:
            released.append(True)
            owner.release()
            assert not owner.held and not path.exists()
        return original(value, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, 'regular', release_before_stat)
            contender = locks.NativeLock(path) if contender is None else contender
            assert contender.acquire()
        assert released and contender.held
        assert locks.NativeLock(path).acquire() is False
    finally:
        if contender is not None:
            contender.release()
        owner.release()
    assert not path.exists()


@pytest.mark.parametrize('stage', ['constructor', 'acquire'])
def test_disappeared_unowned_path_is_revalidated_against_a_replacement_link(tmp_path, monkeypatch, stage):
    path, sentinel = tmp_path / '.admission.lock', tmp_path / 'original.txt'
    sentinel.write_bytes(b'Original bytes must not become a lock file.')
    owner = locks.NativeLock(path)
    assert owner.acquire()
    contender = locks.NativeLock(path) if stage == 'acquire' else None
    original, replaced = state.regular, []
    def released_then_replaced(value, **kwargs):
        if Path(value) == path and not replaced:
            replaced.append(True)
            owner.release()
            try:
                original(value, **kwargs)
            except FileNotFoundError:
                # A different regular-file link appears after the legitimate
                # disappearance. Catching absence must not authorize this link.
                os.link(sentinel, path)
                raise
            raise AssertionError('Native owner release did not remove its file.')
        return original(value, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(state, 'regular', released_then_replaced)
            with pytest.raises(ValueError, match='without links'):
                contender = locks.NativeLock(path) if contender is None else contender
                contender.acquire()
        assert replaced and sentinel.read_bytes() == b'Original bytes must not become a lock file.'
        assert contender is None or not contender.held
    finally:
        if contender is not None:
            contender.release()
        owner.release()


@pytest.mark.parametrize('stage', ['constructor', 'acquire'])
def test_preflight_does_not_hide_a_disappearing_parent_directory(tmp_path, monkeypatch, stage):
    directory = tmp_path / 'coordinator'
    directory.mkdir()
    path = directory / '.admission.lock'
    owner = locks.NativeLock(path)
    assert owner.acquire()
    contender = locks.NativeLock(path) if stage == 'acquire' else None
    original, removed = state.regular, []
    def remove_parent_before_stat(value, **kwargs):
        if Path(value) == path and not removed:
            removed.append(True)
            owner.release()
            directory.rmdir()
        return original(value, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(state, 'regular', remove_parent_before_stat)
            with pytest.raises(FileNotFoundError):
                contender = locks.NativeLock(path) if contender is None else contender
                contender.acquire()
        assert removed and not directory.exists()
    finally:
        if contender is not None:
            contender.release()
        owner.release()


def test_a_live_native_owner_is_never_treated_as_a_disappeared_file(tmp_path):
    path = tmp_path / '.admission.lock'
    owner, contender = locks.NativeLock(path), locks.NativeLock(path)
    try:
        assert owner.acquire()
        for _ in range(3):
            assert contender.acquire() is False
            assert owner.held and path.exists()
        owner.release()
        assert contender.acquire()
        assert contender.held
    finally:
        contender.release()
        owner.release()
