"""Actual bounded state, concurrent reader choices and complete backup coverage."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader import appearance as state


def chosen():
    return state.ReaderAppearancePreferences(state.ReaderFont('Local Test Face', 13.5, 700, 'italic'),
                                             '#102030', '#F1F2F3', 175)


def filename(root):
    return root / 'state/ui/reader-appearance.json'


def test_defaults_save_reopen_reset_and_concurrent_readers(tmp_path):
    first = state.load_appearance(tmp_path)
    second = state.load_appearance(tmp_path)
    assert first.preferences == state.ReaderAppearancePreferences() and first.token is None
    assert not filename(tmp_path).exists()
    saved = state.save_appearance(tmp_path, chosen(), expected_token=first.token)
    assert saved == state.load_appearance(tmp_path) and not saved.warning
    before = filename(tmp_path).read_bytes()
    with pytest.raises(ValueError, match='Another window'):
        state.save_appearance(tmp_path, replace(chosen(), zoom_percent=200), expected_token=second.token)
    assert filename(tmp_path).read_bytes() == before
    reset = state.save_appearance(tmp_path, state.ReaderAppearancePreferences(), expected_token=saved.token)
    assert reset.preferences == state.ReaderAppearancePreferences()
    assert state.load_appearance(tmp_path) == reset


@pytest.mark.parametrize('field,value', [
    ('zoom_percent', True), ('zoom_percent', 49), ('zoom_percent', 401), ('zoom_percent', 125.0),
    ('foreground', 'blue'), ('foreground', '#FFFFFF00'), ('foreground', '#abcDEF'),
    ('background', 0), ('background', '#12345G'), ('font', {}),
])
def test_invalid_preference_values_are_not_serializable(field, value):
    with pytest.raises(ValueError):
        replace(chosen(), **{field: value})


@pytest.mark.parametrize('field,value', [
    ('face', ''), ('face', ' '), ('face', 'A\nB'), ('face', 'A' * 129), ('face', '\ud800'),
    ('points', True), ('points', 3.9), ('points', 145), ('points', float('nan')),
    ('points', float('inf')), ('weight', True), ('weight', 0), ('weight', 1001),
    ('style', 'bold'), ('style', None), ('underlined', 1), ('strikethrough', 'false'),
])
def test_invalid_font_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        replace(chosen().font, **{field: value})


@pytest.mark.parametrize('damage', ['schema_bool', 'schema_future', 'extra', 'missing', 'font_extra',
                                   'duplicate', 'nonfinite', 'nonutf8', 'large'])
def test_invalid_saved_bytes_are_preserved_even_on_reset(tmp_path, damage):
    path = filename(tmp_path)
    path.parent.mkdir(parents=True)
    value = {'schema': 1, 'preferences': asdict(chosen())}
    if damage == 'schema_bool': value['schema'] = True
    elif damage == 'schema_future': value['schema'] = 2
    elif damage == 'extra': value['other'] = True
    elif damage == 'missing': del value['preferences']['background']
    elif damage == 'font_extra': value['preferences']['font']['path'] = 'unapproved.ttf'
    raw = encode(value)
    if damage == 'duplicate': raw = raw.replace(b'"schema":1', b'"schema":1,"schema":1')
    elif damage == 'nonfinite': raw = raw.replace(b'13.5', b'NaN')
    elif damage == 'nonutf8': raw = b'\xff'
    elif damage == 'large': raw = b' ' * (state.MAX_APPEARANCE_BYTES + 1)
    path.write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        state.load_appearance(tmp_path)
    with pytest.raises((ValueError, UnicodeError)):
        state.save_appearance(tmp_path, state.ReaderAppearancePreferences(),
                              expected_token=sha256(raw).hexdigest())
    assert path.read_bytes() == raw


def test_hardlinked_preference_is_rejected_without_touching_either_name(tmp_path):
    saved = state.save_appearance(tmp_path, chosen(), expected_token=None)
    path = filename(tmp_path)
    other = tmp_path / 'unrelated.json'
    os.link(path, other)
    raw = path.read_bytes()
    with pytest.raises(ValueError, match='without links'):
        state.load_appearance(tmp_path)
    with pytest.raises(ValueError, match='without links'):
        state.save_appearance(tmp_path, state.ReaderAppearancePreferences(), expected_token=saved.token)
    assert path.read_bytes() == other.read_bytes() == raw


def test_failure_before_publication_preserves_previous_preferences(tmp_path, monkeypatch):
    saved = state.save_appearance(tmp_path, chosen(), expected_token=None)
    before = filename(tmp_path).read_bytes()
    def denied(*args, **kwargs):
        raise OSError('Synthetic storage denial before publication.')
    monkeypatch.setattr(state, 'atomic_json', denied)
    with pytest.raises(OSError, match='could not be confirmed'):
        state.save_appearance(tmp_path, replace(chosen(), zoom_percent=200), expected_token=saved.token)
    assert filename(tmp_path).read_bytes() == before


def test_actual_postpublication_directory_sync_failure_returns_saved_warning(tmp_path, monkeypatch):
    from carr_library import durable_state
    saved = state.save_appearance(tmp_path, chosen(), expected_token=None)
    def failed_sync(path):
        raise OSError('Synthetic directory sync error after real atomic replacement.')
    monkeypatch.setattr(durable_state, 'sync_directory', failed_sync)
    result = state.save_appearance(tmp_path, replace(chosen(), zoom_percent=200), expected_token=saved.token)
    assert result.warning and 'saved' in result.warning
    assert state.load_appearance(tmp_path).preferences == result.preferences
    assert result.token == sha256(filename(tmp_path).read_bytes()).hexdigest()


@pytest.mark.parametrize('substitution', ['replace', 'grow', 'link'])
def test_opened_file_is_validated_before_any_preference_bytes_are_consumed(tmp_path, monkeypatch, substitution):
    state.save_appearance(tmp_path, chosen(), expected_token=None)
    target = filename(tmp_path)
    original = target.read_bytes()
    opened, consumed = state._open_preferences, []
    class Stream:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, size):
            consumed.append(size)
            return self.stream.read(size)
    def raced(path):
        assert path == target
        if substitution == 'replace':
            os.replace(target, target.with_suffix('.previous'))
            target.write_bytes(original)
        elif substitution == 'grow':
            target.write_bytes(b' ' * (state.MAX_APPEARANCE_BYTES + 1))
        else:
            os.link(target, target.with_suffix('.linked'))
        return Stream(opened(path))
    monkeypatch.setattr(state, '_open_preferences', raced)
    with pytest.raises(ValueError, match='before its bounded file was opened'):
        state.load_appearance(tmp_path)
    assert consumed == [], 'Even a caught parse error must not mask reading an unvalidated descriptor.'
    if substitution == 'replace':
        assert target.with_suffix('.previous').read_bytes() == original


def test_full_library_backup_restores_exact_preferences_without_active_lock(tmp_path):
    app = LibraryApp(tmp_path / 'Оригінальна бібліотека')
    app.create_workspace('Reader appearance preservation')
    state.save_appearance(app.root, chosen(), expected_token=None)
    raw = filename(app.root).read_bytes()
    archive = tmp_path / 'Резервна копія'
    digest = create_backup(app, archive)
    manifest = json.loads((archive / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    entry, = [row for row in manifest['sidecars'] if row['filename'] == 'state/ui/reader-appearance.json']
    assert entry == {'filename': 'state/ui/reader-appearance.json', 'size_bytes': len(raw),
                     'sha256': sha256(raw).hexdigest()}
    restored = LibraryApp(restore_backup(archive, tmp_path / 'Відновлена бібліотека', digest))
    assert filename(restored.root).read_bytes() == filename(app.root).read_bytes() == raw
    assert state.load_appearance(restored.root) == state.load_appearance(app.root)
    assert not (restored.root / 'state/ui/.reader-appearance.lock').exists()
