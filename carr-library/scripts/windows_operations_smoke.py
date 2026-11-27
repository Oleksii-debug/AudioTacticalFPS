"""Installed Tasks/resources acceptance through real native keyboard controls.

Reuses PR143's task-view lifecycle cases and the existing native command
registry. Saved jobs and concurrent policy edits are synthetic test data; no
inference, external request, hardware benchmark or manual NVDA claim is made.
"""
from dataclasses import replace
from hashlib import sha256
from importlib import import_module
import json
import os
from pathlib import Path
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
import threading
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'native_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Scope
    from carr_library.operations import ResourceBroker, ResourceLimits
    from carr_library.operations._locks import NativeLock
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import operations_dialog as ui
    from carr_library.operations import facade as facade_module

    checks, errors = [], []
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    application = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    frame, timer, gate, detached = None, None, None, None
    held = [None]
    phase = ['start', monotonic()]
    deadline = monotonic() + 120
    detached_state = {}

    def expired():
        print(json.dumps({'status': 'FAIL', 'reason': 'Native Tasks cleanup watchdog expired',
                          'stage': phase[0], 'checks': checks}), flush=True)
        os._exit(1)

    watchdog = threading.Timer(150, expired)
    watchdog.daemon = True
    watchdog.start()

    def advance(name):
        phase[:] = [name, monotonic()]
        print('STAGE:', name, flush=True)

    def release():
        value, held[0] = held[0], None
        if value is not None and not simulator.KeyUp(*value):
            raise AssertionError('Native task-control key release failed.')

    def foreground(control):
        top = wx.GetTopLevelParent(control)
        handle = user32.GetForegroundWindow()
        owner = wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        ready = (owner.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
        return ready

    def key(control, value, next_phase, modifiers=wx.MOD_NONE):
        if held[0] is not None or not foreground(control):
            return False
        advance(next_phase)
        held[0] = (value, modifiers)
        try:
            if not simulator.KeyDown(value, modifiers):
                raise AssertionError('Native task-control key press failed.')
            wx.CallLater(40, release)
        except Exception:
            release()
            raise
        return True

    def window(title):
        return next((item for item in wx.GetTopLevelWindows()
                     if item and isinstance(item, wx.Dialog) and item.GetParent() is frame
                     and item.GetTitle() == title and item.IsShown()), None)

    def named(parent, accessible_name):
        def descendants(window):
            for child in window.GetChildren():
                yield child
                yield from descendants(child)
        found = [item for item in descendants(parent) if item.GetName() == accessible_name]
        if len(found) != 1:
            raise AssertionError('Expected one native control named ' + accessible_name)
        return found[0]

    def abort(detail):
        errors.append(detail)
        print('FAIL:', detail, flush=True)
        if timer is not None:
            timer.Stop()
        try:
            release()
        except Exception:
            errors.append(traceback.format_exc())
        for item in wx.GetTopLevelWindows():
            if item and isinstance(item, wx.Dialog) and item.GetParent() is frame:
                controller = getattr(item, 'controller', None)
                if controller is not None:
                    controller.dispose()
                if item.IsModal():
                    item.EndModal(wx.ID_CANCEL)
        if gate is not None and gate.held:
            gate.release()
        wx.CallAfter(application.ExitMainLoop)

    def open_palette(next_phase):
        return key(frame.status, ord('P'), next_phase, wx.MOD_CONTROL | wx.MOD_SHIFT)

    def tick(event=None):
        nonlocal gate, detached
        try:
            if monotonic() > deadline or monotonic() - phase[1] > 15:
                raise TimeoutError('Native Tasks/resources timed out at ' + phase[0])
            if held[0] is not None:
                return
            palette = window('Find a command')
            dialog = window('Tasks and resources')
            name = phase[0]
            if name == 'start':
                if frame.working:
                    return
                manager = frame.keyboard_commands
                command, = [item for item in manager.registry.commands('main')
                            if item.command_id == 'operations.open']
                binding = manager.bindings[command.command_id]
                check('Tasks action belongs to the existing native command inventory',
                      command.label == 'Tasks and resources'
                      and binding.item.GetItemLabel().split(chr(9), 1)[0].replace('&', '') == 'Tasks and resources...'
                      and manager.available(command.command_id))
                check('actual wx main event loop is active', application.GetMainLoop() is not None)
                advance('open')
            elif name in ('open', 'open_blocked'):
                open_palette('palette' if name == 'open' else 'palette_blocked')
            elif name in ('palette', 'palette_blocked') and palette is not None:
                named(palette, 'Search commands by name').SetValue('Tasks and resources')
                advance('select' if name == 'palette' else 'select_blocked')
            elif name in ('select', 'select_blocked') and palette is not None:
                results = named(palette, 'Matching commands and shortcuts')
                run = wx.Window.FindWindowById(wx.ID_OK, palette)
                expected = 'Tasks and resources — ' + (frame.keyboard_commands.shortcut('operations.open') or 'Not assigned')
                check('native palette resolves only the existing Tasks action',
                      results.GetCount() == 1 and results.GetSelection() == 0
                      and results.GetString(0) == expected and run is not None and run.IsEnabled())
                # The canonical launcher binds its actual Run button, not an
                # undocumented Enter handler on its ordinary search TextCtrl.
                key(run, wx.WXK_SPACE, 'loaded' if name == 'select' else 'blocked')
            elif name == 'loaded' and dialog is not None:
                if dialog.controller.busy:
                    return
                check('canonical journals load on the host executor', dialog.controller.policy is not None)
                check('native job controls have readable accessible names',
                      dialog.listing.GetName() == 'Saved jobs'
                      and dialog.status.GetName() == 'Tasks and resources status')
                check('stored completion is not presented as verified output',
                      'journal observation' in dialog.status.GetValue())
                check('initial task selection grants no implicit controls',
                      dialog.selection() is None and not dialog.buttons['pause'].IsEnabled()
                      and not dialog.buttons['cancel'].IsEnabled())
                key(dialog.listing, wx.WXK_HOME, 'selected_job')
            elif name == 'selected_job' and dialog is not None:
                selected = dialog.selection()
                check('actual saved assistant job is selected by native keyboard',
                      selected is not None and selected.key == 'assistant:' + identity)
                check('original pause action is available', dialog.buttons['pause'].IsEnabled())
                key(dialog.buttons['pause'], wx.WXK_SPACE, 'paused')
            elif name == 'paused' and dialog is not None and not dialog.controller.busy:
                if not dialog.controller.status.startswith('Pause requested.'):
                    return
                check('native Space records original assistant Pause without resubmission',
                      assistant.store.control(identity) == 'pause' and assistant.get(identity).status == 'queued')
                key(dialog.buttons['cancel'], wx.WXK_SPACE, 'cancelled')
            elif name == 'cancelled' and dialog is not None and not dialog.controller.busy:
                if not dialog.controller.status.startswith('Cancel requested.'):
                    return
                check('native Space records original Cancel without claiming acknowledgement',
                      assistant.store.control(identity) == 'cancel' and len(assistant.jobs()) == 1
                      and assistant.get(identity).status == 'queued')
                key(dialog.listing, wx.WXK_TAB, 'policy_page', wx.MOD_CONTROL)
            elif name == 'policy_page' and dialog is not None:
                if dialog.pages.GetSelection() != 1:
                    return
                check('native Ctrl+Tab reaches resource allowances',
                      dialog.cpu.GetName() == 'Allowed CPU slots'
                      and dialog.max_leases.GetName() == 'Maximum active reservations'
                      and bool(dialog.usage.GetWindowStyleFlag() & wx.TE_READONLY))
                # Advance before editing the form. Acquiring native focus for
                # the later Space action may require another timer tick.
                advance('save_stale_policy')
                # Synthetic concurrent owner edit: the reviewed form still
                # contains policy 1, while the canonical policy now has 3 slots.
                broker.configure(ResourceLimits(cpu_slots=3, memory_bytes=1024**3,
                                                disk_bytes=2*1024**3, min_free_disk_bytes=0))
                dialog.cpu.SetValue(2)
            elif name == 'save_stale_policy' and dialog is not None:
                key(dialog.buttons['save'], wx.WXK_SPACE, 'stale_policy')
            elif name == 'stale_policy' and dialog is not None and not dialog.controller.busy:
                if 'changed' not in dialog.controller.status:
                    return
                check('stale native policy cannot overwrite a newer owner edit', broker.limits.cpu_slots == 3)
                key(dialog.buttons['refresh'], wx.WXK_SPACE, 'refreshed_policy')
            elif name == 'refreshed_policy' and dialog is not None and not dialog.controller.busy:
                if dialog.controller.policy.cpu_slots != 3:
                    return
                check('native Refresh shows the actual current policy', dialog.cpu.GetValue() == 3)
                advance('save_refreshed_policy')
                dialog.cpu.SetValue(2)
            elif name == 'save_refreshed_policy' and dialog is not None:
                key(dialog.buttons['save'], wx.WXK_SPACE, 'saved_policy')
            elif name == 'saved_policy' and dialog is not None and not dialog.controller.busy:
                if not dialog.controller.status.startswith('Resource allowances saved.'):
                    return
                check('reviewed native policy save commits through the shared broker',
                      broker.limits.cpu_slots == 2 and dialog.controller.policy.cpu_slots == 2)
                key(dialog.status, wx.WXK_ESCAPE, 'closed')
            elif name == 'closed' and dialog is None and not frame.working:
                gate = NativeLock(broker.root / '.admission.lock')
                check('a real native admission lock is held for the responsiveness case', gate.acquire())
                advance('open_blocked')
            elif name == 'blocked' and dialog is not None:
                if not dialog.controller.busy or not frame.working:
                    return
                check('native close remains available during actual admission waiting',
                      dialog.buttons['close'].IsEnabled() and gate.held)
                key(dialog.buttons['close'], wx.WXK_SPACE, 'closed_blocked')
            elif name == 'closed_blocked' and dialog is None and not frame.working:
                check('native close cancels only the waiting task-view operation',
                      gate.held and not frame.cancel_event.is_set())
                gate.release()
                gate = None
                detached = ui.create_operations_dialog(frame)
                row = facade_module.OperationFacade(library, assistant=assistant).get('assistant', identity)
                detached.controller.policy = broker.limits
                detached.controller.jobs = (row,)
                detached.render()
                detached.listing.SetSelection(0)
                detached.selected()
                check('explicit selected native row exposes only its controls',
                      detached.buttons['pause'].IsEnabled())
                detached.controller.jobs = (replace(row, id='f' * 32),)
                detached.render()
                check('a refreshed replacement row never inherits selection or control',
                      detached.selection() is None and not detached.buttons['pause'].IsEnabled()
                      and not detached.buttons['cancel'].IsEnabled())
                detached.listing.Destroy()
                advance('child_destroyed')
            elif name == 'child_destroyed':
                check('native child destruction does not dispose its parent controller',
                      not detached.controller.closed)
                detached_state['before'] = detached.controller.status
                def destroyed(event):
                    if event.GetEventObject() is detached:
                        detached_state['destroyed'] = True
                    event.Skip()
                def late():
                    detached_state['late'] = True
                    detached.controller.report('Late callback must not touch destroyed controls.', focus=True)
                detached.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
                detached.Destroy()
                check('controller detaches before deferred native destruction', detached.controller.closed)
                wx.CallAfter(late)
                advance('destroyed')
            elif name == 'destroyed':
                if not detached_state.get('destroyed') or not detached_state.get('late'):
                    return
                check('queued callback cannot touch a destroyed native view',
                      detached.controller.status == detached_state['before'])
                check('job identity and original queued result survive every task-view action',
                      assistant.get(identity).status == 'queued' and len(assistant.jobs()) == 1)
                advance('complete')
                timer.Stop()
                application.ExitMainLoop()
        except Exception:
            abort(traceback.format_exc())

    try:
        purelib = Path(sysconfig.get_path('purelib')).resolve()
        check('Tasks UI and facade load from the installed wheel',
              all(Path(module.__file__).resolve().is_relative_to(purelib) for module in (ui, facade_module)))
        for relative in ('operations/models.py', 'operations/_locks.py', 'operations/broker.py',
                         'operations/facade.py', 'operations/cli.py', 'ui/operations_dialog.py',
                         'ui/transcription_dialog.py'):
            name = 'carr_library.' + relative[:-3].replace('/', '.')
            module = import_module(name)
            installed = Path(module.__file__).resolve(strict=True)
            source = Path(__file__).resolve().parents[1] / 'src' / 'carr_library' / relative
            check('installed source identity: ' + relative,
                  installed.is_relative_to(purelib)
                  and sha256(installed.read_bytes()).digest() == sha256(source.read_bytes()).digest())
        check('native Windows wx is active', wx.Platform == '__WXMSW__')
        with TemporaryDirectory(prefix='resonance-tasks-native-') as directory:
            library = LibraryApp(Path(directory) / 'library')
            workspace = library.create_workspace('Synthetic native task controls')
            assistant = AssistantService(library)
            identity = assistant.submit(assistant.preview('List books', Scope(workspace)))
            broker = ResourceBroker.for_app(library.root, limits=ResourceLimits(cpu_slots=1,
                memory_bytes=1024**3, disk_bytes=2*1024**3, min_free_disk_bytes=0))
            frame = DesktopFrame(library)
            try:
                application.SetTopWindow(frame)
                frame.Show()
                timer = wx.Timer(frame)
                frame.Bind(wx.EVT_TIMER, tick, timer)
                timer.Start(35)
                application.MainLoop()
                check('all native Tasks/resources stages completed', phase[0] == 'complete' and not errors)
            finally:
                if timer is not None:
                    timer.Stop()
                try:
                    release()
                finally:
                    try:
                        if gate is not None and gate.held:
                            gate.release()
                    finally:
                        if frame and not frame.IsBeingDeleted():
                            frame.cancel_event.set()
                            try:
                                frame.Close(force=True)
                            finally:
                                frame.executor.shutdown(wait=True, cancel_futures=True)
                                application.Yield()
    except Exception:
        errors.append(traceback.format_exc())
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            watchdog.cancel()
    result = {'status': 'FAIL' if errors else 'PASS', 'checks': checks, 'errors': errors,
              'native_keyboard': 'INCOMPLETE' if errors else 'PASS', 'nvda': 'NOT_RUN',
              'manual_keyboard': 'NOT_RUN', 'measured_hardware_capacity': 'NOT_RUN',
              'model_inference': 'NOT_RUN'}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
