"""Actual native task controls and responsive host-executor initialization."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import traceback


def run_native_operations(frame, directory):
    """Use an installed host; does not certify manual NVDA or hardware capacity."""
    import wx
    from carr_library.operations import ResourceBroker
    from carr_library.ui.operations_dialog import OperationsController, create_operations_dialog, policy_from_fields

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
            assert isinstance(actual.pages.GetPage(1), wx.ScrolledWindow)
            assert actual.pages.GetPage(1).GetScrollPixelsPerUnit() == (0, 12)
            assert actual.cpu.GetName() == 'Allowed CPU slots'
            assert actual.buttons['save'].IsEnabled()
            assert actual.maximum.GetName() == 'Maximum live reservations'
            assert actual.reservations.GetName() == 'Current resource reservations'
            actual.pages.SetSelection(1)
            before = ResourceBroker.for_app(frame.library.root).limits
            actual.gpu.ChangeValue('cuda:0=not-a-number')
            click(actual.buttons['save'])
            assert not actual.controller.busy
            assert ResourceBroker.for_app(frame.library.root).limits == before
            actual.cpu.SetValue(1)
            actual.memory.SetValue(128)
            actual.disk.SetValue(1)
            actual.free.SetValue(0)
            actual.maximum.SetValue(3)
            actual.gpu.ChangeValue('')
            state['saved_limits'] = policy_from_fields(cpu=1, memory_mib=128, disk_gib=1,
                free_mib=0, gpu='', max_leases=3, previous=actual.controller.policy).to_dict()
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
    destroyed, late_reports, destroy_ids = [], [], []
    detached_id, detached_handle = detached.GetId(), detached.GetHandle()
    deletion_loop = wx.GUIEventLoop()

    def observed_destruction(event):
        # Top-level destruction is sent after the derived C++/Python wrapper
        # lifetime. Correlate the captured native ID, not Python ``is``.
        destroy_ids.append(event.GetId())
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
        # This probe runs before App.MainLoop. Service a real wx idle pass
        # after the queued late callback, including native pending deletion.
        wx.CallAfter(wx.GetApp().ProcessIdle)
        deletion_loop.Run()
    finally:
        watchdog.Stop()
    assert destroyed == [True], ('The native task-view destruction event was not observed: '
                                  + repr({'expected_id': detached_id, 'observed_ids': destroy_ids}))
    import ctypes
    from ctypes import wintypes
    is_window = ctypes.WinDLL('user32', use_last_error=True).IsWindow
    is_window.argtypes, is_window.restype = [wintypes.HWND], wintypes.BOOL
    assert not is_window(detached_handle), 'The destroyed task view retained its native HWND.'
    assert late_reports == [True] and controller.status == before_status
    assert controller.closed and controller._token is None
    return {'status': 'PASS', 'host_executor_initialization': 'PASS',
            'native_close_during_initialization': 'PASS',
            'named_controls_and_policy_save': 'PASS', 'native_destruction': 'PASS',
            'manual_nvda': 'NOT_RUN', 'measured_hardware_capacity': 'NOT_RUN'}


def main():
    """Launch against the installed package; never add a source checkout to sys.path."""
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Windows native wx controls are required.'}))
        return 77
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
