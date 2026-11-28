"""Current metadata handlers share real admission and stop at native owner death."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import runpy
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.audio import metadata
from carr_library.audio.cleanup import CleanupOwner
from carr_library.audio.models import AudioCleanupPending
from carr_library.operations import ResourceBroker, ResourceUnavailable
from carr_library.operations.broker import ResourceLease
from carr_library.ui import audio_metadata_dialog as ui


_helpers = runpy.run_path(str(Path(__file__).with_name('test_audio_metadata_dialog.py')))
session = _helpers['session']
native_view = _helpers['native_view']
reviewed = _helpers['reviewed']


def test_inspect_review_and_save_hold_the_actual_shared_policy(session, monkeypatch):
    observed = []
    for name in ('inspect_metadata', 'review_metadata', 'publish_metadata'):
        original = getattr(ui, name)
        def invoke(runtime, *args, _name=name, _original=original, **kwargs):
            leases = ResourceBroker.for_app(session.frame.library.root).snapshot()['leases']
            assert len(leases) == 1
            lease = leases[0]
            assert lease['operation_id'].startswith('audio-metadata:')
            assert lease['request']['cpu_slots'] == runtime.budget.threads
            assert lease['request']['memory_bytes'] >= 2 * runtime.budget.max_alloc_bytes
            if _name == 'publish_metadata':
                assert lease['request']['disk_bytes'] >= metadata.metadata_disk_bytes(args[0])
            observed.append(_name)
            return _original(runtime, *args, **kwargs)
        monkeypatch.setattr(ui, name, invoke)
    reviewed(session)
    session.controller.save(session.form)
    result, error, _ = session.perform()
    assert error is None and result.receipt_path.is_file()
    assert observed == ['inspect_metadata', 'review_metadata', 'publish_metadata']
    assert ResourceBroker.for_app(session.frame.library.root).snapshot()['leases'] == []


def test_policy_refusal_launches_no_inspection_or_output(session, monkeypatch):
    broker = ResourceBroker.for_app(session.frame.library.root)
    broker.configure(replace(broker.limits, memory_bytes=0))
    launched = []
    def forbidden(*args, **kwargs):
        launched.append(True)
        raise AssertionError('Native inspection was started without resource admission.')
    monkeypatch.setattr(ui, 'inspect_metadata', forbidden)
    session.controller.inspect(str(session.source))
    result, error, _ = session.perform()
    assert result is None and isinstance(error, ResourceUnavailable)
    assert launched == [] and not Path(session.form.output).exists()
    assert broker.snapshot()['leases'] == []


def test_pending_native_owner_retains_its_actual_staging_and_reservation(session, monkeypatch):
    reviewed(session)
    before = session.source.read_bytes()
    ready = Event()
    staged, handles = [], []
    def close():
        if not ready.is_set():
            return False
        for handle in handles:
            handle.close()
        return True
    owner = CleanupOwner(close)
    original = metadata.run_process
    def pending(command, **kwargs):
        if str(command[-1]).endswith('tagged.flac'):
            directory = Path(command[-1]).parent
            staged.append(directory)
            handles.append((directory / 'held-worker-input').open('wb'))
            raise AudioCleanupPending('Injected unconfirmed native shutdown', cleanup_owner=owner)
        return original(command, **kwargs)
    monkeypatch.setattr(metadata, 'run_process', pending)
    session.controller.save(session.form)
    try:
        result, error, _ = session.perform()
        assert result is None and isinstance(error, AudioCleanupPending)
        assert error.cleanup_owner is owner and owner.close() is False
        assert len(staged) == 1 and staged[0].is_dir()
        assert len(ResourceBroker.for_app(session.frame.library.root).snapshot()['leases']) == 1
        assert not Path(session.form.output).exists() and session.source.read_bytes() == before
    finally:
        ready.set()
        assert owner.close() is True
    assert not staged[0].exists()
    assert ResourceBroker.for_app(session.frame.library.root).snapshot()['leases'] == []


@pytest.mark.parametrize('exception', [OSError, KeyboardInterrupt])
def test_verified_saved_copy_is_not_relabelled_after_resource_release_error(session, monkeypatch, exception):
    reviewed(session)
    release = ResourceLease.release
    def interrupted(self):
        release(self)
        if Path(session.form.output).exists():
            raise exception('injected error after actual reservation release')
    monkeypatch.setattr(ResourceLease, 'release', interrupted)
    session.controller.save(session.form)
    result, error, _ = session.perform()
    assert error is None and result.audio.path == Path(session.form.output)
    assert result.audio.sha256 == sha256(result.audio.path.read_bytes()).hexdigest()
    assert result.receipt_path.is_file() and 'resource release' in result.warning.lower()


def test_released_lease_metadata_notice_preserves_actual_saved_copy(session, monkeypatch):
    reviewed(session)
    broker = ResourceBroker.for_app(session.frame.library.root)
    unlink = Path.unlink
    def unavailable(path, *args, **kwargs):
        if path.parent == broker.leases_root and path.suffix == '.json':
            raise OSError('injected stale lease JSON cleanup failure')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', unavailable)
        session.controller.save(session.form)
        result, error, _ = session.perform()
    assert error is None and result.receipt_path.is_file()
    assert 'Shared resource release was not confirmed: OSError' in result.warning
    assert 'injected stale lease JSON cleanup failure' in result.warning
    assert result.audio.sha256 == sha256(result.audio.path.read_bytes()).hexdigest()
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('method', ['direct', 'native-parent'])
@pytest.mark.parametrize('phase', ['pending', 'queued', 'saved'])
def test_actual_dialog_owner_death_rejects_late_handlers_and_worker_views(native_view, method, phase):
    view, session = native_view, native_view.session
    pending = []
    controllers = []
    def scenario(dialog):
        controller = dialog.metadata_controller
        controllers.append(controller)
        if phase == 'saved':
            reviewed(session)
            # Bind the actual view controller to the same explicitly reviewed plan.
            controller.snapshot = session.controller.snapshot
            controller.plan = session.controller.plan
            controller.runtime = session.controller.runtime
            controller.form = session.controller.form
            controller.save(session.form)
            result, error, apply = session.perform(finish=False)
            assert error is None and result.audio.path.is_file()
            pending.append((apply, result))
        else:
            controller.inspect(str(session.source))
            if phase == 'queued':
                result, error, apply = session.perform(finish=False)
                assert error is None and result.source.path == session.source
                pending.append((apply, None))
        if method == 'direct':
            dialog.Destroy()
        else:
            view.widget_type.Destroy(dialog)
        assert controller.closed and controller.cancel_event.is_set()
        assert dialog.Destroy() is False
        for widget in view.widgets.values():
            for event in (view.wx.EVT_BUTTON, view.wx.EVT_TEXT, view.wx.EVT_CHOICE):
                if event in widget.handlers:
                    widget.handlers[event]()
    view.state.scenario = scenario
    ui.show_audio_metadata(session.frame)
    if phase == 'pending':
        _, error, _ = session.perform()
        assert error is not None
    else:
        apply, saved = pending[0]
        apply()
        if saved is not None:
            assert controllers[0].result is saved and session.notices[-1].startswith('Saved ')
    assert view.forbidden == []


@pytest.mark.parametrize('method', ['direct', 'native-parent'])
@pytest.mark.parametrize('label', ['Choose source audio', 'Choose replacement cover', 'Choose new output filename'])
def test_owner_deletion_inside_actual_picker_never_reads_or_destroys_a_dead_peer(native_view, method, label):
    view, session = native_view, native_view.session
    view.pickers.append(session.source)
    def scenario(dialog):
        if label == 'Choose replacement cover':
            mode = view.widgets['Cover action']
            mode.SetSelection(1)
            mode.handlers[view.wx.EVT_CHOICE]()
        def delete(picker):
            if method == 'direct':
                dialog.Destroy()
            else:
                view.widget_type.Destroy(dialog)
            assert picker.destroyed
        view.state.picker_scenario = delete
        view.press(label)
        assert dialog.metadata_controller.closed
    view.state.scenario = scenario
    assert ui.show_audio_metadata(session.frame) is None
    assert view.forbidden == [] and not session.queue


def test_unrelated_child_destroy_does_not_close_the_metadata_owner(native_view):
    view = native_view
    def scenario(dialog):
        dialog.handlers[view.wx.EVT_WINDOW_DESTROY](SimpleNamespace(
            GetId=lambda: id(view.widgets['Title']), Skip=lambda: None))
        assert not dialog.metadata_controller.closed
        view.press('Close')
    view.state.scenario = scenario
    ui.show_audio_metadata(view.session.frame)
    assert view.forbidden == []


class _ProbeMetadataWindow:
    def __init__(self, parent, *, title='Audio metadata and cover', close_error=False):
        self.parent, self.title, self.close_error = parent, title, close_error
        self.modal, self.closed, self.ended = True, 0, []
        self.metadata_controller = SimpleNamespace(close=self.close)

    def GetParent(self):
        return self.parent

    def GetTitle(self):
        return self.title

    def close(self):
        self.closed += 1
        if self.close_error:
            raise RuntimeError('injected metadata controller close failure')

    def IsModal(self):
        return self.modal

    def EndModal(self, result):
        assert self.modal
        self.ended.append(result)
        self.modal = False


def _metadata_probe_driver(frame, windows, parents, errors, *, release=lambda: None):
    """Run the actual native probe's failure closures without claiming wx execution."""
    import ast
    import sys
    import traceback

    path = Path(__file__).resolve().parents[2] / 'scripts/windows_audio_metadata_smoke.py'
    parsed = ast.parse(path.read_text(encoding='utf-8'))
    names = {'metadata_dialog', 'owned_metadata_dialogs', 'abort', 'later', 'timeout'}
    functions = [node for node in ast.walk(parsed)
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    # The optional helper allows the same behavioral regression to run on the old probe.
    required = names - {'owned_metadata_dialogs'}
    assert required <= {node.name for node in functions}
    assert len(functions) == len({node.name for node in functions})
    factory = ast.parse(
        'def bind():\n'
        '    owner_loop = None\n'
        '    return SimpleNamespace(metadata_dialog=metadata_dialog, abort=abort, '
        'later=later, timeout=timeout)\n').body[0]
    factory.body[1:1] = functions
    scheduled, exits = [], []
    loop = SimpleNamespace(ScheduleExit=exits.append)

    def call_later(delay, callback):
        timer = SimpleNamespace(delay=delay, callback=callback)
        scheduled.append(timer)
        return timer

    environment = {
        'SimpleNamespace': SimpleNamespace, 'frame': frame,
        'gui': SimpleNamespace(GetMainLoop=lambda: loop), 'errors': errors,
        'metadata_parents': parents, 'release': release, 'timers': [],
        'traceback': traceback, 'sys': sys,
        'wx': SimpleNamespace(Dialog=_ProbeMetadataWindow, ID_CANCEL=2,
                              GetTopLevelWindows=lambda: list(windows), CallLater=call_later),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(path), 'exec'), environment)
    driver = environment['bind']()
    watchdogs = [node for node in ast.walk(parsed)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == 'later' and node.args
                 and isinstance(node.args[0], ast.Name) and node.args[0].id == 'timeout']
    assert len(watchdogs) == 1
    watchdog = compile(ast.Expression(body=watchdogs[0]), str(path), 'eval')
    driver.schedule_watchdog = lambda: eval(watchdog, {
        **environment, 'later': driver.later, 'timeout': driver.timeout})
    driver.scheduled, driver.exits = scheduled, exits
    return driver


def test_native_metadata_watchdog_still_unwinds_after_a_recorded_error(capsys):
    frame = SimpleNamespace(cancel_event=Event())
    parent = object()
    window = _ProbeMetadataWindow(parent)
    errors, ordinary = ['original native scenario failure'], []
    driver = _metadata_probe_driver(frame, [window], [parent], errors)
    driver.later(lambda: ordinary.append('must remain suppressed'))
    driver.schedule_watchdog()
    assert [timer.delay for timer in driver.scheduled] == [25, 120000]
    for timer in driver.scheduled:
        timer.callback()
    assert ordinary == [] and frame.cancel_event.is_set()
    assert window.closed == 1 and window.ended == [2] and not window.modal
    assert driver.exits == [1]
    assert errors[0] == 'original native scenario failure' and len(errors) == 2
    assert 'TimeoutError' in errors[1] and '120-second budget' in errors[1]
    assert 'NATIVE_METADATA_FAILURE:' in capsys.readouterr().err


@pytest.mark.parametrize('close_failure', [False, True])
def test_native_metadata_abort_unwinds_only_its_two_owned_modal_paths(close_failure):
    frame = SimpleNamespace(cancel_event=Event())
    parent, foreign_parent = object(), object()
    direct = _ProbeMetadataWindow(frame, close_error=close_failure)
    nested = _ProbeMetadataWindow(parent)
    foreign = _ProbeMetadataWindow(foreign_parent)
    other_title = _ProbeMetadataWindow(frame, title='Unrelated native dialog')
    errors = ['first failure remains authoritative']
    driver = _metadata_probe_driver(
        frame, [direct, nested, foreign, other_title, object()], [parent], errors)
    assert driver.metadata_dialog() is direct
    driver.later(lambda: None).callback()  # Retain the real outer-loop object despite errors.
    driver.abort()
    assert frame.cancel_event.is_set()
    assert direct.closed == nested.closed == 1
    assert direct.ended == nested.ended == [2] and not direct.modal and not nested.modal
    assert foreign.closed == other_title.closed == 0
    assert foreign.modal and other_title.modal and foreign.ended == other_title.ended == []
    assert driver.exits == [1] and errors[0] == 'first failure remains authoritative'
    assert len(errors) == 1 + int(close_failure)
    if close_failure:
        assert 'injected metadata controller close failure' in errors[1]


@pytest.mark.parametrize('failed_step', ['cancel', 'release'])
def test_native_metadata_abort_continues_owned_cleanup_after_an_earlier_cleanup_error(failed_step):
    def broken():
        raise OSError('injected ' + failed_step + ' failure')

    cancellation = Event()
    frame = SimpleNamespace(cancel_event=(
        SimpleNamespace(set=broken) if failed_step == 'cancel' else cancellation))
    window = _ProbeMetadataWindow(frame)
    errors = ['original failure']
    driver = _metadata_probe_driver(
        frame, [window], [], errors, release=broken if failed_step == 'release' else lambda: None)
    driver.later(lambda: None).callback()
    driver.abort()
    assert window.closed == 1 and window.ended == [2] and not window.modal
    assert driver.exits == [1] and errors[0] == 'original failure' and len(errors) == 2
    assert 'injected ' + failed_step + ' failure' in errors[1]


def _native_metadata_lifetime_selection(windows, host):
    """Execute the actual begin() selection, including its real helper when present."""
    import ast

    path = Path(__file__).resolve().parents[2] / 'scripts/windows_audio_metadata_smoke.py'
    parsed = ast.parse(path.read_text(encoding='utf-8'))
    begins = [node for node in ast.walk(parsed)
              if isinstance(node, ast.FunctionDef) and node.name == 'begin']
    assert len(begins) == 1
    entry = next(node for node in begins[0].body if isinstance(node, ast.Try)).body[0]
    assert isinstance(entry, ast.Assign)
    assert len(entry.targets) == 1 and isinstance(entry.targets[0], ast.Name)
    assert entry.targets[0].id == 'window'
    helpers = [node for node in ast.walk(parsed)
               if isinstance(node, ast.FunctionDef) and node.name == 'current_metadata_dialog']
    assert len(helpers) <= 1
    environment = {
        'host': host,
        'wx': SimpleNamespace(Dialog=_ProbeMetadataWindow,
                              GetTopLevelWindows=lambda: list(windows)),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[*helpers, entry], type_ignores=[])),
                 str(path), 'exec'), environment)
    return environment['window']


def _selectable_metadata_modal(parent, *, closed=False, destroying=False, modal=True):
    window = _ProbeMetadataWindow(parent)
    window.modal = modal
    window._destroy_requested = destroying
    window.metadata_controller.closed = closed
    return window


def test_native_metadata_lifetime_selects_current_modal_after_previous_owner_close():
    parent, foreign_parent = object(), object()
    previous = _selectable_metadata_modal(parent, closed=True, destroying=True, modal=False)
    wrong_owner = _selectable_metadata_modal(foreign_parent)
    current = _selectable_metadata_modal(parent)
    selected = _native_metadata_lifetime_selection(
        [previous, wrong_owner, current], parent)
    assert selected is current
    assert not current.metadata_controller.closed and current.modal
    assert previous.metadata_controller.closed and previous._destroy_requested
    assert previous.closed == wrong_owner.closed == current.closed == 0
    assert previous.ended == wrong_owner.ended == current.ended == []


@pytest.mark.parametrize('state', ['closed', 'destroying', 'not-modal'])
def test_native_metadata_lifetime_does_not_adopt_an_inactive_owner(state):
    parent = object()
    window = _selectable_metadata_modal(parent,
        closed=state == 'closed', destroying=state == 'destroying', modal=state != 'not-modal')
    with pytest.raises(AssertionError, match='exactly one live metadata modal'):
        _native_metadata_lifetime_selection([window], parent)
    assert window.closed == 0 and window.ended == []


def test_native_metadata_lifetime_rejects_ambiguous_live_modals():
    parent = object()
    first, second = (_selectable_metadata_modal(parent) for _ in range(2))
    with pytest.raises(AssertionError, match='exactly one live metadata modal'):
        _native_metadata_lifetime_selection([first, second], parent)
    assert first.closed == second.closed == 0
    assert first.ended == second.ended == []
