"""Installed native command-search probe; synthetic notes and no NVDA claim."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.', 'nvda': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import command_palette as ui

    checks, errors = [], []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    application = ProbeApp(False)

    def pump(predicate):
        deadline = monotonic() + 15
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() > deadline:
                raise TimeoutError('Native command palette setup did not complete.')
            application.Yield()
            sleep(0.01)

    def enter_filter(dialog):
        event = wx.CommandEvent(wx.EVT_TEXT_ENTER.typeId, dialog.filter.GetId())
        event.SetEventObject(dialog.filter)
        check('native filter Enter event is handled', dialog.filter.GetEventHandler().ProcessEvent(event))

    def run_palette(keyboard, action):
        def ready(dialog):
            try:
                action(dialog)
            except Exception:
                errors.append(traceback.format_exc())
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)

        def expired():
            errors.append('Native command palette did not close before its deadline.')
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)

        watchdog = wx.CallLater(15000, expired)
        try:
            result = ui.show_command_palette(keyboard, on_ready=ready)
        finally:
            watchdog.Stop()
        if errors:
            raise RuntimeError('\n'.join(errors))
        return result

    check('command palette loads from the installed wheel',
          'site-packages' in [part.casefold() for part in Path(ui.__file__).parts])
    check('native Windows wx is active', wx.Platform == '__WXMSW__')
    with TemporaryDirectory(prefix='research-command-palette-native-') as directory:
        library = LibraryApp(Path(directory) / 'library')
        workspace = library.create_workspace('Synthetic command palette')
        topic = library.create_topic(workspace, 'Native command tests')
        library.add_text(workspace, 'Synthetic source', 'A railway book for command testing.')
        hit, = library.search(workspace, 'railway')
        library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original synthetic note')
        frame = DesktopFrame(library)
        try:
            application.SetTopWindow(frame)
            frame.Show()
            frame._open_workspace(workspace, topic)
            pump(lambda: not frame.working and frame.evidence is not None and frame.evidence.displayed is not None)
            keyboard = frame.keyboard_settings
            palette_binding, = [row for row in keyboard.commands if row.action == ui.PALETTE_IDENTITY]
            check('palette belongs to the existing native shortcut inventory',
                  palette_binding.item.GetItemLabel().endswith('\tCtrl+Shift+P')
                  and sum(row.item.GetItemLabel().endswith('\tCtrl+Shift+P') for row in keyboard.commands) == 1)
            save_binding, = [row for row in keyboard.commands if row.action == 'file/save']
            observed = []

            def observe_original_dispatch(event):
                observed.append(not any(isinstance(window, wx.Dialog) and window.IsModal()
                                        for window in wx.GetTopLevelWindows()))
                event.Skip()

            frame.Bind(wx.EVT_MENU, observe_original_dispatch, id=save_binding.item.GetId())
            saved_note = 'Saved through the searchable native command palette.'
            frame.evidence.note.SetValue(saved_note)

            def choose_save(dialog):
                check('filter and results have accessible names',
                      dialog.filter.GetName() == 'Find a command by name, menu path or shortcut'
                      and dialog.commands.GetName() == 'Matching commands with current shortcuts and availability')
                check('status uses a native read-only multiline control',
                      bool(dialog.status.GetWindowStyleFlag() & wx.TE_READONLY)
                      and bool(dialog.status.GetWindowStyleFlag() & wx.TE_MULTILINE))
                check('Escape is bound to native cancellation', dialog.GetEscapeId() == wx.ID_CANCEL)
                dialog.filter.SetValue('file/save Ctrl+S')
                dialog.refresh()
                check('filter resolves the existing native Save binding',
                      len(dialog.rows) == 1 and dialog.rows[0].command is save_binding)
                dialog.filter.SetFocus()
                application.Yield()
                check('filter can receive native keyboard focus', wx.Window.FindFocus() is dialog.filter)
                dialog.filter.Navigate()
                application.Yield()
                check('Tab reaches the native matching-command list', wx.Window.FindFocus() is dialog.commands)
                enter_filter(dialog)

            check('palette dispatch reports the original menu event', run_palette(keyboard, choose_save))
            pump(lambda: not frame.working)
            check('original Save handler commits the synthetic research note',
                  library.evidence(workspace, topic)[0].note == saved_note)
            check('original menu event runs after the palette closes', observed == [True])

            keyboard.apply({save_binding.action: 'Ctrl+Shift+F12'})
            previous_focus = frame.evidence.note
            previous_focus.SetFocus()
            application.Yield()
            check('research note has focus before command search', wx.Window.FindFocus() is previous_focus)

            def inspect_remapped(dialog):
                dialog.filter.SetValue('Ctrl+Shift+F12')
                dialog.refresh()
                check('palette search and list use the remapped native shortcut',
                      len(dialog.rows) == 1 and dialog.rows[0].command is save_binding
                      and dialog.rows[0].shortcut == 'Ctrl+Shift+F12')
                dialog.EndModal(wx.ID_CANCEL)

            check('cancelling search does not dispatch a command', not run_palette(keyboard, inspect_remapped))
            check('cancel preserves the dispatch count', observed == [True])
            check('cancel restores focus to the originating research note', wx.Window.FindFocus() is previous_focus)

            previous_focus.SetSelection(0, 0)
            def choose_select_all(dialog):
                dialog.filter.SetValue('edit/select all')
                dialog.refresh()
                check('Edit command comes from the same native menu registry',
                      len(dialog.rows) == 1 and dialog.rows[0].command.action == 'edit/select all')
                enter_filter(dialog)

            check('palette dispatches the existing Edit Select all handler', run_palette(keyboard, choose_select_all))
            check('Edit handler acts on the originating note after the palette closes',
                  previous_focus.GetSelection() == (0, len(saved_note))
                  and wx.Window.FindFocus() is previous_focus)
            save_binding.item.Enable(False)
            try:
                def inspect_disabled(dialog):
                    dialog.filter.SetValue('file/save Ctrl+Shift+F12')
                    dialog.refresh()
                    check('disabled native commands remain discoverable and clearly marked',
                          len(dialog.rows) == 1 and not dialog.rows[0].enabled
                          and 'unavailable now' in dialog.commands.GetString(0)
                          and not dialog.run.IsEnabled())
                    enter_filter(dialog)
                    check('Enter cannot accept a disabled native command', dialog.IsModal() and dialog.result is None)
                    dialog.EndModal(wx.ID_CANCEL)

                check('disabled command does not dispatch', not run_palette(keyboard, inspect_disabled))
            finally:
                save_binding.item.Enable(True)

            unsaved = 'A synthetic draft that must remain unsaved after command invalidation.'
            frame.evidence.note.SetValue(unsaved)
            try:
                def disable_during_close(dialog):
                    dialog.filter.SetValue('file/save Ctrl+Shift+F12')
                    dialog.refresh()
                    enter_filter(dialog)
                    save_binding.item.Enable(False)

                check('command availability is checked again after modal acceptance',
                      not run_palette(keyboard, disable_during_close))
                check('invalidated command neither executes nor discards the draft',
                      observed == [True] and frame.evidence.note.GetValue() == unsaved
                      and library.evidence(workspace, topic)[0].note == saved_note)
            finally:
                save_binding.item.Enable(True)
        finally:
            if not frame.IsBeingDeleted():
                frame.Close(force=True)
                application.Yield()
            if native_handle is not None:
                native_handle.close()
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'count': len(checks), 'nvda': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
