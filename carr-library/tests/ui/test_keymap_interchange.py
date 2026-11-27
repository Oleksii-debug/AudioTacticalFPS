"""Real bounded preference files, CAS and backup; no native/NVDA claim."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.keyboard_settings import KeyboardSettings, MAX_SETTINGS_BYTES, effective_shortcuts

# Installed verification uses pytest importlib mode and does not add tests/ to
# sys.path. Load only this known fixture file; production imports stay installed.
_spec = importlib.util.spec_from_file_location('keymap_menu_fixtures', Path(__file__).with_name('test_keyboard_settings.py'))
_menu_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_menu_fixtures)
Frame, Item, frame, wx = _menu_fixtures.Frame, _menu_fixtures.Item, _menu_fixtures.frame, _menu_fixtures.wx


def keymap(path, choices, originals=None):
    defaults = {'file/save': 'Ctrl+S', 'file/exit': 'Alt+F4', 'library/new library': 'Ctrl+N',
                'library/import books': 'Ctrl+I', 'help/keyboard guide': 'F1'}
    value = {'schema': 1, 'overrides': choices,
             'originals': originals if originals is not None else {key: defaults[key] for key in choices}}
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def state_path(controller):
    return controller.root / 'state/ui/shortcuts.json'


def test_review_merge_and_complete_undo_keep_unmentioned_saved_choices(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'library/new library': 'F9', 'help/keyboard guide': ''})
    before, table = state_path(controller).read_bytes(), frame.table
    source = keymap(tmp_path / 'Клавіші 😀.json', {'file/save': 'Ctrl+Shift+S'})
    raw = source.read_bytes()
    plan = controller.preview_import(source)
    assert not plan.issues and plan.source_token == sha256(raw).hexdigest()
    assert 'File / Save [file/save]: Ctrl+S → Ctrl+Shift+S' in plan.description
    assert state_path(controller).read_bytes() == before and frame.table == table
    assert source.read_bytes() == raw
    undo = controller.apply_import(plan)
    assert controller.overrides == {'library/new library': 'F9', 'help/keyboard guide': '', 'file/save': 'Ctrl+Shift+S'}
    assert json.loads(state_path(controller).read_bytes())['overrides'] == controller.overrides
    controller.undo(undo)
    assert state_path(controller).read_bytes() == before and frame.table == table
    assert source.read_bytes() == raw


def test_implicit_default_collision_is_explicitly_reviewed_before_unassignment(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'library/new library': 'F9'})
    source = keymap(tmp_path / 'collision.json', {'file/save': 'Ctrl+I'})
    plan = controller.preview_import(source)
    assert not plan.issues and len(plan.notices) == 1
    assert 'Default made unassigned: Library / Import books: Ctrl+I is used by File / Save' in plan.description
    assert effective_shortcuts(controller.commands, controller.overrides)['library/import books'] == 'Ctrl+I'
    controller.apply_import(plan)
    assert controller.overrides == {'file/save': 'Ctrl+I', 'library/new library': 'F9', 'library/import books': ''}
    assert len([row for row in frame.table if row[:2] == (wx.ACCEL_CTRL, ord('I'))]) == 1


def test_existing_explicit_choice_conflict_blocks_entire_import(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'library/new library': 'F9'})
    before, table = state_path(controller).read_bytes(), frame.table
    source = keymap(tmp_path / 'conflicting.json', {'file/save': 'F9', 'library/import books': 'F8'})
    plan = controller.preview_import(source)
    assert plan.issues and 'already assigned' in plan.description
    with pytest.raises(ValueError, match='every reported import conflict'):
        controller.apply_import(plan)
    assert state_path(controller).read_bytes() == before and frame.table == table


@pytest.mark.parametrize('case', ['unknown', 'changed_original', 'reserved', 'duplicate_explicit'])
def test_incompatible_commands_are_explained_without_partial_apply(frame, wx, tmp_path, case):
    controller = KeyboardSettings(frame, wx_module=wx)
    choices, originals = {'file/save': 'F9'}, {'file/save': 'Ctrl+S'}
    if case == 'unknown':
        choices['library/removed command'] = 'F8'; originals['library/removed command'] = ''
    elif case == 'changed_original':
        originals['file/save'] = 'Ctrl+Alt+S'
    elif case == 'reserved':
        choices['file/save'] = 'Ctrl+C'
    else:
        choices['library/new library'] = 'F9'; originals['library/new library'] = 'Ctrl+N'
    plan = controller.preview_import(keymap(tmp_path / 'bad.json', choices, originals))
    assert plan.issues
    with pytest.raises(ValueError, match='conflict'):
        controller.apply_import(plan)
    assert controller.overrides == {} and not state_path(controller).exists()


def test_empty_import_merges_nothing_and_reset_remains_full_map_undoable(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'file/save': 'F9', 'library/import books': ''})
    before = state_path(controller).read_bytes()
    plan = controller.preview_import(keymap(tmp_path / 'empty.json', {}))
    assert 'no explicit shortcut choices' in plan.description
    controller.apply_import(plan)
    assert state_path(controller).read_bytes() == before
    undo = controller.apply_with_undo({})
    assert controller.overrides == {}
    controller.undo(undo)
    assert state_path(controller).read_bytes() == before


def test_source_same_size_and_timestamp_mutation_invalidates_review(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'race.json', {'file/save': 'F8'})
    plan, stamp = controller.preview_import(source), source.stat()
    source.write_bytes(source.read_bytes().replace(b'F8', b'F9'))
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError, match='changed after review'):
        controller.apply_import(plan)
    assert not state_path(controller).exists()


@pytest.mark.parametrize('operation', ['import', 'undo', 'export'])
def test_another_saved_window_blocks_review_undo_and_export(frame, wx, tmp_path, operation):
    controller = KeyboardSettings(frame, wx_module=wx)
    undo = controller.apply_with_undo({'file/save': 'F8'})
    plan = controller.preview_import(keymap(tmp_path / 'map.json', {'file/save': 'F9'}))
    other = KeyboardSettings(Frame(frame.library.root), wx_module=wx)
    other.apply({'file/save': 'F12'})
    current, table = state_path(controller).read_bytes(), frame.table
    action = {'import': lambda: controller.apply_import(plan), 'undo': lambda: controller.undo(undo),
              'export': lambda: controller.export_keymap(tmp_path / 'export.json')}[operation]
    with pytest.raises(ValueError, match='Another window'):
        action()
    assert state_path(controller).read_bytes() == current and frame.table == table
    assert not (tmp_path / 'export.json').exists()


@pytest.mark.parametrize('change', ['same_root_library', 'new_root', 'menu_id', 'menu_replacement', 'menu_move',
                                  'live_shortcut', 'closing'])
def test_review_cannot_cross_changed_library_or_command_identity(frame, wx, tmp_path, change):
    controller = KeyboardSettings(frame, wx_module=wx)
    plan = controller.preview_import(keymap(tmp_path / 'map.json', {'file/save': 'F9'}))
    if change == 'same_root_library':
        frame.library = SimpleNamespace(root=controller.root)
    elif change == 'new_root':
        target = tmp_path / 'new-root'; target.mkdir()
        frame.library.root = target
    elif change == 'menu_id':
        frame.menu_bar.GetMenu(0).items[0].identity += 10000
    elif change == 'menu_replacement':
        frame.menu_bar.GetMenu(0).items[0] = Item('&Save\tCtrl+S')
    elif change == 'menu_move':
        frame.menu_bar.GetMenu(1).items.append(frame.menu_bar.GetMenu(0).items.pop(0))
    elif change == 'live_shortcut':
        frame.menu_bar.GetMenu(0).items[0].SetItemLabel('&Save\tF12')
    else:
        frame._closing = True
    with pytest.raises(ValueError, match='changed|closing'):
        controller.apply_import(plan)
    assert not state_path(controller).exists()


def test_forged_or_replaced_preview_cannot_apply_unreviewed_choices(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    plan = controller.preview_import(keymap(tmp_path / 'map.json', {'file/save': 'F9'}))
    with pytest.raises(ValueError, match='changed after review'):
        controller.apply_import(replace(plan, overrides=(('file/save', 'F12'),)))
    assert not state_path(controller).exists()


@pytest.mark.parametrize('case', ['oversize', 'too_many', 'duplicate_json', 'schema_bool', 'non_string', 'nonfinite'])
def test_bounded_structural_intake_rejects_without_mutating_state(frame, wx, tmp_path, monkeypatch, case):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'invalid.json', {'file/save': 'F9'})
    if case == 'oversize':
        source.write_bytes(b' ' * (MAX_SETTINGS_BYTES + 1))
        real_open = Path.open
        def bounded(path, *args, **kwargs):
            assert path != source, 'Oversize keymap must be rejected before opening its contents.'
            return real_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', bounded)
    elif case == 'too_many':
        choices = {f'fake/{i}': '' for i in range(513)}
        keymap(source, choices, choices)
    elif case == 'duplicate_json':
        source.write_bytes(b'{"schema":1,"schema":1,"overrides":{},"originals":{}}')
    elif case == 'schema_bool':
        source.write_bytes(source.read_bytes().replace(b'"schema": 1', b'"schema": true'))
    elif case == 'non_string':
        keymap(source, {'file/save': []})
    elif case == 'nonfinite':
        source.write_bytes(b'{"schema":NaN,"overrides":{},"originals":{}}')
    with pytest.raises(ValueError):
        controller.preview_import(source)
    assert controller.overrides == {} and not state_path(controller).exists()


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'utf-16', 'utf-32'])
def test_existing_schema1_bom_encodings_load_and_import_without_rewrite(frame, wx, tmp_path, encoding):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'legacy.json', {'file/save': 'F9'})
    raw = source.read_text(encoding='utf-8').encode(encoding)
    source.write_bytes(raw)
    # The original schema 1 decoder accepted these exact bytes; retain that
    # contract both for an existing saved preference and the new import.
    assert json.loads(raw)['overrides'] == {'file/save': 'F9'}
    path = state_path(controller); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    assert controller.reload() is True and controller.overrides == {'file/save': 'F9'}
    assert path.read_bytes() == raw
    controller.export_keymap(tmp_path / 'canonical.json')
    exported = (tmp_path / 'canonical.json').read_bytes()
    assert exported.startswith(b'{') and json.loads(exported.decode('utf-8'))['overrides'] == controller.overrides
    other = KeyboardSettings(Frame(tmp_path / 'fresh-library'), wx_module=wx)
    other.apply_import(other.preview_import(source))
    assert other.overrides == controller.overrides and state_path(other).read_bytes() == exported
    assert source.read_bytes() == raw and path.read_bytes() == raw


def test_selected_hardlink_rejected_before_file_contents_are_opened(frame, wx, tmp_path, monkeypatch):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'original.json', {'file/save': 'F9'})
    linked = tmp_path / 'linked.json'; os.link(source, linked)
    real_open = Path.open
    def no_link_read(path, *args, **kwargs):
        assert path not in (source, linked), 'Selected linked file contents must not be opened.'
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', no_link_read)
    with pytest.raises(ValueError, match='links'):
        controller.preview_import(linked)


@pytest.mark.parametrize('operation', ['preview', 'apply', 'export'])
def test_explicit_cancellation_has_no_preference_or_export_side_effect(frame, wx, tmp_path, operation):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'map.json', {'file/save': 'F9'})
    plan = controller.preview_import(source)
    actions = {'preview': lambda: controller.preview_import(source, cancelled=lambda: True),
               'apply': lambda: controller.apply_import(plan, cancelled=lambda: True),
               'export': lambda: controller.export_keymap(tmp_path / 'cancelled.json', cancelled=lambda: True)}
    with pytest.raises((InterruptedError, TimeoutError), match='cancelled'):
        actions[operation]()
    assert not state_path(controller).exists() and not (tmp_path / 'cancelled.json').exists()
    assert not list(tmp_path.glob('.research-export-*'))


def test_export_uses_existing_schema_stable_identity_and_no_clobber(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'file/save': 'Ctrl+Shift+S', 'library/import books': ''})
    destination = tmp_path / 'Клавіші.json'
    controller.export_keymap(destination)
    assert destination.read_bytes() == state_path(controller).read_bytes()
    raw = destination.read_bytes()
    with pytest.raises(FileExistsError, match='already exists'):
        controller.export_keymap(destination)
    assert destination.read_bytes() == raw
    with pytest.raises(ValueError, match='outside the private library'):
        controller.export_keymap(frame.library.root / 'export.json')
    moved = Frame(tmp_path / 'other-library')
    moved.menu_bar.GetMenu(0).items[0].label = 'Save research notes...\tCtrl+S'
    moved.menu_bar.GetMenu(0).items[0].carr_command_name = 'save'
    target = KeyboardSettings(moved, wx_module=wx)
    assert target.commands[0].menu_id != controller.commands[0].menu_id
    target.apply_import(target.preview_import(destination))
    assert target.overrides == controller.overrides
    assert moved.menu_bar.GetMenu(0).items[0].label == 'Save research notes...\tCtrl+Shift+S'


def test_keymap_roundtrips_actual_manifest_bound_library_backup(wx, tmp_path):
    original = LibraryApp(tmp_path / 'Оригінальна бібліотека')
    original.create_workspace('Keyboard preservation')
    frame = Frame(original.root); frame.library = original
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply_import(controller.preview_import(keymap(tmp_path / 'map.json', {'file/save': 'F9'})))
    raw = state_path(controller).read_bytes()
    backup = tmp_path / 'Резервна копія'
    digest = create_backup(original, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    entry, = [row for row in manifest['state'] if row['filename'] == 'ui/shortcuts.json']
    assert entry == {'filename': 'ui/shortcuts.json', 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
    restored = LibraryApp(restore_backup(backup, tmp_path / 'Відновлена бібліотека', digest))
    restored_frame = Frame(restored.root); restored_frame.library = restored
    reopened = KeyboardSettings(restored_frame, wx_module=wx)
    assert state_path(reopened).read_bytes() == raw and reopened.overrides == {'file/save': 'F9'}
    assert reopened.commands[0].menu_id != controller.commands[0].menu_id
