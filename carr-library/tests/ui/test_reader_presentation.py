"""Guarded appearance changes on the same editor, with no document mutations."""
from types import SimpleNamespace
import sys

import pytest

from carr_library.speech.presentation import ReadingAppearance, ReadingAppearanceStore, ReadingFont
from carr_library.ui.reader_presentation import ReaderPresentationControls


class Font:
    def __init__(self, other=None):
        self.face, self.points, self.weight, self.style, self.underline = 'System', 10.0, 400, 0, False
        if other is not None:
            self.__dict__.update(other.__dict__)
    def SetFaceName(self, value):
        if value == 'Missing font':
            return False
        self.face = value
        return True
    def SetPointSize(self, value): self.points = value
    def SetNumericWeight(self, value): self.weight = value
    def SetStyle(self, value): self.style = value
    def SetUnderlined(self, value): self.underline = value
    def GetFractionalPointSize(self): return self.points
    def SetFractionalPointSize(self, value): self.points = value
    def IsOk(self): return True


class Editor:
    def __init__(self):
        self.font, self.foreground, self.background = Font(), '#111111', '#EEEEEE'
        self.text, self.selection, self.undo = 'A\r\nУкраїна 🧭 text', (3, 12), ['retained edit']
        self.refreshes = 0
    def GetFont(self): return self.font
    def GetForegroundColour(self): return self.foreground
    def GetBackgroundColour(self): return self.background
    def SetFont(self, value): self.font = value
    def SetForegroundColour(self, value): self.foreground = value
    def SetBackgroundColour(self, value): self.background = value
    def Refresh(self): self.refreshes += 1
    def SetValue(self, value): pytest.fail('appearance replaced document text')
    def Replace(self, *args): pytest.fail('appearance edited document text')
    def SetSelection(self, *args): pytest.fail('appearance moved the selection')


@pytest.fixture
def context(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Font=Font, Colour=lambda value: value,
        FONTSTYLE_NORMAL=0, FONTSTYLE_ITALIC=1, FONTSTYLE_SLANT=2))
    flags = dict(alive=True, busy=False, cancelled=False, revoked=False)
    reports = []
    def guard():
        if flags['revoked']:
            raise ValueError('source identity changed')
    host = SimpleNamespace(guard=guard, alive=lambda: flags['alive'], busy=lambda: flags['busy'],
        cancelled=lambda: flags['cancelled'], report=lambda message, **kw: reports.append(message))
    editor = Editor()
    controller = ReaderPresentationControls(None, editor, host, library_root=tmp_path)
    return editor, controller, flags, reports


def test_apply_uses_same_editor_preserving_text_positions_and_undo(context):
    editor, control, _, reports = context
    before = (editor.text, editor.selection, list(editor.undo))
    chosen = ReadingAppearance(ReadingFont('Segoe UI', 18, 700, 'italic'), '#FFFFFF', '#000000', 150)
    control.apply(chosen)
    assert control.current == control.store.load() == chosen
    assert (editor.text, editor.selection, editor.undo) == before
    assert editor.font.points == 27 and editor.font.weight == 700 and editor.font.style == 1
    assert (editor.foreground, editor.background) == ('#FFFFFF', '#000000')
    assert 'Saved for this library' in reports[-1]


def test_zoom_does_not_compound_and_reset_restores_native_baseline(context):
    editor, control, _, _ = context
    control.apply(ReadingAppearance(zoom=200))
    control.apply(ReadingAppearance(zoom=150))
    assert editor.font.points == 15
    control.apply(ReadingAppearance())
    assert editor.font.points == 10
    assert (editor.foreground, editor.background) == ('#111111', '#EEEEEE')


@pytest.mark.parametrize('flag,value', [('alive', False), ('busy', True), ('revoked', True)])
def test_revoked_busy_or_closed_reader_cannot_apply(context, flag, value):
    editor, control, flags, _ = context
    flags[flag] = value
    before = (editor.font.points, editor.refreshes)
    with pytest.raises(ValueError):
        control.apply(ReadingAppearance(zoom=200))
    assert (editor.font.points, editor.refreshes) == before
    assert not control.store.path.exists()


def test_preview_does_not_publish_or_modify_actual_editor(context):
    editor, control, _, _ = context
    preview = Editor()
    before = (editor.font.points, editor.refreshes)
    control._draw(preview, ReadingAppearance(zoom=300))
    assert preview.font.points == 30
    assert (editor.font.points, editor.refreshes) == before
    assert not control.store.path.exists()


def test_missing_font_cannot_change_saved_or_visible_appearance(context):
    editor, control, _, _ = context
    with pytest.raises(ValueError, match='unavailable'):
        control.apply(ReadingAppearance(ReadingFont('Missing font', 12)))
    assert editor.font.points == 10 and not control.store.path.exists()


def test_concurrent_saved_appearance_keeps_actual_editor_unchanged(context):
    editor, control, _, _ = context
    external = ReadingAppearanceStore(control.store.root)
    external.save(ReadingAppearance(zoom=125), expected=ReadingAppearance())
    with pytest.raises(ValueError, match='another window'):
        control.apply(ReadingAppearance(zoom=150))
    assert editor.font.points == 10 and external.load().zoom == 125


def test_new_reader_loads_saved_appearance_without_document_changes(context):
    editor, control, _, _ = context
    control.apply(ReadingAppearance(zoom=175))
    other = Editor()
    reopened = ReaderPresentationControls(None, other, control.host, library_root=control.store.root)
    assert reopened.current.zoom == 175 and other.font.points == 17.5
    assert (other.text, other.selection, other.undo) == (editor.text, editor.selection, editor.undo)


def test_unbound_manual_reader_applies_only_for_its_session(context):
    _, control, _, reports = context
    manual = ReaderPresentationControls(None, Editor(), control.host)
    manual.apply(ReadingAppearance(zoom=225))
    assert manual.current.zoom == 225 and manual.store is None
    assert 'reader session' in reports[-1] and not control.store.path.exists()


def test_native_failure_rolls_back_before_any_durable_publication(context, monkeypatch):
    editor, control, _, _ = context
    original = editor.SetBackgroundColour
    calls = []
    def fail_once(value):
        calls.append(value)
        if len(calls) == 1:
            raise RuntimeError('native colour failure')
        original(value)
    monkeypatch.setattr(editor, 'SetBackgroundColour', fail_once)
    with pytest.raises(RuntimeError, match='native colour'):
        control.apply(ReadingAppearance(ReadingFont('Segoe UI', 20), '#FFFFFF', '#000000', 200))
    assert editor.font.points == 10
    assert (editor.foreground, editor.background) == ('#111111', '#EEEEEE')
    assert control.current == ReadingAppearance() and not control.store.path.exists()


def test_a_resolved_font_is_not_resolved_again_after_saving(context, monkeypatch):
    editor, control, _, _ = context
    original = control._resolved
    calls = []
    def once(value):
        calls.append(value)
        if len(calls) > 1:
            raise RuntimeError('font availability changed')
        return original(value)
    monkeypatch.setattr(control, '_resolved', once)
    control.apply(ReadingAppearance(zoom=200))
    assert len(calls) == 1 and editor.font.points == 20
    assert control.current == control.store.load()


def test_completion_error_after_atomic_save_keeps_matching_visible_state(context, monkeypatch):
    editor, control, _, reports = context
    original = control.store.save
    def after_commit(value, **kwargs):
        original(value, **kwargs)
        raise OSError('post-publication confirmation')
    monkeypatch.setattr(control.store, 'save', after_commit)
    control.apply(ReadingAppearance(zoom=175))
    assert control.current == control.store.load() == ReadingAppearance(zoom=175)
    assert editor.font.points == 17.5 and 'was saved' in reports[-1]


def test_explicit_idle_appearance_edit_after_stop_preserves_worker_cancellation(context):
    editor, control, flags, _ = context
    flags['cancelled'] = True
    control.apply(ReadingAppearance(zoom=150))
    assert editor.font.points == 15 and control.store.load().zoom == 150
    assert flags['cancelled'] is True


def test_tab_binding_uses_held_appearance_without_preference_io_or_editing(context, monkeypatch):
    original, control, _, _ = context
    control.apply(ReadingAppearance(zoom=175, foreground='#FFFFFF', background='#000000'))
    saved = control.store.path.read_bytes()
    fresh = Editor()
    fresh.text, fresh.selection, fresh.undo = 'Independent tab text', (2, 6), ['other edit']
    before = (fresh.text, fresh.selection, list(fresh.undo))
    monkeypatch.setattr(control.store, 'load', lambda: pytest.fail('Tab activation reread appearance.'))
    monkeypatch.setattr(control.store, 'save', lambda *_a, **_kw: pytest.fail('Tab activation wrote appearance.'))
    assert control.bind_editor(fresh)
    assert control.editor is fresh and fresh.font.points == 17.5
    assert (fresh.text, fresh.selection, fresh.undo) == before
    assert original.font.points == 17.5 and control.store.path.read_bytes() == saved
    control.bind_editor(original)
    assert original.font.points == 17.5  # Returning to a tab does not compound zoom.


@pytest.mark.parametrize('flag,value', [('alive', False), ('busy', True), ('revoked', True)])
def test_rebinding_a_tab_requires_current_idle_reader(context, flag, value):
    original, control, flags, _ = context
    flags[flag] = value
    fresh = Editor()
    with pytest.raises(ValueError):
        control.bind_editor(fresh)
    assert control.editor is original and fresh.refreshes == 0


def test_missing_held_font_does_not_rebind_later_appearance_to_an_inactive_tab(context):
    original, control, _, reports = context
    control.current = ReadingAppearance(ReadingFont('Missing font', 12))
    fresh = Editor()
    assert control.bind_editor(fresh) is False
    assert control.editor is fresh and fresh.text == original.text
    assert 'could not be applied' in reports[-1] and not control.store.path.exists()
