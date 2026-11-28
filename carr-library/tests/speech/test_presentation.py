"""Appearance persistence never changes the reader's document or runtime."""
from dataclasses import replace
import json

import pytest

from carr_library.durable_state import atomic_json
from carr_library.speech.presentation import (MAX_BYTES, ReadingAppearance,
                                              ReadingAppearanceStore, ReadingFont)


def test_exact_font_colours_zoom_round_trip_and_defaults(tmp_path):
    store = ReadingAppearanceStore(tmp_path)
    assert store.load() == ReadingAppearance()
    assert not (tmp_path / 'state').exists()
    chosen = ReadingAppearance(ReadingFont('Segoe UI', 17, 700, 'italic', True),
                               '#FFFFAA', '#101018', 175)
    assert ReadingAppearance.from_dict(chosen.to_dict()) == chosen
    store.save(chosen, expected=ReadingAppearance())
    assert ReadingAppearanceStore(tmp_path).load() == chosen
    store.save(ReadingAppearance(), expected=chosen)
    assert store.load() == ReadingAppearance()


@pytest.mark.parametrize('field,value', [
    ('zoom', True), ('zoom', 49), ('zoom', 401), ('zoom', 100.0),
    ('foreground', 'red'), ('foreground', '#aaFFFF'), ('background', ''),
    ('background', '#FFFFFF00'), ('font', {}), ('font', 'Segoe UI'),
])
def test_invalid_appearance_fields_are_not_accepted(field, value):
    with pytest.raises(ValueError):
        ReadingAppearance(**{field: value})


@pytest.mark.parametrize('field,value', [
    ('face', ''), ('face', '\x00Font'), ('face', 'x' * 129), ('face', 1),
    ('points', True), ('points', 5), ('points', 73), ('weight', True),
    ('weight', 99), ('weight', 1001), ('style', []), ('style', 'script'),
    ('underlined', 1),
])
def test_invalid_native_font_values_are_rejected(field, value):
    chosen = {'face': 'Segoe UI', 'points': 12, field: value}
    with pytest.raises(ValueError):
        ReadingFont(**chosen)


@pytest.mark.parametrize('change', [
    lambda d: {**d, 'schema': 'future'}, lambda d: {**d, 'command': 'run'},
    lambda d: {k: v for k, v in d.items() if k != 'zoom'},
    lambda d: {**d, 'font': {'face': 'A', 'points': 12}},
])
def test_unknown_schema_or_missing_fields_are_not_silently_defaulted(change):
    with pytest.raises(ValueError):
        ReadingAppearance.from_dict(change(ReadingAppearance().to_dict()))


def test_concurrent_reader_change_cannot_be_overwritten(tmp_path):
    first, second = ReadingAppearanceStore(tmp_path), ReadingAppearanceStore(tmp_path)
    initial = first.load()
    accepted = replace(initial, zoom=150)
    second.save(accepted, expected=initial)
    before = second.path.read_bytes()
    with pytest.raises(ValueError, match='another window'):
        first.save(replace(initial, zoom=200), expected=initial)
    assert second.path.read_bytes() == before


def test_guard_is_rechecked_at_publication_and_no_preferences_publish(tmp_path):
    store = ReadingAppearanceStore(tmp_path)
    calls = []
    def guard():
        calls.append(True)
        if len(calls) == 2:
            raise ValueError('Source revoked')
    with pytest.raises(ValueError, match='revoked'):
        store.save(ReadingAppearance(zoom=125), expected=ReadingAppearance(), guard=guard)
    assert len(calls) == 2 and not store.path.exists()


@pytest.mark.parametrize('raw', [
    b'{"schema":"carr.reading-appearance.v1","schema":"future"}',
    b'[]', b'{', b' ' * (MAX_BYTES + 1),
])
def test_invalid_file_is_retained_and_not_overwritten(tmp_path, raw):
    store = ReadingAppearanceStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        store.save(ReadingAppearance(), expected=ReadingAppearance())
    assert store.path.read_bytes() == raw


def test_replaced_library_identity_is_rejected(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    store = ReadingAppearanceStore(library)
    library.rename(tmp_path / 'retained')
    library.mkdir()
    with pytest.raises(ValueError, match='library changed'):
        store.save(ReadingAppearance(zoom=125), expected=ReadingAppearance())
    assert not store.path.exists()


def test_hardlinked_preferences_are_rejected(tmp_path):
    import os
    store = ReadingAppearanceStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    atomic_json(store.path, ReadingAppearance().to_dict())
    os.link(store.path, tmp_path / 'alias.json')
    with pytest.raises(ValueError, match='links'):
        store.load()
