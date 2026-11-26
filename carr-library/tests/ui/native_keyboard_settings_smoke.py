"""Installed native Main-window shortcuts, settings, and command launcher.

Run: python -I tests/ui/native_keyboard_settings_smoke.py --native
Checkout debugging only: append --source path/to/carr-library.
Real wx.UIActionSimulator keys exercise native menu accelerators and buttons.
The source library, evidence note, test menu command, and keymap are synthetic.
Missing display/wx prerequisites return NOT_RUN (77); NVDA is always NOT_RUN.
"""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None, timeout=120):
    if sys.platform not in ('win32', 'darwin') and not os.environ.get('DISPLAY'):
        return not_run('A supported native display is unavailable; wx key simulation requires X11 on Linux.')
    if os.environ.get('GDK_BACKEND') == 'wayland':
        return not_run('wx.UIActionSimulator does not support the GTK Wayland backend.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    if not hasattr(wx, 'UIActionSimulator'):
        return not_run('This wxPython build does not provide native key simulation.')
    if source is not None:
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / 'src'))

    import carr_library.application as application_module
    import carr_library.keyboard_preferences as preferences_module
    import carr_library.ui.desktop_app as desktop_module
    import carr_library.ui.keyboard_settings as keyboard_module
    from carr_library.application import LibraryApp
    from carr_library.keyboard_preferences import KeymapStore
    from carr_library.ui.command_registry import CommandSpec
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.keyboard_settings import KeyboardSettingsDialog, show_command_launcher

    checks, failures = [], []
    deadline = monotonic() + timeout
    frame = None

    def abort(reason):
        failures.append(reason)
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    try:
        application = ProbeApp(False)
    except (Exception, SystemExit) as exc:
        return not_run('Native wx application could not open: ' + str(exc))
    # A manually pumped native probe still needs an active event loop. On MSW,
    # App.Yield without one processes pending Python work but cannot dispatch
    # the actual OS keyboard messages through wxApp::Dispatch.
    native_loop = wx.GUIEventLoop()
    loop_activation = wx.EventLoopActivator(native_loop)
    simulator = wx.UIActionSimulator()
    watchdog = wx.CallLater(int(timeout * 1000), abort, 'Native keyboard probe exceeded its bounded timeout.')

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def healthy():
        if failures:
            raise AssertionError('; '.join(failures))
        if monotonic() >= deadline:
            raise TimeoutError('Native keyboard probe exceeded its bounded timeout.')

    def pump(predicate, *, seconds=8):
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            application.Yield()
            healthy()
            if predicate():
                return True
            wx.MilliSleep(10)
        healthy()
        return False

    def settle(seconds=0.25):
        # Negative shortcut assertions must allow actual OS input delivery.
        end = min(deadline, monotonic() + seconds)
        while monotonic() < end:
            application.Yield()
            healthy()
            wx.MilliSleep(10)

    def press(keycode, modifiers=wx.MOD_NONE):
        if not simulator.Char(keycode, modifiers):
            raise RuntimeError('Native input backend could not inject a key press.')
        settle()

    def focus(control):
        control.SetFocus()
        if not pump(lambda: wx.Window.FindFocus() == control, seconds=3):
            raise AssertionError('Native keyboard focus did not reach ' + control.GetName())

    def button(dialog, *, identifier=None, caption=None):
        return next(child for child in dialog.GetChildren()
                    if isinstance(child, wx.Button)
                    and (child.GetId() == identifier if identifier is not None
                         else child.GetLabel().replace('&', '') == caption))

    def activate(control):
        if not control.IsEnabled():
            raise AssertionError('Attempted to use a disabled native button: ' + control.GetLabel())
        focus(control)
        press(wx.WXK_SPACE)

    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in
               (application_module, preferences_module, desktop_module, keyboard_module)}
    checkout = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix='carr-native-main-keyboard-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'private-library')
        workspace = library.create_workspace('Synthetic keyboard library')
        topic = library.create_topic(workspace, 'Synthetic keyboard research')
        source_text = 'Synthetic keyboard quotation. Exact original source — Україна.'
        imported = library.add_text(workspace, 'Synthetic keyboard source', source_text)
        hit, = library.search(workspace, 'keyboard quotation')
        saved_note = 'Saved research note remains separate from source.'
        evidence_id = library.save_hit(workspace, topic, hit.ref, hit.revision, saved_note)
        original_bytes = library.assets.resolve(imported['asset']).read_bytes()
        draft = 'Unsaved keyboard draft — keep this exact note.'
        calls = []
        test_id = 'probe.keyboard_action'
        test_caption = 'Synthetic keyboard action'
        settings = None

        def preserved():
            return (frame.library is library and frame.active == (workspace, topic)
                    and frame.evidence.note.GetValue() == draft and frame._dirty()
                    and library.evidence(workspace, topic)[0].note == saved_note
                    and library.assets.resolve(imported['asset']).read_bytes() == original_bytes)

        try:
            frame = DesktopFrame(library)
            frame._open_workspace(workspace, topic)
            application.SetTopWindow(frame)
            frame.SetSize((1100, 900))
            frame.Show()
            frame.Raise()
            check('production DesktopFrame opens the synthetic evidence topic',
                  pump(lambda: frame.active == (workspace, topic) and frame.evidence is not None
                       and frame.evidence.displayed is not None
                       and frame.evidence.displayed.evidence_id == evidence_id and not frame.working))
            frame._restore_note_focus()
            frame.evidence.note.SetValue(draft)
            focus(frame.evidence.note)
            check('probe starts with a real unsaved research note', preserved())
            if source is None:
                check('application and keyboard modules load from the installed distribution',
                      all(not Path(origin).is_relative_to(checkout) for origin in origins.values()))

            menu = wx.Menu()
            action = menu.Append(wx.ID_ANY, test_caption + '\tCtrl+Shift+J')
            frame.GetMenuBar().Append(menu, '&Probe')
            frame.Bind(wx.EVT_MENU, lambda event: calls.append(event.GetId()), action)
            manager = frame.keyboard_commands
            manager.register_menu_command(CommandSpec(test_id, test_caption, 'main', 'Ctrl+Shift+J'), action)
            check('extension command uses the production stable-ID registry', manager.available(test_id))
            keymap = manager.store.path

            def host_key(letter):
                frame.Raise()
                frame._restore_note_focus()
                focus(frame.evidence.note)
                press(ord(letter), wx.MOD_CONTROL | wx.MOD_SHIFT)

            host_key('J')
            check('actual default shortcut dispatches the current native menu handler',
                  pump(lambda: len(calls) == 1) and calls == [action.GetId()])

            def settings_session(operation):
                nonlocal settings
                settings = KeyboardSettingsDialog(frame, manager)
                errors = []

                def edit():
                    try:
                        check('settings use named native controls in a real modal dialog',
                              settings.dialog.IsModal() and isinstance(settings.shortcut, wx.TextCtrl)
                              and settings.context.GetName() == 'Command context')
                        settings.search.SetValue(test_caption)
                        if not pump(lambda: len(settings.rows) == 1
                                    and settings.rows[0].command_id == test_id):
                            raise AssertionError('Native command filter did not find the stable test command.')
                        operation(settings)
                        if settings.dialog.IsShown():
                            raise AssertionError('Native settings operation did not close the dialog: '
                                                 + settings.status.GetValue())
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        # IsModal stays true until ShowModal unwinds on MSW,
                        # even after native Save has called EndModal(ID_OK).
                        # Do not overwrite that real result during cleanup.
                        if settings.dialog.IsModal() and settings.dialog.IsShown():
                            settings.dialog.EndModal(wx.ID_CANCEL)

                wx.CallAfter(edit)
                try:
                    result = settings.show()
                finally:
                    settings = None
                if errors:
                    raise errors[0]
                healthy()
                return result

            def remap(dialog):
                dialog.shortcut.SetValue('Ctrl+Shift+K')
                activate(button(dialog.dialog, caption='Assign shortcut'))
                check('native Assign edits a draft before persistence',
                      dialog.draft[test_id] == 'Ctrl+Shift+K'
                      and manager.shortcut(test_id) == 'Ctrl+Shift+J' and not keymap.exists())
                activate(button(dialog.dialog, identifier=wx.ID_OK))

            check('native Save closes the settings dialog successfully', settings_session(remap))
            check('saved native settings persist a semantic command ID and update the menu',
                  KeymapStore(keymap).load()[test_id] == 'Ctrl+Shift+K'
                  and action.GetItemLabel().endswith('\tCtrl+Shift+K'))
            host_key('J')
            check('old native menu accelerator stops after remapping', len(calls) == 1)
            host_key('K')
            check('new native menu accelerator dispatches exactly once', pump(lambda: len(calls) == 2))

            action.Enable(False)
            host_key('K')
            check('actual shortcut cannot dispatch a disabled native menu item',
                  len(calls) == 2 and not manager.available(test_id))
            action.Enable(True)
            before = keymap.read_bytes()

            # Integrated modules legitimately own additional shortcuts (the
            # assistant owns Ctrl+Alt+J). The cancellation case needs a valid,
            # unapplied draft; the separate case below tests a real conflict.
            cancel_shortcut = None
            for letter in 'JKLMNOPQRSTUVWXYZ':
                candidate = 'Ctrl+Alt+Shift+' + letter
                try:
                    manager.validate({**manager.overrides, test_id: candidate})
                except ValueError:
                    continue
                cancel_shortcut = candidate
                break
            check('cancel probe chooses an available integrated shortcut', cancel_shortcut is not None)

            def cancel(dialog):
                dialog.shortcut.SetValue(cancel_shortcut)
                activate(button(dialog.dialog, caption='Assign shortcut'))
                check('cancel probe has an unapplied native settings draft',
                      dialog.draft[test_id] == cancel_shortcut and manager.shortcut(test_id) == 'Ctrl+Shift+K')
                press(wx.WXK_ESCAPE)

            check('actual Escape cancels the native settings dialog', not settings_session(cancel))
            check('Cancel preserves saved bindings and the complete unsaved evidence note',
                  keymap.read_bytes() == before and manager.shortcut(test_id) == 'Ctrl+Shift+K' and preserved())

            def conflict(dialog):
                dialog.shortcut.SetValue('Ctrl+N')
                activate(button(dialog.dialog, identifier=wx.ID_OK))
                check('conflicting shortcut remains in the open native settings dialog',
                      dialog.dialog.IsModal() and 'not saved' in dialog.status.GetValue().lower()
                      and 'both' in dialog.status.GetValue().lower())
                check('failed native Save preserves the previous file, binding, source, and note',
                      keymap.read_bytes() == before and manager.shortcut(test_id) == 'Ctrl+Shift+K'
                      and preserved())
                press(wx.WXK_ESCAPE)

            check('rejected settings can be cancelled without publication', not settings_session(conflict))

            def unassign(dialog):
                dialog.shortcut.SetValue('')
                activate(button(dialog.dialog, identifier=wx.ID_OK))

            check('native settings support explicit unassignment', settings_session(unassign)
                  and manager.shortcut(test_id) == '' and '\t' not in action.GetItemLabel())
            host_key('K')
            check('unassigned native accelerator no longer dispatches', len(calls) == 2)

            launcher_errors = []

            def choose_command():
                dialog = None
                try:
                    dialog = next(window for window in wx.GetTopLevelWindows()
                                  if isinstance(window, wx.Dialog) and window.GetTitle() == 'Find a command')
                    search = next(child for child in dialog.GetChildren()
                                  if child.GetName() == 'Search commands by name')
                    results = next(child for child in dialog.GetChildren()
                                   if child.GetName() == 'Matching commands and shortcuts')
                    search.SetValue(test_caption)
                    check('native launcher finds an available command with no assigned shortcut',
                          pump(lambda: results.GetCount() == 1)
                          and 'Not assigned' in results.GetString(0))
                    activate(button(dialog, identifier=wx.ID_OK))
                    if dialog.IsShown():
                        raise AssertionError('Native Run command did not close the launcher.')
                except BaseException as exc:
                    launcher_errors.append(exc)
                finally:
                    if dialog is not None and dialog.IsModal() and dialog.IsShown():
                        dialog.EndModal(wx.ID_CANCEL)

            wx.CallAfter(choose_command)
            launched = show_command_launcher(frame, manager)
            if launcher_errors:
                raise launcher_errors[0]
            check('native launcher invokes the original current menu handler', launched and len(calls) == 3)

            def reset(dialog):
                activate(button(dialog.dialog, caption='Reset selected'))
                check('native reset selects the original command shortcut',
                      dialog.shortcut.GetValue() == 'Ctrl+Shift+J')
                activate(button(dialog.dialog, identifier=wx.ID_OK))

            check('native settings save the reset shortcut', settings_session(reset)
                  and manager.shortcut(test_id) == 'Ctrl+Shift+J')
            host_key('K')
            check('reset removes the previously custom native accelerator', len(calls) == 3)
            host_key('J')
            check('reset restores the original native accelerator', pump(lambda: len(calls) == 4))
            focus(frame.evidence.note)
            frame.evidence.note.SetSelection(0, 0)
            press(ord('A'), wx.MOD_CONTROL)
            check('native Ctrl+A still selects editable note text',
                  frame.evidence.note.GetStringSelection() == draft and len(calls) == 4)
            check('all keyboard workflows preserve the original data root, source, saved note, and unsaved draft',
                  preserved())
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                              'platform': platform.system(), 'origins': origins,
                              'native_key_backend': 'wx.UIActionSimulator',
                              'settings_controls': 'native; command filter and shortcut text supplied by probe',
                              'source_override': str(source) if source is not None else None,
                              'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
            return 0
        except Exception as exc:
            print(json.dumps({'status': 'FAIL', 'checks_passed': len(checks), 'error': str(exc),
                              'nvda': 'NOT_RUN'}), flush=True)
            traceback.print_exc()
            return 1
        finally:
            watchdog.Stop()
            if frame is not None:
                frame.cancel_event.set()
                frame._closing = True
                frame.executor.shutdown(wait=False, cancel_futures=True)
                frame.Destroy()
                application.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path, default=None,
                        help='Explicit checkout override; the default tests the installed package.')
    parser.add_argument('--timeout', type=float, default=120,
                        help='Maximum native event-loop duration in seconds (5–170).')
    args = parser.parse_args()
    if not 5 <= args.timeout <= 170:
        parser.error('--timeout must be between 5 and 170 seconds.')
    return run_native(args.source.resolve() if args.source is not None else None, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
