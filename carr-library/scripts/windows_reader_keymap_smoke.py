"""Actual Windows key dispatch for the installed Reading Desk and shared keymap.

The source document and host menu are synthetic fixtures. Navigation uses real
wx.UIActionSimulator OS events and the production dialog/registry. No voice,
network call, or NVDA claim is involved. Missing native prerequisites fail.
"""
from argparse import ArgumentParser
import json
import faulthandler
import traceback
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic


def not_run(reason):
    raise RuntimeError(reason)

def run_native(source, report, checkpoint):
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

    import carr_library.reader.reading_desk as backend_module
    import carr_library.reader.reading_state as state_module
    import carr_library.ui.reading_desk as ui_module
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.ui.reading_desk import ReadingDialog

    try:
        application = wx.App(False)
    except (Exception, SystemExit) as exc:
        return not_run('Native wx application could not open: ' + str(exc))
    # Keep native OS dispatch active before and after the Reader's modal loop.
    native_loop = wx.GUIEventLoop()
    loop_activation = wx.EventLoopActivator(native_loop)
    simulator = wx.UIActionSimulator()
    checks = []
    report['checks'] = checks
    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (backend_module, state_module, ui_module)}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        checkpoint(name)

    def installed_origins():
        if source is None:
            from importlib.metadata import distribution
            package = Path(distribution('universal-research-library').locate_file('carr_library')).resolve(strict=True)
            check('reader and keyboard modules load from the installed distribution',
                  all(Path(origin).is_relative_to(package) for origin in origins.values()))

    def pump(predicate, *, timeout=10):
        end = monotonic() + timeout
        while monotonic() < end:
            application.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    def settle(duration=0.25):
        # A negative assertion must wait for real OS key delivery, not merely
        # observe the unchanged state immediately after enqueueing a key.
        end = monotonic() + duration
        while monotonic() < end:
            application.Yield()
            wx.MilliSleep(10)

    def press(keycode, modifiers=wx.MOD_NONE):
        if not simulator.Char(keycode, modifiers):
            raise RuntimeError('The native input backend could not inject a key press.')
        settle()

    class Answers:
        """Only modal prompt answers are synthetic; dispatch uses actual keys."""

        def __init__(self):
            self.text_calls, self.choice_calls, self.guides = [], [], []

        def text(self, parent, title, *args, **kwargs):
            self.text_calls.append(title)
            return 'Native keyboard marker'

        def choose(self, parent, title, *args, **kwargs):
            self.choice_calls.append(title)
            return None

        def show(self, parent, title, content, *args, **kwargs):
            self.guides.append((title, content))

    with TemporaryDirectory(prefix='carr-native-reader-keys-') as tmp:
        library = LibraryApp(Path(tmp) / 'library')
        workspace = library.create_workspace('Native keyboard source')
        raw = 'First exact source. Україна.\fSecond exact source. Slovenská citácia.\fThird source.'
        book = library.add_text(workspace, 'Native keyboard source', raw)
        state = ReadingState(library.root)
        parent = wx.Frame(None, title='Reader keyboard context probe', size=(900, 750))
        note = wx.TextCtrl(parent, value='Unsaved host note — keep exactly.')
        host_calls = []
        menu, bar = wx.Menu(), wx.MenuBar()
        host_command = menu.Append(wx.ID_ANY, 'Academic library\tCtrl+Shift+B')
        parent.Bind(wx.EVT_MENU, lambda event: host_calls.append(event.GetId()), host_command)
        bar.Append(menu, '&Academic')
        parent.SetMenuBar(bar)
        from carr_library.ui.productivity import KeyboardActions, collect_commands
        from carr_library.keybindings import KeybindingStore
        origins.update({module.__name__: str(Path(module.__file__).resolve())
                        for module in (sys.modules[KeyboardActions.__module__], sys.modules[KeybindingStore.__module__])})
        parent.library, parent.menu_bar = library, bar
        parent.report = lambda message: None
        bindings = collect_commands(bar)
        parent.keyboard_actions = KeyboardActions(parent, bindings)
        parent.keyboard_actions.reload()
        parent.Show()
        parent.Raise()
        reader = ReadingDialog(parent, ReadingDesk(library, workspace, book['edition']),
                               state, lambda: None)
        answers = Answers()
        reader.dialogs = answers
        failures = []
        reopened = None
        release_worker = Event()

        def focus_source():
            reader.dialog.Raise()
            reader.text.SetFocus()
            if not pump(lambda: wx.Window.FindFocus() == reader.text, timeout=3):
                raise AssertionError('Native source text did not receive keyboard focus.')

        def at_page(ordinal):
            return (not reader.busy and reader.controller.current is not None
                    and reader.controller.current.ordinal == ordinal)

        def configure_next_with_native_controls():
            # The command enters via actual Ctrl+Alt+K. The answers are entered
            # in the production SingleChoiceDialog and field-dialog controls; Return is
            # delivered through the OS, not a constructed wx command event.
            state = {'active': True, 'step': 'choice', 'stages': [], 'error': None}
            pending = []
            deadline = monotonic() + 10
            def drive():
                if not state['active']:
                    return
                try:
                    windows = [window for window in wx.GetTopLevelWindows()
                               if isinstance(window, wx.Dialog) and window.IsModal()
                               and window is not reader.dialog]
                    if monotonic() >= deadline:
                        state['error'] = 'Native Reader shortcut settings did not complete.'
                        for window in windows:
                            window.EndModal(wx.ID_CANCEL)
                        return
                    for window in windows:
                        if state['step'] == 'choice' and isinstance(window, wx.SingleChoiceDialog):
                            controls = list(window.GetChildren())
                            for control in tuple(controls):
                                controls.extend(control.GetChildren())
                            choices = next(control for control in controls if isinstance(control, wx.ListBox))
                            matches = [index for index in range(choices.GetCount())
                                       if choices.GetString(index).startswith('Next source location;')]
                            if len(matches) != 1:
                                raise AssertionError('The native Reader command choice was ambiguous.')
                            choices.SetSelection(matches[0])
                            choices.SetFocus()
                            state['stages'].append('native command choice')
                            state['step'] = 'key'
                            if not simulator.Char(wx.WXK_RETURN):
                                raise RuntimeError('Native selection confirmation was unavailable.')
                            break
                        if state['step'] == 'key' and window.GetTitle() == 'Change keyboard shortcut':
                            controls = list(window.GetChildren())
                            for control in tuple(controls):
                                controls.extend(control.GetChildren())
                            fields = [control for control in controls if isinstance(control, wx.TextCtrl)]
                            if len(fields) != 1:
                                raise AssertionError('The production shortcut field dialog was ambiguous.')
                            fields[0].SetValue('Ctrl+Shift+J')
                            fields[0].SetFocus()
                            state['stages'].append('native key entry')
                            state['step'] = 'done'
                            if not simulator.Char(wx.WXK_RETURN):
                                raise RuntimeError('Native key confirmation was unavailable.')
                            return
                    if state['step'] != 'done':
                        pending.append(wx.CallLater(25, drive))
                except BaseException:
                    state['error'] = traceback.format_exc()
                    for window in wx.GetTopLevelWindows():
                        if isinstance(window, wx.Dialog) and window.IsModal() and window is not reader.dialog:
                            window.EndModal(wx.ID_CANCEL)
            pending.append(wx.CallLater(25, drive))
            try:
                focus_source()
                press(ord('K'), wx.MOD_CONTROL | wx.MOD_ALT)
                check('actual Reader shortcut settings save through native chooser and text controls',
                      state['error'] is None
                      and state['stages'] == ['native command choice', 'native key entry']
                      and reader.keyboard.shortcut('reader.next') == 'Ctrl+Shift+J')
            finally:
                state['active'] = False
                for callback in pending:
                    callback.Stop()

        def scenario():
            try:
                reader._run('Opening native keyboard source', reader.controller.start, reader._page)
                check('modal reader opens the first exact source', pump(lambda: at_page(1))
                      and reader.text.GetValue() == raw.split('\f')[0])
                check('reader is tested in a real native modal context', reader.dialog.IsModal())
                check('reader exposes the shared configurable command adapter',
                      hasattr(reader, 'keyboard') and callable(reader.keyboard.apply_overrides))
                adapter_module = sys.modules[type(reader.keyboard).__module__]
                origins[adapter_module.__name__] = str(Path(adapter_module.__file__).resolve())
                installed_origins()

                focus_source()
                press(wx.WXK_PAGEDOWN, wx.MOD_CONTROL)
                check('actual Ctrl+PageDown opens the next source', pump(lambda: at_page(2)))
                press(wx.WXK_PAGEUP, wx.MOD_CONTROL)
                check('actual Ctrl+PageUp opens the previous source', pump(lambda: at_page(1)))
                press(ord('L'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                check('actual Where am I shortcut focuses the named native location control',
                      pump(lambda: not reader.busy and wx.Window.FindFocus() == reader.location))

                focus_source()
                press(ord('B'), wx.MOD_CONTROL)
                check('actual bookmark shortcut persists the current source location',
                      pump(lambda: not reader.busy)
                      and len(state.bookmarks(workspace, book['edition'])) == 1
                      and state.bookmarks(workspace, book['edition'])[0].location.index == 1)
                focus_source()
                press(ord('B'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                check('shared chord dispatches to Reader bookmarks in the modal context',
                      pump(lambda: bool(answers.choice_calls) and not reader.busy)
                      and host_calls == [])

                # This exercises the production adapter's complete remap path,
                # including removal of the old native accelerator table entry.
                configure_next_with_native_controls()
                focus_source()
                press(wx.WXK_PAGEDOWN, wx.MOD_CONTROL)
                check('old next shortcut is inactive after remapping', at_page(1)
                      and state.resume(workspace, book['edition']).index == 1)
                press(ord('J'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                check('new next shortcut dispatches the real source navigation', pump(lambda: at_page(2)))
                press(wx.WXK_PAGEUP, wx.MOD_CONTROL)
                check('unmodified previous shortcut still works after remapping', pump(lambda: at_page(1)))
                press(wx.WXK_F1)
                check('native keyboard guide reports the current remapped shortcut',
                      bool(answers.guides) and 'Ctrl+Shift+J' in answers.guides[-1][1]
                      and 'Ctrl+PageDown' not in answers.guides[-1][1])

                reader.keyboard.apply_overrides({'reader.next': ''})
                focus_source()
                press(wx.WXK_PAGEDOWN, wx.MOD_CONTROL)
                press(ord('J'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                check('an explicitly unassigned action has neither old nor custom key', at_page(1))
                before_keys = KeybindingStore(library.root).path.read_bytes()
                try:
                    reader.keyboard.apply_overrides({'reader.next': 'Ctrl+PageUp'})
                except ValueError:
                    pass
                else:
                    raise AssertionError('A same-context Reader collision was accepted.')
                check('a rejected native-context collision preserves saved choices',
                      KeybindingStore(library.root).path.read_bytes() == before_keys)
                for protected in ('J', 'Shift+J', 'Ctrl+C', 'Ctrl+Shift+C', 'Ctrl+Left',
                                  'Insert+X', 'CapsLock+X', 'Ctrl+Alt+N'):
                    try:
                        reader.keyboard.apply_overrides({'reader.next': protected})
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('A native editing or screen-reader key was captured: ' + protected)
                check('editing and screen-reader protected assignments leave the keymap unchanged',
                      KeybindingStore(library.root).path.read_bytes() == before_keys)
                focus_source()
                press(ord('J'))
                press(wx.WXK_INSERT)
                check('ordinary typing and Insert do not navigate or change the read-only original',
                      at_page(1) and reader.text.GetValue() == raw.split('\f')[0])
                reader.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})

                next_button = next(button for button in reader.buttons
                                   if button.GetName() == 'Next source location')
                next_button.Disable()
                focus_source()
                press(ord('J'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                check('shortcut cannot bypass an explicitly disabled native command', at_page(1)
                      and not next_button.IsEnabled())
                next_button.Enable()

                started = Event()
                original_move = reader.controller.move

                def blocked_move(direction):
                    started.set()
                    if not release_worker.wait(10):
                        raise TimeoutError('Probe failed to release the held reader operation.')
                    return original_move(direction)

                reader.controller.move = blocked_move
                reader.navigate(1)
                check('reader operation is held while its native commands are disabled',
                      pump(started.is_set) and reader.busy
                      and all(not button.IsEnabled() for button in reader.buttons))
                reader.cancelled.set()
                prompt_counts = (len(answers.text_calls), len(answers.choice_calls), len(answers.guides))
                focus_source()
                press(ord('J'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                press(ord('B'), wx.MOD_CONTROL)
                press(ord('B'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                press(wx.WXK_F1)
                check('busy native shortcuts neither open prompts nor clear pending cancellation',
                      reader.busy and reader.cancelled.is_set()
                      and prompt_counts == (len(answers.text_calls), len(answers.choice_calls),
                                            len(answers.guides)))
                release_worker.set()
                check('cancelled work leaves the displayed and persisted source unchanged',
                      pump(lambda: at_page(1))
                      and state.resume(workspace, book['edition']).index == 1
                      and 'cancelled' in reader.status.GetValue().lower())
                reader.controller.move = original_move

                reader.keyboard.apply_overrides({})
                focus_source()
                press(ord('J'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                check('reset removes the custom native shortcut', at_page(1))
                press(wx.WXK_PAGEDOWN, wx.MOD_CONTROL)
                check('reset restores the default native shortcut', pump(lambda: at_page(2)))
                check('keyboard reading preserves the unsaved host note and citation state',
                      note.GetValue() == 'Unsaved host note — keep exactly.' and library._selections == {})
                reader.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})
                check('custom Reader key is saved through the shared preference store',
                      KeybindingStore(library.root).load() == {'reader.next': 'Ctrl+Shift+J'})
                focus_source()
                press(wx.WXK_ESCAPE)
                check('actual Escape closes the modal Reader', pump(lambda: reader.closed)
                      and reader.cancelled.is_set())
            except BaseException as exc:
                failures.append(exc)
            finally:
                release_worker.set()
                reader.close()

        try:
            wx.CallAfter(scenario)
            reader.dialog.ShowModal()
            if failures:
                raise failures[0]
            # A fresh registry/dialog must load the persisted context, not an
            # in-memory copy of the first dialog's accelerator table.
            parent.keyboard_actions = KeyboardActions(parent, bindings)
            parent.keyboard_actions.reload()
            reopened = ReadingDialog(parent, ReadingDesk(library, workspace, book['edition']), state, lambda: None)
            reopened.dialogs = answers
            def restored_scenario():
                try:
                    reopened._run('Reopening source', reopened.controller.start, reopened._page)
                    check('a reopened native Reader restores the measured source location',
                          pump(lambda: not reopened.busy and reopened.controller.current is not None
                               and reopened.controller.current.ordinal == 2))
                    reopened.dialog.Raise()
                    reopened.text.SetFocus()
                    check('the reopened source has native focus', pump(lambda: wx.Window.FindFocus() == reopened.text))
                    press(wx.WXK_PAGEDOWN, wx.MOD_CONTROL)
                    check('reopen preserves removal of the old default key',
                          reopened.controller.current.ordinal == 2)
                    press(ord('J'), wx.MOD_CONTROL | wx.MOD_SHIFT)
                    check('persisted custom key drives the reopened production dialog',
                          pump(lambda: not reopened.busy and reopened.controller.current.ordinal == 3))
                    press(wx.WXK_ESCAPE)
                    check('reopened Reader closes through actual Escape', pump(lambda: reopened.closed))
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    reopened.close()
            wx.CallAfter(restored_scenario)
            reopened.dialog.ShowModal()
            if failures:
                raise failures[0]
            check('all original source pages remain unchanged after remap, clear, reset and reopen',
                  [ReadingDesk(library, workspace, book['edition']).page(i).raw_text for i in (1, 2, 3)]
                  == raw.split('\f'))
            parent.Raise()
            note.SetFocus()
            check('host accepts native keyboard focus after Reader closes',
                  pump(lambda: wx.Window.FindFocus() == note, timeout=3))
            press(ord('B'), wx.MOD_CONTROL | wx.MOD_SHIFT)
            check('same chord invokes the host context only after Reader closes',
                  pump(lambda: len(host_calls) == 1)
                  and len(answers.choice_calls) == 1)
            report.update(wx=wx.version(), platform=platform.system(),
                          origins=origins, native_key_backend='wx.UIActionSimulator',
                          source_override=source is not None, manual_nvda='NOT_RUN')

        finally:
            release_worker.set()
            reader.close()
            reader.dialog.Destroy()
            if reopened is not None:
                reopened.close()
                reopened.dialog.Destroy()
            parent.Destroy()
            application.Yield()
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=None,
                        help='Explicit trusted checkout; omit for the installed distribution.')
    args = parser.parse_args()
    report = {'status': 'FAIL', 'phase': 'incomplete', 'checks': [], 'manual_nvda': 'NOT_RUN'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def checkpoint(phase):
        report['phase'] = phase
        raw = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        args.output.write_text(raw, encoding='utf-8')
        print('READER_KEYMAP_PHASE ' + phase, flush=True)
    checkpoint('starting')
    if sys.platform != 'win32' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run the native Reader keymap probe with Windows Python -I -B.')
    faulthandler.dump_traceback_later(120, repeat=False, exit=True)
    handle = None
    try:
        source = args.source.resolve(strict=True) if args.source is not None else None
        if source is not None:
            sys.path.insert(0, str(source))
            sys.path.insert(0, str(source / 'src'))
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        result = run_native(source, report, checkpoint)
        if result != 0:
            raise RuntimeError('Native keymap acceptance failed.')
        report['status'] = 'PASS'
        checkpoint('complete')
        return 0
    except BaseException:
        report['status'] = 'FAIL'
        report['failure'] = traceback.format_exc()[-32000:]
        checkpoint('failed')
        raise
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
