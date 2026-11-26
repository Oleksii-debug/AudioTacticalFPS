"""Real native keyboard events for Reader shortcuts, remapping, and dispatch.

Installed-wheel check: python -I tests/ui/native_reader_keymap_smoke.py --native
Explicit checkout debugging: append --source path/to/carr-library.
This probe uses wx.UIActionSimulator, never constructed command/button events.
Missing native prerequisites return NOT_RUN (77). NVDA remains NOT_RUN.
"""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None):
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
    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (backend_module, state_module, ui_module)}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    checkout = Path(__file__).resolve().parents[2]

    def installed_origins():
        if source is None:
            check('reader and keyboard modules load from the installed distribution',
                  all(not Path(origin).is_relative_to(checkout) for origin in origins.values()))

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
        host_command = menu.Append(wx.ID_ANY, 'Main academic context\tCtrl+Shift+B')
        parent.Bind(wx.EVT_MENU, lambda event: host_calls.append(event.GetId()), host_command)
        bar.Append(menu, '&Library')
        parent.SetMenuBar(bar)
        parent.Show()
        parent.Raise()
        reader = ReadingDialog(parent, ReadingDesk(library, workspace, book['edition']),
                               state, lambda: None)
        answers = Answers()
        reader.dialogs = answers
        failures = []
        release_worker = Event()

        def focus_source():
            reader.dialog.Raise()
            reader.text.SetFocus()
            if not pump(lambda: wx.Window.FindFocus() == reader.text, timeout=3):
                raise AssertionError('Native source text did not receive keyboard focus.')

        def at_page(ordinal):
            return (not reader.busy and reader.controller.current is not None
                    and reader.controller.current.ordinal == ordinal)

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
                reader.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})
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
            parent.Raise()
            note.SetFocus()
            check('host accepts native keyboard focus after Reader closes',
                  pump(lambda: wx.Window.FindFocus() == note, timeout=3))
            press(ord('B'), wx.MOD_CONTROL | wx.MOD_SHIFT)
            check('same chord invokes the host context only after Reader closes',
                  pump(lambda: len(host_calls) == 1)
                  and len(answers.choice_calls) == 1)
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                              'platform': platform.system(), 'origins': origins,
                              'native_key_backend': 'wx.UIActionSimulator',
                              'source_override': str(source) if source is not None else None,
                              'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
        finally:
            release_worker.set()
            reader.close()
            reader.dialog.Destroy()
            parent.Destroy()
            application.Yield()
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path, default=None,
                        help='Explicit checkout override; the default tests the installed package.')
    args = parser.parse_args()
    return run_native(args.source.resolve() if args.source is not None else None)


if __name__ == '__main__':
    raise SystemExit(main())
