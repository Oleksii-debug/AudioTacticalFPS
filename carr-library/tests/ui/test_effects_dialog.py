"""Actual typed effect-dialog handlers with poison-aware native-widget stand-ins."""
from pathlib import Path
import runpy
from threading import get_ident
from types import SimpleNamespace

import pytest

from carr_library.audio import Effect
from carr_library.audio.effect_presets import EffectPresetStore
from carr_library.ui import effects_dialog as ui


@pytest.fixture
def view(tmp_path, monkeypatch):
    authored = runpy.run_path(str(Path(__file__).parents[1] / 'speech' / 'test_pronunciation_handlers.py'))
    Base = authored['Widget']
    state = SimpleNamespace(scripts={}, windows=[], forbidden=[], allowed=True, guard_action=None)
    class Widget(Base):
        def __init__(self, parent=None, *args, **kwargs):
            super().__init__(parent, *args, **kwargs)
            self.title = kwargs.get('title', args[1] if len(args) > 1 else '')
            if len(args) > 2:
                self.choices = list(args[2])
            self.result = None
            state.windows.append(self)
        def native(self):
            if self.destroyed or get_ident() != self.owner:
                state.forbidden.append((self.name, 'dead' if self.destroyed else 'wrong-thread'))
            super().native()
        def SetEscapeId(self, value): self.native(); self.escape = value
        def EndModal(self, value): self.native(); self.result = value
        def ShowModal(self):
            self.native()
            action = state.scripts.get(self.title)
            result = action(self) if action is not None else wx.ID_CANCEL
            return self.result if self.result is not None else result

    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL',
        'EXPAND', 'LEFT', 'TOP', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_LISTBOX',
        'EVT_LISTBOX_DCLICK', 'EVT_WINDOW_DESTROY', 'ID_OK', 'ID_CANCEL')
    wx = SimpleNamespace(**{name: 1 << index for index, name in enumerate(constants)})
    wx.NOT_FOUND = -1
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl', 'CheckBox', 'Button',
                 'ListBox', 'Choice', 'SingleChoiceDialog', 'TextEntryDialog'):
        setattr(wx, name, Widget)
    class Sizer(authored['Sizer']):
        def __init__(self, *args, **kwargs):
            super().__init__(*args)
    wx.BoxSizer = wx.GridSizer = Sizer
    monkeypatch.setitem(__import__('sys').modules, 'wx', wx)
    parent = Widget()
    root = tmp_path / 'library'; root.mkdir()

    def guard():
        if state.guard_action is not None:
            callback, state.guard_action = state.guard_action, None
            callback()
        if not state.allowed:
            raise ValueError('The selected library changed.')

    def named(dialog, name):
        pending = list(dialog.children)
        while pending:
            item = pending.pop(0)
            if item.name == name:
                return item
            pending.extend(item.children)
        raise AssertionError('Missing actual native control: ' + name)

    def press(dialog, name):
        control = named(dialog, name)
        assert control.enabled
        control.handlers[wx.EVT_BUTTON](None)

    def native_parent_delete(dialog):
        identity = dialog.GetId()
        Base.Destroy(dialog)
        dialog.handlers[wx.EVT_WINDOW_DESTROY](SimpleNamespace(
            GetId=lambda: identity, GetEventObject=lambda: SimpleNamespace(),
            Skip=lambda: None))

    yield SimpleNamespace(root=root, parent=parent, state=state, wx=wx, guard=guard,
                          named=named, press=press, native_parent_delete=native_parent_delete)
    if parent:
        parent.Destroy()
    assert not state.forbidden


def test_real_ordered_dialog_edit_bypass_undo_redo_and_apply(view):
    initial = (Effect('gain', gain_db=-6),)
    def edit(dialog):
        numeric = next(child for child in view.state.windows if child.parent in dialog.children
                       and child.name.startswith('gain db (dB)'))
        numeric.ChangeValue('-9')
        view.press(dialog, 'Apply effect')
        return view.wx.ID_OK
    def ordered(dialog):
        view.press(dialog, 'Edit selected effect')
        view.press(dialog, 'Toggle effect bypass')
        view.press(dialog, 'Undo chain change')
        view.press(dialog, 'Redo chain change')
        return view.wx.ID_OK
    view.state.scripts.update({'Ordered audio effects': ordered, 'Edit gain effect': edit})
    result = ui.show_effect_chain(view.parent, view.root, initial, guard=view.guard)
    assert result == (Effect('gain', gain_db=-9, enabled=False),)
    assert initial == (Effect('gain', gain_db=-6),)
    assert not list((view.root / 'state/studio/effect-presets').glob('*.json'))


def test_explicit_saved_preset_survives_cancel_and_loads_through_actual_store(view):
    chain = (Effect('gain', gain_db=-8),)
    def name(dialog):
        dialog.ChangeValue('Вечірня передача')
        return view.wx.ID_OK
    def ordered(dialog):
        view.press(dialog, 'Save chain as a new preset')
        assert 'Saved preset' in view.named(dialog, 'Effect chain status').value
        return view.wx.ID_CANCEL
    view.state.scripts.update({'Ordered audio effects': ordered, 'Save effect preset': name})
    assert ui.show_effect_chain(view.parent, view.root, chain, guard=view.guard) is None
    saved, = EffectPresetStore(view.root).list()
    assert saved.name == 'Вечірня передача' and saved.effects == chain
    def pick(dialog):
        dialog.SetSelection(3)  # Three existing built-ins precede the actual saved record.
        return view.wx.ID_OK
    def load(dialog):
        view.press(dialog, 'Load effect preset')
        return view.wx.ID_OK
    view.state.scripts.update({'Ordered audio effects': load, 'Load effect preset': pick})
    assert ui.show_effect_chain(view.parent, view.root, (), guard=view.guard) == chain


@pytest.mark.parametrize('index', [-1, 99, True])
def test_invalid_preset_selection_never_selects_the_last_available_chain(view, index):
    initial = (Effect('gain', gain_db=-6),)
    def pick(dialog):
        dialog.SetSelection(index)
        return view.wx.ID_OK
    def ordered(dialog):
        view.press(dialog, 'Load effect preset')
        assert 'Choose one listed' in view.named(dialog, 'Effect chain status').value
        return view.wx.ID_OK
    view.state.scripts.update({'Ordered audio effects': ordered, 'Load effect preset': pick})
    assert ui.show_effect_chain(view.parent, view.root, initial, guard=view.guard) == initial


@pytest.mark.parametrize('path', ['edit', 'save', 'load'])
@pytest.mark.parametrize('close_path', ['direct', 'parent'])
def test_nested_modal_native_owner_deletion_cannot_resume_controls_or_save(view, path, close_path):
    holder = {}
    def nested(dialog):
        owner = holder['owner']
        if close_path == 'direct':
            owner.Destroy()
        else:
            view.native_parent_delete(owner)
        return view.wx.ID_OK
    def ordered(dialog):
        holder['owner'] = dialog
        view.press(dialog, {'edit': 'Edit selected effect', 'save': 'Save chain as a new preset',
                            'load': 'Load effect preset'}[path])
        return view.wx.ID_OK
    view.state.scripts.update({'Ordered audio effects': ordered, 'Edit gain effect': nested,
                              'Save effect preset': nested, 'Load effect preset': nested})
    assert ui.show_effect_chain(view.parent, view.root, (Effect('gain'),), guard=view.guard) is None
    assert not list((view.root / 'state/studio/effect-presets').glob('*.json'))
    assert not view.state.forbidden


def test_changed_library_during_name_modal_cannot_write_a_preset(view):
    def name(dialog):
        dialog.ChangeValue('Do not save')
        view.state.allowed = False
        return view.wx.ID_OK
    def ordered(dialog):
        view.press(dialog, 'Save chain as a new preset')
        assert 'library changed' in view.named(dialog, 'Effect chain status').value
        return view.wx.ID_CANCEL
    view.state.scripts.update({'Ordered audio effects': ordered, 'Save effect preset': name})
    assert ui.show_effect_chain(view.parent, view.root, (Effect('gain'),), guard=view.guard) is None
    assert not list((view.root / 'state/studio/effect-presets').glob('*.json'))


@pytest.mark.parametrize('close_path', ['direct', 'parent'])
def test_all_queued_chain_actions_after_native_deletion_are_inert(view, close_path):
    def ordered(dialog):
        callbacks = [handler for window in tuple(view.state.windows)
                     for event, handler in window.handlers.items()
                     if event in (view.wx.EVT_BUTTON, view.wx.EVT_LISTBOX, view.wx.EVT_LISTBOX_DCLICK)]
        if close_path == 'direct':
            dialog.Destroy()
        else:
            view.native_parent_delete(dialog)
        for callback in callbacks:
            callback(None)
        assert dialog.Destroy() is False
        return view.wx.ID_OK
    view.state.scripts['Ordered audio effects'] = ordered
    assert ui.show_effect_chain(view.parent, view.root, (Effect('gain'),), guard=view.guard) is None
    assert not view.state.forbidden


def test_reset_guard_owner_deletion_never_reports_to_a_dead_effect_editor(view):
    def edit(dialog):
        view.state.guard_action = lambda: view.native_parent_delete(dialog)
        view.press(dialog, 'Reset numeric controls to defaults')
        return view.wx.ID_OK
    view.state.scripts['Edit gain effect'] = edit
    assert ui.edit_effect(view.parent, Effect('gain'), sample_rate=24000, guard=view.guard) is None
    assert not view.state.forbidden


def test_postpublication_warning_is_shown_instead_of_an_ordinary_preset_saved_claim(view, monkeypatch):
    from carr_library import durable_state
    def interrupted(path):
        raise OSError('Synthetic postpublication directory acknowledgement')
    monkeypatch.setattr(durable_state, 'sync_directory', interrupted)
    def name(dialog):
        dialog.ChangeValue('Present record')
        return view.wx.ID_OK
    def ordered(dialog):
        view.press(dialog, 'Save chain as a new preset')
        text = view.named(dialog, 'Effect chain status').value
        assert 'present and checked' in text and 'completion could not be confirmed' in text
        assert not text.startswith('Saved preset')
        return view.wx.ID_CANCEL
    view.state.scripts.update({'Ordered audio effects': ordered, 'Save effect preset': name})
    assert ui.show_effect_chain(view.parent, view.root, (Effect('gain'),), guard=view.guard) is None
    value, = EffectPresetStore(view.root).list()
    assert value.name == 'Present record' and value.effects == (Effect('gain'),)
