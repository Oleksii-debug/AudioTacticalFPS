"""Canonical UI preferences migrate through the current stores without data loss."""
from hashlib import sha256
import json
import os

from filelock import FileLock
import pytest

from carr_library.application import LibraryApp
from carr_library import legacy_ui_preferences as legacy
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.progress import ProgressPreferences, load_preferences, save_preferences


def progress_value(**changes):
    return {'schema': 1, 'preferences': dict(mode='both', interval_seconds=120,
        volume=.37, quiet=False, background=True, mute_during_audio=False, **changes)}


def progress_raw(**changes):
    value = progress_value()
    value['preferences'].update(changes)
    return (json.dumps(value, ensure_ascii=False, indent=3) + '\r\n').encode('utf-8')


def shortcuts_raw(**changes):
    value = {'schema': 1, 'overrides': {'library/import books': 'F24'},
             'originals': {'library/import books': 'Ctrl+I'}}
    value.update(changes)
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\r\n').encode('utf-8')


def saved(root, name, raw):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


@pytest.mark.parametrize('mode', ['off', 'beep', 'speech', 'both'])
def test_progress_migrates_full_interval_and_flags_but_never_selects_a_voice(tmp_path, mode):
    path = saved(tmp_path, legacy.PROGRESS, progress_raw(mode=mode))
    before = path.read_bytes()
    message = legacy.migrate_progress(tmp_path)
    current = load_preferences(tmp_path)
    assert current.mode == ('beep_speech' if mode == 'both' else mode)
    assert current.interval_seconds == 120 and current.volume == .37
    assert current.background and not current.quiet and not current.suppress_during_media
    assert current.muted is (mode in ('speech', 'both'))
    assert (current.voice_id, current.voice_language, current.voice_fingerprint) == ('', '', '')
    assert 'original file was kept' in message
    if current.muted:
        assert 'Speech stays muted' in message
    assert path.read_bytes() == before
    digest = sha256((tmp_path / 'progress-preferences.json').read_bytes()).hexdigest()
    assert legacy.migrate_progress(tmp_path) == ''
    assert sha256((tmp_path / 'progress-preferences.json').read_bytes()).hexdigest() == digest


def test_current_progress_is_authoritative_even_when_old_file_is_malformed(tmp_path):
    path = saved(tmp_path, legacy.PROGRESS, b'old unsupported preferences')
    expected = ProgressPreferences(mode='beep', interval_seconds=2)
    save_preferences(tmp_path, expected)
    assert legacy.migrate_progress(tmp_path) == ''
    assert load_preferences(tmp_path) == expected
    assert path.read_bytes() == b'old unsupported preferences'


@pytest.mark.parametrize('field,value', [('mode', 'auto'), ('interval_seconds', True),
    ('interval_seconds', 121), ('volume', -1), ('quiet', 1), ('background', 'yes'),
    ('mute_during_audio', None), ('voice_id', 'stale-executable-voice')])
def test_invalid_progress_is_never_activated_or_erased(tmp_path, field, value):
    raw = progress_raw(**{field: value})
    path = saved(tmp_path, legacy.PROGRESS, raw)
    with pytest.raises(ValueError):
        legacy.migrate_progress(tmp_path)
    assert not (tmp_path / 'progress-preferences.json').exists()
    assert path.read_bytes() == raw


@pytest.mark.parametrize('raw', [b'{"schema":1,"schema":1}', b'{"schema":NaN}', b'{}',
                                b'[]', b'\xff', b'x' * (legacy.MAX_PROGRESS_BYTES + 1)],
                         ids=['duplicate', 'nonfinite', 'empty-map', 'list', 'invalid-utf8', 'oversize'])
def test_progress_schema_is_bounded_unique_finite_utf8(raw):
    with pytest.raises(ValueError):
        legacy.decode_progress(raw)


@pytest.mark.parametrize('change', ['schema', 'originals', 'unknown', 'path_case', 'path_empty',
                                   'path_control', 'key', 'too_many'])
def test_shortcut_shape_is_checked_without_current_command_activation(change):
    value = json.loads(shortcuts_raw())
    if change == 'schema':
        value['schema'] = True
    elif change == 'originals':
        value['originals'] = {}
    elif change == 'unknown':
        value['executable'] = 'must not be accepted'
    elif change in ('path_case', 'path_empty', 'path_control'):
        name = {'path_case': 'Library/import books', 'path_empty': 'library//import',
                'path_control': 'library/import\nbooks'}[change]
        value['overrides'] = {name: 'F24'}
        value['originals'] = {name: 'Ctrl+I'}
    elif change == 'key':
        value['overrides']['library/import books'] = 'Insert'
    else:
        value['overrides'] = {f'library/action {index}': '' for index in range(513)}
        value['originals'] = dict(value['overrides'])
    with pytest.raises(ValueError):
        legacy.decode_shortcuts(json.dumps(value).encode('utf-8'))


def test_canonical_progress_mutation_during_migration_does_not_publish(tmp_path, monkeypatch):
    from carr_library import progress
    path = saved(tmp_path, legacy.PROGRESS, progress_raw())
    actual = progress.save_preferences
    def interleave(*args, **kwargs):
        path.write_bytes(progress_raw(volume=.74))
        return actual(*args, **kwargs)
    monkeypatch.setattr(progress, 'save_preferences', interleave)
    with pytest.raises(ValueError, match='changed'):
        legacy.migrate_progress(tmp_path)
    assert not (tmp_path / 'progress-preferences.json').exists()
    assert legacy.decode_progress(path.read_bytes())['volume'] == .74


def test_late_current_progress_file_is_never_overwritten(tmp_path, monkeypatch):
    from carr_library.audio import jobs
    saved(tmp_path, legacy.PROGRESS, progress_raw())
    original = jobs.publish_noreplace
    target = tmp_path / 'progress-preferences.json'
    concurrent = b'{"owned":"another window"}\n'
    def competing(source, destination):
        target.write_bytes(concurrent)
        return original(source, destination)
    monkeypatch.setattr(jobs, 'publish_noreplace', competing)
    with pytest.raises(FileExistsError):
        legacy.migrate_progress(tmp_path)
    assert target.read_bytes() == concurrent


@pytest.mark.parametrize('name', [legacy.PROGRESS, legacy.SHORTCUTS])
def test_foreign_hardlinked_preference_is_rejected_without_activation(tmp_path, name):
    raw = progress_raw() if name == legacy.PROGRESS else shortcuts_raw()
    path = saved(tmp_path, name, raw)
    os.link(path, tmp_path / 'foreign.json')
    with pytest.raises(ValueError, match='links'):
        legacy.read_snapshot(tmp_path, name)
    assert path.read_bytes() == raw


def test_symlinked_ui_ancestor_is_not_read(tmp_path):
    outside = tmp_path / 'foreign'
    outside.mkdir()
    (outside / 'progress.json').write_bytes(progress_raw())
    root = tmp_path / 'library'
    (root / 'state').mkdir(parents=True)
    (root / 'state' / 'ui').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='links'):
        legacy.migrate_progress(root)
    assert not (root / 'progress-preferences.json').exists()


@pytest.mark.parametrize('lock_name', ['progress', 'shortcuts'])
def test_migration_and_backup_obey_actual_canonical_native_writer_lock(tmp_path, lock_name):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Original UI preferences')
    path = saved(app.root, legacy.PROGRESS, progress_raw())
    with FileLock(path.parent / ('.' + lock_name + '.lock'), timeout=0):
        with pytest.raises(RuntimeError, match='being saved'):
            legacy.migrate_progress(app.root)
        with pytest.raises(RuntimeError, match='being saved'):
            create_backup(app, tmp_path / 'blocked')
    assert not (tmp_path / 'blocked').exists()
    assert not (app.root / 'progress-preferences.json').exists()


def test_complete_backup_restores_exact_canonical_and_current_bytes_without_activation(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Saved preference sources')
    payloads = {legacy.PROGRESS: progress_raw(), legacy.SHORTCUTS: shortcuts_raw()}
    for name, raw in payloads.items():
        saved(app.root, name, raw)
    legacy.migrate_progress(app.root)
    modern = (app.root / 'progress-preferences.json').read_bytes()
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    for name, raw in payloads.items():
        assert (restored / name).read_bytes() == raw
        assert (app.root / name).read_bytes() == raw
    assert (restored / 'progress-preferences.json').read_bytes() == modern
    assert not (restored / 'keyboard-shortcuts.json').exists()
    assert load_preferences(restored).muted and not load_preferences(restored).voice_id
    again = tmp_path / 'again'
    next_digest = create_backup(LibraryApp(restored), again)
    second = restore_backup(again, tmp_path / 'second', next_digest)
    assert all((second / name).read_bytes() == raw for name, raw in payloads.items())


def test_malformed_canonical_preferences_cannot_be_called_complete_backup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Invalid retained preferences')
    path = saved(app.root, legacy.SHORTCUTS, b'{"schema":1,"overrides":{},"originals":{},"voice":"stale"}')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'invalid')
    assert not (tmp_path / 'invalid').exists() and path.read_bytes() == before
