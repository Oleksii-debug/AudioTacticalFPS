"""Actual owned children with a precise private-directory disappearance race."""
from pathlib import Path
import os
import sys

import pytest

from carr_library.audio import process as implementation
from carr_library.audio.models import AudioBudget, AudioError


def command():
    executable = Path(getattr(sys, '_base_executable', sys.executable)
                      if os.name == 'nt' else sys.executable).resolve()
    return [str(executable), '-I', '-S', '-B', '-c',
            "import sys,time; time.sleep(.1); sys.stdout.buffer.write(b'completed\\n'); sys.stdout.buffer.flush()"]


@pytest.mark.parametrize('replacement', [None, 'directory', 'file'])
def test_real_scandir_disappearance_is_accepted_only_while_the_exact_path_is_absent(
        tmp_path, monkeypatch, replacement):
    scratch = tmp_path / 'native-scratch'
    scratch.mkdir()
    nested = scratch / 'transient-child'
    nested.mkdir()
    ordinary = scratch / 'kept.bin'
    ordinary.write_bytes(b'owned-output')
    actual, seen = implementation.os.scandir, []
    def disappearing(path):
        if not isinstance(path, (str, Path)) or Path(path) != nested or seen:
            return actual(path)
        seen.append(nested.lstat())
        nested.rmdir()
        try:
            return actual(path)  # A real OS ENOENT, after the production lstat.
        except FileNotFoundError:
            if replacement == 'directory':
                nested.mkdir()
            elif replacement == 'file':
                nested.write_bytes(b'replacement is not an absent scratch directory')
            raise
    monkeypatch.setattr(implementation.os, 'scandir', disappearing)
    options = dict(cwd=tmp_path, watch=(scratch,), owned_tree=True,
        budget=AudioBudget(timeout=5, max_output_bytes=4096, reserve_disk_bytes=0))
    if replacement is None:
        result = implementation.run_process(command(), **options)
        assert result.returncode == 0 and result.stdout == b'completed\n'
    else:
        with pytest.raises(AudioError, match='staging directory changed'):
            implementation.run_process(command(), **options)
        assert nested.exists()
    assert len(seen) == 1 and ordinary.read_bytes() == b'owned-output'


def test_permission_error_is_never_reclassified_as_a_disappeared_directory(tmp_path, monkeypatch):
    scratch = tmp_path / 'native-scratch'
    scratch.mkdir()
    actual = implementation.os.scandir
    denied = PermissionError('Controlled directory-read denial at the native syscall boundary.')
    def unreadable(path):
        if isinstance(path, (str, Path)) and Path(path) == scratch:
            raise denied
        return actual(path)
    monkeypatch.setattr(implementation.os, 'scandir', unreadable)
    with pytest.raises(PermissionError) as caught:
        implementation.run_process(command(), cwd=tmp_path, watch=(scratch,), owned_tree=True,
            budget=AudioBudget(timeout=5, reserve_disk_bytes=0))
    assert caught.value is denied and scratch.is_dir()


def test_disappearing_child_does_not_waive_other_actual_output_bytes(tmp_path, monkeypatch):
    scratch = tmp_path / 'native-scratch'
    scratch.mkdir()
    gone = scratch / 'temporary'
    gone.mkdir()
    retained = scratch / 'large.bin'
    retained.write_bytes(b'x' * 8192)
    actual = implementation.os.scandir
    seen = []
    def disappearing(path):
        if isinstance(path, (str, Path)) and Path(path) == gone and not seen:
            seen.append(True)
            gone.rmdir()
        return actual(path)
    monkeypatch.setattr(implementation.os, 'scandir', disappearing)
    with pytest.raises(AudioError, match='output disk budget'):
        implementation.run_process(command(), cwd=tmp_path, watch=(retained, gone), owned_tree=True,
            budget=AudioBudget(timeout=5, max_output_bytes=4096, reserve_disk_bytes=0))
    assert seen == [True] and retained.stat().st_size == 8192



@pytest.mark.parametrize('replacement', [None, 'directory', 'file'])
def test_windows_error_five_requires_actual_current_absence_at_the_same_path(
        tmp_path, monkeypatch, replacement):
    """The child is real; only the observed Windows syscall error is injected."""
    scratch = tmp_path / 'native-scratch'
    scratch.mkdir()
    transient = scratch / 'transient-child'
    transient.mkdir()
    retained = scratch / 'kept.bin'
    retained.write_bytes(b'actual other output remains bounded')
    actual, seen = implementation.os.scandir, []
    denied = PermissionError('Observed Windows access-denied directory-read boundary.')
    denied.winerror = 5

    def deleting(path):
        if not isinstance(path, (str, Path)) or Path(path) != transient or seen:
            return actual(path)
        seen.append(transient.lstat())
        transient.rmdir()
        if replacement == 'directory':
            transient.mkdir()
        elif replacement == 'file':
            transient.write_bytes(b'new object is never the missing old directory')
        raise denied

    monkeypatch.setattr(implementation.os, 'scandir', deleting)
    options = dict(cwd=tmp_path, watch=(scratch,), owned_tree=True,
        budget=AudioBudget(timeout=5, max_output_bytes=4096, reserve_disk_bytes=0))
    if os.name == 'nt' and replacement is None:
        result = implementation.run_process(command(), **options)
        assert result.returncode == 0 and result.stdout == b'completed\n'
        assert not transient.exists()
    else:
        with pytest.raises(PermissionError) as caught:
            implementation.run_process(command(), **options)
        assert caught.value is denied
        assert transient.exists() is (replacement is not None)
    assert len(seen) == 1
    assert retained.read_bytes() == b'actual other output remains bounded'


@pytest.mark.parametrize('recheck_error', [PermissionError('recheck denied'), OSError('recheck unknown')])
def test_error_five_with_an_unconfirmed_recheck_keeps_the_original_failure(
        tmp_path, monkeypatch, recheck_error):
    scratch = tmp_path / 'native-scratch'
    scratch.mkdir()
    transient = scratch / 'transient-child'
    transient.mkdir()
    actual_scan, actual_stat = implementation.os.scandir, Path.lstat
    denied = PermissionError('Original directory-read denial.')
    denied.winerror = 5
    seen, rechecks = [], []

    def deleting(path):
        if isinstance(path, (str, Path)) and Path(path) == transient and not seen:
            seen.append(actual_stat(transient))
            transient.rmdir()
            raise denied
        return actual_scan(path)

    def observed_stat(path, *args, **kwargs):
        if path == transient and seen:
            rechecks.append(path)
            raise recheck_error
        return actual_stat(path, *args, **kwargs)

    monkeypatch.setattr(implementation.os, 'scandir', deleting)
    monkeypatch.setattr(Path, 'lstat', observed_stat)
    with pytest.raises(PermissionError) as caught:
        implementation.run_process(command(), cwd=tmp_path, watch=(scratch,), owned_tree=True,
            budget=AudioBudget(timeout=5, reserve_disk_bytes=0))
    assert caught.value is denied
    assert len(seen) == 1
    assert len(rechecks) == (1 if os.name == 'nt' else 0)


@pytest.mark.parametrize('winerror', [None, 32])
def test_other_permission_errors_stay_failures_even_after_actual_directory_removal(
        tmp_path, monkeypatch, winerror):
    scratch = tmp_path / 'native-scratch'
    scratch.mkdir()
    actual, seen = implementation.os.scandir, []
    denied = PermissionError('Unqualified directory-read denial.')
    if winerror is not None:
        denied.winerror = winerror

    def deleting(path):
        if isinstance(path, (str, Path)) and Path(path) == scratch and not seen:
            seen.append(scratch.lstat())
            scratch.rmdir()
            raise denied
        return actual(path)

    monkeypatch.setattr(implementation.os, 'scandir', deleting)
    with pytest.raises(PermissionError) as caught:
        implementation.run_process(command(), cwd=tmp_path, watch=(scratch,), owned_tree=True,
            budget=AudioBudget(timeout=5, reserve_disk_bytes=0))
    assert caught.value is denied and len(seen) == 1 and not scratch.exists()


def test_confirmed_windows_disappearance_still_counts_every_surviving_output_byte(
        tmp_path, monkeypatch):
    scratch = tmp_path / 'native-scratch'
    scratch.mkdir()
    transient = scratch / 'temporary'
    transient.mkdir()
    retained = scratch / 'large.bin'
    retained.write_bytes(b'x' * 8192)
    actual, seen = implementation.os.scandir, []
    denied = PermissionError('Observed Windows scratch-removal boundary.')
    denied.winerror = 5

    def deleting(path):
        if isinstance(path, (str, Path)) and Path(path) == transient and not seen:
            seen.append(transient.lstat())
            transient.rmdir()
            raise denied
        return actual(path)

    monkeypatch.setattr(implementation.os, 'scandir', deleting)
    expected = AudioError if os.name == 'nt' else PermissionError
    with pytest.raises(expected) as caught:
        implementation.run_process(command(), cwd=tmp_path,
            watch=(retained, transient), owned_tree=True,
            budget=AudioBudget(timeout=5, max_output_bytes=4096, reserve_disk_bytes=0))
    if os.name == 'nt':
        assert 'output disk budget' in str(caught.value)
    else:
        assert caught.value is denied
    assert len(seen) == 1 and retained.stat().st_size == 8192
