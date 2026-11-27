"""Production Studio/Reader dialog closures, real shared worker and project storage."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json
from carr_library.studio.project import Line, Project, Role, Scene
from carr_library.ui import studio_pronunciation as ui


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.owner, self.parent = get_ident(), parent if isinstance(parent, Widget) else None
        self.children, self.handlers = [], {}
        self.value, self.label, self.choices = value, label, list(choices)
        self.selection, self.enabled, self.name, self.destroyed = -1, True, '', False
        if self.parent is not None:
            self.parent.children.append(self)
    def native(self):
        assert get_ident() == self.owner, 'Worker accessed a native control.'
        assert not self.destroyed, 'Late callback accessed a destroyed control.'
    def SetName(self, value): self.native(); self.name = value
    def GetName(self): self.native(); return self.name
    def GetValue(self): self.native(); return self.value
    def ChangeValue(self, value): self.native(); self.value = value
    def SetValue(self, value): self.native(); self.value = value
    def Set(self, values): self.native(); self.choices = list(values); self.selection = -1
    def SetSelection(self, *values): self.native(); self.selection = values[0] if len(values) == 1 else tuple(values)
    def GetSelection(self): self.native(); return self.selection
    def GetRange(self, first, last):
        self.native(); return self.value.encode('utf-16-le')[first*2:last*2].decode('utf-16-le')
    def Enable(self, value=True): self.native(); self.enabled = value
    def IsEnabled(self): self.native(); return self.enabled
    def Bind(self, event, callback, *args): self.native(); self.handlers[event] = callback
    def SetSizer(self, *args): self.native()
    def SetScrollRate(self, *args): self.native()
    def FitInside(self): self.native()
    def Wrap(self, *args): self.native()
    def SetFocus(self): self.native(); self.focused = True
    def SetEscapeId(self, value): self.native()
    def IsModal(self): self.native(); return False
    def Hide(self): self.native(); self.hidden = True
    def Destroy(self):
        self.native()
        for child in tuple(self.children):
            child.Destroy()
        if self.parent is not None:
            self.parent.children.remove(self)
        self.destroyed = True


class Text(Widget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.ChangeValue(self.value)
    def ChangeValue(self, value):
        super().ChangeValue(value.replace('\r\n', '\n').replace('\r', '\n')); self.selection = (0, 0)
    def GetRange(self, first, last):
        self.native()
        native = self.value.replace('\n', '\r\n').encode('utf-16-le')
        return native[first*2:last*2].decode('utf-16-le').replace('\r\n', '\n')


class Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass


class SharedFrame:
    def __init__(self, library):
        self.library, self.working, self._closing = library, False, False
        self.owner, self.cancel_event = get_ident(), Event()
        self.gate, self.queue = Event(), Queue(); self.gate.set()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future, self.worker_threads = None, []
        self.reject = False
    def run_job(self, title, operation, done, failed):
        assert get_ident() == self.owner
        if self.working or self.reject:
            return
        self.working = True; self.cancel_event.clear()
        def work():
            self.worker_threads.append(get_ident())
            assert self.gate.wait(5), 'Shared-worker admission barrier timed out.'
            return operation()
        self.future = self.executor.submit(work)
        self.future.add_done_callback(lambda future: self.queue.put((future, done, failed)))
    def ready(self):
        try:
            self.future.result(timeout=5)
        except Exception:
            pass
    def drain(self):
        assert get_ident() == self.owner
        future, done, failed = self.queue.get(timeout=5)
        self.working = False
        try:
            done(future.result(timeout=5))
        except Exception as exc:
            failed(exc)
    def close(self):
        self.cancel_event.set(); self.gate.set()
        self.executor.shutdown(wait=True)
        while not self.queue.empty():
            self.drain()


@pytest.fixture
def view(tmp_path):
    library = LibraryApp(tmp_path / 'library'); library.create_workspace('Native handler fixture')
    frame = SharedFrame(library)
    modal, answers = [None], []
    class Dialog(Widget):
        def ShowModal(self):
            self.native(); return modal[0](self)
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
        'TE_MULTILINE', 'TE_READONLY', 'EVT_BUTTON', 'EVT_CHOICE', 'EVT_CLOSE', 'ID_CANCEL', 'ID_OK',
        'YES', 'NO', 'CANCEL', 'YES_NO', 'ICON_QUESTION', 'VSCROLL', 'EVT_LISTBOX', 'EVT_TEXT', 'EVT_CHECKBOX')
    wx = SimpleNamespace(**{name: 1 << i for i, name in enumerate(constants)})
    wx.Dialog, wx.TextCtrl, wx.BoxSizer = Dialog, Text, Sizer
    for name in ('StaticText', 'Choice', 'Button', 'ScrolledWindow', 'ListBox', 'CheckBox'):
        setattr(wx, name, Widget)
    wx.MessageBox = lambda *args: answers.pop(0) if answers else wx.NO
    dialog = ui.create_studio_pronunciation_dialog(frame, wx_module=wx)
    project = Project('spoken-fixture', 'Synthetic original',
        (Role('narrator', 'Narrator', 'fixture:one', 'en'), Role('reader', 'Reader', 'fixture:two', 'en')),
        (Scene('archive', 'Original archive', (
            Line('first', 'narrator', '😀 Dr. One\r\nDr. Two', source_ref='original-source-1'),
            Line('second', 'reader', 'Original untouched line.'))),),
        original_script='Exact original source script, retained independently.', mode='authored')
    dialog.service.projects.save(project)
    dialog.reload(); frame.drain()
    dialog.open_line(); frame.drain()
    assert dialog.draft.project == project
    harness = SimpleNamespace(dialog=dialog, frame=frame, project=project, modal=modal, answers=answers, wx=wx)
    try:
        yield harness
    finally:
        frame.close()
        if dialog.alive:
            dialog.finish_close()
        dialog.Destroy()


def review_change(view, *, selected=False, save_dictionary=False):
    def driver(child):
        view.frame.drain()
        child.pattern.ChangeValue(r'Dr\.')
        child.replacement.ChangeValue('Doctor')
        child.regex.SetValue(True)
        child.on_add()
        if save_dictionary:
            child.on_save(); view.frame.drain()
        child.on_preview(); view.frame.drain()
        assert child.preview is not None
        child.on_apply()
        assert child.preview is None and child.applied is not None
        child.finish_close()
    if selected:
        value = view.dialog.text.GetValue().replace('\n', '\r\n')
        start = len(value[:value.rindex('Dr.')].encode('utf-16-le')) // 2
        view.dialog.text.SetSelection(start, start + 3)
    view.modal[0] = driver
    view.dialog.review()


def test_real_nested_review_then_save_and_saved_undo_use_existing_history(view):
    dialog = view.dialog
    review_change(view, save_dictionary=True)
    assert dialog.draft.document.text == '😀 Doctor One\r\nDoctor Two'
    assert dialog.draft.dirty and 'Unsaved' in dialog.status.GetValue()
    assert dialog.service.projects.load(view.project.id) == view.project
    assert dialog.save(); view.frame.drain()
    saved = dialog.service.projects.load(view.project.id)
    assert saved.revision == 2 and saved.original_script == view.project.original_script
    assert saved.scenes[0].lines[1] == view.project.scenes[0].lines[1]
    assert not dialog.draft.dirty and dialog.last_saved.after == saved
    assert dialog.undo_saved(); view.frame.drain()
    assert dialog.service.projects.load(view.project.id) == replace(view.project, revision=3)
    assert dialog.last_saved is None
    assert all(identity != view.frame.owner for identity in view.frame.worker_threads)


def test_nested_selected_unicode_crlf_range_preserves_other_words(view):
    review_change(view, selected=True)
    assert view.dialog.draft.document.text == '😀 Dr. One\r\nDoctor Two'
    assert view.dialog.save(); view.frame.drain()
    assert view.dialog.draft.line.text == '😀 Dr. One\r\nDoctor Two'


@pytest.mark.parametrize('original,prefix,selection,expected', [
    ('😀 Dr. One\r\nDr. Two', '😀 Dr. One\n', 'Dr.', (11, 14)),
    ('One\rTwo\r\nThree\nFour', 'One\nTwo\n', 'Three\n', (9, 15)),
    ('One\r\nTwo', 'One', '\nTwo', (3, 8)),
    ('😀 One\r\nTwo', '', '😀 One\nTwo', (0, 10)),
])
def test_native_display_range_maps_to_retained_mixed_line_endings(original, prefix, selection, expected):
    displayed = original.replace('\r\n', '\n').replace('\r', '\n')
    assert ui.native_reading_range(original, displayed, prefix, selection) == expected


def test_native_range_rejects_silently_changed_display():
    with pytest.raises(ValueError, match='changed'):
        ui.native_reading_range('Dr. One\r\nDr. Two', 'Altered\nDr. Two', 'Altered\n', 'Dr.')


def test_shared_review_rejects_silent_parent_line_selection_change(view):
    def driver(child):
        view.frame.drain()
        child.pattern.ChangeValue('Dr.'); child.replacement.ChangeValue('Doctor'); child.on_add()
        child.on_preview(); view.frame.ready()
        view.dialog.line_choice.SetSelection(1)
        view.frame.drain()
        assert child.preview is None and 'changed' in child.status.GetValue()
        child.finish_close()
    view.modal[0] = driver
    view.dialog.review()
    assert not view.dialog.draft.dirty
    assert view.dialog.service.projects.load(view.project.id) == view.project


def test_dirty_draft_blocks_selection_until_explicit_discard(view):
    review_change(view)
    assert not view.dialog.project_choice.IsEnabled() and not view.dialog.line_choice.IsEnabled()
    view.dialog.line_choice.SetSelection(1); view.dialog.select_line()
    assert view.dialog.line_choice.GetSelection() == 0 and view.dialog.draft.dirty
    assert 'Save or discard' in view.dialog.status.GetValue()
    view.dialog.discard()
    assert view.dialog.draft.document.text == view.project.scenes[0].lines[0].text
    assert view.dialog.line_choice.IsEnabled() and not view.dialog.draft.dirty
    assert view.dialog.service.projects.load(view.project.id) == view.project


@pytest.mark.parametrize('stage', ['admission', 'queued', 'completion'])
def test_root_relocation_rejects_operation_without_approving_another_library(view, tmp_path, stage):
    dialog = view.dialog
    original_library = view.frame.library
    if stage == 'admission':
        view.frame.library = SimpleNamespace(root=tmp_path / 'other')
        assert not dialog.open_line()
    else:
        view.frame.gate.clear()
        assert dialog.open_line()
        if stage == 'completion':
            view.frame.gate.set(); view.frame.ready()
        view.frame.library = SimpleNamespace(root=original_library.root)
        view.frame.gate.set(); view.frame.drain()
    assert dialog.draft is None and 'library changed' in dialog.status.GetValue().lower()
    assert dialog.service.projects.load(view.project.id) == view.project


def test_cancel_during_shared_review_keeps_line_and_dictionary_unchanged(view):
    def driver(child):
        view.frame.drain()
        child.pattern.ChangeValue('Dr.'); child.replacement.ChangeValue('Doctor'); child.on_add()
        view.frame.gate.clear(); child.on_preview(); child.on_cancel()
        view.frame.gate.set(); view.frame.drain()
        assert child.preview is None
        child.finish_close()
    view.modal[0] = driver
    view.dialog.review()
    assert not view.dialog.draft.dirty
    assert view.dialog.service.projects.load(view.project.id) == view.project


def test_close_pending_waits_for_shared_worker_and_suppresses_late_ui(view):
    view.frame.gate.clear()
    assert view.dialog.open_line()
    view.dialog.close()
    assert view.dialog.pending and view.dialog.closing and view.dialog.alive
    assert view.frame.cancel_event.is_set()
    view.frame.gate.set(); view.frame.drain()
    assert not view.dialog.pending and not view.dialog.alive
    assert view.dialog.service.projects.load(view.project.id) == view.project
    before = view.dialog.status.GetValue()
    view.dialog.report('Late result')
    assert view.dialog.status.GetValue() == before


def test_refused_shared_worker_leaves_controls_usable(view):
    view.frame.reject = True
    assert not view.dialog.open_line()
    assert not view.dialog.pending and view.dialog.buttons['open'].IsEnabled()
    assert 'could not start' in view.dialog.status.GetValue()


def test_project_change_while_save_is_queued_is_not_overwritten(view):
    review_change(view)
    view.frame.gate.clear(); assert view.dialog.save()
    changed = replace(view.project, title='External same-revision edit')
    atomic_json(view.dialog.service.projects.directory(view.project.id) / 'project.json', changed.to_dict(), replace=True)
    view.frame.gate.set(); view.frame.drain()
    assert 'changed' in view.dialog.status.GetValue()
    assert view.dialog.draft.dirty and view.dialog.last_saved is None
    assert view.dialog.service.projects.load(view.project.id) == changed


def test_changed_draft_at_worker_admission_cannot_be_silently_saved(view):
    review_change(view)
    view.frame.gate.clear(); assert view.dialog.save()
    view.dialog.draft.document.set_text('Unreviewed late text')
    view.frame.gate.set(); view.frame.drain()
    assert 'draft changed' in view.dialog.status.GetValue()
    assert view.dialog.service.projects.load(view.project.id) == view.project


def test_cancellation_after_atomic_publication_keeps_saved_ui_and_receipt(view, monkeypatch):
    review_change(view)
    save = view.dialog.service.projects.save
    def committed(project, **kwargs):
        result = save(project, **kwargs)
        view.frame.cancel_event.set()
        return result
    monkeypatch.setattr(view.dialog.service.projects, 'save', committed)
    assert view.dialog.save(); view.frame.drain()
    assert view.dialog.last_saved is not None and not view.dialog.draft.dirty
    assert 'saved as project revision 2' in view.dialog.status.GetValue()
    assert view.dialog.service.projects.load(view.project.id) == view.dialog.last_saved.after


def test_unsaved_close_requires_explicit_discard(view):
    review_change(view)
    view.answers.append(view.wx.NO); view.dialog.close()
    assert view.dialog.alive and view.dialog.draft.dirty
    view.answers.append(view.wx.YES); view.dialog.close()
    assert not view.dialog.alive
    assert view.dialog.service.projects.load(view.project.id) == view.project


def test_later_project_edit_blocks_saved_undo_and_preserves_both_revisions(view):
    review_change(view); view.dialog.save(); view.frame.drain()
    saved = view.dialog.last_saved.after
    changed = replace(saved, title='New ordinary edit', revision=3)
    view.dialog.service.projects.save(changed, expected_revision=2)
    assert view.dialog.undo_saved(); view.frame.drain()
    assert 'changed' in view.dialog.status.GetValue()
    assert view.dialog.service.projects.load(view.project.id) == changed


def test_empty_audio_only_scene_never_offers_fake_narration(view):
    scene = Scene('audio-only', 'Silence', (), minimum_duration=1)
    project = replace(view.project, id='audio-only', scenes=(scene,))
    view.dialog.service.projects.save(project)
    view.dialog.reload(); view.frame.drain()
    index = next(i for i, item in enumerate(view.dialog.projects) if item.id == project.id)
    view.dialog.project_choice.SetSelection(index); view.dialog.select_project()
    assert not view.dialog.open_line()
    assert 'Audio clips have no pronunciation text' in view.dialog.status.GetValue()
    assert view.dialog.service.projects.load(project.id) == project
