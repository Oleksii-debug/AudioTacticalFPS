"""Real appearance handlers and persistence with explicit native-widget stand-ins."""
from dataclasses import replace
from threading import get_ident
from types import SimpleNamespace

import pytest

from carr_library.speech.presentation import PresentationStore, ReadingFont, ReadingPresentation
from carr_library.speech.reading import ReadingDocument
from carr_library.ui import reading_presentation as ui

# Reuse the pinned donor's two native-control stand-ins directly. The donor
# test_pronunciation_handlers module is not part of this composed test tree.
# Keep thread/destruction checks without importing its unrelated worker fixture.
class _NativeWidget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.owner = get_ident()
        self.value, self.label, self.choices = value, label, list(choices)
        self.selection, self.enabled, self.name = -1, True, ''
        self.destroyed, self.children, self.handlers = False, [], {}
        self.parent = parent if isinstance(parent, _NativeWidget) else None
        if isinstance(parent, _NativeWidget):
            parent.children.append(self)

    def native(self):
        assert get_ident() == self.owner, 'Native control accessed from the worker.'
        assert not self.destroyed, 'Native control accessed after destruction.'

    def SetName(self, name): self.native(); self.name = name
    def GetName(self): self.native(); return self.name
    def GetValue(self): self.native(); return self.value
    def ChangeValue(self, value): self.native(); self.value = value
    def SetValue(self, value): self.native(); self.value = value
    def Set(self, values): self.native(); self.choices = list(values); self.selection = -1
    def SetSelection(self, index): self.native(); self.selection = index
    def GetSelection(self): self.native(); return self.selection
    def Enable(self, enabled=True): self.native(); self.enabled = enabled
    def IsEnabled(self): self.native(); return self.enabled
    def Bind(self, event, handler, *args): self.native(); self.handlers[event] = handler
    def SetSizer(self, *args): self.native()
    def SetScrollRate(self, *args): self.native()
    def FitInside(self): self.native()
    def Wrap(self, *args): self.native()
    def SetFocus(self): self.native(); self.focused = True
    def IsModal(self): self.native(); return False
    def Hide(self): self.native(); self.hidden = True
    def Destroy(self):
        self.native()
        for child in tuple(self.children):
            child.Destroy()
        if self.parent is not None:
            self.parent.children.remove(self)
        self.destroyed = True


class _Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass

class Font:
    def __init__(self, other=None):
        self.face, self.points, self.weight, self.style = 'OS font', 10.0, 400, 0
        if other is not None: self.__dict__.update(other.__dict__)
    def IsOk(self): return True
    def SetFaceName(self, value): self.face = value; return True
    def GetFaceName(self): return self.face
    def SetFractionalPointSize(self, value): self.points = value
    def GetFractionalPointSize(self): return self.points
    def SetNumericWeight(self, value): self.weight = value
    def GetNumericWeight(self): return self.weight
    def SetStyle(self, value): self.style = value
    def GetStyle(self): return self.style
    def SetUnderlined(self, value): pass
    def SetStrikethrough(self, value): pass


class Colour:
    def __init__(self, *values): self.values = values
    def IsOk(self): return len(self.values) == 3
    def Red(self): return self.values[0]
    def Green(self): return self.values[1]
    def Blue(self): return self.values[2]
    def Alpha(self): return 255


class Widget(_NativeWidget):
    def __init__(self, parent=None, *args, initial=None, **kwargs):
        super().__init__(parent, *args, **kwargs)
        if initial is not None: self.value = initial
        self.font, self.foreground, self.background = Font(), Colour(), Colour()
        self.selection, self.cursor, self.reset_selection = (0, 0), 0, False
        self.focus_calls = 0
    def GetId(self): return id(self)
    def SetEscapeId(self, value): self.native(); self.escape = value
    def EndModal(self, result): self.native(); self.result = result
    def GetClassDefaultAttributes(self): self.native(); return SimpleNamespace(font=Font())
    def GetFont(self): self.native(); return self.font
    def SetFont(self, value):
        self.native(); self.font = Font(value)
        if self.reset_selection: self.selection, self.cursor = (0, 0), 0
        return False  # A valid no-op/unchanged native result must not become failure.
    def GetForegroundColour(self):
        self.native(); return self.foreground if self.foreground.IsOk() else Colour(0, 0, 0)
    def GetBackgroundColour(self):
        self.native(); return self.background if self.background.IsOk() else Colour(255, 255, 255)
    def SetForegroundColour(self, value): self.native(); self.foreground = value
    def SetBackgroundColour(self, value): self.native(); self.background = value
    def GetInsertionPoint(self): self.native(); return self.cursor
    def SetInsertionPoint(self, value): self.native(); self.selection, self.cursor = (value, value), value
    def SetSelection(self, first, last): self.native(); self.selection, self.cursor = tuple(sorted((first, last))), last
    def Refresh(self): self.native()
    def SetFocus(self): super().SetFocus(); self.focus_calls += 1


@pytest.fixture
def view(tmp_path):
    state = SimpleNamespace(allowed=True, picks=[], pick_action=lambda: None, pick_result=None,
                            chosen_font=Font(), chosen_color=Colour(10, 20, 30))
    def guard():
        if not state.allowed: raise ValueError('Reader source or library changed')
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL',
             'EXPAND', 'LEFT', 'TOP', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON',
             'EVT_SPINCTRL', 'EVT_CLOSE', 'EVT_TEXT', 'ID_OK', 'ID_CANCEL', 'VSCROLL')
    wx = SimpleNamespace(**{name: 1 << index for index, name in enumerate(names)})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'SpinCtrl', 'Button', 'ScrolledWindow'): setattr(wx, name, Widget)
    wx.BoxSizer, wx.Font, wx.Colour, wx.NullColour = _Sizer, Font, Colour, Colour()
    wx.FontEnumerator = SimpleNamespace(IsValidFacename=lambda face: face in ('OS font', 'Chosen font'))
    wx.FONTSTYLE_NORMAL, wx.FONTSTYLE_ITALIC, wx.FONTSTYLE_SLANT = 0, 1, 2
    class FontData:
        def SetAllowSymbols(self, value): self.symbols = value
        def EnableEffects(self, value): self.effects = value
        def SetRange(self, low, high): self.bounds = low, high
        def SetInitialFont(self, value): self.initial = value
        def GetChosenFont(self): return state.chosen_font
    class ColourData:
        def SetChooseFull(self, value): self.full = value
        def SetChooseAlpha(self, value): self.alpha = value
        def SetColour(self, value): self.initial = value
        def GetColour(self): return state.chosen_color
    class Picker(Widget):
        def __init__(self, parent, data):
            super().__init__(parent); self.data = data; state.picks.append(self)
        def ShowModal(self):
            self.native(); state.pick_action()
            return wx.ID_OK if state.pick_result is None else state.pick_result
        def GetFontData(self): return self.data
        def GetColourData(self): return self.data
    wx.FontData, wx.ColourData, wx.FontDialog, wx.ColourDialog = FontData, ColourData, Picker, Picker
    document = ReadingDocument('😀 Source\r\nДругий рядок.')
    document.set_text('😀 Reading edit\r\nДругий рядок.')
    editor = Widget(value=document.text)
    editor.SetSelection(4, 18)
    presenter = ui.NativeReadingPresentation(editor, wx)
    dialog = ui.create_reading_presentation(None, library_root=tmp_path, presenter=presenter,
                                            guard=guard, wx_module=wx)
    yield SimpleNamespace(dialog=dialog, state=state, store=PresentationStore(tmp_path), root=tmp_path,
                          wx=wx, editor=editor, presenter=presenter, document=document)
    dialog.alive = False
    if not dialog.destroyed: dialog.Destroy()
    editor.Destroy()


def choose(view):
    font = view.state.chosen_font
    font.face, font.points, font.weight, font.style = 'Chosen font', 14.5, 700, 1
    view.dialog.on_font(); view.dialog.on_color('foreground'); view.dialog.on_color('background')
    view.dialog.zoom.SetValue(150); view.dialog.on_changed()


def test_native_preview_apply_reset_preserve_exact_text_native_positions_source_and_history(view):
    before = view.document.to_dict()
    text, selection, cursor = view.editor.GetValue(), view.editor.GetSelection(), view.editor.GetInsertionPoint()
    choose(view)
    assert all(item.destroyed for item in view.state.picks)
    assert not view.store.path.exists() and view.presenter.current == ReadingPresentation()
    view.dialog.on_preview()
    assert view.dialog.buttons['apply'].enabled and view.dialog.sample.font.points == 21.75
    assert view.editor.font.points == 10 and view.presenter.current == ReadingPresentation()
    view.dialog.on_apply()
    saved = view.store.load()[0]
    assert saved.font.face == 'Chosen font' and saved.zoom == 150
    assert view.presenter.current == saved and view.editor.font.points == 21.75
    assert view.editor.GetValue() == text and view.editor.GetSelection() == selection
    assert view.editor.GetInsertionPoint() == cursor and view.editor.focus_calls == 0
    assert view.document.to_dict() == before
    view.dialog.on_reset()
    assert view.store.load()[0] == saved and view.presenter.current == saved
    view.dialog.on_apply()
    assert view.store.load()[0] == ReadingPresentation() and view.editor.font.points == 10
    assert not view.editor.foreground.IsOk() and not view.editor.background.IsOk(), 'Use native OS color defaults.'
    assert view.document.to_dict() == before and view.document.undo()
    assert view.document.text == view.document.source_text


@pytest.mark.parametrize('reverse', [False, True])
def test_font_change_restores_native_selection_and_caret_without_character_conversion(view, reverse):
    view.editor.SetSelection(*(18, 4) if reverse else (4, 18))
    expected = view.editor.GetSelection(), view.editor.GetInsertionPoint()
    view.editor.reset_selection = True
    view.presenter.apply(ReadingPresentation(zoom=200))
    assert (view.editor.GetSelection(), view.editor.GetInsertionPoint()) == expected


def test_silently_edited_zoom_invalidates_review_even_without_native_event(view):
    choose(view); view.dialog.on_preview()
    view.dialog.zoom.SetValue(175)
    view.dialog.on_apply()
    assert 'Preview the current' in view.dialog.status.value
    assert view.presenter.current == ReadingPresentation() and not view.store.path.exists()


def test_partial_zoom_edit_is_visible_and_cannot_escape_native_handler(view):
    view.dialog.zoom.SetValue(1)
    view.dialog.on_changed()
    assert '50 to 300' in view.dialog.details.value and not view.dialog.buttons['apply'].enabled
    view.dialog.on_preview(); view.dialog.on_apply()
    assert not view.store.path.exists() and view.presenter.current == ReadingPresentation()
    view.dialog.zoom.SetValue(150); view.dialog.on_changed(); view.dialog.on_preview(); view.dialog.on_apply()
    assert view.store.load()[0].zoom == 150


@pytest.mark.parametrize('method', ['on_font', 'on_color', 'on_reset', 'on_apply'])
def test_scope_change_and_closed_dialog_prevent_profile_or_reader_changes(view, method):
    choose(view); view.dialog.on_preview()
    previous_status = view.dialog.status.value
    view.state.allowed = False
    call = lambda: view.dialog.on_color('foreground') if method == 'on_color' else getattr(view.dialog, method)()
    call()
    assert view.dialog.status.value == previous_status and not view.store.path.exists()
    assert view.presenter.current == ReadingPresentation()
    view.dialog.on_close(); view.dialog.Destroy()
    call()  # NativeWidget.native rejects any access after destruction.
    assert not view.store.path.exists()


@pytest.mark.parametrize('kind', ['font', 'color'])
def test_native_picker_cancel_and_changed_scope_preserve_original_choices(view, kind):
    method = view.dialog.on_font if kind == 'font' else lambda: view.dialog.on_color('foreground')
    view.state.pick_result = view.wx.ID_CANCEL
    method(); assert view.dialog.current() == ReadingPresentation()
    assert view.state.picks[-1].destroyed
    view.state.pick_result = view.wx.ID_OK
    view.state.pick_action = lambda: setattr(view.state, 'allowed', False)
    method()
    assert view.dialog.current() == ReadingPresentation() and view.state.picks[-1].destroyed
    assert not view.store.path.exists()


def test_saved_font_missing_after_restore_stays_explicit_and_reset_is_reviewable(view):
    saved = ReadingPresentation(ReadingFont('Not installed here', 12), zoom=125)
    view.store.save(saved, expected_token=None)
    view.dialog.on_reload(); view.dialog.on_preview()
    assert 'not installed' in view.dialog.status.value and not view.dialog.buttons['apply'].enabled
    assert view.presenter.current == ReadingPresentation() and view.store.load()[0] == saved
    view.dialog.on_reset(); view.dialog.on_apply()
    assert view.store.load()[0] == ReadingPresentation()


def test_other_window_save_rolls_back_native_display_and_requires_reload(view):
    choose(view); view.dialog.on_preview()
    other = ReadingPresentation(zoom=180)
    view.store.save(other, expected_token=None)
    view.dialog.on_apply()
    assert 'Another window' in view.dialog.status.value and view.store.load()[0] == other
    assert view.presenter.current == ReadingPresentation() and view.editor.font.points == 10
    assert not view.editor.foreground.IsOk() and not view.editor.background.IsOk()
    view.dialog.on_reload(); view.dialog.on_preview(); view.dialog.on_apply()
    assert view.presenter.current == other and view.editor.font.points == 18


def test_profile_file_write_failure_rolls_back_display_without_touching_text(view, monkeypatch):
    choose(view); view.dialog.on_preview(); original = view.editor.GetValue()
    def fail(*args, **kwargs): raise OSError('synthetic disk failure')
    monkeypatch.setattr(ui.PresentationStore, 'save', fail)
    view.dialog.on_apply()
    assert 'disk failure' in view.dialog.status.value and not view.store.path.exists()
    assert view.presenter.current == ReadingPresentation() and view.editor.GetValue() == original


def test_controls_have_distinct_accessible_names_and_escape_closes(view):
    dialog = view.dialog
    controls = [dialog.zoom, dialog.details, dialog.sample, dialog.status, *dialog.buttons.values()]
    assert all(control.name for control in controls)
    assert len({control.name for control in controls}) == len(controls)
    assert dialog.escape == dialog.buttons['close'].GetId()


@pytest.mark.parametrize('kind', ['absent', 'valid', 'unavailable', 'invalid', 'no_library'])
def test_reader_startup_restores_only_display_and_preserves_invalid_profile(view, kind):
    profile = ReadingPresentation(zoom=150)
    if kind in ('valid', 'unavailable', 'invalid'):
        if kind == 'unavailable': profile = ReadingPresentation(ReadingFont('Not installed here', 12))
        view.store.save(profile, expected_token=None)
        if kind == 'invalid': view.store.path.write_bytes(b'{invalid profile')
    raw = view.store.path.read_bytes() if view.store.path.exists() else None
    editor = Widget(value='😀 Reading\r\nCopy'); editor.SetSelection(4, 14)
    try:
        restored, store, notice = ui.load_reading_presentation(editor, None if kind == 'no_library' else view.root, view.wx)
        assert restored.current == (profile if kind == 'valid' else ReadingPresentation())
        assert bool(notice) == (kind in ('unavailable', 'invalid'))
        assert editor.GetValue() == '😀 Reading\r\nCopy' and editor.GetSelection() == (4, 14)
        assert (view.store.path.read_bytes() if view.store.path.exists() else None) == raw
        assert (store is None) == (kind == 'no_library')
    finally:
        editor.Destroy()
