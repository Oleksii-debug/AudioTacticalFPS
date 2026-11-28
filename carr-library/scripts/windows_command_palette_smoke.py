"""Installed native command search, reusing PR152's business acceptance cases.

The actual DesktopFrame, menu registry, Save and Select all handlers are used.
Keyboard events are sent by wx.UIActionSimulator only to this process's visible
foreground window. The note, shortcut and deliberate late menu-state change are
synthetic. This is automated native acceptance, not physical keyboard or NVDA QA.
"""
import argparse
import faulthandler
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
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
        dialog = wx.Dialog(parent, title='Owned command palette failure control — ' + label)
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
        budget=AudioBudget(timeout=180, max_log_bytes=2 * 1024 * 1024),
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
    faulthandler.dump_traceback_later(150, exit=True)
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
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import command_palette as ui
    from carr_library.ui import keyboard_settings, desktop_app, command_registry
    from carr_library import keyboard_preferences

    checks, errors, observed = [], [], []
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

    failure_handler = None

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            if failure_handler is not None:
                failure_handler(traceback.format_exc())
            else:
                abort(traceback.format_exc())
            return True

    application = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    held = [None]
    frame, timer = None, None
    stage = ['starting', monotonic()]
    overall_deadline = monotonic() + 110
    saved_note = 'Saved through the searchable native command palette.'
    unsaved = 'A synthetic draft that must remain unsaved after command invalidation.'
    save_binding = None

    def advance(name):
        stage[:] = [name, monotonic()]
        print('STAGE:', name, flush=True)

    def release():
        key = held[0]
        held[0] = None
        if key is not None and not simulator.KeyUp(*key):
            raise AssertionError('Native command palette key release failed.')

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

    def key(control, code, next_stage, modifiers=wx.MOD_NONE):
        if held[0] is not None or not foreground(control):
            return False
        advance(next_stage)
        held[0] = (code, modifiers)
        try:
            if not simulator.KeyDown(code, modifiers):
                raise AssertionError('Native command palette key press failed.')
            # Let the actual event loop process the pressed key before release.
            wx.CallLater(40, release)
        except Exception:
            release()
            raise
        return True

    def palette_window():
        for window in wx.GetTopLevelWindows():
            if (window and isinstance(window, wx.Dialog) and window.GetParent() is frame
                    and window.GetTitle() == 'Find a command' and window.IsShown()):
                return window
        return None

    def open_palette(next_stage):
        return key(frame.evidence.note, ord('P'), next_stage,
                   wx.MOD_CONTROL | wx.MOD_SHIFT)

    def record_failure(detail):
        errors.append(detail)
        print('FAIL:', detail, flush=True)

    def owned_modals():
        owned = []
        windows = wx.GetTopLevelWindows()
        if len(windows) > 256:
            raise RuntimeError('The command palette probe has too many native owners.')
        for window in windows:
            if not window or not isinstance(window, wx.Dialog) or not window.IsModal():
                continue
            parent, depth = window, 0
            while parent is not None and parent is not frame and depth < 64:
                parent = parent.GetParent()
                depth += 1
            if frame is not None and parent is frame:
                owned.append((depth, SimpleNamespace(
                    dialog=window, close=lambda target=window: target.EndModal(wx.ID_CANCEL))))
        return tuple(owner for _depth, owner in sorted(owned, key=lambda row: -row[0]))

    modal_abort = _OwnedModalAbort(wx, application, owned_modals, record_failure)

    def abort(detail):
        record_failure(detail)
        if timer is not None:
            timer.Stop()
        if frame is not None:
            frame.cancel_event.set()
        try:
            release()
        except (Exception, KeyboardInterrupt):
            record_failure(traceback.format_exc())
        modal_abort.request()

    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value

    _nested_modal_failure_acceptance(wx, application, check, select_failure_handler)

    def tick(event=None):
        nonlocal save_binding
        try:
            if monotonic() > overall_deadline or monotonic() - stage[1] > 15:
                raise TimeoutError('Native command palette timed out at ' + stage[0])
            if held[0] is not None:
                return
            dialog = palette_window()
            name = stage[0]
            if name == 'starting':
                if frame.working or frame.evidence is None or frame.evidence.displayed is None:
                    return
                binding = keyboard.registry.get(ui.PALETTE_IDENTITY)
                palette_item = keyboard.bindings[ui.PALETTE_IDENTITY].item
                check('palette belongs to the existing native shortcut inventory',
                      palette_item.GetItemLabel().endswith('\tCtrl+Shift+P')
                      and sum(row.item.GetItemLabel().endswith('\tCtrl+Shift+P')
                              for row in keyboard.bindings.values()) == 1)
                save_binding, = ui.CommandPalette(keyboard).matching('file/save Ctrl+S')
                def observe_original_dispatch(event):
                    observed.append(not any(isinstance(window, wx.Dialog) and window.IsModal()
                                            for window in wx.GetTopLevelWindows()))
                    event.Skip()
                frame.Bind(wx.EVT_MENU, observe_original_dispatch, id=save_binding.menu_id)
                check('actual native main event loop is active', application.GetMainLoop() is not None)
                advance('open_navigation')
            elif name == 'open_navigation':
                open_palette('navigation_opened')
            elif name == 'navigation_opened' and dialog is not None:
                check('native accelerator opens the registered palette', dialog.IsModal())
                check('filter and results have accessible names',
                      dialog.filter.GetName() == 'Find a command by name, menu path or shortcut'
                      and dialog.commands.GetName() == 'Matching commands with current shortcuts and availability')
                check('status uses a native read-only multiline control',
                      bool(dialog.status.GetWindowStyleFlag() & wx.TE_READONLY)
                      and bool(dialog.status.GetWindowStyleFlag() & wx.TE_MULTILINE))
                check('Escape is bound to native cancellation', dialog.GetEscapeId() == wx.ID_CANCEL)
                if not foreground(dialog.filter):
                    return
                advance('navigation_filtered')
                check('native typing reaches the filter', simulator.Text(b'file'))
            elif name == 'navigation_filtered' and dialog is not None:
                if dialog.filter.GetValue() != 'file' or len(dialog.rows) < 2:
                    return
                key(dialog.filter, wx.WXK_TAB, 'navigation_list')
            elif name == 'navigation_list' and dialog is not None:
                if wx.Window.FindFocus() is not dialog.commands:
                    return
                check('native Tab reaches the matching-command list', dialog.commands.GetSelection() == 0)
                key(dialog.commands, wx.WXK_DOWN, 'navigation_down')
            elif name == 'navigation_down' and dialog is not None:
                if dialog.commands.GetSelection() != 1:
                    return
                check('native Down selects the next command', True)
                key(dialog.commands, wx.WXK_UP, 'navigation_up')
            elif name == 'navigation_up' and dialog is not None:
                if dialog.commands.GetSelection() != 0:
                    return
                check('native Up selects the preceding command', True)
                key(dialog.commands, wx.WXK_ESCAPE, 'navigation_cancelled')
            elif name == 'navigation_cancelled' and dialog is None:
                check('native Escape cancels without dispatch', observed == [])
                check('cancel restores focus to the originating note',
                      wx.Window.FindFocus() is frame.evidence.note)
                frame.evidence.note.SetValue(saved_note)
                advance('open_save')
            elif name == 'open_save':
                open_palette('save_opened')
            elif name == 'save_opened' and dialog is not None:
                dialog.filter.SetValue('file/save Ctrl+S')
                advance('save_filtered')
            elif name == 'save_filtered' and dialog is not None:
                check('filter resolves the existing native Save binding',
                      len(dialog.rows) == 1 and dialog.rows[0].command is save_binding.command)
                key(dialog.filter, wx.WXK_RETURN, 'save_accepted')
            elif name == 'save_accepted' and dialog is None and not frame.working:
                check('original Save handler commits the synthetic research note',
                      library.evidence(workspace, topic)[0].note == saved_note)
                check('original menu event runs after the palette closes', observed == [True])
                keyboard.apply_overrides({save_binding.command.command_id: 'Ctrl+Shift+F12'})
                advance('open_remapped')
            elif name == 'open_remapped':
                open_palette('remapped_opened')
            elif name == 'remapped_opened' and dialog is not None:
                dialog.filter.SetValue('Ctrl+Shift+F12')
                advance('remapped_filtered')
            elif name == 'remapped_filtered' and dialog is not None:
                check('palette search and list use the remapped native shortcut',
                      len(dialog.rows) == 1 and dialog.rows[0].command is save_binding.command
                      and dialog.rows[0].shortcut == 'Ctrl+Shift+F12')
                key(dialog.filter, wx.WXK_ESCAPE, 'remapped_cancelled')
            elif name == 'remapped_cancelled' and dialog is None:
                check('cancelling remapped search preserves the dispatch count', observed == [True])
                check('cancel returns native focus to the note',
                      wx.Window.FindFocus() is frame.evidence.note)
                frame.evidence.note.SetSelection(0, 0)
                advance('open_select_all')
            elif name == 'open_select_all':
                open_palette('select_all_opened')
            elif name == 'select_all_opened' and dialog is not None:
                dialog.filter.SetValue('edit/select all')
                advance('select_all_filtered')
            elif name == 'select_all_filtered' and dialog is not None:
                check('Edit command comes from the same native menu registry',
                      len(dialog.rows) == 1 and dialog.rows[0].path == ('edit', 'select all'))
                key(dialog.filter, wx.WXK_RETURN, 'select_all_accepted')
            elif name == 'select_all_accepted' and dialog is None:
                check('original Edit Select all acts on the originating note after close',
                      frame.evidence.note.GetSelection() == (0, len(saved_note))
                      and wx.Window.FindFocus() is frame.evidence.note)
                save_binding.binding.item.Enable(False)
                advance('open_disabled')
            elif name == 'open_disabled':
                open_palette('disabled_opened')
            elif name == 'disabled_opened' and dialog is not None:
                dialog.filter.SetValue('file/save Ctrl+Shift+F12')
                advance('disabled_filtered')
            elif name == 'disabled_filtered' and dialog is not None:
                check('disabled native commands remain discoverable and clearly marked',
                      len(dialog.rows) == 1 and not dialog.rows[0].enabled
                      and 'unavailable now' in dialog.commands.GetString(0)
                      and not dialog.run.IsEnabled())
                key(dialog.filter, wx.WXK_RETURN, 'disabled_rejected')
            elif name == 'disabled_rejected' and dialog is not None:
                if wx.Window.FindFocus() is not dialog.status:
                    return
                check('native Enter cannot accept a disabled command',
                      dialog.IsModal() and dialog.result is None and observed == [True])
                key(dialog.status, wx.WXK_ESCAPE, 'disabled_cancelled')
            elif name == 'disabled_cancelled' and dialog is None:
                save_binding.binding.item.Enable(True)
                frame.evidence.note.SetValue(unsaved)
                advance('open_invalidation')
            elif name == 'open_invalidation':
                open_palette('invalidation_opened')
            elif name == 'invalidation_opened' and dialog is not None:
                dialog.filter.SetValue('file/save Ctrl+Shift+F12')
                # A deliberate test-side menu mutation precisely at the existing
                # modal adapter boundary; the real dialog and original handler
                # are retained. Acceptance still uses a real native Enter key.
                original_destroy = dialog.Destroy
                def disable_during_destroy():
                    result = original_destroy()
                    save_binding.binding.item.Enable(False)
                    return result
                dialog.Destroy = disable_during_destroy
                advance('invalidation_filtered')
            elif name == 'invalidation_filtered' and dialog is not None:
                key(dialog.filter, wx.WXK_RETURN, 'invalidation_closed')
            elif name == 'invalidation_closed' and dialog is None:
                check('availability is checked again after modal acceptance',
                      'unavailable now' in frame.status.GetValue())
                check('invalidated command neither executes nor discards the draft',
                      observed == [True] and frame.evidence.note.GetValue() == unsaved
                      and library.evidence(workspace, topic)[0].note == saved_note)
                save_binding.binding.item.Enable(True)
                advance('complete')
                timer.Stop()
                application.ExitMainLoop()
        except Exception:
            abort(traceback.format_exc())

    try:
        source_identity = _acceptance_bindings().verify_installed_sources(ui, keyboard_settings, desktop_app, command_registry, keyboard_preferences)
        check('command palette loads exact current installed bytes', bool(source_identity))
        check('native Windows wx is active', wx.Platform == '__WXMSW__')
        with TemporaryDirectory(prefix='research-command-palette-native-') as directory:
            library = LibraryApp(Path(directory) / 'library')
            workspace = library.create_workspace('Synthetic command palette')
            topic = library.create_topic(workspace, 'Native command tests')
            library.add_text(workspace, 'Synthetic source', 'A railway book for command testing.')
            hit, = library.search(workspace, 'railway')
            library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original synthetic note')
            frame = DesktopFrame(library)
            keyboard = frame.keyboard_commands
            try:
                application.SetTopWindow(frame)
                frame.Show()
                frame._open_workspace(workspace, topic)
                timer = wx.Timer(frame)
                frame.Bind(wx.EVT_TIMER, tick, timer)
                timer.Start(35)
                application.MainLoop()
                check('native command probe completed every stage', stage[0] == 'complete' and not errors)
            finally:
                modal_abort.stop()
                if timer is not None:
                    timer.Stop()
                    timer.SetOwner(None)
                release()
                if frame and not frame.IsBeingDeleted():
                    if save_binding is not None:
                        save_binding.binding.item.Enable(True)
                    # This synthetic draft was intentionally left unsaved by
                    # the rejection case. Restore its already-committed text
                    # before normal teardown, avoiding an unrelated save prompt.
                    if frame.evidence is not None and frame.evidence.displayed is not None:
                        frame.evidence.note.ChangeValue(library.evidence(workspace, topic)[0].note)
                    frame.Close(force=True)
                    application.Yield()
    except Exception:
        errors.append(traceback.format_exc())
    if not errors:
        check('selected installed command palette bytes remain unchanged',
              _acceptance_bindings().verify_installed_sources(
                  ui, keyboard_settings, desktop_app, command_registry, keyboard_preferences) == source_identity)
    if errors:
        print(json.dumps({'status': 'FAIL', 'errors': errors, 'checks': checks,
                          'native_keyboard': 'INCOMPLETE', 'nvda': 'NOT_RUN',
                          'manual_keyboard': 'NOT_RUN'}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps({'status': 'PASS', 'checks': checks, 'count': len(checks),
                      'source_identity': source_identity,
                      'native_keyboard': 'PASS', 'nvda': 'NOT_RUN',
                      'manual_keyboard': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
