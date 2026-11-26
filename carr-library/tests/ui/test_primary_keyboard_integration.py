"""Actual primary menu installers with explicit native-menu doubles.

These checks exercise shared registry discovery, conflicts, persistence and
dispatch. Real modal key routing remains in the Windows native smoke script.
"""
import itertools
import json
import sys
from types import SimpleNamespace

import pytest

from carr_library.keyboard_preferences import KeymapStore, MAX_BYTES, MAX_IMPORT_BYTES
from carr_library.ui import assistant_dialog, document_conversion, save_actions, speech_dialog, studio_dialog
from carr_library.ui.keyboard_settings import install_keyboard_settings


class MenuItem:
    def __init__(self, identity, label, *, separator=False):
        self.identity, self.label, self.separator = identity, label, separator

    def GetId(self): return self.identity
    def GetItemLabel(self): return self.label
    def SetItemLabel(self, value): self.label = value
    def IsSeparator(self): return self.separator
    def GetSubMenu(self): return None
    def IsEnabled(self): return True


class Menu:
    ids = itertools.count(1000)
    def __init__(self): self.items = []
    def Append(self, identity, label):
        item = MenuItem(next(self.ids) if identity < 0 else identity, label)
        self.items.append(item)
        return item
    def AppendSeparator(self): self.items.append(MenuItem(next(self.ids), '', separator=True))
    def GetMenuItems(self): return self.items


class Bar:
    def __init__(self): self.menus = []
    def Append(self, menu, label): self.menus.append((label, menu))
    def Insert(self, index, menu, label): self.menus.insert(index, (label, menu))
    def GetMenuCount(self): return len(self.menus)
    def GetMenu(self, index): return self.menus[index][1]
    def GetMenuLabel(self, index): return self.menus[index][0]
    def IsEnabledTop(self, index): return True


class Event:
    def __init__(self, event_type, identity): self.identity = identity
    def SetEventObject(self, source): self.source = source
    def GetId(self): return self.identity


class Timer:
    def __init__(self, frame): self.frame = frame
    def Start(self, interval): self.interval = interval


INSTALLERS = (save_actions.install_save_actions, speech_dialog.install_speech_actions,
              studio_dialog.install_studio_actions, assistant_dialog.install_assistant_actions,
              document_conversion.install_conversion_actions)
PRIMARY = {'file.save', 'file.save_all', 'file.recover_note', 'edit.undo', 'edit.redo', 'edit.cut',
           'edit.copy', 'edit.paste', 'edit.select_all', 'speech.reader', 'speech.book',
           'studio.open', 'studio.convert', 'studio.engines', 'assistant.open', 'assistant.providers',
           'conversion.documents', 'conversion.formats'}


@pytest.fixture
def frame(tmp_path, monkeypatch):
    menu_event = SimpleNamespace(typeId=1)
    focused = SimpleNamespace(copies=0)
    focused.Copy = lambda: setattr(focused, 'copies', focused.copies + 1)
    wx = SimpleNamespace(Menu=Menu, Timer=Timer, ID_ANY=-1, ID_EXIT=9, EVT_MENU=menu_event,
        EVT_TIMER=object(), CommandEvent=Event, CallAfter=lambda fn, *args: fn(*args),
        Window=SimpleNamespace(FindFocus=lambda: focused))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda key, identity: (0, 0, int(identity)))
    bar, handlers, notices = Bar(), {}, []
    frame = SimpleNamespace(menu_bar=bar, library=SimpleNamespace(root=tmp_path),
        working=False, busy=False, _closing=False, active=None, dirty=True, saved=0, enabled=True,
        focused=focused, report=notices.append, notices=notices)
    def bind(event_type, handler, source):
        if event_type is menu_event:
            handlers[source.GetId()] = handler
    def dispatch(event):
        handler = handlers.get(event.GetId())
        if handler is None:
            return False
        handler(event)
        return True
    def saved():
        frame.saved += 1
        frame.dirty = False
    frame.Bind = bind
    frame.GetMenuBar = lambda: bar
    frame.GetEventHandler = lambda: frame
    frame.ProcessEvent = dispatch
    frame.IsEnabled = lambda: frame.enabled
    frame._dirty = lambda: frame.dirty
    frame.evidence = SimpleNamespace(save_note=saved)
    frame.focused_controls = []
    frame.choice = SimpleNamespace(SetFocus=lambda: frame.focused_controls.append('library'))
    frame.status = SimpleNamespace(SetFocus=lambda: frame.focused_controls.append('status'))
    return frame


@pytest.mark.parametrize('manager_first', [False, True])
def test_all_primary_actions_are_registered_once_before_or_after_manager_install(frame, manager_first):
    if manager_first:
        install_keyboard_settings(frame)
    for install in INSTALLERS:
        install(frame)
    manager = install_keyboard_settings(frame)
    assert PRIMARY <= manager.registry.specs.keys()
    assert PRIMARY <= manager.bindings.keys()
    assert all(manager.available(identity) for identity in PRIMARY)
    assert manager.shortcut('file.save') == 'Ctrl+S'
    assert manager.shortcut('speech.reader') == 'Ctrl+Alt+V'
    assert manager.shortcut('studio.open') == 'Ctrl+Alt+A'
    assert manager.shortcut('assistant.open') == 'Ctrl+Alt+J'
    before = tuple(frame.menu_bar.menus)
    for install in INSTALLERS:
        install(frame)
    assert tuple(frame.menu_bar.menus) == before
    assert len({label.replace('&', '') for label, _ in before}) == len(before)
    labels = [item.GetItemLabel() for _, menu in before for item in menu.GetMenuItems()]
    shortcuts = [label.split('\t', 1)[1] for label in labels if '\t' in label]
    assert len(shortcuts) == len(set(shortcuts))


def installed(frame):
    for install in INSTALLERS:
        install(frame)
    return install_keyboard_settings(frame)


def test_primary_remap_persists_semantic_id_and_keeps_original_handler(frame, monkeypatch):
    manager = installed(frame)
    opened = []
    monkeypatch.setattr(studio_dialog, 'show_studio', opened.append)
    manager.apply_overrides({'studio.open': 'Ctrl+Alt+Shift+A', 'file.save': 'Ctrl+Shift+S'})
    assert KeymapStore(manager.store.path).load() == {
        'studio.open': 'Ctrl+Alt+Shift+A', 'file.save': 'Ctrl+Shift+S'}
    assert manager.bindings['studio.open'].item.GetItemLabel().endswith('\tCtrl+Alt+Shift+A')
    assert manager.bindings['file.save'].item.GetItemLabel().endswith('\tCtrl+Shift+S')
    assert manager.invoke('studio.open') and opened == [frame]
    assert manager.invoke('file.save') and frame.saved == 1
    assert not frame.dirty


@pytest.mark.parametrize('key', ['Ctrl+A', 'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+Z', 'Ctrl+Y',
                               'Ctrl+Left', 'Shift+Left', 'Insert', 'CapsLock'])
def test_editable_and_screen_reader_keys_cannot_be_captured_by_primary_remapping(frame, key):
    manager = installed(frame)
    labels = tuple(binding.item.GetItemLabel() for binding in manager.bindings.values())
    with pytest.raises(ValueError, match='reserved'):
        manager.apply_overrides({'studio.open': key})
    assert not manager.store.path.exists()
    assert tuple(binding.item.GetItemLabel() for binding in manager.bindings.values()) == labels


def test_primary_save_and_assistant_bindings_are_real_conflicts(frame):
    manager = installed(frame)
    for target in ('Ctrl+S', 'Ctrl+Alt+J'):
        with pytest.raises(ValueError, match='assigned to both'):
            manager.apply_overrides({'studio.open': target})
    assert not manager.store.path.exists()


def test_main_menu_invocation_is_unavailable_while_its_modal_child_disables_frame(frame):
    manager = installed(frame)
    frame.enabled = False
    assert not manager.invoke('file.save')
    assert not manager.invoke('studio.open')
    assert frame.saved == 0 and frame.dirty


def test_edit_command_dispatches_to_current_native_editor_without_binding_copy_keys(frame):
    manager = installed(frame)
    assert manager.shortcut('edit.copy') == ''
    assert manager.invoke('edit.copy')
    assert frame.focused.copies == 1


def test_focus_commands_use_the_canonical_registry_and_existing_native_controls(frame):
    manager = installed(frame)
    assert manager.shortcut('focus.library') == 'Ctrl+Alt+L'
    assert manager.shortcut('focus.status') == 'Ctrl+Alt+S'
    assert manager.invoke('focus.library') and manager.invoke('focus.status')
    assert frame.focused_controls == ['library', 'status']
    assert sum(label.replace('&', '') == 'Options' for label, _ in frame.menu_bar.menus) == 1


def test_conversion_commands_use_original_handlers_without_a_second_convert_menu(frame, monkeypatch):
    calls = []
    monkeypatch.setattr(document_conversion, 'convert_selected_documents', calls.append)
    manager = installed(frame)
    # Reinstalling is inert; the registered handler remains the existing menu's
    # handler. A new remap changes only that one item's accelerator label.
    before = tuple(frame.menu_bar.menus)
    document_conversion.install_conversion_actions(frame)
    assert tuple(frame.menu_bar.menus) == before
    manager.apply_overrides({'conversion.documents': 'Ctrl+Alt+D'})
    assert manager.bindings['conversion.documents'].item.GetItemLabel().endswith('\tCtrl+Alt+D')
    assert manager.available('conversion.documents') and manager.available('conversion.formats')
    assert manager.invoke('conversion.documents') and calls == [frame]


def test_earlier_profile_is_explicitly_imported_to_a_draft_then_saved_as_semantic_ids(frame):
    path = frame.library.root / 'state' / 'ui' / 'shortcuts.json'
    path.parent.mkdir(parents=True)
    legacy = {'schema': 1, 'overrides': {
        'audio studio/script and audio production': 'Ctrl+Alt+Shift+A',
        'voice/voice reader and reading copy': 'Ctrl+Alt+Shift+V',
        'file/save': 'Ctrl+Shift+S',
        'convert/convert documents for reading': 'Ctrl+Alt+D',
    }, 'originals': {
        'audio studio/script and audio production': 'Ctrl+Alt+A',
        'voice/voice reader and reading copy': 'Ctrl+Alt+V',
        'file/save': 'Ctrl+S',
        'convert/convert documents for reading': '',
    }}
    original = json.dumps(legacy).encode()
    path.write_bytes(original)
    manager = installed(frame)
    assert manager.overrides == {} and manager.shortcut('file.save') == 'Ctrl+S'
    labels = [binding.item.GetItemLabel() for binding in manager.bindings.values()]
    draft, notice = manager.import_keymap(path)
    assert draft == {'studio.open': 'Ctrl+Alt+Shift+A', 'speech.reader': 'Ctrl+Alt+Shift+V',
                     'file.save': 'Ctrl+Shift+S', 'conversion.documents': 'Ctrl+Alt+D'}
    assert 'draft' in notice and 'original file was kept' in notice
    assert manager.overrides == {} and not manager.store.path.exists()
    assert [binding.item.GetItemLabel() for binding in manager.bindings.values()] == labels
    assert path.read_bytes() == original
    manager.apply_overrides(draft)
    assert KeymapStore(manager.store.path).load() == draft
    assert json.loads(manager.store.path.read_bytes()) == {'version': 1, 'bindings': draft}
    assert path.read_bytes() == original


def test_current_profile_import_keeps_existing_live_bindings_until_save(frame, tmp_path):
    manager = installed(frame)
    manager.apply_overrides({'studio.open': 'Ctrl+Alt+Shift+A'})
    previous = manager.store.path.read_bytes()
    imported = tmp_path / 'chosen-profile.json'
    imported.write_text(json.dumps({'version': 1, 'bindings': {'speech.reader': 'Ctrl+Alt+Shift+V'}}))
    draft, notice = manager.import_keymap(imported)
    assert draft == {'speech.reader': 'Ctrl+Alt+Shift+V'} and 'draft' in notice
    assert manager.overrides == {'studio.open': 'Ctrl+Alt+Shift+A'}
    assert manager.shortcut('speech.reader') == 'Ctrl+Alt+V'
    assert manager.store.path.read_bytes() == previous


def test_legacy_help_paths_are_explicit_aliases_and_changed_defaults_are_reported(frame, tmp_path):
    manager = installed(frame)
    imported = tmp_path / 'previous-help-menu.json'
    imported.write_text(json.dumps({'schema': 1,
        'overrides': {'help/keyboard shortcuts': 'Ctrl+Alt+Shift+K',
                      'help/focus library and research topic': 'Ctrl+Alt+Shift+L',
                      'help/focus operation status': 'Ctrl+Alt+Shift+S'},
        'originals': {'help/keyboard shortcuts': 'Ctrl+Alt+K',
                      'help/focus library and research topic': 'Ctrl+Alt+L',
                      'help/focus operation status': 'Ctrl+Alt+Shift+U'}}))
    draft, notice = manager.import_keymap(imported)
    assert draft == {'options.keyboard': 'Ctrl+Alt+Shift+K',
                     'focus.library': 'Ctrl+Alt+Shift+L', 'focus.status': 'Ctrl+Alt+Shift+S'}
    assert '1 original defaults differ' in notice
    assert not manager.store.path.exists()


@pytest.mark.parametrize('overrides, originals', [
    ({'voice/renamed unknown action': 'Ctrl+Alt+Shift+V'}, {'voice/renamed unknown action': ''}),
    ({'file/save': 'Ctrl+C'}, {'file/save': 'Ctrl+S'}),
    ({'voice/voice reader and reading copy': 'Ctrl+S'}, {'voice/voice reader and reading copy': 'Ctrl+Alt+V'}),
    ({'help/keyboard shortcuts': '', 'options/keyboard shortcuts': ''},
     {'help/keyboard shortcuts': 'Ctrl+Alt+K', 'options/keyboard shortcuts': 'Ctrl+Alt+K'}),
    ({'file/save': 'Ctrl+Shift+S'}, {}),
], ids=['unknown-action', 'protected-edit-key', 'collision', 'duplicate-target', 'missing-original'])
def test_invalid_legacy_profile_does_not_change_current_profile_or_native_menus(frame, tmp_path, overrides, originals):
    manager = installed(frame)
    manager.apply_overrides({'studio.open': 'Ctrl+Alt+Shift+A'})
    previous = manager.store.path.read_bytes()
    labels = [binding.item.GetItemLabel() for binding in manager.bindings.values()]
    imported = tmp_path / 'invalid-legacy.json'
    original = json.dumps({'schema': 1, 'overrides': overrides, 'originals': originals}).encode()
    imported.write_bytes(original)
    with pytest.raises(ValueError):
        manager.import_keymap(imported)
    assert manager.store.path.read_bytes() == previous and imported.read_bytes() == original
    assert manager.overrides == {'studio.open': 'Ctrl+Alt+Shift+A'}
    assert [binding.item.GetItemLabel() for binding in manager.bindings.values()] == labels


@pytest.mark.parametrize('raw', [None, b'[]', b'{"schema":1,"schema":1,"overrides":{},"originals":{}}',
    b'{"schema":NaN,"overrides":{},"originals":{}}', b' ' * (MAX_IMPORT_BYTES + 1),
    b'{"version":1,"bindings":{}}' + b' ' * MAX_BYTES,
], ids=['missing-file', 'not-object', 'duplicate-key', 'nonfinite-number', 'import-size', 'canonical-size'])
def test_import_is_bounded_strict_and_never_writes_on_failure(frame, tmp_path, raw):
    manager = installed(frame)
    imported = tmp_path / 'invalid-profile.json'
    if raw is not None:
        imported.write_bytes(raw)
    with pytest.raises(ValueError):
        manager.import_keymap(imported)
    assert manager.overrides == {} and not manager.store.path.exists()
    assert imported.read_bytes() == raw if raw is not None else not imported.exists()
