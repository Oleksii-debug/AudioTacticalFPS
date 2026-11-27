"""Installed native command search, reusing PR152's business acceptance cases.

The actual DesktopFrame, menu registry, Save and Select all handlers are used.
Keyboard events are sent by wx.UIActionSimulator only to this process's visible
foreground window. The note, shortcut and deliberate late menu-state change are
synthetic. Keymap file selection is an explicit picker fixture; the settings
dialog, publication, preview, apply and undo handlers are real. This is automated
native acceptance, not physical keyboard, native picker or NVDA QA.
"""
from hashlib import sha256
import faulthandler
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def _abort_native_probe(wx, application, frame, owner_loop, timer, release, errors, detail):
    """Unwind owned modal loops and schedule the retained outer loop to stop."""
    errors.append(detail)
    print('FAIL:', detail, flush=True)
    callbacks = [release]
    if timer is not None:
        callbacks.insert(0, timer.Stop)
    if frame is not None:
        callbacks.insert(0, frame.cancel_event.set)
    try:
        for callback in callbacks:
            try:
                callback()
            except Exception:
                errors.append(traceback.format_exc())
        for window in tuple(wx.GetTopLevelWindows()):
            try:
                if (window and isinstance(window, wx.Dialog)
                        and window.GetParent() is frame and window.IsModal()):
                    window.EndModal(wx.ID_CANCEL)
            except Exception:
                errors.append(traceback.format_exc())
    finally:
        # App.ExitMainLoop targets the active loop, which may still belong to
        # ShowModal here. ScheduleExit also terminates an inactive outer loop
        # once its modal children unwind (wxWidgets 2.9.5+).
        loop = owner_loop if owner_loop is not None else application.GetMainLoop()
        if loop is None:
            raise RuntimeError('The palette acceptance main event loop is unavailable during abort.')
        loop.ScheduleExit(1)


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'native_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN',
                          'native_keymap': 'NOT_RUN', 'native_file_picker': 'NOT_RUN',
                          'manual_keyboard': 'NOT_RUN'}), flush=True)
        return 77
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
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
    from carr_library.ui import keyboard_settings as keyboard_module
    from carr_library.research import export_file as export_module

    checks, errors, observed = [], [], []
    source_identity, keymap_evidence = {}, {}
    original_picker = wx.FileDialog
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
    held = [None]
    in_tick, owner_loop = [False], [None]
    frame, timer = None, None
    stage = ['starting', monotonic()]
    overall_deadline = monotonic() + 140
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

    def keyboard_window():
        return next((window for window in wx.GetTopLevelWindows()
                     if window and isinstance(window, wx.Dialog) and window.GetParent() is frame
                     and window.GetTitle() == 'Keyboard shortcuts' and window.IsShown()), None)

    def setting_control(dialog, name):
        return next(control for control in dialog.GetChildren() if control.GetName() == name)

    class ExplicitKeymapPicker:
        """No native file-picker claim: the reviewed synthetic path is fixed."""
        def __init__(self, parent, title, **kwargs):
            keymap_evidence.setdefault('pickers', []).append((title, kwargs['style']))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ShowModal(self): return wx.ID_OK
        def GetPath(self): return str(keymap_file)

    def abort(detail):
        _abort_native_probe(wx, application, frame, owner_loop[0], timer, release, errors,
                            'Stage ' + stage[0] + ': ' + detail)

    def tick(event=None):
        # Native simulation/control updates may dispatch another timer message
        # before this callback returns. A stage must be evaluated once at a time.
        if in_tick[0] or errors:
            return
        in_tick[0] = True
        try:
            if owner_loop[0] is None:
                owner_loop[0] = application.GetMainLoop()
            advance_stage()
        finally:
            in_tick[0] = False

    def advance_stage():
        nonlocal save_binding
        try:
            if monotonic() > overall_deadline or monotonic() - stage[1] > 15:
                raise TimeoutError('Native command palette timed out at ' + stage[0])
            if held[0] is not None:
                return
            dialog = palette_window()
            settings = keyboard_window()
            name = stage[0]
            if name == 'starting':
                if frame.working or frame.evidence is None or frame.evidence.displayed is None:
                    return
                # Opening a scope initially selects Search. Reveal the exact
                # retained note before the foreground/focus driver targets it.
                evidence_page, = [index for index in range(frame.book.GetPageCount())
                                  if frame.book.GetPage(index) is frame.evidence]
                frame.book.ChangeSelection(evidence_page)
                check('the originating evidence note is on the visible native page',
                      frame.book.GetSelection() == evidence_page and frame.evidence.note.IsShownOnScreen())
                binding, = [row for row in keyboard.commands if row.action == ui.PALETTE_IDENTITY]
                check('palette belongs to the existing native shortcut inventory',
                      binding.item.GetItemLabel().endswith('\tCtrl+Shift+P')
                      and sum(row.item.GetItemLabel().endswith('\tCtrl+Shift+P')
                              for row in keyboard.commands) == 1)
                save_binding, = [row for row in keyboard.commands if row.action == 'file/save']
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
                # The pinned wxPython 4.2.4 char* binding requires ASCII bytes.
                # The next stage still verifies the actual native text value.
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
                      len(dialog.rows) == 1 and dialog.rows[0].command is save_binding)
                key(dialog.filter, wx.WXK_RETURN, 'save_accepted')
            elif name == 'save_accepted' and dialog is None and not frame.working:
                check('original Save handler commits the synthetic research note',
                      library.evidence(workspace, topic)[0].note == saved_note)
                check('original menu event runs after the palette closes', observed == [True])
                keyboard.apply({save_binding.action: 'Ctrl+Shift+F12'})
                advance('open_remapped')
            elif name == 'open_remapped':
                open_palette('remapped_opened')
            elif name == 'remapped_opened' and dialog is not None:
                dialog.filter.SetValue('Ctrl+Shift+F12')
                advance('remapped_filtered')
            elif name == 'remapped_filtered' and dialog is not None:
                check('palette search and list use the remapped native shortcut',
                      len(dialog.rows) == 1 and dialog.rows[0].command is save_binding
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
                      len(dialog.rows) == 1 and dialog.rows[0].command.action == 'edit/select all')
                key(dialog.filter, wx.WXK_RETURN, 'select_all_accepted')
            elif name == 'select_all_accepted' and dialog is None:
                check('original Edit Select all acts on the originating note after close',
                      frame.evidence.note.GetSelection() == (0, len(saved_note))
                      and wx.Window.FindFocus() is frame.evidence.note)
                save_binding.item.Enable(False)
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
                save_binding.item.Enable(True)
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
                    save_binding.item.Enable(False)
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
                save_binding.item.Enable(True)
                wx.FileDialog = ExplicitKeymapPicker
                advance('open_keymap')
            elif name == 'open_keymap':
                key(frame.evidence.note, ord('K'), 'keymap_opened', wx.MOD_CONTROL | wx.MOD_ALT)
            elif name == 'keymap_opened' and settings is not None:
                check('native registered shortcut opens keyboard settings', settings.IsModal())
                check('keymap review uses named native read-only status',
                      bool(setting_control(settings, 'Shortcut settings status').GetWindowStyleFlag() & wx.TE_READONLY))
                check('reviewed import and undo begin disabled',
                      not setting_control(settings, 'Apply reviewed import').IsEnabled()
                      and not setting_control(settings, 'Undo last change').IsEnabled())
                key(setting_control(settings, 'Export keymap...'), wx.WXK_SPACE, 'keymap_exported')
            elif name == 'keymap_exported' and settings is not None:
                if not keymap_file.exists():
                    return
                raw = keymap_file.read_bytes()
                keymap_evidence['raw'] = raw
                check('native export writes the exact existing schema with stable commands',
                      raw == (library.root / 'state/ui/shortcuts.json').read_bytes()
                      and json.loads(raw)['overrides'] == {save_binding.action: 'Ctrl+Shift+F12'})
                key(setting_control(settings, 'Export keymap...'), wx.WXK_SPACE, 'keymap_duplicate_rejected')
            elif name == 'keymap_duplicate_rejected' and settings is not None:
                check('native keymap export refuses an existing destination without changing its bytes',
                      'already exists' in setting_control(settings, 'Shortcut settings status').GetValue()
                      and keymap_file.read_bytes() == keymap_evidence['raw'])
                key(setting_control(settings, 'Reset all shortcuts to original keys'), wx.WXK_SPACE, 'keymap_reset')
            elif name == 'keymap_reset' and settings is not None:
                check('native reset restores the original command key',
                      keyboard.overrides == {} and save_binding.item.GetItemLabel().endswith('\tCtrl+S'))
                key(setting_control(settings, 'Preview keymap import...'), wx.WXK_SPACE, 'keymap_reviewed')
            elif name == 'keymap_reviewed' and settings is not None:
                check('native import preview explains the stable command and changes no saved keys',
                      'File / Save [file/save]: Ctrl+S → Ctrl+Shift+F12' in
                          setting_control(settings, 'Shortcut settings status').GetValue()
                      and keyboard.overrides == {}
                      and json.loads((library.root / 'state/ui/shortcuts.json').read_bytes())['overrides'] == {}
                      and setting_control(settings, 'Apply reviewed import').IsEnabled())
                key(setting_control(settings, 'Apply reviewed import'), wx.WXK_SPACE, 'keymap_applied')
            elif name == 'keymap_applied' and settings is not None:
                check('native reviewed import restores the key through the original command registry',
                      keyboard.overrides == {save_binding.action: 'Ctrl+Shift+F12'}
                      and save_binding.item.GetItemLabel().endswith('\tCtrl+Shift+F12')
                      and (library.root / 'state/ui/shortcuts.json').read_bytes() == keymap_evidence['raw']
                      and next(row for row in keyboard.commands if row.action == 'file/save') is save_binding
                      and not setting_control(settings, 'Apply reviewed import').IsEnabled())
                key(setting_control(settings, 'Undo last change'), wx.WXK_SPACE, 'keymap_undone')
            elif name == 'keymap_undone' and settings is not None:
                check('native undo restores the whole preceding keymap',
                      keyboard.overrides == {} and save_binding.item.GetItemLabel().endswith('\tCtrl+S')
                      and not setting_control(settings, 'Undo last change').IsEnabled()
                      and json.loads((library.root / 'state/ui/shortcuts.json').read_bytes())['overrides'] == {})
                key(setting_control(settings, 'Shortcut settings status'), wx.WXK_ESCAPE, 'keymap_closed')
            elif name == 'keymap_closed' and settings is None:
                wx.FileDialog = original_picker
                check('keymap review and transfer never dispatch Save or discard the draft',
                      observed == [True] and frame.evidence.note.GetValue() == unsaved
                      and library.evidence(workspace, topic)[0].note == saved_note)
                check('only explicit keymap file choices were supplied by the fixture',
                      keymap_evidence['pickers'] == [('Export keymap to a new file', wx.FD_SAVE)] * 2
                      + [('Preview keymap import', wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)])
                advance('complete')
                timer.Stop()
                application.ExitMainLoop()
        except Exception:
            abort(traceback.format_exc())

    try:
        bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
        installed_root = bundle or Path(sysconfig.get_path('purelib')).resolve()
        check('command palette loads from the installed package', Path(ui.__file__).resolve().is_relative_to(installed_root))
        checkout = Path(__file__).resolve().parents[1]
        for module in (ui, keyboard_module, export_module):
            actual = Path(module.__file__).resolve()
            expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
            digest = sha256(actual.read_bytes()).hexdigest()
            check('installed exact source: ' + module.__name__, not actual.is_relative_to(checkout)
                  and digest == sha256(expected.read_bytes()).hexdigest())
            source_identity[module.__name__] = digest
        check('native Windows wx is active', wx.Platform == '__WXMSW__')
        with TemporaryDirectory(prefix='research-command-palette-native-') as directory:
            keymap_file = Path(directory) / 'synthetic-keymap.json'
            library = LibraryApp(Path(directory) / 'library')
            workspace = library.create_workspace('Synthetic command palette')
            topic = library.create_topic(workspace, 'Native command tests')
            library.add_text(workspace, 'Synthetic source', 'A railway book for command testing.')
            hit, = library.search(workspace, 'railway')
            library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original synthetic note')
            frame = DesktopFrame(library)
            keyboard = frame.keyboard_settings
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
                try:
                    try:
                        if timer is not None:
                            timer.Stop()
                    finally:
                        release()
                finally:
                    wx.FileDialog = original_picker
                    if frame and not frame.IsBeingDeleted():
                        if save_binding is not None:
                            save_binding.item.Enable(True)
                        # Restore the deliberately uncommitted synthetic draft
                        # before teardown so it cannot trigger a save prompt.
                        if frame.evidence is not None and frame.evidence.displayed is not None:
                            frame.evidence.note.ChangeValue(library.evidence(workspace, topic)[0].note)
                        frame.Close(force=True)
                        application.Yield()
    except Exception:
        errors.append(traceback.format_exc())
    if errors:
        print(json.dumps({'status': 'FAIL', 'errors': errors, 'checks': checks,
                          'native_keyboard': 'INCOMPLETE', 'nvda': 'NOT_RUN',
                          'native_keymap': 'INCOMPLETE', 'source_identity': source_identity,
                          'native_file_picker': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN'}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps({'status': 'PASS', 'checks': checks, 'count': len(checks),
                      'native_keyboard': 'PASS', 'nvda': 'NOT_RUN',
                      'native_keymap': 'PASS', 'source_identity': source_identity,
                      'native_file_picker': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
