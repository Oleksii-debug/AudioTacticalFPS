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
