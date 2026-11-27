"""Controlled dialogs prove queued and late scope; native widgets have a separate gate."""
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys

import pytest

from carr_library.ui import audio_conversion, audio_conversion_batch


class Control:
    def __init__(self, owner=None, *args, **options):
        self.owner, self.value = owner, options.get('value', False if options.get('_checkbox') else '')
        self.items, self.selection = list(options.get('choices', ())), 0
        self.bindings, self.enabled, self.name = {}, True, options.get('label', '')
    def SetName(self, value): self.name = value
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def GetString(self, value): return self.items[value]
    def SetFocus(self): pass
    def SetDefault(self):
        if hasattr(self, 'default_recorder'): self.default_recorder(self.name)
    def SetSizer(self, value): pass
    def SetScrollRate(self, *values): pass
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def Add(self, *args, **options): pass
    def Bind(self, event, callback): self.bindings[event] = callback
    def Destroy(self): pass


@pytest.fixture
def view(tmp_path, monkeypatch):
    root, output = tmp_path / 'library', tmp_path / 'outputs'
    root.mkdir(); output.mkdir()
    sources = [tmp_path / 'explicit-a.wav', tmp_path / 'explicit-b.wav']
    for path in sources:
        path.write_bytes(b'Explicit source; domain real-codec tests handle decoding.')
    frame = SimpleNamespace(library=SimpleNamespace(root=root), _closing=False,
        working=False, cancel_event=Event(), reports=[], jobs=[])
    frame.report = frame.reports.append
    def run_job(title, operation, done, failed, **kwargs):
        assert kwargs.get('with_progress') is True and kwargs.get('progress_unit') == 'files'
        future = Future()
        frame.jobs.append(SimpleNamespace(title=title, operation=operation, done=done,
            failed=failed, future=future))
        return future
    frame.run_job = run_job
    env = SimpleNamespace(root=tmp_path, frame=frame, output=output, sources=sources,
        hooks={}, approve=True, cancel=None, defaults=[], calls=[], selected=[], destroyed=[])
    class Dialog(Control):
        def __init__(self, owner=None, *args, **options):
            super().__init__(owner, *args, **options)
            self.title = options.get('title', '')
            self.stage = ('settings' if self.title == 'Batch audio conversion settings' else
                          'confirm' if self.title == 'Run reviewed audio batch?' else 'batch-options')
        def ShowModal(self):
            if self.stage in env.hooks: env.hooks[self.stage]()
            if self.stage == 'confirm': return 3 if env.approve else 4
            return 2 if env.cancel == self.stage else 1
        def EndModal(self, value): pass
        def CreateButtonSizer(self, flags): return Control(self)
        def FindWindowById(self, identity):
            value = Control(self)
            value.default_recorder = lambda name: env.defaults.append(name)
            return value
        def Destroy(self): env.destroyed.append(self.stage)
    class FileDialog(Dialog):
        def __init__(self, owner, message, **options):
            super().__init__(owner, title=message)
            self.stage = 'source'
            assert options['style'] & 128
        def GetPaths(self): return list(map(str, env.sources))
    class DirDialog(Dialog):
        def __init__(self, owner, message, **options):
            super().__init__(owner, title=message); self.stage = 'folder'
        def GetPath(self): return str(env.output)
    class MessageDialog(Dialog):
        def __init__(self, owner, message, title, flags):
            super().__init__(owner, title=title); self.stage = 'confirm'
            env.defaults.append(flags)
        def ShowModal(self):
            if self.stage in env.hooks: env.hooks[self.stage]()
            return 3 if env.approve else 4
    wx = SimpleNamespace(
        Dialog=Dialog, FileDialog=FileDialog, DirDialog=DirDialog, MessageDialog=MessageDialog,
        BoxSizer=Control, ScrolledWindow=Control, TextCtrl=Control, StaticText=Control,
        Button=Control, Choice=Control,
        CheckBox=lambda *args, **kwargs: Control(*args, _checkbox=True, **kwargs),
        ID_OK=1, ID_CANCEL=2, ID_YES=3, ID_NO=4, OK=1, CANCEL=2, YES_NO=4, NO_DEFAULT=8, YES=16, NO=32,
        ICON_QUESTION=16, DEFAULT_DIALOG_STYLE=0, RESIZE_BORDER=0, VERTICAL=0, ALL=0,
        EXPAND=0, TE_READONLY=0, TE_MULTILINE=0, DD_DEFAULT_STYLE=0, DD_DIR_MUST_EXIST=1,
        FD_OPEN=16, FD_FILE_MUST_EXIST=32, FD_MULTIPLE=128, EVT_BUTTON='button')
    monkeypatch.setitem(sys.modules, 'wx', wx)
    class Service:
        def __init__(self, library_root, *, scope_guard):
            self.guard, self.root = scope_guard, library_root
        def prepare(self, sources, directory, **options):
            self.guard()
            env.calls.append('prepare')
            env.selected.append(tuple(sources))
            return SimpleNamespace(fingerprint='f'*64, plan={
                'sources': [{'source': str(path), 'output': f'{index:03d}.wav'}
                            for index, path in enumerate(sources)],
                'directory': str(directory), 'format': options['format'],
                'settings': {'sample_rate': options['settings'].sample_rate,
                             'channels': options['settings'].channels,
                             'effects': [effect.to_dict() for effect in options['settings'].effects]}})
        def run(self, review, **options):
            self.guard()
            env.calls.append('run')
            return {'status': 'completed', 'done': 2, 'total': 2, 'receipt_persisted': True,
                    'manifest': str(env.output / 'conversion-manifest.json'), 'warning': None}
    monkeypatch.setattr(audio_conversion_batch, 'AudioConversionBatch', Service)
    return env


def change(view, kind):
    frame = view.frame
    if kind == 'object':
        frame.library = SimpleNamespace(root=frame.library.root)
    elif kind == 'root':
        root = view.root / 'different'; root.mkdir(); frame.library.root = root
    elif kind == 'same-path-replaced':
        root = frame.library.root
        root.rename(root.with_name(root.name + '-old')); root.mkdir()
    else:
        frame._closing = True


def complete_review(view):
    job = view.frame.jobs[0]
    value = job.operation(lambda *args: None)
    job.done(value)


def test_explicit_many_file_route_reuses_settings_and_the_shared_worker(view):
    audio_conversion.show_audio_conversion_batch(view.frame)
    assert len(view.frame.jobs) == 1 and view.calls == []
    complete_review(view)
    assert view.calls == ['prepare'] and view.selected == [tuple(view.sources)]
    assert len(view.frame.jobs) == 2 and view.defaults == ['Keep audio review without running']
    job = view.frame.jobs[1]
    result = job.operation(lambda *args: None)
    job.done(result)
    assert view.calls == ['prepare', 'run']
    assert '2/2 committed outputs' in view.frame.reports[-1]
    assert {'source', 'settings', 'folder', 'batch-options', 'confirm'} <= set(view.destroyed)


def test_default_no_review_never_admits_conversion(view):
    view.approve = False
    audio_conversion.show_audio_conversion_batch(view.frame)
    complete_review(view)
    assert view.calls == ['prepare'] and len(view.frame.jobs) == 1
    assert view.defaults == ['Keep audio review without running']
    assert 'not started' in view.frame.reports[-1]


@pytest.mark.parametrize('kind', ['object', 'root', 'same-path-replaced', 'closing'])
def test_queued_review_rechecks_exact_host_before_service_work(view, kind):
    audio_conversion.show_audio_conversion_batch(view.frame)
    change(view, kind)
    with pytest.raises(ValueError, match='library changed or is closing'):
        view.frame.jobs[0].operation(lambda *args: None)
    assert view.calls == [] and view.frame.reports == []


@pytest.mark.parametrize('kind', ['object', 'root', 'same-path-replaced', 'closing'])
def test_late_review_and_error_do_not_show_dialog_or_change_stale_view(view, kind):
    audio_conversion.show_audio_conversion_batch(view.frame)
    job = view.frame.jobs[0]
    value = job.operation(lambda *args: None)
    change(view, kind)
    job.done(value); job.failed(RuntimeError('Late worker error.'))
    assert len(view.frame.jobs) == 1 and view.defaults == [] and view.frame.reports == []


@pytest.mark.parametrize('stage', ['source', 'settings', 'folder', 'batch-options', 'confirm'])
def test_every_modal_return_rechecks_same_path_library_replacement(view, stage):
    view.hooks[stage] = lambda: change(view, 'same-path-replaced')
    audio_conversion.show_audio_conversion_batch(view.frame)
    if stage == 'confirm':
        complete_review(view)
        assert view.calls == ['prepare'] and len(view.frame.jobs) == 1
    else:
        assert view.frame.jobs == [] and view.calls == []
    assert view.frame.reports == []


@pytest.mark.parametrize('kind', ['object', 'root', 'same-path-replaced', 'closing'])
def test_queued_run_and_late_result_cannot_escape_reviewed_host(view, kind):
    audio_conversion.show_audio_conversion_batch(view.frame)
    complete_review(view)
    job = view.frame.jobs[1]
    change(view, kind)
    with pytest.raises(ValueError, match='library changed or is closing'):
        job.operation(lambda *args: None)
    job.done({'status': 'completed', 'done': 2, 'total': 2, 'receipt_persisted': True,
              'manifest': 'unused', 'warning': None})
    job.failed(RuntimeError('Late run error.'))
    assert view.calls == ['prepare'] and view.frame.reports == []


def test_cancelled_review_result_never_opens_approval_or_starts_run(view):
    audio_conversion.show_audio_conversion_batch(view.frame)
    job = view.frame.jobs[0]
    value = job.operation(lambda *args: None)
    view.frame.cancel_event.set(); job.done(value)
    assert len(view.frame.jobs) == 1 and view.defaults == []
    assert 'no audio outputs' in view.frame.reports[-1]


def test_partial_attention_is_presented_as_partial_not_success(view):
    audio_conversion.show_audio_conversion_batch(view.frame)
    complete_review(view)
    view.frame.jobs[1].done({'status': 'attention', 'done': 1, 'total': 2,
        'receipt_persisted': False, 'manifest': 'explicit folder/conversion-manifest.json',
        'warning': 'An unreceipted output needs explicit review.'})
    assert 'attention: 1/2' in view.frame.reports[-1]
    assert 'explicit review is required' in view.frame.reports[-1]
