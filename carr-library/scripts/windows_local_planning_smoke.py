"""Installed native assistant proposal/review using synthetic loopback JSON.

Exercises actual wx controls, Gateway HTTP, typed service validation and durable
execution. Run before outbound firewall rules: only this explicit in-process
loopback fixture is contacted. No actual model, manual keyboard or NVDA claim.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import file_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import traceback


def verify_installed_sources(*modules):
    """Bind every selected module to this interpreter and the reviewed checkout."""
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Installed local planning acceptance requires Python -I -B.')
    source_root = (Path(__file__).resolve().parents[1] / 'src').resolve(strict=True)
    installed_roots = {Path(sysconfig.get_path(key)).resolve(strict=True)
                       for key in ('purelib', 'platlib')}
    result = {}
    for module in modules:
        actual = Path(module.__file__).resolve(strict=True)
        if (actual.is_relative_to(source_root)
                or not any(actual.is_relative_to(root) for root in installed_roots)):
            raise RuntimeError('Acceptance imported a module outside the installed interpreter: ' + module.__name__)
        reviewed = source_root.joinpath(*module.__name__.split('.')).with_suffix('.py')
        if not reviewed.is_file() or actual.read_bytes() != reviewed.read_bytes():
            raise RuntimeError('Installed source differs from the reviewed checkout: ' + module.__name__)
        with actual.open('rb') as stream:
            digest = file_digest(stream, 'sha256').hexdigest()
        result[module.__name__] = {'path': str(actual), 'sha256': digest}
    return result


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Use the installed interpreter with -I -B.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run()
    finally:
        if native_handle is not None:
            native_handle.close()


def _run():
    import wx
    from carr_library import application, native_runtime
    from carr_library.application import LibraryApp
    from carr_library.assistant import ProfileStore, ProviderProfile
    from carr_library.assistant import llm, providers, service as assistant_service
    from carr_library.studio import cli, render
    from carr_library.ui import assistant_dialog as ui, desktop_app
    from carr_library.ui.desktop_app import DesktopFrame

    errors, checks, pools = [], [], []

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            return False

    gui = ProbeApp(False)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def until(predicate, timeout=15, *, fail_on_errors=True):
        deadline = monotonic() + timeout
        while not predicate():
            if errors and fail_on_errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= deadline:
                raise TimeoutError('Native local proposal did not finish.')
            gui.Yield()
            sleep(.01)

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.GetEventHandler().ProcessEvent(event)

    class ObservedExecutor(ThreadPoolExecutor):
        """Observe actual queued/running work; never substitute its results."""
        def __init__(self, *args, **kwargs):
            self.submissions = []
            super().__init__(*args, **kwargs)
            pools.append(self)

        def submit(self, callback, /, *args, **kwargs):
            future = super().submit(callback, *args, **kwargs)
            self.submissions.append((callback.__name__, future))
            return future

    state = {'requests': [], 'hold': None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers['Content-Length'])
            if not 0 <= size <= 1024 * 1024:
                raise ValueError('The synthetic request exceeded its test budget.')
            state['requests'].append(json.loads(self.rfile.read(size)))
            hold = state['hold']
            if hold is not None:
                hold.wait(10)
            proposal = {'summary': 'Synthetic protocol response, no actual LLM', 'steps': [
                {'tool': 'library.create', 'arguments': {'name': 'Explicitly reviewed library'}}]}
            raw = json.dumps({'choices': [{'message': {'content': json.dumps(proposal)}}]}).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_executor = ui.ThreadPoolExecutor
    ui.ThreadPoolExecutor = ObservedExecutor
    dialog = frame = None
    source_bindings = {}

    def lifetime_probe(termination, app):
        """Delete a real native view while its real loopback request is pending."""
        owner, window = None, None
        destroyed, delivered, queued = [], [], []
        pool = None
        destroyed_requested = False

        class ObservedWx:
            def __getattr__(self, name):
                return getattr(wx, name)

            def CallAfter(self, callback, *args, **kwargs):
                queued.append(callback.__name__)

                def apply():
                    delivered.append({'callback': callback.__name__,
                                      'native_deleted': window is not None and not window})
                    callback(*args, **kwargs)

                return wx.CallAfter(apply)

        old_libraries = app.workspaces()
        old_jobs = dialog.assistant_service.jobs()
        try:
            check('lifecycle probe runs inside actual MainLoop: ' + termination, gui.IsMainLoopRunning())
            host = frame
            if termination == 'native-parent':
                owner = wx.Dialog(frame, title='Native assistant parent deletion')
                owner.library, owner.active = frame.library, frame.active
                host = owner
            window = ui.create_assistant_dialog(host, wx_module=ObservedWx())
            pool = pools[-1]
            window.Show()
            gui.Yield()
            native_id = window.GetId()

            def observe(event):
                if event.GetId() == native_id:
                    destroyed.append(native_id)
                event.Skip()

            window.Bind(wx.EVT_WINDOW_DESTROY, observe)
            wx.Panel(window).Destroy()
            gui.Yield()
            check('child event preserves actual assistant owner: ' + termination,
                  bool(window) and not pool._shutdown and not destroyed)
            controls = window.assistant_controls
            controls['planning_source'].SetSelection(1)
            controls['instruction'].SetValue('Create one named research library.')
            previous_count = len(state['requests'])
            state['hold'] = Event()
            click(controls['buttons']['preview'])
            until(lambda: len(state['requests']) == previous_count + 1)
            pending = [future for name, future in pool.submissions if name == 'propose_local']
            check('actual loopback proposal is pending before native deletion: ' + termination,
                  len(pending) == 1 and not pending[0].done()
                  and not controls['buttons']['run'].IsEnabled())
            if termination == 'native-parent':
                def forbidden_python_destroy():
                    raise AssertionError('Native parent deletion must bypass the Python Destroy override.')
                window.Destroy = forbidden_python_destroy
                check('native parent destroys its actual assistant child', owner.DestroyChildren())
            else:
                check('direct assistant Destroy request accepted', window.Destroy())
                check('direct Destroy detaches before native deletion',
                      window._assistant_destroy_requested and pool._shutdown)
            destroyed_requested = True
            until(lambda: destroyed == [native_id] and not window, timeout=5)
            check('one actual owner-ID event and deleted peer: ' + termination,
                  destroyed == [native_id] and not window and pool._shutdown)
            state['hold'].set()
            until(lambda: pending[0].done() and len(delivered) >= 1, timeout=15)
            until(lambda: all(future.done() for _, future in pool.submissions), timeout=15)
            until(lambda: all(not worker.is_alive() for worker in pool._threads), timeout=5)
            check('real queued proposal completion runs only after native deletion: ' + termination,
                  bool(queued) and all(item['native_deleted'] for item in delivered))
            check('destroyed pending proposal cannot create work or touch native controls: ' + termination,
                  isinstance(pending[0].exception(), InterruptedError)
                  and app.workspaces() == old_libraries
                  and dialog.assistant_service.jobs() == old_jobs and not errors)
            check('owned cleanup ran after pending request: ' + termination,
                  [name for name, _ in pool.submissions] == ['propose_local', 'close_production']
                  and all(future.exception() is None for name, future in pool.submissions
                          if name == 'close_production'))
        finally:
            if state['hold'] is not None:
                state['hold'].set()
            if window is not None and window and not destroyed_requested:
                window.assistant_handlers['close']()
                wx.Dialog.Destroy(window)
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=False)
            if owner is not None and owner:
                owner.Destroy()
            gui.Yield()

    try:
        source_bindings = verify_installed_sources(ui, assistant_service, llm, providers, render, cli,
                                                    desktop_app, application, native_runtime)
        check('actual installed Windows widgets', wx.Platform == '__WXMSW__')
        with TemporaryDirectory(prefix='resonance-native-local-planning-') as temporary:
            app = LibraryApp(Path(temporary) / 'library')
            profile = ProviderProfile('synthetic_local', 'Synthetic protocol fixture', 'openai-compatible',
                f'http://127.0.0.1:{server.server_port}/v1', 'synthetic-protocol-model', local=True, enabled=True)
            ProfileStore(app.root / 'state' / 'providers').save(profile)
            frame = DesktopFrame(app)
            frame.Show()
            dialog = ui.create_assistant_dialog(frame)
            dialog.Show()

            def scenario():
                try:
                    check('actual native application main loop is active', gui.IsMainLoopRunning())
                    controls, handlers = dialog.assistant_controls, dialog.assistant_handlers
                    buttons = controls['buttons']
                    check('named native planner selector defaults to guided commands',
                          isinstance(controls['planning_source'], wx.Choice)
                          and controls['planning_source'].GetName() == 'Planning source'
                          and controls['planning_source'].GetSelection() == 0 and not state['requests'])
                    controls['planning_source'].SetSelection(1)
                    controls['instruction'].SetValue('Create one named research library.')
                    click(buttons['preview'])
                    check('request is pending with run disabled', not buttons['run'].IsEnabled())
                    until(buttons['run'].IsEnabled)
                    check('proposal remains unexecuted until separate review action',
                          app.workspaces() == () and dialog.assistant_service.jobs() == ())
                    check('exact selected model reached the loopback gateway',
                          len(state['requests']) == 1 and state['requests'][0]['model'] == profile.model)
                    check('review receives native keyboard focus', wx.Window.FindFocus() == controls['review'])

                    state['hold'] = Event()
                    click(buttons['preview'])
                    until(lambda: len(state['requests']) == 2)
                    controls['instruction'].SetValue('Edited goal while local response is pending.')
                    state['hold'].set()
                    until(buttons['preview'].IsEnabled)
                    check('edited instructions invalidate pending native proposal',
                          not buttons['run'].IsEnabled() and 'discarded' in controls['review'].GetValue()
                          and app.workspaces() == () and dialog.assistant_service.jobs() == ())

                    state['hold'] = None
                    controls['instruction'].SetValue('Create one named research library.')
                    click(buttons['preview'])
                    until(buttons['run'].IsEnabled)
                    click(buttons['run'])
                    until(lambda: 'complete: 1 of 1' in controls['review'].GetValue())
                    check('separate run uses existing durable assistant service',
                          [row.name for row in app.workspaces()] == ['Explicitly reviewed library']
                          and len(dialog.assistant_service.jobs()) == 1 and len(state['requests']) == 3)
                    check('completion returns focus to readable result',
                          wx.Window.FindFocus() == controls['review'])
                    for termination in ('direct', 'native-parent'):
                        lifetime_probe(termination, app)
                    handlers['close']()
                    check('native Close requests owned service cleanup', pools[0]._shutdown)
                except BaseException:
                    errors.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()

            wx.CallAfter(scenario)
            gui.MainLoop()
            try:
                if errors:
                    raise RuntimeError('\n'.join(errors))
            finally:
                if state['hold'] is not None:
                    state['hold'].set()
                if dialog is not None and dialog:
                    dialog.assistant_handlers['close']()
                    dialog.Destroy()
                if frame is not None:
                    frame.cancel_event.set()
                    frame._closing = True
                    frame.executor.shutdown(wait=False, cancel_futures=True)
                for pool in pools:
                    pool.shutdown(wait=False, cancel_futures=False)
                until(lambda: all(future.done() for pool in pools
                                  for _, future in pool.submissions), timeout=15, fail_on_errors=False)
                until(lambda: all(not worker.is_alive() for pool in pools for worker in pool._threads),
                      timeout=5, fail_on_errors=False)
                if frame is not None and frame:
                    frame.Destroy()
                gui.Yield()
                dialog = frame = None
    finally:
        if state['hold'] is not None:
            state['hold'].set()
        if dialog is not None and dialog:
            dialog.assistant_handlers['close']()
            dialog.Destroy()
        if frame is not None and frame:
            frame.cancel_event.set()
            frame.executor.shutdown(wait=False, cancel_futures=True)
            frame.Destroy()
        for pool in pools:
            pool.shutdown(wait=False, cancel_futures=False)
        ui.ThreadPoolExecutor = original_executor
        server.shutdown()
        server.server_close()
        thread.join(2)
    check('synthetic loopback server stops', not thread.is_alive())
    print(json.dumps({'result': 'PASS', 'checks': checks, 'installed_sources': source_bindings,
                      'actual_llm_model': 'NOT_RUN — synthetic protocol fixture',
                      'native_controls': 'PASS',
                      'native_direct_and_parent_destruction': 'PASS',
                      'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
