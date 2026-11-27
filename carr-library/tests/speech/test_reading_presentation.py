"""Actual bounded sidecars and backup restore, independent of native font rendering."""
from dataclasses import replace
from hashlib import sha256
import json
import os
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.speech import presentation as core
from carr_library.speech.presentation import ReadingFont, ReadingPresentation, PresentationStore
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume


PROFILE = ReadingPresentation(ReadingFont('DejaVu Sans', 12.5, 700, 'italic'), (10, 20, 30), (240, 230, 220), 125)


def test_real_profile_defaults_save_reopen_reset_and_independent_libraries(tmp_path):
    store = PresentationStore(tmp_path)
    assert store.load() == (ReadingPresentation(), None) and not (tmp_path / 'state').exists()
    saved, token = store.save(PROFILE, expected_token=None)
    assert PresentationStore(tmp_path).load() == (PROFILE, token)
    assert token == sha256(store.path.read_bytes()).hexdigest()
    saved, token = store.save(ReadingPresentation(), expected_token=token)
    assert PresentationStore(tmp_path).load() == (ReadingPresentation(), token)
    other = tmp_path / 'another'; other.mkdir()
    assert PresentationStore(other).load() == (ReadingPresentation(), None)


def test_exact_compare_before_save_rejects_other_window_and_format_only_changes(tmp_path):
    store = PresentationStore(tmp_path)
    _, first = store.save(PROFILE, expected_token=None)
    changed = replace(PROFILE, zoom=150)
    _, latest = store.save(changed, expected_token=first)
    raw = store.path.read_bytes()
    with pytest.raises(ValueError, match='Another window'):
        store.save(PROFILE, expected_token=first)
    assert store.path.read_bytes() == raw
    store.path.write_bytes(json.dumps(changed.to_dict(), indent=2).encode('utf-8'))
    with pytest.raises(ValueError, match='Another window'):
        store.save(PROFILE, expected_token=latest)
    current, token = store.load()
    assert current == changed and token != latest


@pytest.mark.parametrize(('field', 'value'), [
    ('zoom', True), ('zoom', 49), ('zoom', 301), ('zoom', 100.0),
    ('foreground', [1, 2]), ('foreground', [1, 2, 256]), ('foreground', [True, 2, 3]),
    ('background', '#ffffff'), ('font', {'face': 'untyped'}), ('schema', 1),
])
def test_typed_profile_rejects_unbounded_or_ambiguous_values(field, value):
    data = PROFILE.to_dict(); data[field] = value
    with pytest.raises(ValueError): ReadingPresentation.from_dict(data)


@pytest.mark.parametrize('kwargs', [
    {'face': ''}, {'face': 'a' * 129}, {'face': 'bad\nfont'}, {'face': '\ud800'},
    {'points': float('nan')}, {'points': float('inf')}, {'points': True}, {'points': 5.9}, {'points': 72.1},
    {'weight': True}, {'weight': 0}, {'weight': 1001}, {'style': 'execute'},
])
def test_native_font_fields_are_finite_and_bounded(kwargs):
    values = dict(face='Installed fixture font', points=12)
    values.update(kwargs)
    with pytest.raises(ValueError): ReadingFont(**values)


@pytest.mark.parametrize('kind', ['unknown', 'duplicate', 'nonfinite', 'oversized'])
def test_invalid_saved_profile_is_preserved_instead_of_silently_reset(tmp_path, kind):
    store = PresentationStore(tmp_path); store.path.parent.mkdir(parents=True)
    raw = json.dumps(PROFILE.to_dict()).encode('utf-8')
    if kind == 'unknown': raw = raw.replace(b'"schema":', b'"execute": true, "schema":')
    elif kind == 'duplicate': raw = raw.replace(b'"zoom": 125', b'"zoom": 125, "zoom": 125')
    elif kind == 'nonfinite': raw = raw.replace(b'12.5', b'NaN')
    else: raw += b' ' * core.MAX_PROFILE_BYTES
    store.path.write_bytes(raw)
    with pytest.raises(ValueError): store.load()
    with pytest.raises(ValueError): store.save(ReadingPresentation(), expected_token=None)
    assert store.path.read_bytes() == raw


def test_growth_between_stat_and_open_is_rejected_before_read(tmp_path, monkeypatch):
    store = PresentationStore(tmp_path); store.save(PROFILE, expected_token=None)
    original = core._open_json_file
    def enlarged(path):
        path.write_bytes(b' ' * (core.MAX_PROFILE_BYTES + 1))
        return original(path)
    monkeypatch.setattr(core, '_open_json_file', enlarged)
    with pytest.raises(ValueError, match='changed during load'): store.load()


def test_replaced_library_and_linked_state_are_not_write_authority(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    store = PresentationStore(root)
    root.rename(tmp_path / 'previous'); root.mkdir()
    with pytest.raises(ValueError, match='directory changed'):
        store.save(PROFILE, expected_token=None)
    assert not list(root.iterdir())
    external = tmp_path / 'outside'; external.mkdir()
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(external), str(root / 'state'))
    else:
        (root / 'state').symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError): PresentationStore(root).save(PROFILE, expected_token=None)
    assert not list(external.iterdir())


def test_actual_backup_delete_restore_preserves_profile_text_source_history_and_cursor(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека 😀')
    app.create_workspace('Appearance recovery')
    document = ReadingDocument('😀 Original\r\nДругий рядок.', title='Source stays exact')
    document.set_text('😀 Reading edit\r\nДругий рядок.')
    draft = ReaderDraftStore(app.root)
    draft.save(ReaderResume(document, cursor=5))
    original_draft = draft.path.read_bytes()
    store = PresentationStore(app.root); _, token = store.save(PROFILE, expected_token=None)
    assert draft.path.read_bytes() == original_draft
    backup = tmp_path / 'backup'; checksum = app.backup(backup)
    shutil.rmtree(app.root)
    restored = tmp_path / 'relocated'
    restore_backup(backup, restored, expected_manifest_sha256=checksum)
    assert PresentationStore(restored).load() == (PROFILE, token)
    session = ReaderDraftStore(restored)
    assert session.path.read_bytes() == original_draft
    recovered = session.load()
    assert recovered.cursor == 5 and recovered.document.text == document.text
    assert recovered.document.source_text == '😀 Original\r\nДругий рядок.'
    assert recovered.document.undo() and recovered.document.text == recovered.document.source_text
