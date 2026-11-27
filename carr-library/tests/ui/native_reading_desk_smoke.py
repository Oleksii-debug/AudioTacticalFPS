"""Real native wx reader controls/events; synthetic text only, NVDA NOT_RUN.

Installed-wheel check: python -I tests/ui/native_reading_desk_smoke.py --native
For explicit checkout debugging only, append --source path/to/carr-library.
Missing display/wx prerequisites return NOT_RUN (77), never a successful check.
"""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import monotonic


def not_run(reason):
    print(json.dumps({'status': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source=None):
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('A native display is unavailable.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
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
    checks = []
    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (backend_module, state_module, ui_module)}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    checkout = Path(__file__).resolve().parents[2]
    if source is None:
        check('reader modules are loaded from the installed distribution',
              all(not Path(origin).is_relative_to(checkout) for origin in origins.values()))

    def pump(predicate, *, timeout=10):
        end = monotonic() + timeout
        while monotonic() < end:
            application.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    with TemporaryDirectory(prefix='carr-native-reader-') as tmp:
        library = LibraryApp(Path(tmp) / 'library')
        workspace = library.create_workspace('Synthetic reading')
        raw = 'First exact source. Україна 🚂.\nFirst paragraph.\fSecond exact source. Slovenská citácia.'
        book = library.add_text(workspace, 'Synthetic reader source', raw,
                                authors=('Synthetic author',))
        state = ReadingState(library.root)
        parent = wx.Frame(None, title='Native reading smoke')
        note = wx.TextCtrl(parent, value='Unsaved note — preserve this.')
        parent.Show()
        reader = ReadingDialog(parent, ReadingDesk(library, workspace, book['edition']),
                               state, lambda: None)
        reader.dialog.Show()
        reader.dialog.Raise()

        def click(label):
            control = next(button for button in reader.buttons if button.GetName() == label)
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
            event.SetEventObject(control)
            check('native button event: ' + label, control.GetEventHandler().ProcessEvent(event))
            if not pump(lambda: not reader.busy):
                raise TimeoutError('Native reader operation did not finish: ' + label)

        try:
            reader._run('Opening synthetic source', reader.controller.start, reader._page)
            check('reader opens asynchronously', pump(lambda: not reader.busy))
            check('source uses a native read-only multiline control',
                  isinstance(reader.text, wx.TextCtrl)
                  and bool(reader.text.GetWindowStyleFlag() & wx.TE_READONLY)
                  and bool(reader.text.GetWindowStyleFlag() & wx.TE_MULTILINE))
            check('first source text remains exact', reader.text.GetValue() == raw.split('\f')[0])
            check('native source control has a spoken name',
                  reader.text.GetName() == 'Full source page or text segment')
            check('native text receives focus after load',
                  pump(lambda: wx.Window.FindFocus() == reader.text, timeout=3))
            click('Next source location')
            check('next source location has exact second text', reader.text.GetValue() == raw.split('\f')[1])
            check('page-level resume stored second segment',
                  state.resume(workspace, book['edition']).index == 2)
            check('reading leaves unsaved host note unchanged', note.GetValue() == 'Unsaved note — preserve this.')
            click('Where am I?')
            check('location identifies segment without inventing printed pagination',
                  'segment 2' in reader.location.GetValue().lower()
                  and 'unpaginated' in reader.location.GetValue().lower()
                  and 'not the screen reader cursor' in reader.location.GetValue())
            check('where am I focuses the actual native location control',
                  pump(lambda: wx.Window.FindFocus() == reader.location, timeout=3))

            class SyntheticAnswers:
                def __init__(self):
                    self.choices = iter((0, 0))
                def text(self, *args, **kwargs):
                    return 'Seminar marker'
                def choose(self, *args, **kwargs):
                    return next(self.choices)
            reader.dialogs = SyntheticAnswers()
            click('Add named page bookmark...')
            bookmarks = state.bookmarks(workspace, book['edition'])
            check('named bookmark persists exact second source location',
                  len(bookmarks) == 1 and bookmarks[0].label == 'Seminar marker'
                  and bookmarks[0].location.index == 2)
            click('Start at first source location')
            check('explicit first location opens first source', reader.controller.current.ordinal == 1)
            click('Manage page bookmarks...')
            check('bookmark action reopens the exact saved source',
                  reader.controller.current.ordinal == 2
                  and reader.text.GetValue() == raw.split('\f')[1])
            click('Back in reading session')
            check('session back restores first source location', reader.controller.current.ordinal == 1)
            click('Forward in reading session')
            check('session forward restores bookmarked source location', reader.controller.current.ordinal == 2)
            check('reading does not manufacture citation selections', library._selections == {})
            reader.close()
            check('closing sets cancellation and ends worker ownership', reader.closed and reader.cancelled.is_set())
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                              'platform': platform.system(), 'origins': origins,
                              'source_override': str(source) if source is not None else None,
                              'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
        finally:
            reader.close()
            reader.dialog.Destroy()
            parent.Destroy()
            application.Yield()
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path, default=None,
                        help='Explicit checkout override for local debugging; default tests installed package.')
    args = parser.parse_args()
    return run_native(args.source.resolve() if args.source is not None else None)


if __name__ == '__main__':
    raise SystemExit(main())
