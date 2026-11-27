"""Installed native search pages and source authority on authored local text.

Uses actual DesktopFrame/SearchCenter, the existing executor, standard native
keyboard input and current search/catalog/source services. The corpus is small;
this is not a throughput, relevance-quality or manual screen-reader claim.
"""
import argparse
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import mkdtemp
import shutil
from threading import Event
from time import monotonic
from types import SimpleNamespace
import traceback


def _acceptance_bindings():
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_search_acceptance_bindings', path)
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
        budget=AudioBudget(timeout=420, max_log_bytes=2 * 1024 * 1024),
        owned_tree=True, allow_failure=True)
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace')); sys.stdout.flush()
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace')); sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    if not args.native_child:
        return _run_owned_native()
    faulthandler.dump_traceback_later(390, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    try:
        return _run_native()
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    import carr_library.application as application_module
    import carr_library.search.paging as paging_module
    import carr_library.search.ranked_scope as ranking_module
    import carr_library.search.literal as literal_module
    import carr_library.search.thematic as thematic_module
    import carr_library.search.federated as federated_module
    import carr_library.catalog.read_views as catalog_module
    import carr_library.storage.revisions as storage_module
    import carr_library.audio.cleanup as cleanup_module
    from carr_library.ui import desktop_app, federated_search

    checks, errors, state = [], [], {}
    failure_handler = None
    modules = (application_module, paging_module, ranking_module, literal_module,
               thematic_module, federated_module, catalog_module, storage_module,
               cleanup_module, desktop_app, federated_search)
    binding = _acceptance_bindings().verify_installed_sources(*modules)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    check('exact installed source identities before native search', bool(binding))

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            if failure_handler is not None:
                failure_handler(traceback.format_exc())
            else:
                fail(traceback.format_exc())
            return True

    app = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    held_key = [None]
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release_key():
        held, held_key[0] = held_key[0], None
        if held is not None:
            check('native key released', simulator.KeyUp(*held))

    def owned_modals():
        view = state.get('view')
        if view is None:
            return ()
        return (SimpleNamespace(dialog=view.window,
            close=lambda: view.window.EndModal(wx.ID_CANCEL)),)

    def record_failure(detail):
        errors.append(detail)
        print('FAIL: native search\n' + detail, file=sys.stderr, flush=True)

    abort = _OwnedModalAbort(wx, app, owned_modals, record_failure)

    def fail(detail):
        record_failure(detail)
        release = state.get('worker_release')
        if release is not None:
            release.set()
        frame = state.get('frame')
        if frame is not None:
            frame.cancel_event.set()
        try:
            release_key()
        except (Exception, KeyboardInterrupt):
            record_failure(traceback.format_exc())
        abort.request()

    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value

    _nested_modal_failure_acceptance(wx, app, check, select_failure_handler)

    def later(callback, milliseconds=25):
        def invoke():
            if errors:
                return
            try:
                callback()
            except (Exception, KeyboardInterrupt):
                fail(traceback.format_exc())
        wx.CallLater(milliseconds, invoke)

    def until(predicate, complete, *, timeout=45, label='native search operation'):
        deadline = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= deadline:
                raise TimeoutError(label + ' did not finish before its bound.')
            else:
                later(poll)
        later(poll)

    def focused(control):
        top = wx.GetTopLevelParent(control)
        handle = user32.GetForegroundWindow()
        process = wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process))
        ready = (process.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
        parent = control.GetParent()
        return ready and parent.GetClientRect().Contains(control.GetRect())

    def key(control, code, done, modifiers=wx.MOD_NONE):
        label = control.GetName()
        def down():
            check('native focused control is visible: ' + label,
                  control.IsEnabled() and control.IsShownOnScreen() and focused(control))
            held_key[0] = (code, modifiers)
            check('native key down: ' + label, simulator.KeyDown(code, modifiers))
            def up():
                release_key()
                later(done)
            later(up, 35)
        until(lambda: held_key[0] is None and focused(control), down, timeout=5,
              label='focus/scroll for ' + label)

    def press(control, done):
        key(control, wx.WXK_SPACE, done)

    def set_text(control, value, done):
        def write():
            check('native typed text', simulator.Text(value.encode('ascii')))
            until(lambda: control.GetValue() == value, done, timeout=5, label='native text entry')
        key(control, ord('A'), write, wx.MOD_CONTROL)

    def choose(control, index, done):
        def move(number):
            if number == index:
                check('native selection index', control.GetSelection() == index)
                done()
            else:
                key(control, wx.WXK_DOWN, lambda: move(number + 1))
        key(control, wx.WXK_HOME, lambda: move(0))

    def sequence(tasks, complete):
        pending = iter(tasks)
        def advance():
            try:
                action = next(pending)
            except StopIteration:
                complete()
                return
            action(advance)
        advance()

    temporary = mkdtemp(prefix='carr-native-search-')
    try:
        library = LibraryApp(Path(temporary) / 'library')
        workspace = library.create_workspace('000 Selected library')
        paragraphs = tuple('Needle exact source %04d: Україна α 😀.' % number for number in range(205))
        imported = library.add_text(workspace, '205 authored segments', '\f'.join(paragraphs),
                                    authors=('Synthetic author',), edition='Original edition')
        check('actual retained segmented input completed', imported['state'] == 'complete')
        checked = [workspace]
        for number in range(1, 21):
            selected = library.create_workspace('%03d Selected library' % number)
            checked.append(selected)
            added = library.add_text(selected, 'Authored source %02d' % number,
                                     'Needle library %02d exact source.' % number)
            check('actual additional library input completed', added['state'] == 'complete')
        foreign = library.create_workspace('999 Never selected')
        library.add_text(foreign, 'Private unrelated source', 'Needle foreign marker must stay outside.')
        original_bytes = {str(path.relative_to(library.assets_dir)): sha256(path.read_bytes()).hexdigest()
                          for path in library.assets_dir.rglob('*') if path.is_file()}
        frame = desktop_app.DesktopFrame(library)
        state['frame'] = frame
        frame._open_workspace(workspace)
        panel = frame.book.GetPage(0)
        check('same live DesktopSearchPanel and scope', isinstance(panel, desktop_app.DesktopSearchPanel)
              and frame.active == (workspace, None))
        frame.SetSize(frame.GetMinSize())
        frame.Show()
        frame.Raise()
        original_factory = federated_search.NativeSearchCenter
        real_submit, real_job = frame.executor.submit, frame.run_job
        modal_returns, callbacks, futures, destroyed = [], [], [], []
        state['scenario'] = 'libraries'

        def idle():
            return not frame.working and not frame.busy

        def desktop_first():
            check('desktop complete rank has all205 sources and first100 results',
                  panel._search_page.ranking_scope == 'all'
                  and panel._search_page.total_results == 205
                  and panel.model.row_count() == 100
                  and panel._search_page.next_source_cursor is None)
            state['desktop_first'] = tuple(row.ref for row in panel.model.hits)
            press(panel.search_navigation['result_next'],
                  lambda: until(idle, desktop_second))

        def desktop_second():
            check('desktop next result page retains same completed order',
                  panel._search_page.offset == 100 and panel.model.row_count() == 100
                  and not set(state['desktop_first']).intersection(row.ref for row in panel.model.hits))
            press(panel.search_navigation['result_next'],
                  lambda: until(idle, desktop_last))

        def desktop_last():
            check('desktop final page reaches beyond100 results',
                  panel._search_page.offset == 200 and panel.model.row_count() == 5
                  and not panel.search_navigation['result_next'].IsEnabled())
            hit = panel.model.hits[-1]
            check('desktop current result still resolves its exact original Unicode quotation',
                  library.copy(hit.workspace, hit.ref, hit.revision, 'quote') == hit.quote
                  and hit.quote in paragraphs)
            sequence([
                lambda done: choose(panel.retrieval, 0, done),
                lambda done: key(panel.query, wx.WXK_RETURN, done),
            ], lambda: until(idle, desktop_window))

        def desktop_window():
            check('literal window explicitly exposes further source pages',
                  panel._search_page.ranking_scope == 'window'
                  and panel._search_page.source_pages == 100
                  and panel._search_page.next_source_cursor is not None)
            press(panel.search_navigation['source_next'],
                  lambda: until(idle, desktop_next_window))

        def desktop_next_window():
            check('native source navigation leaves the first100 source rows',
                  panel._search_page.source_pages == 100
                  and panel.model.row_count() == 100
                  and all(row.quote in paragraphs for row in panel.model.hits))
            press(panel.search_navigation['source_next'],
                  lambda: until(idle, desktop_last_window))

        def desktop_last_window():
            check('native source navigation reaches the remaining actual sources',
                  panel._search_page.source_pages == 5 and panel.model.row_count() == 5
                  and panel._search_page.next_source_cursor is None)
            open_center()

        class BorrowedSearchHost(wx.Dialog):
            # A real native parent borrows the one live Desktop queue. All
            # worker-visible properties remain ordinary captured Python state.
            @property
            def library(self):
                return frame.library

            @property
            def active(self):
                return frame.active

            @property
            def working(self):
                return frame.working

            @property
            def busy(self):
                return frame.busy

            @property
            def cancel_event(self):
                return frame.cancel_event

            @property
            def _closing(self):
                return frame._closing

            def run_job(self, *args, **kwargs):
                return frame.run_job(*args, **kwargs)

        def center_factory(*args, **kwargs):
            if state['scenario'] == 'parent':
                parent = BorrowedSearchHost(frame, title='Owned SearchCenter native parent')
                parent.Show()
                state['parent'] = parent
                args = (parent, *args[1:])
            view = original_factory(*args, **kwargs)
            state['view'] = view
            view.window.SetSize(view.window.GetMinSize())
            later(lambda: center_ready(view))
            return view

        federated_search.NativeSearchCenter = center_factory

        def open_center():
            key(frame.status, ord('F'), lambda: None, wx.MOD_CONTROL | wx.MOD_ALT)

        def center_ready(view):
            check('actual Research menu opened the current SearchCenter',
                  view.window.IsModal() and isinstance(view, original_factory)
                  and view.controller.frame is state.get('parent', frame))
            if state['scenario'] == 'libraries':
                pick_libraries(view)
            else:
                prepare_held(view)

        def pick_libraries(view):
            identities = tuple(row.id for row in view.libraries)
            check('all22 actual libraries are available without truncation',
                  len(identities) == 22 and identities[:21] == tuple(checked) and identities[-1] == foreign)
            def move(index):
                if index == 20:
                    check('actual native checklist admits an explicit21-library scope',
                          tuple(row.id for i, row in enumerate(view.libraries)
                                if view.libraries_control.IsChecked(i)) == tuple(checked))
                    configure_center(view)
                else:
                    key(view.libraries_control, wx.WXK_DOWN,
                        lambda: press(view.libraries_control, lambda: move(index + 1)))
            key(view.libraries_control, wx.WXK_HOME, lambda: move(0))

        def configure_center(view):
            sequence([
                lambda done: set_text(view.query, 'Needle', done),
                lambda done: choose(view.retrieval, 3, done),
                lambda done: choose(view.limit, 3, done),
                lambda done: set_text(view.timeout, '360', done),
                lambda done: set_text(view.memory, '64', done),
                lambda done: set_text(view.disk, '64', done),
            ], lambda: press(view.search_button, lambda: until(idle, center_results)))

        def center_results():
            view = state['view']
            controller = view.controller
            check('first20 selected libraries have bounded results without foreign scope',
                  controller.options.workspaces == tuple(checked)
                  and controller.options.library_offset == 0
                  and len(controller.rows) == 119
                  and {row.workspace for row in controller.rows} == set(checked[:20])
                  and not any(row.workspace == foreign for row in controller.rows))
            check('configured native time budget exceeds former60 seconds',
                  controller.options.timeout == 360
                  and controller.options.memory_bytes == 64 * 1024**2
                  and controller.options.disk_bytes == 64 * 1024**2)
            key(view.results, wx.WXK_HOME,
                lambda: press(view.quote_button, clipboard_done))

        def clipboard_done():
            view = state['view']
            until(idle, lambda: verify_clipboard(view))

        def verify_clipboard(view):
            selected = view.controller.rows[view.results.GetSelection()]
            value = wx.TextDataObject()
            check('actual clipboard opens', wx.TheClipboard.Open())
            try:
                check('actual clipboard accepted verified quotation', wx.TheClipboard.GetData(value))
            finally:
                wx.TheClipboard.Close()
            check('actual copied quotation belongs to the same current source',
                  value.GetText() == selected.result.quote
                  and selected.result.verified is True
                  and selected.workspace in checked)
            press(view.scope_buttons['library_next'],
                  lambda: until(idle, final_library))

        def final_library():
            view = state['view']
            check('native next-library button reaches the21st checked library',
                  view.controller.options.library_offset == 20
                  and len(view.controller.rows) == 1
                  and view.controller.rows[0].workspace == checked[20]
                  and '21–21 of 21' in view.status.GetValue()
                  and not view.scope_buttons['library_next'].IsEnabled())
            press(view.close_button, lambda: until(lambda: not view.window, begin_cancel))

        def begin_cancel():
            state['scenario'] = 'cancel'
            open_center()

        def prepare_held(view):
            sequence([
                lambda done: set_text(view.query, 'Needle', done),
                lambda done: choose(view.retrieval, 3, done),
            ], lambda: start_held(view))

        def start_held(view):
            scenario = state['scenario']
            ready, release = Event(), Event()
            state.update(worker_ready=ready, worker_release=release, actual_result=None)
            native_id = view.window.GetId()
            def native_death(event):
                if event.GetId() == native_id:
                    destroyed.append((scenario, native_id))
                event.Skip()
            view.window.Bind(wx.EVT_WINDOW_DESTROY, native_death)
            if scenario == 'parent':
                check('actual search dialog has the owned native parent before its modal loop',
                      view.window.GetParent() is state['parent'])

            def submit(work, *args, **kwargs):
                frame.executor.submit = real_submit
                def held_work():
                    value = work(*args, **kwargs)
                    state['actual_result'] = value
                    ready.set()
                    if not release.wait(45):
                        raise TimeoutError('Actual search result was not released.')
                    return value
                future = real_submit(held_work)
                futures.append(future)
                return future

            def observed_job(title, operation, done=None, failed=None, **kwargs):
                frame.run_job = real_job
                def accepted(value):
                    try:
                        if done is not None:
                            done(value)
                    finally:
                        callbacks.append((scenario, value))
                return real_job(title, operation, accepted, failed, **kwargs)

            frame.executor.submit, frame.run_job = submit, observed_job
            press(view.search_button, lambda: until(ready.is_set, lambda: held_ready(view), timeout=60))

        def held_ready(view):
            scenario = state['scenario']
            check('existing Desktop executor holds a real completed search result: ' + scenario,
                  state['actual_result'] is not None and frame.working
                  and view.controller.running and not futures[-1].done()
                  and state['actual_result'].hits)
            if scenario == 'cancel':
                press(view.cancel_button, lambda: cancelled(view))
                return
            if scenario == 'direct':
                view.window.Destroy()
            else:
                state['parent'].DestroyChildren()
            until(lambda: not view.window and destroyed.count((scenario, view._window_id)) == 1,
                  lambda: dead_view(view))

        def cancelled(view):
            check('native Cancel requests cancellation on the same active Desktop job',
                  frame.cancel_event.is_set() and view.controller.running)
            state['worker_release'].set()
            until(idle, lambda: cancelled_done(view))

        def cancelled_done(view):
            check('late completed search remains unpublished after native Cancel',
                  not view.controller.running and view.controller.rows == ()
                  and futures[-1].done() and any(name == 'cancel' for name, _ in callbacks))
            press(view.close_button, lambda: until(lambda: not view.window, begin_direct))

        def begin_direct():
            state['scenario'] = 'direct'
            open_center()

        def dead_view(view):
            scenario = state['scenario']
            check('actual native deletion withdraws the same pending owner: ' + scenario,
                  not view._window_alive and not view.controller.alive
                  and frame.cancel_event.is_set() and view.controller.running)
            def forbidden(*args, **kwargs):
                raise AssertionError('A deleted SearchCenter received a native view update.')
            for method in ('report', 'set_busy', 'clear_results', 'show_results',
                           'focus_results', 'show_details', 'set_navigation', 'set_scope_navigation'):
                setattr(view, method, forbidden)
            state['worker_release'].set()
            until(lambda: idle() and futures[-1].done()
                         and any(name == scenario for name, _ in callbacks),
                  lambda: dead_done(view))

        def dead_done(view):
            scenario = state['scenario']
            check('real late result callback performs no deleted-view update: ' + scenario,
                  not view.controller.running and view.controller.rows == ()
                  and destroyed.count((scenario, view._window_id)) == 1
                  and any(name == scenario and value is state['actual_result'] for name, value in callbacks))
            # Replay the actual already-delivered controller callback is covered
            # by the common owner tests. This native proof observes its real
            # one-time Desktop delivery after the peer has actually died.
            parent = state.pop('parent', None)
            if parent is not None and parent:
                parent.Destroy()
            if scenario == 'direct':
                state['scenario'] = 'parent'
                open_center()
            else:
                finish()

        def finish():
            check('all original asset bytes remain unchanged',
                  {str(path.relative_to(library.assets_dir)): sha256(path.read_bytes()).hexdigest()
                   for path in library.assets_dir.rglob('*') if path.is_file()} == original_bytes)
            check('desktop selected scope is unchanged', frame.active == (workspace, None))
            check('all genuine held futures completed', len(futures) == 3 and all(f.done() for f in futures))
            check('same search owner actually releases all private results', library.close_search())
            check('installed source identities unchanged after all native operations',
                  _acceptance_bindings().verify_installed_sources(*modules) == binding)
            state['completed'] = True
            frame.Close(force=True)
            until(lambda: not frame, app.ExitMainLoop, timeout=5, label='owned desktop destruction')

        def launch():
            sequence([
                lambda done: set_text(panel.query, 'Needle', done),
                lambda done: choose(panel.retrieval, 3, done),
                lambda done: set_text(panel.search_timeout, '360', done),
                lambda done: set_text(panel.search_disk, '64', done),
                lambda done: key(panel.query, wx.WXK_RETURN, done),
            ], lambda: until(idle, desktop_first, timeout=60))

        try:
            later(launch)
            app.MainLoop()
        finally:
            abort.stop()
            release_key()
            state.get('worker_release', Event()).set()
            federated_search.NativeSearchCenter = original_factory
            frame.executor.submit, frame.run_job = real_submit, real_job
            if frame:
                frame.cancel_event.set()
                frame.Close(force=True)
            # Do not delete an unconfirmed worker's library. The outer owned
            # process watchdog retains this fixture on an unexpected refusal.
            if any(not future.done() for future in futures):
                raise RuntimeError('Native search worker cleanup is unconfirmed.')
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('whole native search workflow completed', state.get('completed') is True)
        print(json.dumps({'result': 'PASS', 'checks': len(checks),
                          'native_keyboard_automation': 'PASS',
                          'source_identity': binding,
                          'source_segments': 205, 'explicitly_selected_libraries': 21,
                          'large_corpus_benchmark': 'NOT_RUN', 'model_quality': 'NOT_RUN',
                          'NVDA': 'NOT_RUN', 'physical_output': 'NOT_RUN'}, ensure_ascii=False))
    finally:
        if state.get('completed') is True and not errors:
            shutil.rmtree(temporary)
        else:
            print('RETAINED: unqualified native fixture ' + temporary, file=sys.stderr, flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
