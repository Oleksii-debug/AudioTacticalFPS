"""Bounded registration recovery with real shortcut files and a named Winreg fixture.

The fixture exercises production capture/restore decisions. Actual Windows
registry and compiled Inno acceptance remain separate native gates.
"""
from copy import deepcopy
from pathlib import Path

import pytest

from carr_library.security._files import TrustError
from carr_library.security.windows_registration import (
    KEY, LINKS, WindowsRegistration, validate_snapshot)


class RegistryFixture:
    HKEY_CURRENT_USER = 1
    KEY_READ, KEY_WRITE, KEY_WOW64_64KEY = 1, 2, 256
    REG_NONE, REG_BINARY = 0, 3

    def __init__(self, values):
        self.values = deepcopy(values)
        self.mutations = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def OpenKey(self, hive, key, reserved, access):
        assert (hive, key, reserved) == (self.HKEY_CURRENT_USER, KEY, 0)
        if self.values is None:
            raise FileNotFoundError('Synthetic missing key')
        return self

    def CreateKeyEx(self, hive, key, reserved, access):
        assert (hive, key, reserved) == (self.HKEY_CURRENT_USER, KEY, 0)
        self.mutations.append('create-fixed-key')
        if self.values is None:
            self.values = {}
        return self

    def QueryInfoKey(self, key):
        assert key is self and self.values is not None
        return 0, len(self.values), 1

    def EnumValue(self, key, index):
        name = sorted(self.values)[index]
        value, kind = self.values[name]
        return name, value, kind

    def SetValueEx(self, key, name, reserved, kind, value):
        assert key is self
        self.mutations.append(('set', name))
        self.values[name] = (value, kind)

    def DeleteValue(self, key, name):
        assert key is self
        self.mutations.append(('delete', name))
        del self.values[name]

    def FlushKey(self, key):
        assert key is self
        self.mutations.append('flush-fixed-key')


@pytest.fixture
def registration(tmp_path):
    root = tmp_path / 'Managed app Ω'
    root.mkdir()
    native = WindowsRegistration.__new__(WindowsRegistration)
    native.winreg = RegistryFixture({
        'InstallLocation': (str(root), 1),
        'DisplayName': ('Resonance', 1),
        'DisplayVersion': ('0.2.0.dev1', 1),
        'UnrelatedUserNote': ('Preserve the original registration note.', 1),
    })
    native.menu_root = tmp_path / 'Programs' / 'Resonance'
    native.menu_root.mkdir(parents=True)
    for name in LINKS:
        (native.menu_root / name).write_bytes(('Original A shortcut: ' + name).encode())
    saved = native.snapshot(root)
    return native, root, saved


@pytest.mark.parametrize('present', [False, True], ids=['absent-key', 'empty-key'])
def test_failed_wrapper_retains_absent_vs_empty_key_and_restores_original_shortcuts(registration, present):
    native, root, saved = registration
    expected_values = deepcopy(native.winreg.values)
    native.winreg.values = {} if present else None
    for path in native.menu_root.iterdir():
        path.unlink()
    native.menu_root.rmdir()
    captured = native.snapshot(root, allow_partial=True)
    assert captured['registry_present'] is present and captured['values'] == {}
    assert captured['shortcuts'] == dict.fromkeys(LINKS)
    with pytest.raises((TrustError, FileNotFoundError)):
        native.snapshot(root)
    native.restore(root, saved)
    assert native.winreg.values == expected_values
    assert native.snapshot(root) == saved
    assert ('create-fixed-key' in native.winreg.mutations) is not present
    assert 'flush-fixed-key' in native.winreg.mutations


@pytest.mark.parametrize('foreign', [True, False], ids=['different-root', 'unbound-nonempty-key'])
def test_partial_snapshot_is_retained_data_but_does_not_authorize_foreign_registration(registration, foreign):
    native, root, saved = registration
    if foreign:
        native.winreg.values['InstallLocation'] = (str(root.parent / 'Other app'), 1)
    else:
        del native.winreg.values['InstallLocation']
    current = deepcopy(native.winreg.values)
    captured = native.snapshot(root, allow_partial=True)
    assert captured['registry_present'] is True and captured['values']
    # Capturing the fixed bounded bytes is permitted. Mutating an unbound
    # current registration is not; the pending marker must stay blocked.
    with pytest.raises(TrustError, match='another program directory'):
        native.restore(root, saved)
    assert native.winreg.values == current and not native.winreg.mutations
    assert captured == native.snapshot(root, allow_partial=True)


def test_inert_absent_registration_snapshot_cannot_authorize_restore(registration):
    native, root, saved = registration
    invalid = deepcopy(saved)
    invalid.update(registry_present=False, values={})
    validate_snapshot(invalid, root)
    with pytest.raises(TrustError, match='captured original'):
        native.restore(root, invalid)
    assert native.snapshot(root) == saved and not native.winreg.mutations


def test_recovery_does_not_retarget_another_users_start_menu(registration, tmp_path):
    native, root, saved = registration
    invalid = deepcopy(saved)
    invalid['menu_root'] = str(tmp_path / 'Other user menu')
    with pytest.raises(TrustError, match='another user profile'):
        native.restore(root, invalid)
    assert native.snapshot(root) == saved and not native.winreg.mutations


def test_recovery_preserves_unrelated_registration_changes_for_review(registration):
    native, root, saved = registration
    native.winreg.values['UnrelatedUserNote'] = ('New external note; retain it.', 1)
    current = deepcopy(native.winreg.values)
    with pytest.raises(TrustError, match='unrelated registration value changed'):
        native.restore(root, saved)
    assert native.winreg.values == current and not native.winreg.mutations


def test_ordinary_original_capture_does_not_admit_an_empty_registration(registration):
    native, root, _ = registration
    native.winreg.values = {}
    with pytest.raises(TrustError, match='another program directory'):
        native.snapshot(root)


@pytest.mark.parametrize('present', [False, True], ids=['recreated-key', 'empty-key'])
def test_interrupted_registration_replay_publishes_root_first_and_can_retry(registration, monkeypatch, present):
    native, root, saved = registration
    native.winreg.values = {} if present else None
    original = native.winreg.SetValueEx
    observed = []

    def interrupted(key, name, reserved, kind, value):
        observed.append(name)
        if name.casefold() == 'displayname':
            assert native.winreg.values == {'InstallLocation': (str(root), 1)}
            assert native.winreg.mutations[-1] == 'flush-fixed-key'
            raise OSError('Synthetic failure after the durable root binding')
        return original(key, name, reserved, kind, value)

    monkeypatch.setattr(native.winreg, 'SetValueEx', interrupted)
    with pytest.raises(OSError, match='durable root binding'):
        native.restore(root, saved)
    assert observed[0] == 'InstallLocation'
    monkeypatch.setattr(native.winreg, 'SetValueEx', original)
    native.restore(root, saved)
    assert native.snapshot(root) == saved
