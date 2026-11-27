"""Real appearance handlers with native stand-ins and actual saved state."""
from dataclasses import replace
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from carr_library.reader.appearance import (ReaderAppearancePreferences, ReaderFont,
    load_appearance, save_appearance)
from carr_library.reader.narration import new_project, encode_project
from carr_library.ui import reader_appearance as ui


from tests.ui.appearance_fixture import Widget as BaseWidget, Sizer


class Font:
    def __init__(self, other=None):
        self.face, self.points, self.weight = 'System Font', 10.0, 400
        self.style, self.underlined, self.strikethrough = 0, False, False
        if other is not None:
            self.__dict__.update(vars(other))
    def IsOk(self): return True
    def GetFaceName(self): return self.face
    def GetFractionalPointSize(self): return self.points
    def GetNumericWeight(self): return self.weight
    def GetStyle(self): return self.style
    def GetUnderlined(self): return self.underlined
    def GetStrikethrough(self): return self.strikethrough
    def SetFaceName(self, value): self.face = value
    def SetFractionalPointSize(self, value): self.points = value
    def SetNumericWeight(self, value): self.weight = value
    def SetStyle(self, value): self.style = value
    def SetUnderlined(self, value): self.underlined = value
    def SetStrikethrough(self, value): self.strikethrough = value


class Colour:
    def __init__(self, value): self.value = value
    def IsOk(self): return True
    def Red(self): return int(self.value[1:3], 16)
    def Green(self): return int(self.value[3:5], 16)
    def Blue(self): return int(self.value[5:7], 16)


class Widget(BaseWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'initial' in kwargs: self.value = kwargs['initial']
        self.default_font = Font()
        self.font, self.foreground, self.background = None, None, None
    def GetDefaultAttributes(self): self.native(); return SimpleNamespace(font=self.default_font)
    def SetFont(self, value): self.native(); self.font = value
    def SetForegroundColour(self, value): self.native(); self.foreground = value
    def SetBackgroundColour(self, value): self.native(); self.background = value
    def Refresh(self): self.native()
    def SetEscapeId(self, value): self.native(); self.escape = value


@pytest.fixture
def view(tmp_path):
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL',
                 'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_CLOSE',
                 'EVT_TEXT', 'EVT_SPINCTRL', 'EVT_SYS_COLOUR_CHANGED', 'EVT_WINDOW_DESTROY',
                 'ID_CANCEL', 'ID_OK', 'ID_ANY', 'SYS_COLOUR_WINDOWTEXT', 'SYS_COLOUR_WINDOW')
    wx = SimpleNamespace(**{name: number + 10 for number, name in enumerate(constants)})
    for name in ('Dialog', 'ScrolledWindow', 'TextCtrl', 'StaticText', 'SpinCtrl', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer, wx.Font, wx.Colour = Sizer, Font, Colour
    wx.NullFont, wx.NullColour = object(), object()
    wx.FONTSTYLE_NORMAL, wx.FONTSTYLE_ITALIC, wx.FONTSTYLE_SLANT = 0, 1, 2
    wx.FontEnumerator = SimpleNamespace(IsValidFacename=lambda face: face in {'System Font', 'Local Test Face'})
    wx.SystemSettings = SimpleNamespace(GetColour=lambda which: Colour(
        '#FFFF00' if which == wx.SYS_COLOUR_WINDOWTEXT else '#000080'))
    state = SimpleNamespace(root=tmp_path, library=SimpleNamespace(root=tmp_path), closed=False,
        high_contrast=False, modal=lambda picker: None, accepted=True, picks=[], reports=[])
    class Data:
        def SetInitialFont(self, font): self.font = font
        def SetAllowSymbols(self, value): pass
        def EnableEffects(self, value): pass
        def SetRange(self, *args): pass
        def GetChosenFont(self): return self.font
        def SetColour(self, colour): self.colour = colour
        def GetColour(self): return self.colour
    class Picker(Widget):
        def __init__(self, parent, data):
            super().__init__(parent)
            self.data = data
            state.picks.append(self)
        def ShowModal(self):
            self.native()
            state.modal(self)
            return wx.ID_OK if state.accepted else wx.ID_CANCEL
        def GetFontData(self): self.native(); return self.data
        def GetColourData(self): self.native(); return self.data
    wx.FontData = wx.ColourData = Data
    wx.FontDialog = wx.ColourDialog = Picker
    owner, control = Widget(), Widget()
    document = new_project('Source 😀\r\nReading copy.\nNext paragraph.')
    control.value, control.selection = document.current_text, (7, 18)
    expected_library = state.library
    def guard():
        if state.library is not expected_library:
            raise ValueError('The library changed. Reopen its appearance settings.')
    binding = ui.ReaderAppearance(owner, control, state.root, guard=guard,
        alive=lambda: not state.closed, report=state.reports.append, wx_module=wx,
        contrast=lambda: state.high_contrast)
    dialog = ui.create_appearance_dialog(binding, binding.snapshot)
    binding.active_dialog = dialog
    result = SimpleNamespace(state=state, wx=wx, owner=owner, control=control, document=document,
                             binding=binding, dialog=dialog)
    try:
        yield result
    finally:
        binding.close()
        if dialog: dialog.Destroy()
        if owner: owner.Destroy()
        if control: control.Destroy()


def saved_path(view):
    return view.state.root / 'state/ui/reader-appearance.json'


def select_font(picker):
    picker.data.font.face, picker.data.font.points = 'Local Test Face', 13.5
    picker.data.font.style, picker.data.font.weight = 1, 700


def select_colour(picker):
    picker.data.colour = Colour('#123456')


def test_preview_explicit_save_reset_and_reopen_preserve_source_and_selection(view):
    before = encode_project(view.document), view.control.value, view.control.selection
    view.state.modal = select_font
    view.dialog.on_font()
    assert view.dialog.candidate.font == ReaderFont('Local Test Face', 13.5, 700, 'italic')
    view.state.modal = select_colour
    view.dialog.on_colour('foreground')
    view.dialog.zoom.SetValue(175); view.dialog.on_zoom()
    assert not saved_path(view).exists() and view.control.font is None
    view.dialog.on_save()
    saved = load_appearance(view.state.root)
    assert saved.preferences == view.dialog.candidate
    assert view.control.font.face == 'Local Test Face'
    assert view.control.font.points == 13.5 * 1.75
    assert view.control.foreground.value == '#123456'
    assert 'saved' in view.dialog.status.value
    raw = saved_path(view).read_bytes()
    view.dialog.on_reset()
    assert saved_path(view).read_bytes() == raw
    assert view.dialog.preview.font is view.wx.NullFont
    view.dialog.on_save()
    assert load_appearance(view.state.root).preferences == ReaderAppearancePreferences()
    assert view.control.font is view.wx.NullFont
    assert view.control.foreground is view.control.background is view.wx.NullColour
    assert (encode_project(view.document), view.control.value, view.control.selection) == before


@pytest.mark.parametrize('kind', ['font', 'foreground', 'background'])
def test_cancelled_native_choice_changes_neither_preview_nor_saved_state(view, kind):
    before = view.dialog.candidate
    view.state.accepted = False
    view.state.modal = select_font if kind == 'font' else select_colour
    view.dialog.on_font() if kind == 'font' else view.dialog.on_colour(kind)
    assert view.dialog.candidate == before and not saved_path(view).exists()
    assert len(view.state.picks) == 1 and not view.state.picks[0]


@pytest.mark.parametrize('contrast', [True, None])
def test_system_contrast_overrides_but_does_not_erase_saved_colours(view, contrast):
    prefs = ReaderAppearancePreferences(foreground='#010203', background='#FAFBFC')
    saved = save_appearance(view.state.root, prefs, expected_token=None)
    view.binding.snapshot = saved
    view.state.high_contrast = contrast
    view.binding.on_colours(SimpleNamespace(Skip=lambda: None))
    assert view.control.foreground.value == '#FFFF00' and view.control.background.value == '#000080'
    assert not view.dialog.buttons['foreground'].enabled
    view.dialog.on_colour('foreground')
    assert view.state.picks == []
    assert load_appearance(view.state.root) == saved
    view.state.high_contrast = False
    view.binding.on_colours(SimpleNamespace(Skip=lambda: None))
    assert view.control.foreground.value == '#010203' and view.control.background.value == '#FAFBFC'


def test_default_font_zoom_tracks_system_changes_without_cumulative_scaling(view):
    view.dialog.zoom.SetValue(150); view.dialog.on_save()
    assert view.control.font.points == 15
    view.control.default_font.points = 14
    for _ in range(3):
        view.binding.on_colours(SimpleNamespace(Skip=lambda: None))
    assert view.control.font.face == 'System Font' and view.control.font.points == 21


def test_missing_saved_font_keeps_choice_but_uses_current_system_font(view):
    prefs = ReaderAppearancePreferences(ReaderFont('Missing Font', 20), zoom_percent=125)
    saved = save_appearance(view.state.root, prefs, expected_token=None)
    view.binding.snapshot = saved
    view.binding.on_colours(SimpleNamespace(Skip=lambda: None))
    assert view.control.font.face == 'System Font' and view.control.font.points == 12.5
    assert 'unavailable' in view.binding.notice
    assert load_appearance(view.state.root) == saved


def test_other_window_save_is_rejected_without_changing_current_view(view):
    saved = save_appearance(view.state.root, ReaderAppearancePreferences(zoom_percent=200), expected_token=None)
    view.dialog.zoom.SetValue(175); view.dialog.on_save()
    assert 'Another window' in view.dialog.status.value
    assert load_appearance(view.state.root) == saved and view.control.font is None


@pytest.mark.parametrize('kind', ['font', 'foreground', 'background'])
@pytest.mark.parametrize('accepted', [True, False])
def test_native_owner_destruction_during_picker_never_reads_deleted_peer(view, kind, accepted):
    before = view.dialog.candidate
    def destroy(picker):
        event = SimpleNamespace(GetId=lambda: view.binding._native_id, Skip=lambda: None,
                                GetEventObject=lambda: object())
        view.binding.on_destroy(event)
        view.owner.Destroy()
        view.control.Destroy()
    view.state.accepted, view.state.modal = accepted, destroy
    view.dialog.on_font() if kind == 'font' else view.dialog.on_colour(kind)
    assert not view.dialog.alive and view.binding.closed and not view.owner
    assert view.dialog.candidate == before and not saved_path(view).exists()


@pytest.mark.parametrize('kind', ['font', 'foreground', 'background'])
def test_picker_return_cannot_retarget_another_library(view, kind):
    before = view.dialog.candidate
    view.state.modal = lambda picker: setattr(view.state, 'library', SimpleNamespace(root=view.state.root))
    view.dialog.on_font() if kind == 'font' else view.dialog.on_colour(kind)
    assert view.dialog.candidate == before and not saved_path(view).exists()
    assert 'library changed' in view.dialog.status.value


def test_cached_owner_id_ignores_child_events_and_late_bound_handlers(view):
    event = lambda identity: SimpleNamespace(GetId=lambda: identity, Skip=lambda: None,
                                            GetEventObject=lambda: object())
    view.binding.on_destroy(event(view.control.GetId()))
    view.dialog.on_destroy(event(view.dialog.preview.GetId()))
    assert not view.binding.closed and view.dialog.alive
    view.dialog.on_destroy(event(view.dialog._native_id))
    view.dialog.Destroy()
    view.dialog.on_font(); view.dialog.on_colour('foreground'); view.dialog.on_zoom()
    view.dialog.on_save(); view.dialog.on_reset(); view.dialog.on_system_font(); view.dialog.on_system_colours()
    assert not saved_path(view).exists()


def test_published_preferences_keep_truthful_status_after_actual_sync_failure(view, monkeypatch):
    from carr_library.audio import jobs
    view.dialog.zoom.SetValue(175)
    def fail(path): raise OSError('Actual state publication already finished.')
    monkeypatch.setattr(jobs, '_sync_directory', fail)
    view.dialog.on_save()
    assert load_appearance(view.state.root).preferences.zoom_percent == 175
    assert 'saved' in view.dialog.status.value and 'needs attention' in view.dialog.status.value
    assert 'save needs attention' not in view.dialog.status.value


@pytest.mark.parametrize('action', ['on_save', 'on_reset', 'on_system_font', 'on_system_colours'])
def test_stale_library_actions_are_explained_without_storage_or_view_change(view, action):
    view.state.library = SimpleNamespace(root=view.state.root)
    getattr(view.dialog, action)()
    assert 'library changed' in view.dialog.status.value
    assert not saved_path(view).exists() and view.control.font is None


def test_root_replacement_rejects_save_even_when_caller_guard_is_unchanged(view, tmp_path):
    # The current owner can stay alive while its directory is moved by a user.
    root = view.state.root
    previous = root.with_name(root.name + '-previous')
    root.rename(previous)
    root.mkdir()
    try:
        view.dialog.zoom.SetValue(175)
        view.dialog.on_save()
        assert 'library changed' in view.dialog.status.value
        assert not saved_path(view).exists()
        assert view.control.font is None
    finally:
        root.rmdir()
        previous.rename(root)


def test_partial_appearance_construction_destroys_its_created_native_window(view, monkeypatch):
    created = []
    original = view.wx.Dialog.__init__
    def observe(window, *args, **kwargs):
        original(window, *args, **kwargs)
        created.append(window)
    monkeypatch.setattr(view.wx.Dialog, '__init__', observe)
    def failed(*args, **kwargs):
        raise RuntimeError('Synthetic native child allocation failure.')
    monkeypatch.setattr(view.wx, 'ScrolledWindow', failed)
    with pytest.raises(RuntimeError, match='allocation'):
        ui.create_appearance_dialog(view.binding, view.binding.snapshot)
    assert len(created) == 1 and not created[0]
    assert not saved_path(view).exists()


def test_default_load_does_not_read_font_files_or_mutate_native_text(view):
    assert view.control.font is None
    assert view.control.foreground is view.control.background is None
    assert not saved_path(view).exists()
    before = view.control.value, view.control.selection, encode_project(view.document)
    view.binding.on_colours(SimpleNamespace(Skip=lambda: None))
    assert (view.control.value, view.control.selection, encode_project(view.document)) == before
    assert view.control.font is None
