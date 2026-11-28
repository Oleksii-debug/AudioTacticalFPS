"""Actual installed native diagnostics preview, export and owner withdrawal.

Only authored file-picker answers and a hold after an actual completed snapshot
are controlled. The Desktop, registry, one executor, controls, keyboard events,
report bytes, resource snapshot and no-clobber writer are real. No NVDA or physical
keyboard claim is made.
"""
import argparse
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import threading
from tempfile import mkdtemp
from time import monotonic, sleep
from types import SimpleNamespace
import traceback

def _acceptance_bindings():
    # Use the checked-in gate helper by explicit sibling path, never product sys.path.
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_reader_acceptance_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OwnedModalAbort:
    """Unwind one owned modal loop per GUI turn, never its suspended parent."""

    def __init__(self, wx, app, owners, report):
        self.wx, self.app, self.owners, self.report = wx, app, owners, report
        self.active = False
        self.timer = None
        self.reported = set()

    def request(self):
        if not self.active:
            self.active = True
            self.wx.CallAfter(self._advance)

    def stop(self):
        self.active = False
        if self.timer is not None:
            self.timer.Stop()
            self.timer = None

    def _advance(self):
        self.timer = None
        if not self.active:
            return
        try:
            # Owners are innermost first. EndModal() requests a return; its
            # IsModal() remains true until that ShowModal() actually unwinds.
            for owner in self.owners():
                if owner is None:
                    continue
                dialog = owner.dialog
                if dialog and dialog.IsModal():
                    if dialog.IsShown():
                        owner.close()
                    self.timer = self.wx.CallLater(10, self._advance)
                    return
            self.active = False
            self.app.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            detail = traceback.format_exc()
            if detail not in self.reported:
                self.reported.add(detail)
                self.report(detail)
            # Cleanup refusal remains a failure. Do not force-delete a native
            # owner or report silence; the outer owned process bound applies.
            self.timer = self.wx.CallLater(25, self._advance)


def _nested_modal_failure_acceptance(wx, app, check, select_failure_handler):
    """Real Windows negative control for the probe's former nested-loop hang."""
    events, returned, failures = [], [], []
    owners = []
    abort = _OwnedModalAbort(wx, app, lambda: tuple(reversed(owners)), failures.append)

    def fail(detail):
        failures.append(detail)
        abort.request()

    def owner(label, parent):
        dialog = wx.Dialog(parent, title='Owned diagnostic failure control — ' + label)
        native_id = dialog.GetId()
        record = SimpleNamespace(dialog=dialog, loop=None, native_id=native_id)

        def destroyed(event):
            if event.GetId() == native_id:
                events.append(label)
            event.Skip()

        def close():
            if wx.EventLoopBase.GetActive() != record.loop:
                raise AssertionError('Attempted to close a suspended ' + label + ' loop.')
            dialog.EndModal(wx.ID_CANCEL)

        record.close = close
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        owners.append(record)
        return record

    def provoke(inner):
        inner.loop = wx.EventLoopBase.GetActive()
        try:
            raise AssertionError('deliberate owned nested-modal failure')
        except AssertionError:
            fail(traceback.format_exc())

    def open_inner(outer):
        outer.loop = wx.EventLoopBase.GetActive()
        inner = owner('inner', outer.dialog)
        wx.CallAfter(provoke, inner)
        try:
            inner.dialog.ShowModal()
            returned.append('inner')
        finally:
            if inner.dialog:
                inner.dialog.Destroy()

    # wxPython MainLoop returns immediately with no top-level windows. Own the
    # actual outer dialog before starting the loop, then show it on that loop.
    outer = owner('outer', None)

    def open_outer():
        wx.CallAfter(open_inner, outer)
        try:
            outer.dialog.ShowModal()
            returned.append('outer')
        finally:
            if outer.dialog:
                outer.dialog.Destroy()
        app.ExitMainLoop()

    select_failure_handler(fail)
    try:
        wx.CallAfter(open_outer)
        app.MainLoop()
    finally:
        abort.stop()
        select_failure_handler(None)
    check('actual nested-modal failure returns child before parent',
          returned == ['inner', 'outer']
          and len(failures) == 1
          and 'deliberate owned nested-modal failure' in failures[0])
    check('actual failed modal owners receive one native destruction each',
          events.count('inner') == 1 and events.count('outer') == 1
          and all(not item.dialog for item in owners))


def _run_owned_native():
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    result = run_process(
        [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-child'],
        budget=AudioBudget(timeout=270, max_log_bytes=2 * 1024 * 1024),
        owned_tree=True, allow_failure=True)
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace')); sys.stdout.flush()
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace')); sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'native_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN',
                          'manual_keyboard': 'NOT_RUN'}), flush=True)
        return 77
    if not args.native_child:
        return _run_owned_native()
    faulthandler.dump_traceback_later(240, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run_native()
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application, support_diagnostics, durable_state
    from carr_library.application import LibraryApp
    from carr_library.operations import broker, models
    from carr_library.research import export_file
    from carr_library.ui import desktop_app, diagnostics_dialog, command_registry, keyboard_settings
    from carr_library.audio import cleanup, process

    modules = (application, support_diagnostics, durable_state, broker, models,
               export_file, desktop_app, diagnostics_dialog, command_registry,
               keyboard_settings, cleanup, process)
    binding = _acceptance_bindings()
    identities = binding.verify_installed_sources(*modules)
    sibling = Path(__file__).with_name('windows_publishing_smoke.py')
    scripts = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), sibling)}
    checks, errors, jobs, callbacks, peers, choices = [], [], [], [], [], []
    frame, timer, failure_handler = None, None, None
    hold = SimpleNamespace(armed=False, ready=threading.Event(), release=threading.Event(), report=None)
    hold.release.set()

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def failed(detail):
        errors.append(detail)
        print('FAIL: diagnostics\n' + detail, file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failed(detail)
                hold.release.set()
                if frame:
                    frame.cancel_event.set()
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND

    def inside(peer, ancestor):
        for _ in range(64):
            if peer is None or not peer:
                return False
            if peer is ancestor:
                return True
            peer = peer.GetParent()
        raise ValueError('Native parent chain exceeds its bound.')

    def owners():
        windows = tuple(wx.GetTopLevelWindows())
        if len(windows) > 256:
            raise ValueError('Native owner inventory exceeds its bound.')
        modals = [p for p in windows if p and isinstance(p, wx.Dialog) and p.IsModal()
                  and (frame and inside(p, frame) or any(p is row.dialog for row in peers))]
        hidden = [p for p in modals if not p.IsShown()]
        if hidden:
            selected = hidden[0]
        elif modals:
            focused = wx.Window.FindFocus()
            top = wx.GetTopLevelParent(focused) if focused else None
            active = [p for p in modals if p.IsEnabled()]
            selected = next((p for p in active if p is top), None)
            if selected is None:
                if len(active) != 1:
                    raise RuntimeError('The owned modal is still returning.')
                selected = active[0]
        else:
            return ()
        return (SimpleNamespace(dialog=selected,
            close=lambda value=selected: value.EndModal(wx.ID_CANCEL)),)

    abort = _OwnedModalAbort(wx, gui, owners, failed)
    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value
    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)

    def pump(predicate, description, seconds=8):
        deadline = monotonic() + seconds
        while not predicate():
            if errors:
                raise RuntimeError('A native diagnostic callback failed.')
            if monotonic() >= deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)

    def focus(control):
        check('native control remains shown and enabled', bool(control) and control.IsShownOnScreen() and control.IsEnabled())
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(),
             'The actual diagnostic control did not receive foreground focus.')
        check('the complete focused control is inside its parent viewport',
              control.GetParent().GetClientRect().Contains(control.GetRect()))

    def raw_key(code, modifiers=()):
        pressed = []
        try:
            for modifier in modifiers:
                check('native modifier down', simulator.KeyDown(modifier))
                pressed.append(modifier)
            check('native key down', simulator.KeyDown(code))
            pressed.append(code)
            sleep(.015)
        finally:
            for selected in reversed(pressed):
                check('native key released', simulator.KeyUp(selected))

    def press(control):
        focus(control)
        raw_key(wx.WXK_SPACE)

    root = Path(mkdtemp(prefix='resonance-native-diagnostics-'))
    library = LibraryApp(root / 'private-library')
    library.create_workspace('Synthetic diagnostic library')
    private = library.root / 'private-source.txt'
    secret = 'private-source-provider-and-credential-928374'
    private.write_text(secret, encoding='utf-8')
    private_bytes = private.read_bytes()
    coordinator = broker.ResourceBroker.for_app(library.root)
    frame = desktop_app.DesktopFrame(library)
    frame.SetSize((1100, 850))
    frame.Show()
    gui.SetTopWindow(frame)
    executor = frame.executor
    actual_submit, actual_run_job = executor.submit, frame.run_job
    actual_prepare, actual_picker = support_diagnostics.prepare_report, wx.FileDialog

    def submit(operation, *args, **kwargs):
        record = SimpleNamespace(thread=None, future=None)
        def observed():
            record.thread = threading.get_ident()
            return operation(*args, **kwargs)
        record.future = actual_submit(observed)
        jobs.append(record)
        return record.future
    executor.submit = submit

    def run_job(title, operation, done=None, failed=None, **kwargs):
        callbacks.append(SimpleNamespace(done=done, failed=failed))
        return actual_run_job(title, operation, done=done, failed=failed, **kwargs)
    frame.run_job = run_job

    def capture(*args, **kwargs):
        report = actual_prepare(*args, **kwargs)
        if hold.armed:
            hold.armed = False
            hold.report = report
            hold.ready.set()
            if not hold.release.wait(20):
                raise TimeoutError('The actual diagnostic snapshot hold was not released.')
        return report
    support_diagnostics.prepare_report = capture

    class SelectedFile:
        """Only the explicit authored new-file selection is supplied by the fixture."""
        def __init__(self, parent, title, **kwargs):
            check('file selection is requested by the actual diagnostic owner', bool(parent) and inside(parent, frame))
            check('file picker is create-only without an overwrite question', kwargs['style'] == wx.FD_SAVE)
            check('an explicit authored destination is available', bool(choices))
            self.path, self.alive = choices.pop(0), True
        def ShowModal(self):
            return wx.ID_OK
        def GetPath(self):
            if not self.alive:
                raise RuntimeError('File picker is no longer alive.')
            return str(self.path)
        def Destroy(self):
            self.alive = False
        def __bool__(self):
            return self.alive
    wx.FileDialog = SelectedFile

    menu_state = SimpleNamespace(opened=0, highlighted=None)
    def menu_opened(event):
        menu_state.opened += 1
        event.Skip()
    def highlighted(event):
        menu_state.highlighted = event.GetId()
        event.Skip()
    frame.Bind(wx.EVT_MENU_OPEN, menu_opened)
    frame.Bind(wx.EVT_MENU_HIGHLIGHT_ALL, highlighted)

    def waiting(predicate, description, seconds=15):
        return predicate, description, monotonic() + seconds

    def quiet():
        return (not frame or not frame.working) and all(row.future.done() for row in jobs)

    def opened():
        dialog = getattr(frame, '_diagnostics_dialog', None)
        return dialog if dialog and dialog.IsShown() and dialog.IsModal() else None

    def observe(dialog):
        native_id, events = dialog.GetId(), []
        def destroyed(event):
            if event.GetId() == native_id:
                events.append(event.GetId())
            event.Skip()
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        peers.append(SimpleNamespace(dialog=dialog, events=events))
        return dialog

    def open_dialog():
        check('Help starts from the same idle desktop', quiet() and not owners())
        bar = frame.GetMenuBar()
        menus = [bar.GetMenu(i) for i in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(i).replace('&', '') == 'Help']
        check('one actual Help menu', len(menus) == 1)
        items = [item for item in menus[0].GetMenuItems() if not item.IsSeparator() and item.IsEnabled()]
        wanted = [i for i, item in enumerate(items) if item.GetItemLabelText() == 'Preview and export diagnostics…']
        check('the diagnostic command is reachable from the real Help menu', len(wanted) == 1)
        check('the same command is registered for the existing palette and keymap',
              frame.keyboard_commands.registry.get('help.diagnostics').command_id == 'help.diagnostics')
        before = menu_state.opened
        menu_state.highlighted = None
        focus(frame.status)
        raw_key(ord('H'), (wx.WXK_ALT,))
        yield waiting(lambda: menu_state.opened > before, 'The actual Help menu did not open', 8)
        raw_key(wx.WXK_HOME)
        yield waiting(lambda: menu_state.highlighted == items[0].GetId(), 'Help did not highlight its first command', 8)
        for item in items[1:wanted[0] + 1]:
            raw_key(wx.WXK_DOWN)
            yield waiting(lambda selected=item.GetId(): menu_state.highlighted == selected,
                          'Native menu navigation did not reach the expected command', 8)
        raw_key(wx.WXK_RETURN)
        yield waiting(lambda: opened() is not None, 'The diagnostic command did not open a native modal')

    def arm():
        hold.report = None
        hold.ready.clear()
        hold.release.clear()
        hold.armed = True

    state = SimpleNamespace(waiting=None, description='', entered=False, finished=False, started=monotonic())
    def scenario():
        check('native Windows controls and actual installed diagnostics are active', wx.Platform == '__WXMSW__' and bool(identities))
        yield from open_dialog()
        dialog = observe(opened())
        check('the initial dialog has no unreviewed report', dialog.session.preview is None and not dialog.save.IsEnabled())
        press(dialog.prepare)
        yield waiting(lambda: quiet() and dialog.session.preview is not None, 'Actual diagnostic collection did not complete')
        report = dialog.session.preview
        check('the native read-only preview contains the exact bytes',
              bool(dialog.preview.GetWindowStyleFlag() & wx.TE_READONLY)
              and dialog.preview.GetValue().encode('ascii') == report.payload)
        check('the preview identifies its exact size and hash',
              str(len(report.payload)) in dialog.status.GetValue() and report.sha256 in dialog.status.GetValue())
        check('private text, identifiers and library path are excluded',
              secret not in report.text and str(library.root) not in report.text)
        check('existing cooperative resource counts are actual and labelled',
              json.loads(report.payload)['resources']['state'] == 'available'
              and json.loads(report.payload)['resources']['usage']['active_reservations'] == 0)
        focus(dialog.preview)
        raw_key(wx.WXK_TAB)
        yield waiting(lambda: wx.Window.FindFocus() is dialog.status, 'Tab did not reach the named report status', 8)
        destination = root / 'selected-report.json'
        choices.append(destination)
        press(dialog.save)
        yield waiting(lambda: quiet() and destination.is_file(), 'The exact reviewed export was not published')
        check('the actual common writer saved precisely the preview', destination.read_bytes() == report.payload)
        choices.append(destination)
        press(dialog.save)
        yield waiting(lambda: quiet() and 'already exists' in dialog.session.message,
                      'An existing report was not visibly refused')
        check('no-clobber refusal preserves the existing report', destination.read_bytes() == report.payload)

        arm()
        press(dialog.prepare)
        yield waiting(hold.ready.is_set, 'The real snapshot did not reach the cancellation hold')
        press(dialog.cancel)
        yield waiting(dialog.session.cancelled.is_set, 'The native Cancel event did not reach its owner')
        check('native Cancel is acknowledged while the real completed snapshot remains held',
              dialog.session.busy and dialog.session.preview is None and not jobs[-1].future.done())
        hold.release.set()
        yield waiting(lambda: quiet() and not dialog.session.busy, 'Cancellation did not settle')
        check('native cancellation publishes no new preview', dialog.session.preview is None
              and 'cancelled' in dialog.session.message.lower())
        press(dialog.prepare)
        yield waiting(lambda: quiet() and dialog.session.preview is not None, 'Explicit retry did not prepare a fresh report')

        for action in ('close', 'direct_destroy', 'parent_close'):
            if action != 'close':
                yield from open_dialog()
                dialog = observe(opened())
            arm()
            press(dialog.prepare)
            yield waiting(hold.ready.is_set, 'The completed snapshot did not reach the native lifetime hold')
            owner = dialog.session
            callback = callbacks[-1].done
            returned_report = hold.report
            if action == 'close':
                press(dialog.close_button)
            elif action == 'direct_destroy':
                dialog.Destroy()
            else:
                frame.Close(force=True)
            yield waiting(lambda: not dialog, 'The actual native diagnostic owner was not destroyed')
            check('withdrawal cancels the current diagnostic owner: ' + action, owner.closed and owner.cancelled.is_set())
            hold.release.set()
            yield waiting(lambda: all(row.future.done() for row in jobs), 'The same existing worker did not release its held snapshot')
            if frame:
                yield waiting(quiet, 'The existing desktop did not deliver its settled completion')
            callback(returned_report)
            check('replayed genuine late result cannot recreate the preview: ' + action,
                  owner.preview is None and not dialog)
        check('all authored file choices were consumed explicitly', not choices)
        check('each actual dialog received exactly one native destruction',
              len(peers) == 3 and all(not row.dialog and len(row.events) == 1 for row in peers))
        check('every operation used the one existing desktop executor',
              jobs and all(row.thread != threading.get_ident() and row.future.done() for row in jobs))
        check('the original synthetic source bytes were never changed', private.read_bytes() == private_bytes)
        check('no diagnostic staging file remains', not list(root.glob('.research-export-*')))
        state.finished = True

    sequence = scenario()
    timer = wx.Timer(gui)
    def drive(event=None):
        if state.entered or state.finished:
            return
        state.entered = True
        try:
            if monotonic() - state.started > 180:
                raise TimeoutError('The native diagnostic workflow exceeded its finite bound.')
            if state.waiting is not None:
                predicate, state.description, deadline = state.waiting
                if not predicate():
                    if monotonic() > deadline:
                        raise TimeoutError(state.description)
                    return
                state.waiting = None
            try:
                state.waiting = next(sequence)
            except StopIteration:
                if not state.finished:
                    raise AssertionError('Native diagnostic scenario ended without all final checks.')
                timer.Stop()
                gui.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            failed(traceback.format_exc() + '\nWaiting: ' + state.description)
            timer.Stop()
            hold.release.set()
            if frame:
                frame.cancel_event.set()
            abort.request()
        finally:
            state.entered = False

    gui.Bind(wx.EVT_TIMER, drive, timer)
    successful = False
    try:
        timer.Start(30)
        gui.MainLoop()
        if errors or not state.finished:
            raise RuntimeError('Native diagnostics failed; see bounded failure records.')
        check('selected installed bytes remain exact after native export and teardown',
              binding.verify_installed_sources(*modules) == identities)
        check('probe and binding helper remain byte exact',
              {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), sibling)} == scripts)
        successful = True
    finally:
        timer.Stop()
        timer.SetOwner(None)
        abort.stop()
        hold.release.set()
        support_diagnostics.prepare_report = actual_prepare
        wx.FileDialog = actual_picker
        executor.submit = actual_submit
        if frame:
            frame.run_job = actual_run_job
            frame._closing = True
            frame.cancel_event.set()
        executor.shutdown(wait=True, cancel_futures=False)
        library.close_search()
        if frame:
            if getattr(frame, 'progress_panel', None) is not None:
                frame.progress_panel.close()
            frame.Destroy()
        gui.Yield()
        if successful:
            import shutil
            shutil.rmtree(root)
        else:
            print('RETAINED_FAILED_NATIVE_FIXTURE ' + str(root), file=sys.stderr, flush=True)

    print(json.dumps({'result': 'PASS', 'checks': len(checks), 'native_keyboard': 'PASS',
        'exact_redacted_preview_export': 'PASS', 'actual_create_only_refusal': 'PASS',
        'same_desktop_executor': 'PASS', 'actual_cancel_and_three_owner_withdrawals': 'PASS',
        'source_identity': identities, 'probe_and_binding': scripts,
        'native_file_picker': 'NOT_RUN', 'NVDA': 'NOT_RUN', 'physical_keyboard': 'NOT_RUN',
        'controlled_seams': ['explicit authored file-picker answers',
                            'hold after a real completed diagnostic snapshot']},
        ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
