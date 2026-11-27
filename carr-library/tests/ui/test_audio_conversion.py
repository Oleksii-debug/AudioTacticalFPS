"""Queued scope and modal ownership for the actual conversion handlers.

These control doubles make no native-widget claim; the separate Windows probe
drives the real editor, preset dialogs and processed PCM with native keys.
"""
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.ui import audio_conversion, effects_dialog
from carr_library.audio.effect_presets import EffectPresetStore
from carr_library.audio import Effect


class Control:
    def __init__(self, owner=None, *args, **options):
        self.owner = owner
        self.name = options.get('label', '')
        self.value = options.get('value', False if options.pop('_checkbox', False) else '')
        self.items = list(options.get('choices', ()))
        self.selection = 0 if self.items else -1
        self.bindings = {}
        self.enabled = True
        self.focus = False
    def SetName(self, name):
        self.name = name
        CURRENT.controls[name] = self
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def Set(self, values): self.items = list(values)
    def GetString(self, index): return self.items[index]
    def GetCount(self): return len(self.items)
    def SetFocus(self): self.focus = True
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def Bind(self, event, callback): self.bindings[event] = callback
    def SetSizer(self, value): pass
    def SetScrollRate(self, *values): pass
    def Add(self, *values, **options): pass
    def Destroy(self): pass


CURRENT = None


def widgets(monkeypatch, source, output, *, modal=None, picker=None, cancel=None, name_modal=None):
    global CURRENT
    env = SimpleNamespace(controls={}, dialogs=[], source=source, output=output,
                          modal=modal, picker=picker, cancel=cancel, name_modal=name_modal)
    CURRENT = env
    class Dialog(Control):
        def __init__(self, parent=None, *args, **options):
            super().__init__(parent, *args, **options)
            self.title = options.get('title', '')
            self.ended = None
            env.dialogs.append(self)
        def ShowModal(self):
            if env.modal:
                env.modal(self)
            return self.ended if self.ended is not None else (2 if env.cancel == 'options' else 1)
        def EndModal(self, result): self.ended = result
        def CreateButtonSizer(self, flags): return Control(self)
        def FindWindowById(self, identity): return Control(self)
    class FileDialog(Dialog):
        def __init__(self, parent, message, **options):
            super().__init__(parent, title=message)
            self.stage = 'output' if options.get('style') == 64 else 'source'
        def ShowModal(self):
            if env.picker:
                env.picker(self.stage)
            return 2 if env.cancel == self.stage else 1
        def GetPath(self): return str(env.output if self.stage == 'output' else env.source)
    class TextEntryDialog(Dialog):
        def __init__(self, parent, message, title):
            super().__init__(parent, title=title)
            self.value = 'Reviewed user preset'
        def ShowModal(self):
            if env.name_modal:
                env.name_modal(self)
            return 1
    wx = SimpleNamespace(
        Dialog=Dialog, FileDialog=FileDialog, TextEntryDialog=TextEntryDialog,
        ScrolledWindow=Control, BoxSizer=Control, GridSizer=Control,
        TextCtrl=Control, StaticText=Control, Button=Control, Choice=Control, ListBox=Control,
        CheckBox=lambda *args, **options: Control(*args, _checkbox=True, **options),
        ID_OK=1, ID_CANCEL=2, OK=1, CANCEL=2, DEFAULT_DIALOG_STYLE=0, RESIZE_BORDER=0,
        VERTICAL=0, HORIZONTAL=0, ALL=0, EXPAND=0, TE_READONLY=0, TE_MULTILINE=0,
        FD_OPEN=16, FD_FILE_MUST_EXIST=32, FD_SAVE=64, NOT_FOUND=-1,
        EVT_BUTTON='button', EVT_LISTBOX='select', EVT_LISTBOX_DCLICK='double')
    monkeypatch.setitem(sys.modules, 'wx', wx)
    return env


@pytest.fixture
def view(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    source = tmp_path / 'explicit-source.wav'
    source.write_bytes(b'Explicit selected bytes; no decoder should run in these scope tests.')
    output = tmp_path / 'new-output.wav'
    frame = SimpleNamespace(library=SimpleNamespace(root=root), _closing=False,
                            working=False, cancel_event=Event(), jobs=[], reports=[])
    frame.report = frame.reports.append
    def run_job(title, operation, complete, failed):
        future = Future()
        frame.jobs.append(SimpleNamespace(title=title, operation=operation, complete=complete,
                                         failed=failed, future=future))
        return future
    frame.run_job = run_job
    return SimpleNamespace(frame=frame, root=tmp_path, source=source, output=output)


def change(view, kind):
    if kind == 'same-path-object':
        view.frame.library = SimpleNamespace(root=view.frame.library.root)
    elif kind == 'root':
        replacement = view.root / 'replacement'; replacement.mkdir()
        view.frame.library.root = replacement
    else:
        view.frame._closing = True


@pytest.mark.parametrize('kind', ['same-path-object', 'root', 'closing'])
def test_queued_conversion_rechecks_exact_host_before_any_codec(view, monkeypatch, kind):
    from carr_library.audio import conversion
    widgets(monkeypatch, view.source, view.output)
    audio_conversion.show_audio_conversion(view.frame)
    job, = view.frame.jobs
    change(view, kind)
    monkeypatch.setattr(conversion, 'audio_runtime',
                        lambda *args, **kwargs: pytest.fail('Stale queued view started a runtime.'))
    with pytest.raises(ValueError, match='library changed or is closing'):
        job.operation()
    assert not view.output.exists() and view.frame.reports == []


@pytest.mark.parametrize('stage', ['source', 'options', 'output'])
@pytest.mark.parametrize('kind', ['same-path-object', 'root', 'closing'])
def test_each_modal_return_keeps_changed_host_from_submitting_or_presenting(view, monkeypatch, stage, kind):
    def picker(current):
        if current == stage:
            change(view, kind)
    def modal(dialog):
        if stage == 'options':
            change(view, kind)
    widgets(monkeypatch, view.source, view.output, modal=modal, picker=picker)
    original = view.source.read_bytes()
    audio_conversion.show_audio_conversion(view.frame)
    assert view.frame.jobs == [] and view.frame.reports == []
    assert not view.output.exists() and view.source.read_bytes() == original


@pytest.mark.parametrize('kind', ['same-path-object', 'root', 'closing'])
def test_late_completion_and_failure_never_report_into_another_host_view(view, monkeypatch, kind):
    widgets(monkeypatch, view.source, view.output)
    audio_conversion.show_audio_conversion(view.frame)
    job, = view.frame.jobs
    change(view, kind)
    job.complete(SimpleNamespace())
    job.failed(ValueError('Old operation diagnostic'))
    assert view.frame.reports == []


@pytest.mark.parametrize('stage', ['source', 'options', 'output'])
def test_explicit_picker_or_options_cancel_never_queues_conversion(view, monkeypatch, stage):
    widgets(monkeypatch, view.source, view.output, cancel=stage)
    audio_conversion.show_audio_conversion(view.frame)
    assert view.frame.jobs == [] and view.frame.reports == []
    assert not view.output.exists()


def test_close_while_capability_review_is_queued_cancels_its_own_work(view, monkeypatch):
    from carr_library.audio import conversion
    def modal(dialog):
        if dialog.title == 'Convert audio and apply effects':
            CURRENT.controls['Edit ordered effect chain and presets'].bindings['button'](None)
            dialog.EndModal(2)
    widgets(monkeypatch, view.source, view.output, modal=modal)
    audio_conversion.show_audio_conversion(view.frame)
    job, = view.frame.jobs
    assert job.title == 'Checking local audio effects' and job.future.cancelled()
    monkeypatch.setattr(conversion, 'audio_runtime',
                        lambda *args, **kwargs: pytest.fail('Closed review started a probe.'))
    with pytest.raises(AudioCancelled):
        job.operation()
    previous = {name: (value.value, value.enabled, value.focus) for name, value in CURRENT.controls.items()}
    job.complete(('gain', 'pitch'))
    job.failed(ValueError('Late closed probe'))
    assert previous == {name: (value.value, value.enabled, value.focus) for name, value in CURRENT.controls.items()}
    assert view.frame.reports == []


def test_named_preset_is_not_written_after_nested_name_dialog_loses_source_scope(view, monkeypatch):
    active = [True]
    def guard():
        if not active[0]:
            raise ValueError('The selected source view changed.')
    def modal(dialog):
        if dialog.title == 'Ordered audio effects':
            CURRENT.controls['Save chain as a new preset'].bindings['button'](None)
    def name_modal(dialog):
        active[0] = False
    widgets(monkeypatch, view.source, view.output, modal=modal, name_modal=name_modal)
    result = effects_dialog.show_effect_chain(view.frame, view.frame.library.root,
        (Effect('gain', gain_db=-3),), guard=guard)
    assert result is None
    store = EffectPresetStore(view.frame.library.root)
    assert store.list() == ()


def test_refused_capability_submission_keeps_modal_cleanup_and_current_status(view, monkeypatch):
    def refused(*args):
        return False
    view.frame.run_job = refused
    def modal(dialog):
        if dialog.title == 'Convert audio and apply effects':
            CURRENT.controls['Edit ordered effect chain and presets'].bindings['button'](None)
            assert 'did not accept effect discovery' in CURRENT.controls['Audio conversion effect settings'].GetValue()
            dialog.EndModal(2)
    widgets(monkeypatch, view.source, view.output, modal=modal)
    audio_conversion.show_audio_conversion(view.frame)
    assert view.frame.reports == [] and not view.output.exists()


def test_preset_editor_reports_visible_commit_identity_after_actual_flush_failure(view, monkeypatch):
    import carr_library.durable_state as durable
    preset_root = view.frame.library.root / 'state/studio/effect-presets'
    committed = []
    def fail_directory_sync(path):
        assert Path(path) == preset_root
        path, = preset_root.glob('*.json')
        committed.append((path, path.read_bytes()))
        raise OSError('Synthetic directory flush failure after actual preset save')
    monkeypatch.setattr(durable, 'sync_directory', fail_directory_sync)
    def modal(dialog):
        if dialog.title == 'Ordered audio effects':
            CURRENT.controls['Save chain as a new preset'].bindings['button'](None)
            status = CURRENT.controls['Effect chain status']
            assert 'Saved preset: Reviewed user preset' in status.GetValue()
            assert 'Durability unconfirmed' in status.GetValue()
            assert 'Do not repeat Save' in status.GetValue()
            assert committed[0][0].stem in status.GetValue()
            assert status.focus
    widgets(monkeypatch, view.source, view.output, modal=modal)
    chain = (Effect('gain', gain_db=-3),)
    assert effects_dialog.show_effect_chain(view.frame, view.frame.library.root, chain) == chain
    assert len(committed) == 1
    path, raw = committed[0]
    assert path.read_bytes() == raw and path.stat().st_nlink == 1
    saved, = EffectPresetStore(view.frame.library.root).list()
    assert saved.id == path.stem and saved.effects == chain
