"""Real transient filesystem races during the current owned Office census."""
import os
from pathlib import Path
import stat
import time

import pytest

from carr_library.conversion import office
from carr_library.conversion.models import ConversionError


@pytest.mark.parametrize('kind', ['directory', 'file-parent'])
def test_native_removed_profile_entry_does_not_abort_the_remaining_stage_census(tmp_path, monkeypatch, kind):
    stage = (tmp_path / 'stage').resolve()
    stage.mkdir()
    transient = stage / 'native-temporary'
    transient.mkdir()
    retained = stage / 'retained.bin'
    retained.write_bytes(b'RETAINED ORIGINAL')
    events = []
    if kind == 'directory':
        original = office._stage_info
        def checked(root, path, identity, **options):
            if Path(path) == transient and not events:
                transient.rmdir()
                events.append('removed-before-directory-guard')
            return original(root, path, identity, **options)
        monkeypatch.setattr(office, '_stage_info', checked)
    else:
        fleeting = transient / 'native.bin'
        fleeting.write_bytes(b'TEMPORARY')
        original = office._stage_info
        def regular(root, path, identity, **options):
            if Path(path) == fleeting and not events:
                fleeting.unlink()
                transient.rmdir()
                events.append('removed-before-file-parent-guard')
            return original(root, path, identity, **options)
        monkeypatch.setattr(office, '_stage_info', regular)
    office._stage_usage(stage, 1024, time.monotonic() + 5)
    assert len(events) == 1 and not transient.exists()
    assert retained.read_bytes() == b'RETAINED ORIGINAL'
    # Continued traversal still enforces the existing complete byte budget.
    with pytest.raises(ConversionError, match='byte budget'):
        office._stage_usage(stage, len(retained.read_bytes()) - 1, time.monotonic() + 5)


@pytest.mark.parametrize('message', ['The project directory does not exist.', 'Unsafe selected directory.'])
def test_valueerror_for_a_still_existing_stage_entry_is_never_treated_as_disappearance(tmp_path, monkeypatch, message):
    stage = (tmp_path / 'stage').resolve()
    stage.mkdir()
    child = stage / 'still-present'
    child.mkdir()
    original = office._stage_info
    def checked(root, path, identity, **options):
        if Path(path) == child:
            raise ValueError(message)
        return original(root, path, identity, **options)
    monkeypatch.setattr(office, '_stage_info', checked)
    with pytest.raises(ValueError) as raised:
        office._stage_usage(stage, 1024, time.monotonic() + 5)
    assert str(raised.value) == message and child.is_dir()


def test_permission_failure_during_missing_entry_confirmation_remains_a_failure(tmp_path, monkeypatch):
    stage = (tmp_path / 'stage').resolve()
    stage.mkdir()
    child = stage / 'unreadable'
    child.mkdir()
    original_checked, original_lstat = office._stage_info, Path.lstat
    armed = []
    def checked(root, path, identity, **options):
        if Path(path) == child:
            armed.append(True)
            raise ValueError('The project directory does not exist.')
        return original_checked(root, path, identity, **options)
    def denied(path, *args, **options):
        if armed and path == child:
            raise PermissionError('Synthetic native metadata access refusal')
        return original_lstat(path, *args, **options)
    monkeypatch.setattr(office, '_stage_info', checked)
    monkeypatch.setattr(Path, 'lstat', denied)
    with pytest.raises(PermissionError, match='metadata access refusal'):
        office._stage_usage(stage, 1024, time.monotonic() + 5)
    assert armed == [True]


@pytest.mark.parametrize('change', ['removed', 'replaced'])
def test_missing_native_entry_cannot_hide_lost_or_replaced_private_stage_root(tmp_path, monkeypatch, change):
    stage = (tmp_path / 'stage').resolve()
    stage.mkdir()
    child = stage / 'native-temporary'
    child.mkdir()
    retained = tmp_path / 'retained-original-root'
    original = office._stage_info
    events = []
    def checked(root, path, identity, **options):
        if Path(path) == child and not events:
            child.rmdir()
            if change == 'removed':
                stage.rmdir()
            else:
                stage.rename(retained)
                stage.mkdir()
            events.append(change)
        return original(root, path, identity, **options)
    monkeypatch.setattr(office, '_stage_info', checked)
    with pytest.raises((ValueError, ConversionError)) as raised:
        office._stage_usage(stage, 1024, time.monotonic() + 5)
    assert events == [change]
    if change == 'removed':
        assert 'staging root disappeared' in str(raised.value)
        assert not stage.exists()
    else:
        assert 'staging was replaced' in str(raised.value)
        assert retained.is_dir() and stage.is_dir()
        assert list(retained.iterdir()) == list(stage.iterdir()) == []


@pytest.mark.parametrize('violation', ['bytes', 'hardlink', 'depth'])
def test_disappearance_after_observed_policy_violation_cannot_make_stage_valid(tmp_path, monkeypatch, violation):
    stage = (tmp_path / 'stage').resolve()
    stage.mkdir()
    foreign = tmp_path / 'foreign.bin'
    foreign.write_bytes(b'RETAINED FOREIGN CONTENT')
    offending = stage / 'native-transient'
    maximum = 1024
    if violation == 'bytes':
        offending.write_bytes(b'0123456789')
        maximum = 9
        expected = 'byte budget'
    elif violation == 'hardlink':
        os.link(foreign, offending)
        expected = 'aliased file'
    else:
        offending = stage
        for number in range(17):
            offending = offending / ('level-' + str(number))
            offending.mkdir()
        expected = 'depth budget'
    observed, classified = [], []
    original = office._missing_transient_entry
    class DisappearedAfterMeasurement(ConversionError):
        def __init__(self, message):
            assert expected in message
            observed.append(message)
            if violation == 'depth':
                offending.rmdir()
            else:
                offending.unlink()
            super().__init__(message)
    def classify(path):
        classified.append(Path(path))
        return original(path)
    monkeypatch.setattr(office, 'ConversionError', DisappearedAfterMeasurement)
    monkeypatch.setattr(office, '_missing_transient_entry', classify)
    with pytest.raises(ConversionError, match=expected):
        office._stage_usage(stage, maximum, time.monotonic() + 5)
    assert len(observed) == 1 and classified == []
    assert not offending.exists() and stage.is_dir()
    assert foreign.read_bytes() == b'RETAINED FOREIGN CONTENT'


@pytest.mark.parametrize('kind', ['directory', 'file-parent'])
def test_reappeared_owned_descendant_does_not_turn_a_missing_snapshot_into_a_live_entry_refusal(tmp_path, monkeypatch, kind):
    stage = (tmp_path / 'stage').resolve()
    stage.mkdir()
    transient = stage / 'native-temporary'
    transient.mkdir()
    payload = b'RETAINED TEMPORARY CONTENT'
    leaf = transient / 'native.bin'
    if kind == 'file-parent':
        leaf.write_bytes(payload)
    retained = stage / 'retained.bin'
    retained.write_bytes(b'RETAINED ORIGINAL')
    root_identity = (stage.lstat().st_dev, stage.lstat().st_ino)
    actual_is_dir = Path.is_dir
    observations, calls = [], [0]
    def disappeared_snapshot(path):
        if path == transient:
            calls[0] += 1
            # The shared guard checks the live parent and then asks whether the
            # selected directory still exists. Preserve that second real OS
            # result while the owned native directory is immediately recreated.
            if calls[0] == 2:
                if kind == 'file-parent':
                    leaf.unlink()
                transient.rmdir()
                observed = actual_is_dir(path)
                transient.mkdir()
                if kind == 'file-parent':
                    leaf.write_bytes(payload)
                observations.append(observed)
                return observed
        return actual_is_dir(path)
    monkeypatch.setattr(Path, 'is_dir', disappeared_snapshot)
    # Demonstrate the ambiguous old boundary without changing the global guard:
    # a real missing snapshot is still refused after the entry has reappeared.
    with pytest.raises(ValueError, match='project directory does not exist'):
        if kind == 'directory':
            office.checked_directory(transient)
        else:
            office.regular_file(leaf)
    assert observations == [False]
    assert office._missing_transient_entry(transient) is False
    calls[0] = 0
    maximum = len(retained.read_bytes()) + (len(payload) if kind == 'file-parent' else 0)
    office._stage_usage(stage, maximum, time.monotonic() + 5)
    assert calls == [0] and observations == [False]
    assert stat.S_ISDIR(transient.lstat().st_mode)
    assert (stage.lstat().st_dev, stage.lstat().st_ino) == root_identity
    assert retained.read_bytes() == b'RETAINED ORIGINAL'
    if kind == 'file-parent':
        assert leaf.read_bytes() == payload
    # The reappeared live entry is measured, never skipped to make a pass.
    with pytest.raises(ConversionError, match='byte budget'):
        office._stage_usage(stage, maximum - 1, time.monotonic() + 5)
