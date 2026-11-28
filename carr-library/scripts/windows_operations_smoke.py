"""Installed Tasks/resources acceptance through real native keyboard controls.

Reuses PR143's task-view lifecycle cases and the existing native command
registry. Saved jobs and concurrent policy edits are synthetic test data; no
inference, external request, hardware benchmark or manual NVDA claim is made.
"""
import json
import os
from pathlib import Path
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
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
            return True  # Keep the queued failure-only modal unwind runnable.

    application = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    frame, timer, gate, detached = None, None, None, None
    held = [None]
    phase = ['start', monotonic()]
    deadline = monotonic() + 120
    detached_state = {}
    frame_identity, owner_loop, aborting = {}, [None], [False]
    native_events, watched_palettes = [], set()
    restore_invoke = None

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

    def owned_dialogs():
        return tuple(item for item in wx.GetTopLevelWindows()
                     if item and isinstance(item, wx.Dialog)
                     and item.GetTitle() in ('Find a command', 'Tasks and resources')
                     and item.GetParent()
                     and item.GetParent().GetId() == frame_identity.get('id')
                     and int(item.GetParent().GetHandle()) == frame_identity.get('handle'))

    def window(title):
        matches = [item for item in owned_dialogs()
                   if item.GetTitle() == title and item.IsShown() and item.IsModal()
                   and not item.IsBeingDeleted()]
        if len(matches) > 1:
            raise AssertionError('More than one live owned native dialog: ' + title)
        return matches[0] if matches else None

    def named(window, name):
        control, = [child for child in window.GetChildren() if child.GetName() == name]
        return control

    def note(kind, **values):
        if len(native_events) < 32:
            native_events.append({'kind': kind, 'stage': phase[0], **values})

    def observe_manager(manager):
        original = manager.invoke

        def invoke(identity):
            try:
                note('invoke-before', command=identity, available=manager.available(identity),
                     frame_enabled=frame.IsEnabled(), frame_working=frame.working)
            except Exception as exc:
                note('invoke-observation-error', error=type(exc).__name__ + ': ' + str(exc)[:128])
            try:
                result = original(identity)
            except Exception as exc:
                note('invoke-error', command=identity,
                     error=type(exc).__name__ + ': ' + str(exc)[:128])
                raise
            note('invoke-return', command=identity, result=result)
            return result

        manager.invoke = invoke

        def restore():
            if manager.invoke is invoke:
                manager.invoke = original
        return restore

    def observe_palette(palette):
        identity = (palette.GetId(), int(palette.GetHandle()))
        if identity in watched_palettes:
            return
        watched_palettes.add(identity)
        search = named(palette, 'Search commands by name')
        results = named(palette, 'Matching commands and shortcuts')
        status = named(palette, 'Command availability')
        run, = [child for child in palette.GetChildren()
                if isinstance(child, wx.Button) and child.GetId() == wx.ID_OK]
        note('palette-ready', run_enabled=run.IsEnabled(), status=status.GetValue()[:256],
             selected=results.GetSelection(), count=results.GetCount(),
             first_row=results.GetString(0)[:256] if results.GetCount() else '',
             frame_working=frame.working, frame_enabled=frame.IsEnabled())

        def observed(kind):
            def received(event):
                try:
                    note(kind, control_id=search.GetId(),
                         key=event.GetKeyCode() if kind == 'native-key-down' else None)
                except Exception as exc:
                    note('key-observation-error', error=type(exc).__name__ + ': ' + str(exc)[:128])
                finally:
                    event.Skip()  # The existing product handler owns activation.
            return received

        search.Bind(wx.EVT_KEY_DOWN, observed('native-key-down'))
        search.Bind(wx.EVT_TEXT_ENTER, observed('native-text-enter'))

    def failure_state():
        from itertools import islice
        def observed(callback):
            try:
                value = callback()
                return value[:512] if isinstance(value, str) else value
            except Exception as exc:
                return type(exc).__name__ + ': ' + str(exc)[:128]
        def describe(item):
            if not item:
                return {'live': False}
            parent = observed(item.GetParent)
            data = {'id': observed(item.GetId), 'title': observed(item.GetTitle),
                    'handle': observed(lambda: int(item.GetHandle())),
                    'shown': observed(item.IsShown),
                    'parent_id': observed(lambda: parent.GetId() if parent else None),
                    'parent_handle': observed(lambda: int(parent.GetHandle()) if parent else None)}
            if isinstance(item, wx.Dialog):
                data['modal'] = observed(item.IsModal)
            controller = getattr(item, 'controller', None)
            if controller is not None:
                data.update(busy=observed(lambda: controller.busy),
                            closed=observed(lambda: controller.closed),
                            policy_loaded=observed(lambda: controller.policy is not None),
                            status=observed(lambda: str(controller.status)))
            return data
        active = wx.EventLoopBase.GetActive()
        focus = wx.Window.FindFocus()
        value = {'stage': phase[0], 'stage_seconds': monotonic() - phase[1],
                 'frame_identity': dict(frame_identity),
                 'frame_working': observed(lambda: frame.working),
                 'frame_closing': observed(lambda: frame._closing),
                 'focus_id': observed(lambda: focus.GetId() if focus else None),
                 'focus_name': observed(lambda: focus.GetName() if focus else None),
                 'active_loop': observed(lambda: repr(active)),
                 'active_loop_running': observed(lambda: active.IsRunning() if active else False),
                 'owner_loop': observed(lambda: repr(owner_loop[0])),
                 'owner_loop_running': observed(lambda: owner_loop[0].IsRunning()
                                                if owner_loop[0] else False),
                 'native_events': list(native_events),
                 'windows': [describe(item) for item in islice(wx.GetTopLevelWindows(), 16)]}
        print('TASKS_FAILURE_STATE: ' + json.dumps(value, ensure_ascii=True), flush=True)

    def abort(detail):
        errors.append(detail)
        print('FAIL:', str(detail).encode('ascii', 'backslashreplace').decode('ascii'), flush=True)
        if aborting[0]:
            return
        aborting[0] = True

        def failed_cleanup():
            value = traceback.format_exc()
            errors.append(value)
            print('TASKS_CLEANUP_FAILURE:', value.encode('ascii', 'backslashreplace').decode('ascii'), flush=True)

        try:
            failure_state()
        except Exception:
            failed_cleanup()
        try:
            if timer is not None:
                timer.Stop()
        except Exception:
            failed_cleanup()
        try:
            release()
        except Exception:
            failed_cleanup()

        def unwind():
            try:
                for item in reversed(owned_dialogs()):
                    try:
                        controller = getattr(item, 'controller', None)
                        if controller is not None:
                            controller.dispose()
                    except Exception:
                        failed_cleanup()
                    finally:
                        try:
                            # EndModal may have hidden a closing dialog before its
                            # nested loop returns. Do not overwrite that result.
                            if item and item.IsModal() and item.IsShown():
                                item.EndModal(wx.ID_CANCEL)
                        except Exception:
                            failed_cleanup()
            except Exception:
                failed_cleanup()
            finally:
                try:
                    if gate is not None and gate.held:
                        gate.release()
                except Exception:
                    failed_cleanup()
                finally:
                    try:
                        loop = owner_loop[0]
                        if loop is None:
                            loop = application.GetMainLoop()
                        if loop is None:
                            raise RuntimeError('The native Tasks owner event loop is unavailable.')
                        # Schedule the captured outer loop, even while a modal
                        # loop is still returning. Failure remains recorded.
                        loop.ScheduleExit(1)
                    except Exception:
                        failed_cleanup()

        wx.CallAfter(unwind)

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
                command = manager.registry.get('operations.open')
                binding = manager.bindings[command.command_id]
                check('Tasks action belongs to the existing native command inventory',
                      command.context == 'main' and command.label == 'Tasks and resources'
                      and binding.item.GetItemLabel().replace('&', '') == 'Tasks and resources...')
                owner_loop[0] = application.GetMainLoop()
                check('actual wx main event loop is active', owner_loop[0] is not None)
                advance('open')
            elif name in ('open', 'open_blocked'):
                open_palette('palette' if name == 'open' else 'palette_blocked')
            elif name in ('palette', 'palette_blocked') and palette is not None:
                named(palette, 'Search commands by name').SetValue('Tasks and resources')
                observe_palette(palette)
                advance('select' if name == 'palette' else 'select_blocked')
            elif name in ('select', 'select_blocked') and palette is not None:
                search = named(palette, 'Search commands by name')
                results = named(palette, 'Matching commands and shortcuts')
                commands = [command for command in frame.keyboard_commands.registry.commands('main')
                            if search.GetValue().strip().casefold() in command.label.casefold()]
                check('native palette resolves only the existing Tasks action',
                      [command.command_id for command in commands] == ['operations.open']
                      and results.GetCount() == 1 and results.GetSelection() == 0
                      and results.GetString(0).split(' — ', 1)[0] == commands[0].label)
                key(search, wx.WXK_RETURN, 'loaded' if name == 'select' else 'blocked')
            elif name == 'loaded' and dialog is not None:
                if dialog.controller.busy:
                    return
                check('canonical journals load on the host executor', dialog.controller.policy is not None)
                check('native job controls have readable accessible names',
                      dialog.listing.GetName() == 'Saved jobs'
                      and dialog.status.GetName() == 'Tasks and resources status')
                check('stored completion is not presented as verified output',
                      'journal observation' in dialog.status.GetValue())
                selected = dialog.selection()
                check('actual saved assistant job is selected', selected is not None and selected.key == 'assistant:' + identity)
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
                # Synthetic concurrent owner edit: the reviewed form still
                # contains policy 1, while the canonical policy now has 3 slots.
                broker.configure(ResourceLimits(cpu_slots=3, memory_bytes=1024**3,
                                                disk_bytes=2*1024**3, min_free_disk_bytes=0))
                dialog.cpu.SetValue(2)
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
                dialog.cpu.SetValue(2)
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
                detached.listing.Destroy()
                advance('child_destroyed')
            elif name == 'child_destroyed':
                check('native child destruction does not dispose its parent controller',
                      not detached.controller.closed)
                detached_state['before'] = detached.controller.status
                detached_state['native_id'] = detached.GetId()
                def destroyed(event):
                    if event.GetId() == detached_state['native_id']:
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
                if not detached_state.get('destroyed') or not detached_state.get('late') or bool(detached):
                    return
                check('matching native task-view event and original wrapper deletion are both observed',
                      detached_state['destroyed'] and not bool(detached))
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
        check('native Windows wx is active', wx.Platform == '__WXMSW__')
        with TemporaryDirectory(prefix='resonance-tasks-native-') as directory:
            library = LibraryApp(Path(directory) / 'library')
            workspace = library.create_workspace('Synthetic native task controls')
            assistant = AssistantService(library)
            identity = assistant.submit(assistant.preview('List books', Scope(workspace)))
            broker = ResourceBroker.for_app(library.root, limits=ResourceLimits(cpu_slots=1,
                memory_bytes=1024**3, disk_bytes=2*1024**3, min_free_disk_bytes=0))
            frame = DesktopFrame(library)
            frame_identity.update(id=frame.GetId(), handle=int(frame.GetHandle()))
            restore_invoke = observe_manager(frame.keyboard_commands)
            try:
                application.SetTopWindow(frame)
                frame.Show()
                timer = wx.Timer(frame)
                frame.Bind(wx.EVT_TIMER, tick, timer)
                timer.Start(35)
                application.MainLoop()
                check('all native Tasks/resources stages completed', phase[0] == 'complete' and not errors)
            finally:
                if restore_invoke is not None:
                    restore_invoke()
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
        if native_handle is not None:
            native_handle.close()
    result = {'status': 'FAIL' if errors else 'PASS', 'checks': checks, 'errors': errors,
              'native_keyboard': 'INCOMPLETE' if errors else 'PASS', 'nvda': 'NOT_RUN',
              'manual_keyboard': 'NOT_RUN', 'measured_hardware_capacity': 'NOT_RUN',
              'model_inference': 'NOT_RUN'}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
