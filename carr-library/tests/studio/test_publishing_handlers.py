"""Actual publication handlers and real FFmpeg exports with widget stand-ins."""
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.operation_progress import ProgressTracker
from carr_library.ui.publishing_dialog import create_publishing_dialog

_spec = importlib.util.spec_from_file_location('publishing_handler_fixtures', Path(__file__).with_name('test_publishing.py'))
_fixtures = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixtures)
actual_job = _fixtures.actual_job
source_job = _fixtures.source_job


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.value, self.label, self.choices = value, label, list(choices)
        self.selection, self.enabled, self.name = -1, True, ''
        self.identity, self.destroyed, self.handlers = id(self), False, {}
    def SetName(self, name): self.name = name
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def Set(self, values): self.choices = list(values); self.selection = -1
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection
    def Enable(self, enabled=True): self.enabled = enabled
    def IsEnabled(self): return self.enabled
    def GetId(self):
        assert not self.destroyed, 'The dying native wrapper was queried.'
        return self.identity
    def Bind(self, event, handler, *args, **kwargs): self.handlers[event] = handler
    def SetSizer(self, *args): pass
    def SetScrollRate(self, *args): pass
    def FitInside(self): pass
    def Wrap(self, *args): pass
    def SetFocus(self): self.focused = True
    def IsModal(self): return False
    def Hide(self): self.hidden = True


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass


@pytest.fixture
def desktop(source_job):
    service, state = source_job
    library = LibraryApp(service.root.parent.parent)
    constants = ('DEFAULT_DIALOG_STYLE','RESIZE_BORDER','VSCROLL','VERTICAL','HORIZONTAL','ALL','EXPAND',
                 'TE_MULTILINE','TE_READONLY','EVT_BUTTON','EVT_CLOSE','EVT_WINDOW_DESTROY','ID_OK','ID_CLOSE','FD_SAVE')
    wx = SimpleNamespace(**{name:index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog','StaticText','TextCtrl','Choice','Button','ScrolledWindow'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: callback(*args)
    progress = ProgressTracker()
    frame = SimpleNamespace(library=library, cancel_event=Event(), working=False, _closing=False,
                            operation_progress=progress, operations=[])
    def run_job(title, operation, done, failed):
        frame.working = True; frame.cancel_event.clear(); frame.operations.append(title)
        progress.start(title)
        try:
            done(operation())
        except Exception as exc:
            failed(exc)
        finally:
            frame.working = False
    frame.run_job = run_job
    frame.cleanups = []
    def register_cleanup(callback):
        assert callable(callback), 'Desktop cleanup accepts the owned close callback.'
        frame.cleanups.append(callback)
    frame.register_worker_cleanup = register_cleanup
    dialog = create_publishing_dialog(frame, wx_module=wx)
    return frame, dialog, state


def test_native_handler_requires_explicit_completed_job_and_destination(desktop):
    frame, dialog, _ = desktop
    assert not frame.operations
    dialog.on_export()
    assert not frame.operations and 'explicitly choose' in dialog.status.GetValue()
    dialog.on_refresh()
    assert len(dialog.jobs) == 1 and dialog.job_choice.GetSelection() == -1
    dialog.job_choice.SetSelection(0); dialog.on_export()
    assert len(frame.operations) == 1 and 'Choose a new EPUB' in dialog.status.GetValue()


def test_actual_native_handler_exports_and_verifies_same_receipt(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0)
    output = tmp_path / 'native-handler.epub'; dialog.output.ChangeValue(str(output))
    dialog.on_export()
    assert output.exists(), dialog.status.GetValue()
    assert not dialog.busy and not dialog.buttons['cancel'].IsEnabled()
    assert 'EPUB saved and its bytes verified' in dialog.status.GetValue()
    assert len(dialog.receipts) == 1 and dialog.receipt_choice.GetSelection() == 0
    dialog.on_verify()
    assert 'Verified exact saved EPUB bytes' in dialog.status.GetValue()
    original = output.read_bytes(); dialog.on_export()
    assert 'not be overwritten' in dialog.status.GetValue() and output.read_bytes() == original


def test_native_handler_obeys_shared_background_job_and_library_scope(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.output.ChangeValue(str(tmp_path / 'blocked.epub'))
    frame.working = True; dialog.on_export()
    assert len(frame.operations) == 1 and 'Another operation' in dialog.status.GetValue()
    frame.working = False; frame.library = LibraryApp(tmp_path / 'other-library'); dialog.on_export()
    assert len(frame.operations) == 1 and 'library location changed' in dialog.status.GetValue()
    assert not (tmp_path / 'blocked.epub').exists()


def test_close_cancels_and_suppresses_late_native_result(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.output.ChangeValue(str(tmp_path / 'late.epub'))
    pending = []
    frame.run_job = lambda title, operation, done, failed: pending.append((operation, done, failed))
    dialog.on_export(); assert dialog.busy
    dialog.on_close(); message = dialog.status.GetValue()
    assert dialog.cancel.is_set() and frame.cancel_event.is_set()
    operation, done, failed = pending[0]
    try: done(operation())
    except Exception as exc: failed(exc)
    assert dialog.status.GetValue() == message and not (tmp_path / 'late.epub').exists()


def test_invalid_budget_does_not_start_native_export(desktop, tmp_path):
    frame, dialog, _ = desktop
    dialog.on_refresh(); dialog.job_choice.SetSelection(0); dialog.output.ChangeValue(str(tmp_path / 'invalid.epub'))
    dialog.minutes.ChangeValue('nan'); dialog.on_export()
    assert len(frame.operations) == 1 and not (tmp_path / 'invalid.epub').exists()



# The router/source contracts have separate actual-PCM suites. These controls
# exercise current native-handler closures on the same real desktop worker.
def _inspection_report(selection):
    parts = selection.split(':')
    result = dict(schema='carr-silence-inspection/1', status='complete',
        job_id=parts[-1], selected_source_id=selection,
        source=dict(artifact='master.wav', sha256='a' * 64, size=16044),
        audio=dict(sample_rate=8000, channels=1, frames=8000, source_codec='pcm_s16le'),
        criteria=dict(threshold_dbfs=-50.0, minimum_seconds=.25,
            minimum_frames=2000, amplitude_limit_s32=6790939,
            channel_policy='every-channel', comparison='absolute-sample-at-or-below'),
        intervals=[dict(start_frame=2000, end_frame=6000,
            start_seconds=.25, end_seconds=.75, duration_seconds=.5)],
        interval_count=1, omitted_count=0, pcm_sha256='b' * 64,
        notice='Modeled handler result; no native PCM claim.')
    if parts[0] == 'director':
        result['source_family'] = 'director'
    return result


@pytest.fixture
def inspection_desktop(tmp_path, monkeypatch):
    import threading
    from carr_library.audio.silence import SilenceCriteria
    from carr_library.ui import publishing_dialog as ui
    from carr_library.ui import studio_silence_inspection as inspection
    owner = threading.get_ident()
    state = SimpleNamespace(queue=[], callbacks=[], instances=[], inspected=[], shown=[],
                            modal=lambda: None, inspect=None, choices=[])
    library = LibraryApp(tmp_path / 'inspection-library')
    class NativeWidget(Widget):
        def check(self):
            assert threading.get_ident() == owner, 'Worker touched a native publication control.'
            assert not self.destroyed, 'Late delivery touched a destroyed publication control.'
        def GetValue(self): self.check(); return super().GetValue()
        def GetSelection(self): self.check(); return super().GetSelection()
        def ChangeValue(self, value): self.check(); return super().ChangeValue(value)
        def Set(self, values): self.check(); return super().Set(values)
        def SetSelection(self, value): self.check(); return super().SetSelection(value)
        def Enable(self, value=True): self.check(); return super().Enable(value)
        def SetFocus(self): self.check(); return super().SetFocus()
    constants = ('DEFAULT_DIALOG_STYLE','RESIZE_BORDER','VSCROLL','VERTICAL','HORIZONTAL','ALL','EXPAND',
                 'TE_MULTILINE','TE_READONLY','EVT_BUTTON','EVT_CLOSE','EVT_WINDOW_DESTROY','ID_OK','ID_CLOSE','FD_SAVE')
    wx = SimpleNamespace(**{name:index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog','StaticText','TextCtrl','Choice','Button','ScrolledWindow'):
        setattr(wx, name, NativeWidget)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: state.callbacks.append((callback, args))
    rows = (
        dict(id='scene:scene-job', family='Scene Studio', title='Modeled scene', lines=2),
        dict(id='director:primary-project:primary-job', family='Audio Studio',
             title='Modeled primary', lines=2))
    class Router:
        def __init__(self, root, *, budget=None):
            assert Path(root) == library.root
            self.root, self.budget, self.closed = root, budget, False
            state.instances.append(self)
        def jobs(self): return rows
        def publications(self): return ()
        def inspect_silence(self, selection, **options):
            assert threading.get_ident() != owner
            assert not options['cancelled']()
            state.inspected.append((self, selection, options))
            if state.inspect is not None:
                return state.inspect(self, selection, options)
            options['progress']('Modeled completed-audio measurement.')
            return _inspection_report(selection)
        def close(self): self.closed = True
    monkeypatch.setattr(ui, 'LibraryPublicationService', Router)
    progress = ProgressTracker()
    frame = SimpleNamespace(library=library, cancel_event=Event(), working=False, _closing=False,
                            operation_progress=progress, operations=[], cleanups=[])
    frame.register_worker_cleanup = frame.cleanups.append
    def run_job(title, operation, done, failed):
        assert not frame.working
        frame.working = True
        frame.cancel_event.clear()
        frame.operations.append(title)
        progress.start(title)
        state.queue.append((operation, done, failed))
        return True
    frame.run_job = run_job
    def complete(*, deliver=True):
        operation, done, failed = state.queue.pop(0)
        outcomes = []
        def run():
            try: outcomes.append((done, operation()))
            except Exception as exc: outcomes.append((failed, exc))
        worker = threading.Thread(target=run)
        worker.start(); worker.join(20)
        assert not worker.is_alive() and len(outcomes) == 1
        frame.working = False
        callback, value = outcomes[0]
        if deliver:
            callback(value)
        return callback, value
    def drain():
        while state.callbacks:
            callback, args = state.callbacks.pop(0)
            callback(*args)
    def choose(parent, *, current):
        assert threading.get_ident() == owner and current()
        state.choices.append(parent)
        state.modal()
        return SilenceCriteria(-50, .25)
    def show(parent, result, *, current):
        assert threading.get_ident() == owner and current()
        text = inspection.result_text(result)
        assert result['source']['sha256'] in text and 'frames [2000, 6000)' in text
        state.shown.append(result)
    monkeypatch.setattr(inspection, 'choose_criteria', choose)
    monkeypatch.setattr(inspection, 'show_result', show)
    dialog = create_publishing_dialog(frame, wx_module=wx)
    dialog.on_refresh()
    complete()
    assert dialog.jobs is rows and dialog.job_choice.GetSelection() == -1
    yield SimpleNamespace(state=state, frame=frame, dialog=dialog, complete=complete,
        drain=drain, library=library, wx=wx, root=tmp_path, owner=owner)
    dialog.on_close()
    for callback in frame.cleanups:
        callback()


def _revoke_inspection(e, change):
    if change == 'library':
        e.frame.library = SimpleNamespace(root=e.library.root)
    elif change == 'job':
        e.dialog.job_choice.SetSelection(1 - e.dialog.job_choice.GetSelection())
    elif change == 'limits':
        e.dialog.minutes.ChangeValue('5')
    elif change == 'cancel':
        e.dialog.on_cancel()
    elif change == 'close':
        e.dialog.on_close()
    elif change == 'destroy':
        e.dialog.destroyed = True
        e.dialog.on_destroy(SimpleNamespace(GetId=lambda: e.dialog.identity, Skip=lambda: None))
    else:
        raise AssertionError(change)


@pytest.mark.parametrize('index', [0, 1], ids=['scene', 'director'])
def test_silence_handler_binds_exact_family_and_current_limits_without_publication_fields(inspection_desktop, index):
    e = inspection_desktop
    e.dialog.job_choice.SetSelection(index)
    for field in (e.dialog.language, e.dialog.creator, e.dialog.publisher, e.dialog.output):
        field.ChangeValue('')
    e.dialog.on_inspect_silence()
    assert e.dialog.inspection_active and e.dialog.busy
    assert e.dialog.buttons['cancel'].enabled and not e.state.inspected
    assert not e.dialog.buttons['silence'].enabled
    _, result = e.complete()
    selected = e.dialog.jobs[index]['id']
    assert result is e.state.shown[0] and result['selected_source_id'] == selected
    assert result['job_id'] == selected.split(':')[-1]
    service, identity, options = e.state.inspected[0]
    assert identity == selected and service.budget.timeout == 3600
    assert service.budget.max_output_bytes == 32 * 1024**3
    assert (options['threshold_dbfs'], options['minimum_seconds']) == (-50.0, .25)
    assert service.closed and options['cancelled']()
    assert e.dialog.receipts == () and e.dialog.output.GetValue() == ''
    assert not e.dialog.busy and not e.dialog.inspection_active
    assert e.dialog._inspection_cancel is None
    e.drain()
    assert e.state.shown == [result]


@pytest.mark.parametrize('invalid', ['no-selection', 'nonfinite-budget', 'unknown-family'])
def test_silence_handler_rejects_invalid_selection_or_limits_before_modal(inspection_desktop, invalid):
    e = inspection_desktop
    if invalid != 'no-selection':
        e.dialog.job_choice.SetSelection(0)
    if invalid == 'nonfinite-budget':
        e.dialog.minutes.ChangeValue('nan')
    elif invalid == 'unknown-family':
        e.dialog.jobs = (dict(e.dialog.jobs[0], id='unknown:scene-job'),)
    e.dialog.on_inspect_silence()
    assert not e.state.queue and not e.state.choices and not e.state.inspected
    assert not e.state.shown and not e.dialog.busy


@pytest.mark.parametrize('change', ['library', 'job', 'limits', 'close', 'destroy'])
def test_silence_handler_rechecks_modal_owner_without_retargeting(inspection_desktop, change):
    e = inspection_desktop
    e.dialog.job_choice.SetSelection(0)
    e.state.modal = lambda: _revoke_inspection(e, change)
    e.dialog.on_inspect_silence()
    assert len(e.state.choices) == 1
    assert not e.state.queue and not e.state.inspected and not e.state.shown


@pytest.mark.parametrize('change', ['library', 'cancel', 'close', 'destroy'])
def test_silence_handler_revokes_queued_worker_before_private_router(inspection_desktop, change):
    e = inspection_desktop
    e.dialog.job_choice.SetSelection(0)
    e.dialog.on_inspect_silence()
    _revoke_inspection(e, change)
    _, result = e.complete()
    assert isinstance(result, InterruptedError)
    assert len(e.state.instances) == 1 and not e.state.inspected and not e.state.shown
    assert not e.dialog.busy and not e.dialog.inspection_active
    e.drain()


@pytest.mark.parametrize('change', ['library', 'job', 'limits', 'cancel', 'close', 'destroy'])
def test_silence_handler_discards_late_report_and_queued_phase(inspection_desktop, change):
    e = inspection_desktop
    e.dialog.job_choice.SetSelection(0)
    e.dialog.on_inspect_silence()
    done, result = e.complete(deliver=False)
    assert len(e.state.inspected) == 1 and e.state.callbacks
    _revoke_inspection(e, change)
    done(result)
    e.drain()
    assert not e.state.shown and not e.dialog.busy
    assert not e.dialog.inspection_active and e.dialog._inspection_cancel is None
    assert e.state.inspected[0][2]['cancelled']()


@pytest.mark.parametrize('field,value', [
    ('job_id', 'other-job'), ('selected_source_id', 'director:other:scene-job'),
    ('source_family', 'director'),
])
def test_silence_handler_requires_both_local_and_picker_identity(inspection_desktop, field, value):
    e = inspection_desktop
    e.dialog.job_choice.SetSelection(0)
    def wrong(service, selected, options):
        return dict(_inspection_report(selected), **{field: value})
    e.state.inspect = wrong
    e.dialog.on_inspect_silence()
    e.complete()
    assert not e.state.shown and 'different selected Studio job' in e.dialog.status.GetValue()
    assert e.state.instances[-1].closed


@pytest.mark.parametrize('rejected', ['false', 'raised'])
def test_silence_worker_admission_failure_clears_operation_flags(inspection_desktop, rejected):
    e = inspection_desktop
    e.dialog.job_choice.SetSelection(0)
    original = e.frame.run_job
    def refuse(*args, **kwargs):
        if rejected == 'raised':
            raise RuntimeError('Modeled shared-worker admission failure.')
        return False
    e.frame.run_job = refuse
    e.dialog.on_inspect_silence()
    assert not e.dialog.busy and not e.dialog.inspection_active
    assert e.dialog._inspection_cancel is None and not e.state.queue
    assert e.dialog.buttons['silence'].enabled and not e.dialog.buttons['cancel'].enabled
    e.frame.run_job = original
    e.dialog.on_refresh()
    assert not e.dialog.inspection_active
    e.dialog.on_cancel()
    assert 'completed EPUB' in e.dialog.status.GetValue()
    e.complete()


@pytest.mark.parametrize('masked', [False, True], ids=['direct-owner', 'masked-owner'])
def test_silence_private_router_close_follows_pending_owner_after_window_close(inspection_desktop, masked):
    from tempfile import TemporaryDirectory
    from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner, retained_context
    e = inspection_desktop
    e.dialog.job_choice.SetSelection(0)
    ready, stages = Event(), []
    owner = CleanupOwner(lambda: ready.is_set())
    def pending(service, selected, options):
        with retained_context(TemporaryDirectory(dir=e.root, prefix='inspection-ui-owner-')) as folder:
            path = Path(folder) / 'owned-input'
            path.write_bytes(b'Actual retained local bytes for the UI ownership boundary.')
            stages.append(Path(folder))
            error = AudioCleanupPending('Modeled missing native acknowledgement.', cleanup_owner=owner)
            if masked:
                raise OSError('Modeled wrapper retaining its cleanup cause.') from error
            raise error
    e.state.inspect = pending
    try:
        e.dialog.on_inspect_silence()
        _, error = e.complete()
        assert isinstance(error, OSError if masked else AudioCleanupPending)
        private = e.state.instances[-1]
        assert len(stages) == 1 and stages[0].is_dir()
        assert not private.closed and not owner.quiescent
        e.dialog.on_close()
        assert e.state.instances[0].closed and not private.closed
        assert stages[0].is_dir() and not e.state.shown
        ready.set()
        assert owner.close()
        assert private.closed and not stages[0].exists()
    finally:
        ready.set()
        assert owner.close()


def test_silence_handler_actual_scene_pcm_keeps_master_job_and_receipts(desktop, monkeypatch):
    import threading
    import wave
    from hashlib import sha256
    from carr_library.audio import AudioBudget, AudioRuntime
    from carr_library.audio.silence import SilenceCriteria
    from carr_library.ui import publishing_dialog as ui
    from carr_library.ui import studio_silence_inspection as inspection
    frame, dialog, state = desktop
    owner, shown, workers = threading.get_ident(), [], []
    from inspect import getclosurevars
    wx = getclosurevars(dialog.on_inspect_silence).nonlocals['wx']
    posted = []
    monkeypatch.setattr(wx, 'CallAfter', lambda callback, *args: posted.append((callback, args)))
    original_router = ui.LibraryPublicationService
    def router(root, *, budget=None):
        service = original_router(root, budget=budget)
        service.production.runtime_factory = lambda directory, **options: AudioRuntime(
            directory, budget=AudioBudget(threads=1), **options)
        return service
    monkeypatch.setattr(ui, 'LibraryPublicationService', router)
    monkeypatch.setattr(inspection, 'choose_criteria',
                        lambda parent, current: SilenceCriteria(0, .001) if current() else None)
    def show(parent, value, *, current):
        assert threading.get_ident() == owner and current()
        assert 'Original master SHA-256' in inspection.result_text(value)
        shown.append(value)
    monkeypatch.setattr(inspection, 'show_result', show)
    dialog.on_refresh()
    dialog.job_choice.SetSelection(0)
    for field in (dialog.language, dialog.creator, dialog.publisher, dialog.output):
        field.ChangeValue('')
    master = Path(state['destination']) / 'master.wav'
    job_file = dialog.service.production.jobs_root / state['id'] / 'job.json'
    original = master.read_bytes(), job_file.read_bytes()
    controls = {str(path): path.read_bytes()
                for path in dialog.service.production.jobs_root.glob('*/control.json')}
    def run_job(title, operation, done, failed):
        frame.working = True; frame.cancel_event.clear()
        frame.operation_progress.start(title)
        values = []
        def run():
            workers.append(threading.get_ident())
            try: values.append((done, operation()))
            except Exception as exc: values.append((failed, exc))
        thread = threading.Thread(target=run)
        thread.start(); thread.join(30)
        assert not thread.is_alive() and len(values) == 1
        frame.working = False
        callback, result = values[0]
        callback(result)
        while posted:
            callback, args = posted.pop(0)
            callback(*args)
        return True
    frame.run_job = run_job
    dialog.on_inspect_silence()
    assert len(shown) == 1, dialog.status.GetValue()
    result = shown[0]
    with wave.open(str(master), 'rb') as pcm:
        assert result['audio']['frames'] == pcm.getnframes()
        assert result['intervals'][0]['end_frame'] == pcm.getnframes()
    assert result['interval_count'] == 1 and result['omitted_count'] == 0
    assert result['intervals'][0]['start_frame'] == 0
    assert result['source']['sha256'] == sha256(original[0]).hexdigest()
    assert result['job_id'] == state['id'] and result['selected_source_id'] == 'scene:' + state['id']
    assert (master.read_bytes(), job_file.read_bytes()) == original
    assert dialog.receipts == ()
    assert {str(path): path.read_bytes()
            for path in dialog.service.production.jobs_root.glob('*/control.json')} == controls
    assert workers and all(thread_id != owner for thread_id in workers)
    assert not dialog.service.production.broker.snapshot()['leases']
    dialog.on_close()
