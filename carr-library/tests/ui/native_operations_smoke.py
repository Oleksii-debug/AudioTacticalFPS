"""Actual native task controls and responsive host-executor initialization."""
from pathlib import Path
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

    def while_blocked():
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
    try:
        dialog.ShowModal()
    finally:
        dialog.controller.dispose()
        frame.executor.submit(lambda: None).result(timeout=10)
        dialog.Destroy()
        wx.YieldIfNeeded()
    if errors:
        raise AssertionError('\n'.join(errors))
    assert state.get('close_dispatched_during_initialization')
    assert not frame.working

    actual = create_operations_dialog(frame)

    def loaded():
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

    wx.CallAfter(begin_actual)
    try:
        actual.ShowModal()
    finally:
        actual.controller.dispose()
        frame.executor.submit(lambda: None).result(timeout=10)
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
    destroyed, late_reports = [], []
    deletion_loop = wx.GUIEventLoop()

    def observed_destruction(event):
        if event.GetEventObject() is detached:
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
    assert late_reports == [True] and controller.status == before_status
    assert controller.closed and controller._token is None
    return {'status': 'PASS', 'host_executor_initialization': 'PASS',
            'native_close_during_initialization': 'PASS',
            'named_controls_and_policy_save': 'PASS', 'native_destruction': 'PASS',
            'manual_nvda': 'NOT_RUN', 'measured_hardware_capacity': 'NOT_RUN'}
