"""Atomic failure recovery, hostile imports and concurrent keyboard settings."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.keyboard_preferences import KeymapChangedError, KeymapError, KeymapStore, MAX_BYTES, validate_keymap
from carr_library.ui.command_registry import CommandRegistry, CommandSpec, ShortcutError


@pytest.fixture
def registry():
    return CommandRegistry((
        CommandSpec('library.import', 'Import books', default_shortcut='Ctrl+I'),
        CommandSpec('library.search', 'Search books', default_shortcut='Ctrl+F'),
        CommandSpec('reader.next', 'Next source', 'reader', 'Ctrl+PageDown'),
    ))


def test_first_run_is_read_only_and_save_roundtrips(tmp_path, registry):
    path = tmp_path / 'private' / 'keyboard-shortcuts.json'
    store = KeymapStore(path)
    assert store.load() == {}
    assert not path.parent.exists()
    expected = {'library.import': 'Ctrl+Shift+K', 'reader.next': ''}
    assert store.save({'library.import': 'control+shift+k', 'reader.next': ''}, registry) == expected
    assert KeymapStore(path).load() == expected
    assert json.loads(path.read_text()) == {'version': 1, 'bindings': expected}
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('raw', [
    b'{', b'\xff\xfe', b'[]', b'null', b'{"version":true,"bindings":{}}',
    b'{"version":1.0,"bindings":{}}', b'{"version":2,"bindings":{}}',
    b'{"version":1,"bindings":{},"extra":false}', b'{"version":1}',
    b'{"version":1,"version":1,"bindings":{}}',
    b'{"version":1,"bindings":{"library.import":"Ctrl+K","library.import":"Ctrl+J"}}',
    b'{"version":1,"bindings":[]}', b'{"version":1,"bindings":{"bad id":"Ctrl+K"}}',
    b'{"version":1,"bindings":{"library.import":true}}',
    b'{"version":1,"bindings":{"library.import":"Ctrl+C"}}',
    b'{"version":' + b'9' * 5000 + b',"bindings":{}}',
    b' ' * (MAX_BYTES + 1),
], ids=['truncated', 'non-utf8', 'array', 'null', 'bool-version', 'float-version',
        'future-version', 'unknown-field', 'missing-bindings', 'duplicate-root',
        'duplicate-command', 'array-bindings', 'invalid-id', 'bool-shortcut',
        'protected-key', 'huge-number', 'oversized'])
def test_corrupt_settings_are_never_overwritten_by_load(tmp_path, raw):
    path = tmp_path / 'keyboard-shortcuts.json'
    path.write_bytes(raw)
    with pytest.raises(KeymapError):
        KeymapStore(path).load()
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]


def test_oversized_binding_population_is_rejected(tmp_path):
    path = tmp_path / 'keyboard-shortcuts.json'
    path.write_text(json.dumps({'version': 1, 'bindings': {f'command.c{i}': '' for i in range(513)}}))
    with pytest.raises(KeymapError, match='512'):
        KeymapStore(path).load()


def test_invalid_save_never_changes_disk_or_caller_mapping(tmp_path, registry):
    path = tmp_path / 'keyboard-shortcuts.json'
    store = KeymapStore(path)
    store.save({'library.import': 'Ctrl+K'}, registry)
    previous = path.read_bytes()
    for invalid in ({'library.import': 'Ctrl+F'}, {'unknown.command': 'Ctrl+K'},
                    {'library.import': 'Ctrl+C'}, {'library.import': False}):
        original = invalid.copy()
        with pytest.raises(ShortcutError):
            store.save(invalid, registry)
        assert invalid == original
        assert path.read_bytes() == previous


@pytest.mark.parametrize('failure', ['replace', 'fsync'])
def test_io_failure_before_commit_preserves_previous_file_and_removes_temporary(tmp_path, registry, monkeypatch, failure):
    path = tmp_path / 'keyboard-shortcuts.json'
    store = KeymapStore(path)
    store.save({'library.import': 'Ctrl+K'}, registry)
    previous = path.read_bytes()

    def disk_failure(*args, **kwargs):
        raise OSError('Synthetic disk full')
    monkeypatch.setattr('carr_library.keyboard_preferences.os.' + failure, disk_failure)
    with pytest.raises(KeymapError, match='could not be saved'):
        store.save({'library.import': 'Ctrl+J'}, registry)
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob('*.tmp'))
    assert not list(tmp_path.glob('.*.tmp'))


def test_other_window_edit_is_reported_without_losing_newer_settings(tmp_path, registry):
    path = tmp_path / 'keyboard-shortcuts.json'
    first, second = KeymapStore(path), KeymapStore(path)
    assert first.load() == second.load() == {}
    first.save({'library.import': 'Ctrl+K'}, registry)
    with pytest.raises(KeymapChangedError, match='another window'):
        second.save({'library.import': 'Ctrl+J'}, registry)
    assert KeymapStore(path).load() == {'library.import': 'Ctrl+K'}
    assert second.load() == {'library.import': 'Ctrl+K'}
    second.save({'library.import': 'Ctrl+J'}, registry)
    assert KeymapStore(path).load() == {'library.import': 'Ctrl+J'}


def test_external_deletion_is_not_silently_recreated(tmp_path, registry):
    path = tmp_path / 'keyboard-shortcuts.json'
    store = KeymapStore(path)
    store.save({'library.import': 'Ctrl+K'}, registry)
    path.unlink()
    with pytest.raises(KeymapChangedError):
        store.save({'library.import': 'Ctrl+J'}, registry)
    assert not path.exists()


def test_directory_and_symlink_targets_are_refused(tmp_path, registry):
    directory = tmp_path / 'not-a-file'
    directory.mkdir()
    for method in ('load', 'save'):
        with pytest.raises(KeymapError, match='regular file'):
            getattr(KeymapStore(directory), method)(*(() if method == 'load' else ({}, registry)))
    original = tmp_path / 'original.json'
    original.write_text('{"version":1,"bindings":{}}')
    link = tmp_path / 'link.json'
    try:
        link.symlink_to(original)
    except (OSError, NotImplementedError):
        # Windows tests require no symlink privilege; directory rejection above
        # is still exercised. The link branch is exercised on POSIX CI.
        return
    with pytest.raises(KeymapError, match='symbolic links'):
        KeymapStore(link).load()
    with pytest.raises(KeymapError, match='symbolic links'):
        KeymapStore(link).save({}, registry)
    assert original.read_text() == '{"version":1,"bindings":{}}'


def test_known_version_file_still_requires_registry_validation(tmp_path, registry):
    path = tmp_path / 'keyboard-shortcuts.json'
    raw = b'{"version":1,"bindings":{"removed.command":"Ctrl+K"}}'
    path.write_bytes(raw)
    bindings = KeymapStore(path).load()
    with pytest.raises(ShortcutError, match='Unknown command'):
        registry.validate(bindings)
    assert path.read_bytes() == raw


def test_corrupt_load_still_detects_external_change_before_explicit_repair(tmp_path, registry):
    path = tmp_path / 'keyboard-shortcuts.json'
    path.write_bytes(b'{broken JSON')
    store = KeymapStore(path)
    with pytest.raises(KeymapError):
        store.load()
    KeymapStore(path).save({'library.import': 'Ctrl+K'}, registry)
    with pytest.raises(KeymapChangedError):
        store.save({}, registry)
    assert KeymapStore(path).load() == {'library.import': 'Ctrl+K'}


def test_explicit_repair_of_unchanged_corrupt_file_is_possible(tmp_path, registry):
    path = tmp_path / 'keyboard-shortcuts.json'
    path.write_bytes(b'{broken JSON')
    store = KeymapStore(path)
    with pytest.raises(KeymapError):
        store.load()
    store.save({}, registry)
    assert KeymapStore(path).load() == {}


def test_hard_link_settings_and_lock_are_rejected_without_touching_original(tmp_path, registry):
    original = tmp_path / 'original.json'
    raw = b'{"version":1,"bindings":{}}'
    original.write_bytes(raw)
    linked = tmp_path / 'keyboard-shortcuts.json'
    os.link(original, linked)
    with pytest.raises(KeymapError, match='hard links'):
        KeymapStore(linked).load()
    with pytest.raises(KeymapError, match='hard links'):
        KeymapStore(linked).save({}, registry)
    linked.unlink()
    os.link(original, tmp_path / 'keyboard-shortcuts.json.lock')
    with pytest.raises(KeymapError, match='hard links'):
        KeymapStore(linked).save({}, registry)
    assert original.read_bytes() == raw
    assert not linked.exists()


def test_windows_reparse_attribute_is_rejected_even_without_posix_symlink(tmp_path, registry, monkeypatch):
    path = tmp_path / 'keyboard-shortcuts.json'
    raw = b'{"version":1,"bindings":{}}'
    path.write_bytes(raw)
    original = Path.lstat

    def metadata(value):
        actual = original(value)
        if value == path:
            return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400)
        return actual
    monkeypatch.setattr(Path, 'lstat', metadata)
    with pytest.raises(KeymapError, match='symbolic links'):
        KeymapStore(path).load()
    with pytest.raises(KeymapError, match='symbolic links'):
        KeymapStore(path).save({}, registry)
    assert path.read_bytes() == raw


def test_backup_validator_preserves_inert_future_commands_but_requires_bounded_strict_bytes():
    assert validate_keymap(b'{"version":1,"bindings":{"future.module":"control+shift+j"}}') == {
        'future.module': 'Ctrl+Shift+J'}
    for invalid in ('{}', bytearray(b'{}'), None, b' ' * (MAX_BYTES + 1),
                    b'{"version":true,"bindings":{}}',
                    b'{"version":1,"bindings":{"future.module":"Ctrl+C"}}',
                    b'{"version":1,"bindings":{},"code":"print(1)"}'):
        with pytest.raises(KeymapError):
            validate_keymap(invalid)


def test_external_edit_during_temporary_write_is_preserved(tmp_path, registry, monkeypatch):
    path = tmp_path / 'keyboard-shortcuts.json'
    store = KeymapStore(path)
    store.save({'library.import': 'Ctrl+K'}, registry)
    external = b'{"version":1,"bindings":{"library.import":"Ctrl+Shift+J"}}'
    original = os.fsync
    edited = False

    def edit_during_flush(descriptor):
        nonlocal edited
        original(descriptor)
        if not edited:
            edited = True
            path.write_bytes(external)
    monkeypatch.setattr('carr_library.keyboard_preferences.os.fsync', edit_during_flush)
    with pytest.raises(KeymapChangedError, match='before saving'):
        store.save({'library.import': 'Ctrl+J'}, registry)
    assert path.read_bytes() == external
    assert not list(tmp_path.glob('.*.tmp'))


def test_windows_path_and_descriptor_identity_difference_does_not_break_reopen_or_cas(tmp_path, registry, monkeypatch):
    from carr_library import keyboard_preferences
    path = tmp_path / 'keyboard-shortcuts.json'
    first = KeymapStore(path)
    first.save({'library.import': 'Ctrl+K'}, registry)
    actual_fstat = os.fstat

    def descriptor_identity(descriptor):
        info = actual_fstat(descriptor)
        fields = {name: getattr(info, name) for name in ('st_dev', 'st_ino', 'st_mode',
            'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        fields.update(st_dev=info.st_dev + 100, st_ino=info.st_ino + 200,
                      st_ctime_ns=info.st_ctime_ns + 300)
        return SimpleNamespace(**fields)

    platform = SimpleNamespace(**vars(os))
    platform.name, platform.fstat = 'nt', descriptor_identity
    monkeypatch.setattr(keyboard_preferences, 'os', platform)
    reopened = KeymapStore(path)
    assert reopened.load() == {'library.import': 'Ctrl+K'}
    reopened.save({'library.import': 'Ctrl+J'}, registry)
    with pytest.raises(KeymapChangedError, match='another window'):
        first.save({'library.import': 'Ctrl+Shift+K'}, registry)
    assert KeymapStore(path).load() == {'library.import': 'Ctrl+J'}


def test_windows_descriptor_change_during_read_is_still_rejected(tmp_path, monkeypatch):
    from carr_library import keyboard_preferences
    path = tmp_path / 'keyboard-shortcuts.json'
    original = b'{"version":1,"bindings":{}}'
    path.write_bytes(original)
    actual_fstat, calls = os.fstat, 0

    def changing_descriptor(descriptor):
        nonlocal calls
        calls += 1
        info = actual_fstat(descriptor)
        fields = {name: getattr(info, name) for name in ('st_dev', 'st_ino', 'st_mode',
            'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        fields.update(st_ino=info.st_ino + 100, st_mtime_ns=info.st_mtime_ns + calls)
        return SimpleNamespace(**fields)

    platform = SimpleNamespace(**vars(os))
    platform.name, platform.fstat = 'nt', changing_descriptor
    monkeypatch.setattr(keyboard_preferences, 'os', platform)
    with pytest.raises(KeymapChangedError, match='changed while reading'):
        KeymapStore(path).load()
    assert path.read_bytes() == original
