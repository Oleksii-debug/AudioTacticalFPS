"""Actual worker-bound tags and keyboard-addressable dialog lifecycle."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import struct
import sys
from threading import Event, get_ident
from types import SimpleNamespace
import wave
import zlib

import pytest

from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.audio.metadata import MetadataChanges, inspect_metadata
from carr_library.ui import audio_metadata_dialog as ui


@pytest.fixture
def session(tmp_path):
    owner = get_ident()
    library = tmp_path / 'library'; library.mkdir()
    output = tmp_path / 'exports'; output.mkdir()
    pcm = tmp_path / 'source.wav'
    with wave.open(str(pcm), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x10\x02' * 2400)
    runtime = AudioRuntime(output, input_roots=(tmp_path,), budget=AudioBudget(reserve_disk_bytes=0))
    source = output / 'source.flac'; runtime.convert(pcm, source)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    cover = tmp_path / 'original-cover.png'
    cover.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress((b'\0' + b'\x20\x40\x80' * 2) * 2)) + chunk(b'IEND', b''))
    queue, notices, notifications, runtime_calls = deque(), [], [], []
    frame = SimpleNamespace(library=SimpleNamespace(root=library), _closing=False, working=False,
                            cancel_event=Event(), report=notices.append)
    def notify(kind, value):
        assert get_ident() == owner, 'View callback ran on the worker'
        notifications.append((kind, value))
    def run_job(title, operation, done, failed, *, with_progress=False):
        assert get_ident() == owner and not frame.working and with_progress
        frame.working = True; frame.cancel_event.clear()
        queue.append((operation, done, failed))
    frame.run_job = run_job
    def factory(directory, *, state_root, input_roots):
        assert get_ident() != owner, 'Runtime construction blocked the UI'
        runtime_calls.append((directory, state_root, input_roots))
        return AudioRuntime(directory, input_roots=input_roots, budget=AudioBudget(reserve_disk_bytes=0))
    controller = ui.MetadataController(frame, notify, runtime_factory=factory)
    def perform(*, finish=True):
        operation, done, failed = queue.popleft()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(operation, lambda *args: None)
            try:
                value, error = future.result(timeout=30), None
            except Exception as exc:
                value, error = None, exc
        def apply():
            frame.working = False
            (failed(error) if error is not None else done(value))
        if finish:
            apply()
        return value, error, apply
    form = ui.MetadataForm(str(source), str(output / 'reviewed.flac'),
        MetadataChanges({'title': 'Reviewed α 😀', 'artist': 'Explicit author'}, 'replace', str(cover)))
    return SimpleNamespace(controller=controller, frame=frame, root=tmp_path, output=output,
        source=source, cover=cover, queue=queue, notices=notices, notifications=notifications,
        runtime_calls=runtime_calls, perform=perform, form=form, runtime=runtime, factory=factory,
        owner=owner)


def reviewed(session):
    session.controller.inspect(str(session.source))
    assert session.perform()[1] is None
    session.controller.review(session.form)
    assert session.perform()[1] is None
    assert session.controller.plan is not None


def test_actual_controller_review_is_separate_from_save_and_preserves_sources(session):
    before = session.source.read_bytes(), session.cover.read_bytes()
    reviewed(session)
    assert not Path(session.form.output).exists()
    assert 'Source SHA-256' in ui.review_text(session.controller.plan)
    session.controller.save(session.form)
    result, error, _ = session.perform()
    assert error is None and session.controller.result is result
    assert result.receipt_path.is_file() and result.warning == ''
    inspected = inspect_metadata(session.runtime, result.audio.path)
    assert dict(inspected.tags)['title'] == 'Reviewed α 😀'
    assert inspected.cover.sha256 == sha256(before[1]).hexdigest()
    assert (session.source.read_bytes(), session.cover.read_bytes()) == before
    assert session.notifications[-1] == ('saved', result)
    assert session.controller.plan is None


def test_save_requires_review_of_the_exact_current_form(session):
    with pytest.raises(ValueError, match='Review'):
        session.controller.save(session.form)
    assert not session.queue
    reviewed(session)
    changed = ui.MetadataForm(session.form.source, str(session.output / 'different.flac'), session.form.changes)
    with pytest.raises(ValueError, match='current'):
        session.controller.save(changed)
    assert not session.queue and not Path(changed.output).exists()


@pytest.mark.parametrize('change', ['object', 'path', 'identity', 'close'])
def test_queued_inspection_never_follows_changed_library_authority(session, change):
    session.controller.inspect(str(session.source))
    if change == 'object':
        session.frame.library = SimpleNamespace(root=session.frame.library.root)
    elif change == 'path':
        session.frame.library.root = session.root
    elif change == 'identity':
        original = session.frame.library.root
        original.rename(original.with_name('previous-library')); original.mkdir()
    else:
        session.frame._closing = True
    _, error, _ = session.perform()
    assert isinstance(error, ValueError) and 'library' in str(error).lower()
    assert not session.runtime_calls and session.controller.snapshot is None


def test_source_changed_after_inspection_must_be_inspected_again(session):
    session.controller.inspect(str(session.source)); session.perform()
    with session.source.open('ab') as stream:
        stream.write(b'changed')
    session.controller.review(session.form)
    _, error, _ = session.perform()
    assert isinstance(error, ValueError) and 'changed after inspection' in str(error)
    assert session.controller.plan is None and not Path(session.form.output).exists()


def test_private_library_cannot_be_selected_for_tagged_output(session):
    session.controller.inspect(str(session.source)); session.perform()
    form = ui.MetadataForm(session.form.source, str(session.frame.library.root / 'hidden.flac'), session.form.changes)
    calls = len(session.runtime_calls)
    session.controller.review(form)
    _, error, _ = session.perform()
    assert isinstance(error, ValueError) and 'outside the private library' in str(error)
    assert len(session.runtime_calls) == calls and session.controller.plan is None


@pytest.mark.parametrize('cancel', ['dialog', 'host', 'form'])
def test_cancelled_or_changed_review_never_admits_a_plan(session, cancel):
    session.controller.inspect(str(session.source)); session.perform()
    session.controller.review(session.form)
    if cancel == 'dialog': session.controller.cancel()
    elif cancel == 'host': session.frame.cancel_event.set()
    else: session.controller.invalidate()
    _, error, _ = session.perform()
    assert error is not None and session.controller.plan is None
    assert not Path(session.form.output).exists()


def test_close_discards_queued_view_callback_without_reading_destroyed_controls(session):
    session.controller.inspect(str(session.source))
    session.controller.close()
    before = tuple(session.notifications)
    _, error, _ = session.perform()
    assert error is not None and tuple(session.notifications) == before
    assert not session.runtime_calls


def test_close_after_actual_publication_keeps_saved_result_and_reports_it(session):
    reviewed(session)
    session.controller.save(session.form)
    result, error, finish = session.perform(finish=False)
    assert error is None and result.audio.path.is_file()
    before = tuple(session.notifications)
    session.controller.close()
    finish()
    assert session.controller.result is result and tuple(session.notifications) == before
    assert session.notices[-1].startswith('Saved ') and str(result.audio.path) in session.notices[-1]


def test_another_host_operation_does_not_invalidate_the_review(session):
    reviewed(session)
    plan = session.controller.plan
    session.frame.working = True
    with pytest.raises(ValueError, match='current operation'):
        session.controller.inspect(str(session.source))
    assert session.controller.plan is plan and not session.queue


def test_output_directory_replacement_is_refused_before_save(session):
    reviewed(session)
    session.output.rename(session.output.with_name('original-exports')); session.output.mkdir()
    session.controller.save(session.form)
    _, error, _ = session.perform()
    assert isinstance(error, ValueError) and 'output changed' in str(error)
    assert list(session.output.iterdir()) == []


@pytest.fixture
def native_view(session, monkeypatch):
    widgets, pickers, destroyed, forbidden = {}, deque(), [], []
    state = SimpleNamespace(scenario=None, picker_scenario=None)
    class Widget:
        def __init__(self, parent=None, *args, value='', choices=(), **kwargs):
            self.parent, self.value, self.choices = parent, value, choices
            self.name, self.handlers, self.selected, self.destroyed = '', {}, -1, False
            self.enabled = True
            self.children = []
            if isinstance(parent, Widget): parent.children.append(self)
        def check(self):
            if get_ident() != session.owner or self.destroyed:
                forbidden.append(self.name or type(self).__name__)
            assert get_ident() == session.owner and not self.destroyed, 'Native access outside its UI lifetime'
        def __bool__(self): return not self.destroyed
        def SetName(self, value): self.check(); self.name = value; widgets[value] = self
        def GetValue(self): self.check(); return self.value
        def ChangeValue(self, value): self.check(); self.value = value
        def SetSelection(self, value): self.check(); self.selected = value
        def GetSelection(self): self.check(); return self.selected
        def Enable(self, enabled=True): self.check(); self.enabled = enabled
        def Bind(self, event, callback): self.check(); self.handlers[event] = callback
        def SetFocus(self): self.check()
        def SetMaxLength(self, value): self.check()
        def SetMinSize(self, value): self.check()
        def SetSizer(self, value): self.check()
        def SetScrollRate(self, *args): self.check()
        def FitInside(self): self.check()
        def Wrap(self, *args): self.check()
        def SetEscapeId(self, value): self.check()
        def GetId(self): self.check(); return id(self)
        def IsModal(self): self.check(); return True
        def EndModal(self, value): self.check()
        def ShowModal(self):
            self.check()
            state.scenario(self)
            return wx.ID_CANCEL
        def Destroy(self):
            self.check()
            identity = id(self)
            for child in tuple(self.children):
                if not child.destroyed:
                    Widget.Destroy(child)
            self.destroyed = True; destroyed.append(self)
            callback = self.handlers.get(wx.EVT_WINDOW_DESTROY)
            if callback is not None:
                callback(SimpleNamespace(GetId=lambda: identity, Skip=lambda: None,
                    GetEventObject=lambda: SimpleNamespace()))
            return True
    class Picker(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); self.path = pickers.popleft()
        def ShowModal(self):
            self.check()
            if state.picker_scenario is not None:
                state.picker_scenario(self)
            return wx.ID_OK
        def GetPath(self): self.check(); return str(self.path)
        def Destroy(self):
            self.parent.children.remove(self)
            super().Destroy()
    class Sizer:
        def __init__(self, *args): pass
        def Add(self, *args): pass
    constants = ('VSCROLL','VERTICAL','ALL','EXPAND','LEFT','TOP','TE_READONLY','TE_MULTILINE','TE_DONTWRAP',
        'DEFAULT_DIALOG_STYLE','RESIZE_BORDER','FD_OPEN','FD_FILE_MUST_EXIST','FD_SAVE','ID_OK','ID_CANCEL',
        'EVT_TEXT','EVT_CHOICE','EVT_BUTTON','EVT_CLOSE','EVT_WINDOW_DESTROY')
    wx = SimpleNamespace(**{name: 1 << index for index, name in enumerate(constants)})
    for name in ('Dialog','ScrolledWindow','StaticText','TextCtrl','Button','Choice'):
        setattr(wx, name, Widget)
    wx.FileDialog, wx.BoxSizer = Picker, Sizer
    monkeypatch.setitem(sys.modules, 'wx', wx)
    from carr_library import runtime_settings
    monkeypatch.setattr(runtime_settings, 'audio_runtime', session.factory)
    def press(label):
        control = widgets[label]
        assert control.enabled, 'Required action is disabled: ' + label
        control.handlers[wx.EVT_BUTTON]()
    def edit(label, value):
        control = widgets[label]; control.ChangeValue(value)
        control.handlers[wx.EVT_TEXT]()
    return SimpleNamespace(session=session, widgets=widgets, pickers=pickers, state=state,
                           destroyed=destroyed, press=press, edit=edit, wx=wx,
                           widget_type=Widget, forbidden=forbidden)


def test_actual_named_native_handlers_inspect_review_then_save_without_ui_thread_io(native_view):
    view, session = native_view, native_view.session
    source_before = session.source.read_bytes()
    view.pickers.extend([session.source, session.cover, Path(session.form.output)])
    def scenario(dialog):
        assert not session.runtime_calls and not session.queue
        view.press('Choose source audio')
        view.press('Inspect source tags and chapters')
        assert not view.widgets['Save reviewed audio copy'].enabled
        session.perform()
        view.edit('Title', 'Native reviewed 😀 title')
        view.edit('Artist or author', 'Explicit native author')
        mode = view.widgets['Cover action']; mode.SetSelection(1); mode.handlers[view.wx.EVT_CHOICE]()
        view.press('Choose replacement cover')
        view.press('Choose new output filename')
        view.press('Review metadata and cover changes')
        session.perform()
        assert 'native author' in view.widgets['Reviewed metadata and cover'].GetValue()
        assert not Path(session.form.output).exists()
        view.press('Save reviewed audio copy')
        result, error, _ = session.perform()
        assert error is None and result.receipt_path.exists()
        assert view.widgets['Audio metadata status'].GetValue().startswith('Saved ')
        assert not view.widgets['Save reviewed audio copy'].enabled
    view.state.scenario = scenario
    result = ui.show_audio_metadata(session.frame)
    assert result is not None and result.audio.path.exists()
    assert dict(inspect_metadata(session.runtime, result.audio.path).tags)['title'] == 'Native reviewed 😀 title'
    assert session.source.read_bytes() == source_before
    assert all(widget.destroyed for widget in view.widgets.values())


def test_native_close_cancels_owned_work_and_late_callback_never_touches_destroyed_widgets(native_view):
    view, session = native_view, native_view.session
    view.pickers.append(session.source)
    def scenario(dialog):
        view.press('Choose source audio')
        view.press('Inspect source tags and chapters')
        view.press('Close')
    view.state.scenario = scenario
    assert ui.show_audio_metadata(session.frame) is None
    assert all(widget.destroyed for widget in view.widgets.values())
    _, error, _ = session.perform()
    assert error is not None and not session.runtime_calls

