"""Actual native task controls and responsive host-executor initialization."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import traceback


def run_native_operations(frame, directory):
    """Use an installed host; does not certify manual NVDA or hardware capacity."""
    import wx
    from carr_library.operations import ResourceBroker
    from carr_library.ui.operations_dialog import OperationsController, create_operations_dialog

    Path(directory).mkdir(parents=True, exist_ok=True)
    entered = threading.Event()
    errors, state = [], {'foreground_ticks': 0}
    deadline = time.monotonic() + 30

    def blocked(library, cancelled):
        entered.set()
        while not cancelled():
            time.sleep(.01)
        raise InterruptedError('Native task initialization cancelled by its close control.')

    dialog = create_operations_dialog(frame, controller_factory=lambda host, **kwargs:
        OperationsController(host, service_factory=blocked, **kwargs))

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event)

    def fail(current):
        errors.append(traceback.format_exc())
        current.controller.dispose()
        if current.IsModal():
            current.EndModal(wx.ID_CANCEL)

    def modal_timeout(current, phase):
        errors.append('Timed out waiting for the native ' + phase + ' dialog to close.')
        current.controller.dispose()
        if current.IsModal():
            current.EndModal(wx.ID_CANCEL)

    def while_blocked():
        if dialog.controller.closed:
            return
        try:
            assert time.monotonic() < deadline, 'Native task initialization did not stop.'
            state['foreground_ticks'] += 1
            if not entered.is_set():
                wx.CallLater(25, while_blocked)
                return
            assert dialog.controller.busy and frame.working
            assert dialog.status.GetName() == 'Tasks and resources status'
            assert dialog.listing.GetName() == 'Saved jobs'
            assert dialog.buttons['close'].IsEnabled()
            dialog.buttons['close'].SetFocus()
            wx.YieldIfNeeded()
            assert wx.Window.FindFocus() is dialog.buttons['close']
            click(dialog.buttons['close'])
            assert dialog.controller.close_pending
            state['close_dispatched_during_initialization'] = True
        except BaseException:
            fail(dialog)

    def begin():
        try:
            assert dialog.controller.refresh()
            wx.CallLater(25, while_blocked)
        except BaseException:
            fail(dialog)

    wx.CallAfter(begin)
    modal_watchdog = wx.CallLater(30000, modal_timeout, dialog, 'initialization')
    try:
        dialog.ShowModal()
    finally:
        modal_watchdog.Stop()
        dialog.controller.dispose()
        try:
            frame.executor.submit(lambda: None).result(timeout=10)
        finally:
            dialog.Destroy()
            wx.YieldIfNeeded()
    if errors:
        raise AssertionError('\n'.join(errors))
    assert state.get('close_dispatched_during_initialization')
    assert not frame.working

    actual = create_operations_dialog(frame)

    def loaded():
        if actual.controller.closed:
            return
        try:
            assert time.monotonic() < deadline, 'Native resource policy did not load.'
            state['foreground_ticks'] += 1
            if actual.controller.busy:
                wx.CallLater(25, loaded)
                return
            assert actual.controller.policy is not None, actual.controller.status
            assert actual.pages.GetPageCount() == 2
            assert actual.cpu.GetName() == 'Allowed CPU slots'
            assert actual.buttons['save'].IsEnabled()
            state['saved_limits'] = actual.controller.policy.to_dict()
            actual.pages.SetSelection(1)
            click(actual.buttons['save'])
            wx.CallLater(25, saved)
        except BaseException:
            fail(actual)

    def saved():
        if actual.controller.closed:
            return
        try:
            assert time.monotonic() < deadline, 'Native resource policy did not save.'
            if actual.controller.busy:
                wx.CallLater(25, saved)
                return
            assert actual.controller.status.startswith('Resource allowances saved.'), actual.controller.status
            assert actual.controller.policy.to_dict() == state['saved_limits']
            state['policy_saved'] = True
            actual.close()
        except BaseException:
            fail(actual)

    def begin_actual():
        try:
            actual.controller.refresh()
            wx.CallLater(25, loaded)
        except BaseException:
            fail(actual)

    deadline = time.monotonic() + 30
    wx.CallAfter(begin_actual)
    modal_watchdog = wx.CallLater(30000, modal_timeout, actual, 'policy')
    try:
        actual.ShowModal()
    finally:
        modal_watchdog.Stop()
        actual.controller.dispose()
        try:
            frame.executor.submit(lambda: None).result(timeout=10)
        finally:
            actual.Destroy()
            wx.YieldIfNeeded()
    if errors:
        raise AssertionError('\n'.join(errors))
    assert state.get('policy_saved')
    assert ResourceBroker.for_app(frame.library.root).limits.to_dict() == state['saved_limits']
    detached = create_operations_dialog(frame)
    controller = detached.controller
    detached.listing.Destroy()
    wx.YieldIfNeeded()
    assert not controller.closed, 'A child destruction detached the whole task view.'
    before_status = controller.status
    detached_id = detached.GetId()
    destroyed, late_reports = [], []
    deletion_loop = wx.GUIEventLoop()

    def observed_destruction(event):
        # SIP may invalidate the original Python wrapper before the native
        # base destructor sends this event. The live window's captured ID
        # identifies its actual notification without depending on that wrapper.
        if event.GetId() == detached_id:
            destroyed.append(True)
            wx.CallAfter(deletion_loop.Exit)
        event.Skip()

    def late_report():
        late_reports.append(True)
        controller.report('Late task-view update after a destruction request.', focus=True)

    detached.Bind(wx.EVT_WINDOW_DESTROY, observed_destruction)
    watchdog = wx.CallLater(5000, deletion_loop.Exit)
    try:
        wx.CallAfter(late_report)
        detached.Destroy()
        # Native top-level deletion happens on a later idle pass. Detachment
        # must already protect queued callbacks before that event is delivered.
        assert controller.closed and controller._token is None
        deletion_loop.Run()
    finally:
        watchdog.Stop()
    assert destroyed == [True], 'The native task-view destruction event was not observed.'
    assert not detached, 'The destroyed task view still has a live native window.'
    assert late_reports == [True] and controller.status == before_status
    assert controller.closed and controller._token is None

    native_parent = wx.Dialog(frame, title='Native task-view parent deletion')
    try:
        native_child = create_operations_dialog(native_parent, controller_factory=lambda _parent, **kwargs:
            OperationsController(frame, **kwargs))
        native_controller = native_child.controller
        native_controller._token = object()
        child_id, child_events = native_child.GetId(), []
        child_status = native_controller.status

        def child_destroyed(event):
            if event.GetId() == child_id:
                child_events.append(True)
            event.Skip()

        def forbidden_python_destroy():
            raise AssertionError('Native parent deletion must bypass the Python Destroy override.')

        native_child.Bind(wx.EVT_WINDOW_DESTROY, child_destroyed)
        native_child.Destroy = forbidden_python_destroy
        # wxWindowBase::DestroyChildren calls the native base Destroy directly.
        assert native_parent.DestroyChildren()
        assert child_events == [True] and not native_child
        assert native_controller.closed and native_controller._token is None
        assert native_controller.cancelled()
        parent_late_reports = []
        def late_parent_report():
            parent_late_reports.append(True)
            native_controller.report('Late task-view report after native parent deletion.', focus=True)
        wx.CallAfter(late_parent_report)
        wx.GetApp().ProcessPendingEvents()
        assert parent_late_reports == [True] and native_controller.status == child_status
    finally:
        native_parent.Destroy()
        wx.YieldIfNeeded()
    # Exercise the public modal footer, including the interval where
    # ShowModal has returned but wx has not delivered EVT_WINDOW_DESTROY yet.
    from carr_library.ui import operations_dialog as operations_ui

    def modal_destroy_case(kind):
        assert not frame.working
        owner = wx.Dialog(frame, title='Tasks modal ' + kind + ' destruction')
        initial_factory = operations_ui.create_operations_dialog
        observed = {'destroy_calls': 0, 'destroy_events': [], 'requested': False}
        entered = threading.Event()
        case_errors, timers = [], []
        stopped = False
        current = None
        controller = None
        end = time.monotonic() + 30

        def blocked_initializer(library, cancelled):
            entered.set()
            while not cancelled():
                time.sleep(.01)
            raise InterruptedError('Actual modal task view was destroyed.')

        def finish_on_error(message):
            case_errors.append(message)
            if controller is not None:
                controller.dispose()
            if current and not current.IsBeingDeleted() and current.IsModal():
                current.EndModal(wx.ID_CANCEL)

        def request_destroy():
            if stopped:
                return
            try:
                assert time.monotonic() < end, 'The actual modal initializer did not start.'
                if not entered.is_set():
                    timers.append(wx.CallLater(25, request_destroy))
                    return
                assert current.IsModal() and controller.busy and frame.working
                observed['requested'] = True
                if kind == 'owner':
                    current.Destroy()
                else:
                    owner.Destroy()
            except BaseException:
                finish_on_error(traceback.format_exc())

        def factory(host):
            nonlocal current, controller
            current = initial_factory(owner, controller_factory=lambda _parent, **options:
                OperationsController(host, service_factory=blocked_initializer, **options))
            controller = current.controller
            identity = current.GetId()
            actual_destroy = current.Destroy

            def tracked_destroy():
                observed['destroy_calls'] += 1
                return actual_destroy()

            def destroyed_event(event):
                if event.GetId() == identity:
                    observed['destroy_events'].append(True)
                event.Skip()

            current.Destroy = tracked_destroy
            current.Bind(wx.EVT_WINDOW_DESTROY, destroyed_event)
            timers.append(wx.CallLater(25, request_destroy))
            return current

        operations_ui.create_operations_dialog = factory
        timers.append(wx.CallLater(30000, finish_on_error,
                                  'Timed out in the actual Tasks modal destruction probe.'))
        try:
            operations_ui.show_operations(frame)
            assert controller is not None and controller.closed
            assert controller.cancelled() and controller._token is None
            frame.executor.submit(lambda: None).result(timeout=10)

            deletion_loop = wx.GUIEventLoop()
            finish_deadline = time.monotonic() + 10
            def settled():
                if not current and not frame.working:
                    deletion_loop.Exit()
                elif time.monotonic() >= finish_deadline:
                    case_errors.append('Native Tasks destruction or its host callback did not finish.')
                    deletion_loop.Exit()
                else:
                    timers.append(wx.CallLater(25, settled))
            wx.CallAfter(settled)
            deletion_loop.Run()
            assert not case_errors, '\n'.join(case_errors)
            assert observed['requested'] and entered.is_set()
            assert observed['destroy_calls'] == (1 if kind == 'owner' else 0), dict(observed)
            assert observed['destroy_events'] == [True] and not current, dict(observed)
            assert not frame.working
        finally:
            stopped = True
            operations_ui.create_operations_dialog = initial_factory
            for timer in timers:
                timer.Stop()
            if controller is not None:
                controller.dispose()
            if current and not current.IsBeingDeleted():
                current.Destroy()
            if owner and not owner.IsBeingDeleted():
                owner.Destroy()
            wx.YieldIfNeeded()
        return dict(observed)

    state['modal_owner'] = modal_destroy_case('owner')
    state['modal_parent'] = modal_destroy_case('parent')
    return {'status': 'PASS', 'host_executor_initialization': 'PASS',
            'native_close_during_initialization': 'PASS',
            'named_controls_and_policy_save': 'PASS', 'native_destruction': 'PASS',
            'native_parent_destruction': 'PASS',
            'native_modal_owner_destruction': state['modal_owner'],
            'native_modal_parent_destruction': state['modal_parent'],
            'manual_nvda': 'NOT_RUN', 'measured_hardware_capacity': 'NOT_RUN'}


def main():
    """Launch against the installed package; never add a source checkout to sys.path."""
    import wx
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame

    application = wx.App(False)
    with TemporaryDirectory(prefix='resonance-native-operations-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Native tasks and resource acceptance')
        frame = DesktopFrame(library)
        frame.Show()
        try:
            result = run_native_operations(frame, root / 'fixture')
        finally:
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.ProcessPendingEvents()
        print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
