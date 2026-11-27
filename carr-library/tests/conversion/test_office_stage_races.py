"""Real transient filesystem races during the current owned Office census."""
import os
from pathlib import Path
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
        original = office.checked_directory
        def checked(path, **options):
            if Path(path) == transient and not events:
                transient.rmdir()
                events.append('removed-before-directory-guard')
            return original(path, **options)
        monkeypatch.setattr(office, 'checked_directory', checked)
    else:
        fleeting = transient / 'native.bin'
        fleeting.write_bytes(b'TEMPORARY')
        original = office.regular_file
        def regular(path):
            if Path(path) == fleeting and not events:
                fleeting.unlink()
                transient.rmdir()
                events.append('removed-before-file-parent-guard')
            return original(path)
        monkeypatch.setattr(office, 'regular_file', regular)
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
    original = office.checked_directory
    def checked(path, **options):
        if Path(path) == child:
            raise ValueError(message)
        return original(path, **options)
    monkeypatch.setattr(office, 'checked_directory', checked)
    with pytest.raises(ValueError) as raised:
        office._stage_usage(stage, 1024, time.monotonic() + 5)
    assert str(raised.value) == message and child.is_dir()


def test_permission_failure_during_missing_entry_confirmation_remains_a_failure(tmp_path, monkeypatch):
    stage = (tmp_path / 'stage').resolve()
    stage.mkdir()
    child = stage / 'unreadable'
    child.mkdir()
    original_checked, original_lstat = office.checked_directory, Path.lstat
    armed = []
    def checked(path, **options):
        if Path(path) == child:
            armed.append(True)
            raise ValueError('The project directory does not exist.')
        return original_checked(path, **options)
    def denied(path, *args, **options):
        if armed and path == child:
            raise PermissionError('Synthetic native metadata access refusal')
        return original_lstat(path, *args, **options)
    monkeypatch.setattr(office, 'checked_directory', checked)
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
    original = office.checked_directory
    events = []
    def checked(path, **options):
        if Path(path) == child and not events:
            child.rmdir()
            if change == 'removed':
                stage.rmdir()
            else:
                stage.rename(retained)
                stage.mkdir()
            events.append(change)
        return original(path, **options)
    monkeypatch.setattr(office, 'checked_directory', checked)
    with pytest.raises((ValueError, ConversionError)) as raised:
        office._stage_usage(stage, 1024, time.monotonic() + 5)
    assert events == [change]
    if change == 'removed':
        assert 'project directory does not exist' in str(raised.value)
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
