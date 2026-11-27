"""Actual pronunciation dialog handlers on one real worker with native-widget stand-ins."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace
import time

import pytest

from carr_library.speech.reading import ReadingDocument, PronunciationRule, export_dictionary, import_dictionary
from carr_library.ui import pronunciation_dialog as ui


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.owner = get_ident()
        self.value, self.label, self.choices = value, label, list(choices)
        self.selection, self.enabled, self.name = -1, True, ''
        self.destroyed, self.children, self.handlers = False, [], {}
        self.parent = parent if isinstance(parent, Widget) else None
        if isinstance(parent, Widget):
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


class Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass


class ReaderWorker:
    """The reader's admission/cancel/queued-completion contract with real threads."""
    def __init__(self, cancel):
        self.cancel, self.owner = cancel, get_ident()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pronunciation-handler')
        self.callbacks, self.gate = Queue(), Event()
        self.gate.set()
        self.busy, self.future, self.worker_threads = False, None, []
        self.reject = False

    def __call__(self, operation, done, *, on_failed):
        assert get_ident() == self.owner
        if self.busy or self.reject:
            return False
        self.busy = True
        self.cancel.clear()
        def work():
            self.worker_threads.append(get_ident())
            if not self.gate.wait(5):
                raise TimeoutError('Synthetic worker admission barrier timed out.')
            return operation()
        self.future = self.executor.submit(work)
        self.future.add_done_callback(lambda future: self.callbacks.put((future, done, on_failed)))
        return True

    def ready(self):
        assert self.future is not None
        try:
            self.future.result(timeout=5)
        except Exception:
            pass

    def drain(self):
        assert get_ident() == self.owner
        future, done, failed = self.callbacks.get(timeout=5)
        self.busy = False
        try:
            done(future.result(timeout=5))
        except Exception as exc:
            failed(exc)

    def close(self):
        self.cancel.set(); self.gate.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
        while not self.callbacks.empty():
            self.drain()


@pytest.fixture
def view(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    owner = get_ident()
    anchor = SimpleNamespace(root=root)
    state = SimpleNamespace(library=anchor, authorized=True, closed=False,
        document=ReadingDocument('Dr. Jones.\r\nCarry 12 kg. Dr. Smith.'), selection=(0, 0),
        language='en', voice='fixture:local', applied=0, undone=0, guard_threads=[])
    def guard():
        state.guard_threads.append(get_ident())
        if state.library is not anchor or state.library.root != root or state.closed:
            raise ValueError('The library changed or the reader closed.')
        if not state.authorized:
            raise ValueError('The source scope is no longer available.')
    def capture():
        assert get_ident() == owner
        guard()
        return ui.ReadingSnapshot.capture(state.document, state.selection,
                                          language=state.language, voice_id=state.voice)
    def apply(preview):
        assert get_ident() == owner
        guard()
        ui.apply_pronunciation_preview(state.document, preview, capture())
        state.selection = (0, 0)
        state.applied += 1
        return capture(), ''
    def undo(expected):
        assert get_ident() == owner
        guard()
        if capture() != expected:
            raise ValueError('The reading copy changed after applying.')
        assert state.document.undo()
        state.selection = (0, 0)
        state.undone += 1
        return ''
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL',
        'HORIZONTAL', 'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON',
        'EVT_CLOSE', 'EVT_TEXT', 'EVT_LISTBOX', 'EVT_CHECKBOX', 'EVT_CHOICE', 'ID_OK',
        'ID_CANCEL', 'FD_OPEN', 'FD_FILE_MUST_EXIST', 'FD_SAVE', 'YES_NO', 'ICON_QUESTION',
        'YES', 'NO', 'CANCEL')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl', 'ListBox',
                 'CheckBox', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    picks, answers = [], []
    class Picker(Widget):
        def ShowModal(self):
            self.native(); return wx.ID_OK
        def GetPath(self):
            self.native(); return str(picks.pop(0))
    wx.FileDialog = Picker
    wx.MessageBox = lambda *args: answers.pop(0) if answers else wx.NO
    cancel = Event()
    worker = ReaderWorker(cancel)
    dialog = ui.create_pronunciation_dialog(None, library_root=root, capture=capture,
        apply=apply, undo=undo, guard=guard, run=worker, cancel=cancel, wx_module=wx)
    harness = SimpleNamespace(dialog=dialog, state=state, worker=worker, cancel=cancel,
                              picks=picks, answers=answers, wx=wx, owner=owner, capture=capture)
    dialog.on_load(); worker.drain()
    assert dialog.loaded and not dialog.busy
    try:
        yield harness
    finally:
        if dialog.alive:
            dialog.finish_close()
        worker.close()
        if not dialog.destroyed:
            dialog.Destroy()


def add_rule(view, rule=PronunciationRule('Dr.', 'Doctor')):
    dialog = view.dialog
    dialog.on_new()
    for control, value in ((dialog.pattern, rule.pattern), (dialog.replacement, rule.replacement),
                           (dialog.rule_language, rule.language), (dialog.rule_voice, rule.voice_id)):
        control.ChangeValue(value)
    dialog.regex.SetValue(rule.regex); dialog.ignore_case.SetValue(rule.ignore_case)
    dialog.on_add()
    assert dialog.rules[-1] == rule
    return rule


def preview(view):
    view.dialog.on_preview(); view.worker.drain()
    assert view.dialog.preview is not None, view.dialog.status.GetValue()


def test_real_regex_preview_apply_undo_preserves_original_and_runs_off_ui(view):
    before = view.state.document.text
    source_hash = view.state.document.source_sha256
    add_rule(view)
    add_rule(view, PronunciationRule(r'(\d+) kg', r'\1 kilograms', regex=True,
                                     language='en', voice_id='fixture:local'))
    preview(view)
    assert view.state.document.text == before and not view.state.applied
    assert view.dialog.after.GetValue() == 'Doctor Jones.\r\nCarry 12 kilograms. Doctor Smith.'
    assert view.dialog.buttons['apply'].IsEnabled()
    view.dialog.on_apply()
    assert view.state.applied == 1 and view.state.document.revision == 1
    assert view.state.document.source_text == before
    assert view.state.document.source_sha256 == source_hash
    assert view.dialog.buttons['undo'].IsEnabled() and not view.dialog.buttons['apply'].IsEnabled()
    view.dialog.on_undo()
    assert view.state.undone == 1 and view.state.document.text == before
    assert view.worker.worker_threads and all(thread != view.owner for thread in view.worker.worker_threads)


def test_selected_scope_requires_actual_selection_and_changes_only_that_range(view):
    before = view.state.document.text
    add_rule(view)
    view.dialog.scope.SetSelection(1)
    view.dialog.on_preview(); view.worker.drain()
    assert view.dialog.preview is None and 'nonempty' in view.dialog.status.GetValue()
    start = before.rindex('Dr.')
    view.state.selection = (start, start + 3)
    preview(view)
    assert view.dialog.before.GetValue() == 'Dr.' and view.dialog.after.GetValue() == 'Doctor'
    view.dialog.on_apply()
    assert view.state.document.text == before[:start] + 'Doctor' + before[start + 3:]


@pytest.mark.parametrize('changed', ['text', 'selection', 'voice', 'language', 'rules', 'form'])
def test_silent_edits_invalidate_apply_even_without_native_change_event(view, changed):
    add_rule(view); preview(view)
    if changed == 'text': view.state.document.set_text('A newer reading edit.')
    if changed == 'selection': view.state.selection = (0, 3)
    if changed == 'voice': view.state.voice = 'fixture:different'
    if changed == 'language': view.dialog.language.ChangeValue('de')
    if changed == 'rules': view.dialog.rules += (PronunciationRule('kg', 'kilograms'),)
    if changed == 'form': view.dialog.replacement.ChangeValue('A new unsaved replacement')
    preserved = view.state.document.text
    view.dialog.on_apply()
    assert view.state.applied == 0 and view.state.document.text == preserved
    assert view.dialog.preview is None
    assert any(word in view.dialog.status.GetValue().lower() for word in ('changed', 'update'))


@pytest.mark.parametrize('when', ['admission', 'queued', 'completion'])
def test_library_relocation_blocks_work_and_late_preview_acceptance(view, when):
    add_rule(view)
    if when == 'admission':
        view.state.library = SimpleNamespace(root=view.state.library.root)
        view.dialog.on_preview()
    elif when == 'queued':
        view.worker.gate.clear()
        view.dialog.on_preview()
        view.state.library = SimpleNamespace(root=view.state.library.root)
        view.worker.gate.set(); view.worker.drain()
    else:
        view.dialog.on_preview(); view.worker.ready()
        view.state.library = SimpleNamespace(root=view.state.library.root)
        view.worker.drain()
    assert view.dialog.preview is None and not view.state.applied
    assert view.state.document.text == view.state.document.source_text


def test_actual_regex_can_be_cancelled_without_blocking_widget_access(view):
    view.state.document = ReadingDocument('a' * 100_000 + '!')
    add_rule(view, PronunciationRule('(a+)+$', 'changed', regex=True))
    started = time.monotonic()
    view.dialog.on_preview()
    assert view.dialog.status.GetName() == 'Pronunciation status'
    view.dialog.on_cancel(); view.worker.drain()
    assert time.monotonic() - started < 5
    assert not view.dialog.busy and view.dialog.preview is None and not view.state.applied
    assert view.state.document.text == view.state.document.source_text


def test_closed_dialog_suppresses_queued_result_without_touching_destroyed_widgets(view):
    add_rule(view)
    view.worker.gate.clear(); view.dialog.on_preview()
    view.dialog.on_close()
    assert not view.dialog.alive and view.cancel.is_set()
    view.dialog.Destroy()
    view.worker.gate.set(); view.worker.drain()
    assert view.state.applied == 0


def test_reader_admission_rejection_restores_controls_and_keeps_reading_copy(view):
    add_rule(view)
    view.worker.reject = True
    view.dialog.on_preview()
    assert not view.dialog.busy and view.dialog.preview is None
    assert view.dialog.buttons['preview'].IsEnabled()
    assert 'reader operation' in view.dialog.status.GetValue()
    assert not view.state.applied


def test_dictionary_save_import_export_are_explicit_and_no_clobber(view, tmp_path):
    rule = add_rule(view)
    before = view.state.document.text
    view.dialog.on_save(); view.worker.drain()
    saved, token = view.dialog.store.load()
    assert saved == (rule,) and token == view.dialog.saved_token
    external = tmp_path / 'rules.json'
    view.picks.append(external); view.dialog.on_export(); view.worker.drain()
    assert import_dictionary(external) == (rule,)
    original = external.read_bytes()
    view.picks.append(external); view.dialog.on_export(); view.worker.drain()
    assert external.read_bytes() == original and 'exists' in view.dialog.status.GetValue()
    incoming = tmp_path / 'imported.json'
    imported = PronunciationRule('kg', 'kilograms')
    export_dictionary((imported,), incoming)
    view.picks.append(incoming); view.dialog.on_import(); view.worker.drain()
    assert view.dialog.rules == (imported,)
    assert view.dialog.store.load() == (saved, token)
    assert view.state.document.text == before and not view.state.applied


def test_concurrent_saved_dictionary_is_preserved_and_unsaved_ui_rules_remain(view):
    rule = add_rule(view)
    independent = PronunciationRule('kg', 'kilograms')
    other = ui.DictionaryStore(view.state.library.root)
    newer = other.save((independent,), expected=None)
    view.dialog.on_save(); view.worker.drain()
    assert other.load() == ((independent,), newer)
    assert view.dialog.rules == (rule,) and view.dialog.saved_token is None
    assert 'Reload' in view.dialog.status.GetValue()


def test_cancellation_after_atomic_save_keeps_committed_dictionary_and_saved_token(view, monkeypatch):
    rule = add_rule(view)
    actual = ui.atomic_bytes
    def committed(*args, **kwargs):
        result = actual(*args, **kwargs)
        view.cancel.set()
        return result
    monkeypatch.setattr(ui, 'atomic_bytes', committed)
    view.dialog.on_save(); view.worker.drain()
    assert view.cancel.is_set()
    assert view.dialog.store.load() == ((rule,), view.dialog.saved_token)
    assert view.dialog.saved_rules == (rule,) and 'dictionary saved' in view.dialog.status.GetValue()
    assert view.state.document.text == view.state.document.source_text


def test_rule_order_edits_require_new_preview_and_undo_refuses_a_newer_reading_edit(view):
    add_rule(view, PronunciationRule('Dr.', 'Doctor'))
    add_rule(view, PronunciationRule('Doctor', 'Prof.'))
    preview(view)
    assert 'Prof. Jones' in view.dialog.after.GetValue()
    view.dialog.on_move(-1)
    assert view.dialog.preview is None and not view.dialog.buttons['apply'].IsEnabled()
    preview(view)
    assert 'Doctor Jones' in view.dialog.after.GetValue()
    view.dialog.on_apply()
    view.state.document.set_text('A later independent reading edit.')
    view.dialog.on_undo()
    assert view.state.document.text == 'A later independent reading edit.'
    assert not view.state.undone and 'changed' in view.dialog.status.GetValue()


def test_dirty_dictionary_close_can_cancel_or_save_before_closing(view):
    rule = add_rule(view)
    view.answers.append(view.wx.CANCEL); view.dialog.on_close()
    assert view.dialog.alive and view.dialog.rules == (rule,)
    view.answers.append(view.wx.YES); view.dialog.on_close()
    assert view.dialog.busy and view.dialog.alive
    view.worker.drain()
    assert not view.dialog.alive and view.dialog.store.load()[0] == (rule,)
