"""Installed native named workflow review, typed macro and owner lifetime.

Uses the actual DesktopFrame command registry/executor, real TemplateStore and
AssistantService. Automated native keys open, save, preview and explicitly run
one typed local operation. No model, speech or network request is used.
"""
import argparse
import faulthandler
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import sys
from time import monotonic
import traceback


def _bindings():
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_workflow_acceptance_bindings', path)
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
        dialog = wx.Dialog(parent, title='Owned EPUB failure control — ' + label)
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
        budget=AudioBudget(timeout=210, max_log_bytes=1024 * 1024),
        owned_tree=True, allow_failure=True)
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace'))
    sys.stdout.flush()
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
    sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    if not options.native_child:
        return _run_owned_native()
    faulthandler.dump_traceback_later(180, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _run()
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application, backup_current
    from carr_library.assistant import models, service, templates
    from carr_library.ui import assistant_dialog, document_batch, keyboard_settings, workflow_templates
    from carr_library.ui import desktop_app

    modules = (application, models, service, templates, assistant_dialog,
               document_batch, keyboard_settings, workflow_templates, desktop_app, backup_current)
    bindings = _bindings()
    origins = bindings.verify_installed_sources(*modules)
    checks, errors, evidence = [], [], {}
    deadline = monotonic() + 150
    held = [None]
    pending_releases = []
    windows = []
    frame = None
    failure_handler = None
    abort = None
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

    def release_key():
        value, held[0] = held[0], None
        if value is not None and not simulator.KeyUp(*value):
            raise AssertionError('Native key release failed.')

    def report_failure(detail):
        errors.append(detail)
        print('FAIL: native workflow\n' + detail, file=sys.stderr, flush=True)

    def failed(detail=None):
        report_failure(detail or traceback.format_exc())
        for event in pending_releases:
            event.set()
        try:
            release_key()
        except Exception:
            errors.append(traceback.format_exc())
        abort.request()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failed(detail)
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    check('actual native Windows controls', wx.Platform == '__WXMSW__')

    def current_owners():
        # These sibling native windows are nested modal calls in this exact
        # order. Never EndModal the suspended workflow while its assistant
        # ShowModal has not returned.
        owners = []
        for title, callback in (
            ('Local assistant — review and run a bounded plan',
             lambda peer: peer.assistant_handlers['close']),
            ('Named workflow templates and keyboard macros', lambda peer: peer.close)):
            peer = next((window for window in wx.GetTopLevelWindows()
                         if window and isinstance(window, wx.Dialog)
                         and window.GetParent() is frame and window.GetTitle() == title), None)
            if peer is not None:
                owners.append(SimpleNamespace(dialog=peer, close=callback(peer)))
        return tuple(owners)

    abort = _OwnedModalAbort(wx, gui, current_owners, report_failure)
    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value
    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)

    def later(callback, delay=25):
        def invoke():
            if errors:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native workflow acceptance exceeded its deadline.')
                callback()
            except Exception:
                failed()
        wx.CallLater(delay, invoke)

    def until(predicate, completed, *, timeout=20):
        expires = monotonic() + timeout
        def poll():
            if predicate():
                completed()
            elif monotonic() >= expires:
                raise TimeoutError('Native workflow stage did not finish.')
            else:
                later(poll)
        later(poll)

    def foreground(control):
        owner = wx.GetTopLevelParent(control)
        handle = user32.GetForegroundWindow()
        process = wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process))
        ready = (process.value == os.getpid() and int(handle or 0) == int(owner.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            owner.Raise()
            user32.SetForegroundWindow(owner.GetHandle())
            control.SetFocus()
        return ready

    def key(control, code, completed, modifiers=wx.MOD_NONE):
        def down():
            check('key target has native foreground and focus', foreground(control))
            held[0] = code, modifiers
            check('native key down', simulator.KeyDown(code, modifiers))
            def up():
                release_key()
                later(completed)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def press(button, completed):
        key(button, wx.WXK_SPACE, completed)

    def dialog(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if window and isinstance(window, wx.Dialog)
                     and window.GetTitle() == title and window.IsShown()), None)

    with TemporaryDirectory(prefix='resonance-workflow-native-') as temporary:
        library = application.LibraryApp(Path(temporary) / 'library')
        store = templates.TemplateStore(library.root)
        original = store.save('Create only after review', 'assistant',
            {'steps': [{'tool': 'library.create', 'arguments': {'name': {'$parameter': 'name'}}}]},
            {'name': {'kind': 'text', 'label': 'New library name', 'example': 'Example only'}})
        saved_path = store.root / (original.id + '.json')
        observer = service.AssistantService(library)
        original_bytes = saved_path.read_bytes()
        desired_name = 'Workflow native explicit run'
        workflow_title = 'Named workflow templates and keyboard macros'
        assistant_title = 'Local assistant — review and run a bounded plan'

        def start():
            check('installer uses the current one-manager registry',
                  original.command_id in frame.keyboard_commands.bindings
                  and frame.keyboard_settings.manager is frame.keyboard_commands)
            check('startup only reads passive templates', not observer.jobs() and not library.workspaces()
                  and saved_path.read_bytes() == original_bytes)
            manager = frame.keyboard_commands
            manager.apply_overrides({**manager.overrides, original.command_id: 'Ctrl+Alt+Shift+F9'})
            manager.guard_native()
            check('typed macro has the actual saved accelerator',
                  manager.shortcut(original.command_id) == 'Ctrl+Alt+Shift+F9')
            key(frame.choice, wx.WXK_F9, opened, wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT)

        def opened():
            until(lambda: dialog(workflow_title) is not None
                  and not dialog(workflow_title).pending and not frame.working, saved_view)

        def saved_view():
            view = dialog(workflow_title)
            check('registered native macro opens the selected saved recipe',
                  view.IsModal() and view.previous is not None and view.previous.id == original.id)
            check('macro opening creates no plan job or library', not observer.jobs() and not library.workspaces())
            # Current explicit values are authored fixture input. The subsequent
            # Save/Review/Preview/Run actions are actual native keyboard dispatch.
            view.values.ChangeValue(json.dumps({'name': desired_name}))
            press(view.buttons['save'], lambda: until(
                lambda: view and not view.pending and not frame.working, lambda: saved(view)))

        def saved(view):
            value = store.get(original.id)
            check('keyboard Save persists the actual typed record without execution',
                  value.revision == 2 and not observer.jobs() and not library.workspaces())
            evidence['template_id'], evidence['template_revision'] = value.id, value.revision
            # Successful Save resets per-use values, so the next run gets an
            # explicit current value rather than inheriting an example.
            check('saved recipe has no inherited per-use value', view.values.GetValue() == '{}')
            view.values.ChangeValue(json.dumps({'name': desired_name}))
            press(view.buttons['review'], lambda: until(
                lambda: dialog(assistant_title) is not None, assistant_open))

        def assistant_open():
            current = dialog(assistant_title)
            controls = current.assistant_controls
            request = json.loads(controls['instruction'].GetValue())
            check('review opens the existing assistant with no inherited scope',
                  current.IsModal() and request['schema'] == templates.REQUEST_SCHEMA
                  and 'scope' not in request and request['values'] == {'name': desired_name}
                  and controls['scope'].GetSelection() == 0
                  and not controls['buttons']['run'].IsEnabled())
            check('review handoff still creates no queued job', not observer.jobs() and not library.workspaces())
            press(controls['buttons']['preview'], lambda: previewed(current))

        def previewed(current):
            controls = current.assistant_controls
            check('current scope preview enables Run without submitting',
                  controls['buttons']['run'].IsEnabled() and not observer.jobs() and not library.workspaces()
                  and desired_name in controls['review'].GetValue())
            press(controls['buttons']['run'], lambda: until(
                lambda: current and controls['buttons']['run'].IsEnabled()
                and len(observer.jobs()) == 1 and observer.jobs()[0].status == 'complete',
                lambda: completed(current)))

        def completed(current):
            rows = observer.jobs()
            spaces = library.workspaces()
            check('explicit native Run executes the existing bounded service',
                  len(rows) == len(spaces) == 1 and spaces[0].name == desired_name
                  and rows[0].status == 'complete' and rows[0].completed == 1)
            state, _plan = current.assistant_service._load(rows[0].id)
            check('new job uses current scope and concrete parameter value',
                  state['plan']['scope']['allow_create'] is True
                  and state['plan']['steps'][0]['arguments'] == {'name': desired_name})
            evidence['job_id'] = rows[0].id
            evidence['saved_recipe_sha256'] = store.get(original.id).fingerprint
            press(current.assistant_controls['buttons']['close'],
                  lambda: until(lambda: dialog(assistant_title) is None, close_recipe))

        def close_recipe():
            view = dialog(workflow_title)
            check('nested assistant close returns to the existing recipe', view is not None and view.IsModal())
            press(view.buttons['close'], lambda: until(
                lambda: dialog(workflow_title) is None and not frame.working,
                lambda: lifetime('direct', lambda: lifetime('parent', finish))))

        def lifetime(ending, continuation):
            """Hold the actual shared executor result until native deletion."""
            check('lifetime work starts inside the actual MainLoop', gui.GetMainLoop() is not None)
            ready, released = Event(), Event()
            pending_releases.append(released)
            delivered, destroyed, forbidden = [], [], []
            raw = saved_path.read_bytes()

            class Owner(wx.Frame):
                def __init__(self):
                    super().__init__(frame, title='Workflow lifetime owner ' + ending)
                    self.library, self._closing = library, False
                @property
                def working(self):
                    return frame.working
                @property
                def cancel_event(self):
                    return frame.cancel_event
                def run_job(self, title, operation, done=None, failed=None, **kwargs):
                    def actual_result():
                        value = operation()
                        ready.set()
                        if not released.wait(20):
                            raise TimeoutError('Actual template result was not released after native deletion.')
                        return value
                    def accepted(value):
                        delivered.append(('done', tuple(row.id for row in value)))
                        if done is not None:
                            done(value)
                    def rejected(error):
                        delivered.append(('failed', str(error)))
                        if failed is not None:
                            failed(error)
                    return frame.run_job(title, actual_result, done=accepted, failed=rejected, **kwargs)

            owner = Owner()
            windows.append(owner)
            owner.Show()
            view = workflow_templates.create_workflow_templates_dialog(owner, selected_id=original.id)
            windows.append(view)
            native_id = view.GetId()
            def observed(event):
                if event.GetId() == native_id:
                    destroyed.append(event.GetId())
                event.Skip()
            view.Bind(wx.EVT_WINDOW_DESTROY, observed)
            previous_report, previous_enable = view.report, view.enable
            def report(value):
                if not view.alive:
                    forbidden.append('late report')
                    raise AssertionError('Report reached a deleted workflow owner.')
                return previous_report(value)
            def enable():
                if not view.alive:
                    forbidden.append('late controls')
                    raise AssertionError('Controls reached a deleted workflow owner.')
                return previous_enable()
            view.report, view.enable = report, enable
            view.Show()

            def destroy():
                check('real TemplateStore result is pending on the shared executor',
                      ready.is_set() and frame.working and view.pending and not delivered)
                if ending == 'direct':
                    view.Destroy()
                    check('direct Destroy immediately detaches the workflow',
                          not view.alive and view.cancelled.is_set())
                else:
                    def forbidden_override():
                        raise AssertionError('Native parent destruction must bypass the Python override.')
                    view.Destroy = forbidden_override
                    owner.DestroyChildren()
                until(lambda: destroyed == [native_id] and not view, deleted)

            def deleted():
                check('native deletion completes before real result delivery',
                      not view.alive and view.cancelled.is_set() and not delivered)
                check('owner disposal does not stop unrelated host work', not frame.cancel_event.is_set())
                released.set()
                until(lambda: bool(delivered) and not frame.working, settled)

            def settled():
                check('actual late shared result is delivered without deleted controls',
                      delivered == [('done', (original.id,))] and not forbidden and not view.pending)
                check('late inspection never writes or replays a recipe',
                      saved_path.read_bytes() == raw and len(observer.jobs()) == 1
                      and len(library.workspaces()) == 1)
                evidence.setdefault('native_lifetime', []).append(
                    {'path': ending, 'native_id': native_id, 'destroy_events': len(destroyed),
                     'actual_result_delivered_after_native_deletion': True})
                if owner:
                    owner.Destroy()
                until(lambda: not owner, continuation)

            until(ready.is_set, destroy)

        def finish():
            check('exact installed source remains bound after all native actions',
                  bindings.verify_installed_sources(*modules) == origins)
            check('only one explicit operation ran throughout acceptance', len(observer.jobs()) == 1)
            evidence['complete'] = True
            gui.ExitMainLoop()

        try:
            frame = desktop_app.DesktopFrame(library)
            gui.SetTopWindow(frame)
            frame.Show()
            later(lambda: until(lambda: not frame.working, start))
            gui.MainLoop()
            if errors:
                raise AssertionError('\n'.join(errors))
            check('normal native MainLoop completed every workflow stage', evidence.get('complete') is True)
        finally:
            abort.stop()
            for event in pending_releases:
                event.set()
            release_key()
            for window in reversed(windows):
                if window:
                    if isinstance(window, wx.Dialog):
                        wx.Dialog.Destroy(window)
                    else:
                        window.Destroy()
            if frame is not None:
                frame._closing = True
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                panel = getattr(frame, 'progress_panel', None)
                if panel is not None:
                    panel.close()
                if frame:
                    frame.Destroy()
                gui.Yield()

    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'source_identity': origins,
        'evidence': evidence, 'native_menu_and_keyboard': 'PASS',
        'actual_template_store_and_assistant_job': 'PASS', 'native_pending_owner_deletion': 'PASS',
        'model_request': 'NOT_RUN', 'speech': 'NOT_RUN', 'physical_keyboard': 'NOT_RUN',
        'nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
