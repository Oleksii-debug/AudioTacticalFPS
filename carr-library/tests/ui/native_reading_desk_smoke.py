"""Real native wx reader controls/events; synthetic voice/playback, NVDA NOT_RUN.

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



def verify_voice_reader_controls(application, parent, library, workspace, check, origins, source):
    """Real wx inputs and reader work; the voice and playback sink are synthetic."""
    from threading import Event
    from time import sleep
    from unittest.mock import patch
    import wave
    import wx
    import carr_library.speech.reading as reading_module
    import carr_library.ui.speech_dialog as speech_module
    import carr_library.ui.pronunciation_dialog as pronunciation_module
    import carr_library.ui.reading_shortcuts as shortcuts_module
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech import ReadingDocument, Voice, VoiceRegistry
    from carr_library.speech.source import read_source_range

    for module in (reading_module, speech_module, pronunciation_module, shortcuts_module):
        origins[module.__name__] = str(Path(module.__file__).resolve())
    if source is None:
        checkout = Path(__file__).resolve().parents[2]
        check('voice-reader modules are loaded from the installed distribution',
              all(not Path(origin).is_relative_to(checkout) for origin in origins.values()))
    raw = '🚂 Dr. Example.\nSecond line has 12 kg.\n\nNext paragraph.'
    imported = library.add_text(workspace, 'Synthetic voice-reader source', raw)
    original = library.assets.resolve(imported['asset'])
    original_bytes = original.read_bytes()
    desk = ReadingDesk(library, workspace, imported['edition'])
    parent.library = library
    requests, played, failures = [], [], []
    holding, entered, disposed = Event(), Event(), Event()
    fixture = {'completed': False}
    dictionary_file = library.root.parent / 'native-pronunciations.json'
    project_file = library.root.parent / 'native-reader.reading.json'
    clipboard_text = 'Explicit native clipboard snapshot — оригінал.'

    def guard():
        with library.read(workspace):
            pass

    class SyntheticVoice:
        name = 'Explicit synthetic native-control fixture'
        def voices(self):
            return (Voice('native-fixture:voice', 'Synthetic native fixture', 'fixture', 'en',
                          capabilities=('rate', 'volume')),)
        def render(self, text, voice, destination, settings, cancelled):
            if holding.is_set():
                entered.set()
                end = monotonic() + 8
                while holding.is_set() and not cancelled():
                    if monotonic() >= end:
                        raise TimeoutError('Synthetic cancellation fixture did not receive Stop.')
                    sleep(.01)
            if cancelled():
                raise InterruptedError('Synthetic voice operation cancelled.')
            with wave.open(str(destination), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b'\0\0' * 800)
        def close(self):
            disposed.set()

    class ObservedRegistry(VoiceRegistry):
        def synthesize(self, text, *args, **kwargs):
            requests.append(text)
            return super().synthesize(text, *args, **kwargs)

    class SyntheticPlayback:
        state, position_seconds = 'stopped', 0
        def play(self, path):
            check('synthetic playback receives a completed PCM file', Path(path).is_file())
            played.append(Path(path))
            self.state = 'playing'
        def stop(self): self.state = 'stopped'
        def pause(self): self.state = 'paused'
        def resume(self): self.state = 'playing'
        def close(self): self.state = 'stopped'

    def registry(*args, **kwargs):
        if kwargs.get('cancelled', lambda: False)():
            raise InterruptedError('Synthetic discovery cancelled.')
        return ObservedRegistry([SyntheticVoice()])

    class SelectedPath:
        """Only picker answers are supplied; no picker automation is claimed."""
        def __init__(self, parent, message, **kwargs):
            self.path = {
                'Export dictionary to a new JSON file': dictionary_file,
                'Import pronunciation dictionary': dictionary_file,
                'Save reading-copy project': project_file,
            }[message]
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def ShowModal(self): return wx.ID_OK
        def GetPath(self): return str(self.path)

    real_message_box = wx.MessageBox
    def confirmation(message, caption, *args, **kwargs):
        if caption != 'Unsaved reading copy':
            return real_message_box(message, caption, *args, **kwargs)
        return wx.NO

    def window(title):
        return next((item for item in wx.GetTopLevelWindows()
                     if isinstance(item, wx.Dialog) and item.GetTitle() == title
                     and item.IsShown()), None)

    def control(dialog, name, *, label=False):
        pending = list(dialog.GetChildren())
        while pending:
            item = pending.pop(0)
            if (item.GetLabel().replace('&', '') if label else item.GetName()) == name:
                return item
            pending.extend(item.GetChildren())
        raise AssertionError('Missing native reader control: ' + name)

    def force_close():
        holding.clear()
        for title in ('Pronunciation dictionary', 'Voice reader and reading copy'):
            item = window(title)
            if item is not None:
                if item.IsModal():
                    item.EndModal(wx.ID_CANCEL)
                else:
                    item.Close()

    def failed(error):
        failures.append(error)
        timer.Stop()
        force_close()

    def post(action):
        def invoke():
            try:
                action()
            except BaseException as error:
                failed(error)
        wx.CallAfter(invoke)

    def press(button):
        check('native named reader button: ' + button.GetName(),
              isinstance(button, wx.Button) and button.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native reader event dispatched: ' + button.GetName(),
              button.GetEventHandler().ProcessEvent(event))

    def await_condition(predicate, message, timeout=12):
        end = monotonic() + timeout
        while not predicate():
            if monotonic() >= end:
                raise TimeoutError(message)
            yield

    saved_clipboard = {'readable': False, 'text': ''}
    if not wx.TheClipboard.Open():
        raise RuntimeError('The native clipboard could not be opened for its explicit fixture.')
    try:
        previous = wx.TextDataObject()
        saved_clipboard['readable'] = bool(wx.TheClipboard.GetData(previous))
        if saved_clipboard['readable']:
            saved_clipboard['text'] = previous.GetText()
        check('explicit fixture text is placed in the native clipboard',
              wx.TheClipboard.SetData(wx.TextDataObject(clipboard_text)))
    finally:
        wx.TheClipboard.Close()

    simulator = wx.UIActionSimulator()
    def key(code):
        check('native keyboard key-down is delivered', simulator.KeyDown(code))
        check('native keyboard key-up is delivered', simulator.KeyUp(code))

    def scenario():
        yield from await_condition(lambda: window('Voice reader and reading copy') is not None,
                                   'The native voice reader did not open.')
        reader = window('Voice reader and reading copy')
        editor = control(reader, 'Reading copy (editable; Ctrl+S saves)')
        read = control(reader, 'Read selection or all')
        yield from await_condition(lambda: read.IsEnabled() and editor.GetValue() == raw,
                                   'Synthetic voice discovery/source loading did not complete.')
        check('voice reader opens without automatic speech', not requests and not played)
        check('native voice editor remains a labelled multiline text control',
              isinstance(editor, wx.TextCtrl) and bool(editor.GetWindowStyleFlag() & wx.TE_MULTILINE))
        editor.SetInsertionPoint(editor.XYToPosition(5, 1))
        post(lambda: press(control(reader, 'Read current logical line')))
        yield from await_condition(lambda: len(played) == 1 and read.IsEnabled(),
                                   'Native logical-line speech did not finish.')
        check('logical-line action uses exact native caret text', requests == ['Second line has 12 kg.'])
        post(lambda: press(control(reader, 'Read current paragraph')))
        yield from await_condition(lambda: len(played) == 2 and read.IsEnabled(),
                                   'Native paragraph speech did not finish.')
        check('paragraph action uses the blank-line boundary',
              requests[1] == '🚂 Dr. Example.\nSecond line has 12 kg.')
        post(lambda: press(control(reader, 'Read clipboard once')))
        yield from await_condition(lambda: len(played) == 3 and read.IsEnabled(),
                                   'Explicit native clipboard speech did not finish.')
        check('explicit clipboard speech does not replace or bind the reading copy',
              requests[2] == clipboard_text and editor.GetValue() == raw)

        post(lambda: press(control(reader, 'Pronunciation dictionary')))
        yield from await_condition(lambda: window('Pronunciation dictionary') is not None,
                                   'The native dictionary did not open.')
        dictionary = window('Pronunciation dictionary')
        pattern = control(dictionary, 'Pattern to pronounce')
        replacement = control(dictionary, 'Spoken replacement')
        rules = control(dictionary, 'Pronunciation rules')
        status = control(dictionary, 'Dictionary operation status')
        pattern.SetValue('Dr.')
        replacement.SetValue('Doc')
        control(dictionary, 'Exact language code; empty means any').SetValue('en')
        post(lambda: press(control(dictionary, 'Add rule')))
        yield from await_condition(lambda: rules.GetCount() == 1, 'The literal rule was not added.')
        replacement.SetValue('Doctor')
        post(lambda: press(control(dictionary, 'Save rule changes')))
        yield from await_condition(lambda: 'Doctor' in rules.GetString(0), 'The rule edit was not saved.')
        pattern.SetValue(r'(\d+) kg')
        replacement.SetValue(r'\1 kilograms')
        control(dictionary, 'Use regular expression').SetValue(True)
        post(lambda: press(control(dictionary, 'Add rule')))
        yield from await_condition(lambda: rules.GetCount() == 2, 'The regex rule was not added.')
        post(lambda: press(control(dictionary, 'Export dictionary JSON')))
        yield from await_condition(lambda: dictionary_file.is_file()
                                   and status.GetValue().startswith('Dictionary exported'),
                                   'Native dictionary export did not finish.')
        exported = json.loads(dictionary_file.read_text(encoding='utf-8'))
        check('native dictionary export uses the existing validated format',
              exported['schema'] == 'carr.pronunciations.v1' and len(exported['rules']) == 2)
        post(lambda: press(control(dictionary, 'Remove selected rule')))
        yield from await_condition(lambda: rules.GetCount() == 1, 'The selected rule was not removed.')
        post(lambda: press(control(dictionary, 'Import dictionary JSON')))
        yield from await_condition(lambda: rules.GetCount() == 2
                                   and status.GetValue().startswith('Imported 2 rules'),
                                   'Native dictionary import did not finish.')
        post(lambda: press(control(dictionary, 'Apply to reading copy')))
        edited = '🚂 Doctor Example.\nSecond line has 12 kilograms.\n\nNext paragraph.'
        yield from await_condition(lambda: editor.GetValue() == edited
                                   and control(dictionary, 'Apply to reading copy').IsEnabled()
                                   and wx.Window.FindFocus() == status,
                                   'Native dictionary application did not finish.')
        check('dictionary completion has a readable focused native status',
              'source is unchanged' in status.GetValue() and wx.Window.FindFocus() == status)
        post(lambda: press(control(dictionary, 'Close dictionary')))
        yield from await_condition(lambda: window('Pronunciation dictionary') is None,
                                   'The native dictionary did not close.')
        post(lambda: press(control(reader, 'Save reading copy as')))
        yield from await_condition(project_file.is_file, 'The source-bound reading copy was not saved.')
        saved = ReadingDocument.load(project_file)
        check('dictionary application is one undoable source-bound edit',
              saved.revision == 1 and saved.source_text == raw and saved.text == edited
              and saved.source_anchor['quotation_authority'] is False
              and saved.source_anchor['asset_sha256'] == imported['asset'])
        post(lambda: press(control(reader, 'Undo reading edit', label=True)))
        yield from await_condition(lambda: editor.GetValue() == raw, 'One Undo did not restore the reading copy.')
        check('native Undo preserves the immutable source', original.read_bytes() == original_bytes)
        editor.SetSelection(0, 0)
        editor.SetFocus()
        yield from await_condition(lambda: wx.Window.FindFocus() == editor, 'The reading editor did not gain focus.')
        post(lambda: key(wx.WXK_F6))
        yield from await_condition(lambda: len(played) == 4 and read.IsEnabled(),
                                   'The shared F6 voice-reader shortcut did not dispatch.')
        check('shared keyboard command renders the current reading copy', requests[3] == raw)
        holding.set()
        editor.SetFocus()
        post(lambda: key(wx.WXK_F6))
        yield from await_condition(entered.is_set, 'The real reader worker did not enter synthetic synthesis.')
        post(lambda: key(wx.WXK_F8))
        yield from await_condition(lambda: read.IsEnabled(), 'The shared F8 command did not cancel synthesis.')
        check('native Stop cancels the actual worker without starting late playback',
              len(requests) == 5 and len(played) == 4
              and 'cancelled' in control(reader, 'Speech and save status').GetValue().lower())
        check('voice-reader actions do not manufacture citation selections', library._selections == {})
        fixture['completed'] = True
        post(lambda: press(control(reader, 'Close', label=True)))
        yield

    sequence = scenario()
    timer = wx.Timer(parent)
    end = monotonic() + 90
    def tick(event):
        try:
            if monotonic() >= end:
                raise TimeoutError('The complete native reader usability scenario exceeded its deadline.')
            next(sequence)
        except StopIteration:
            timer.Stop()
        except BaseException as error:
            failed(error)
    parent.Bind(wx.EVT_TIMER, tick, timer)
    try:
        with patch('carr_library.runtime_settings.voice_registry', registry), \
                patch.object(speech_module, 'WavePlayer', SyntheticPlayback), \
                patch.object(wx, 'FileDialog', SelectedPath), \
                patch.object(wx, 'MessageBox', confirmation):
            timer.Start(30)
            speech_module.show_speech_reader(parent, source_guard=guard,
                recovery_slot='native-reader-usability',
                source_loader=lambda cancelled: read_source_range(desk, 1, 1,
                    guard=guard, cancelled=cancelled))
        if failures:
            raise AssertionError('Native voice reader usability failed.') from failures[0]
        check('all native reader usability phases completed', fixture['completed'])
        end_cleanup = monotonic() + 5
        while not disposed.is_set() and monotonic() < end_cleanup:
            application.Yield()
            wx.MilliSleep(10)
        check('closing the native reader releases its owned synthetic voice', disposed.is_set())
        check('all voice-reader actions preserve the retained source bytes', original.read_bytes() == original_bytes)
    finally:
        timer.Stop()
        parent.Unbind(wx.EVT_TIMER, source=timer)
        if not fixture['completed']:
            force_close()
        if wx.TheClipboard.Open():
            try:
                current = wx.TextDataObject()
                if wx.TheClipboard.GetData(current) and current.GetText() == clipboard_text:
                    if saved_clipboard['readable']:
                        wx.TheClipboard.SetData(wx.TextDataObject(saved_clipboard['text']))
                    else:
                        wx.TheClipboard.Clear()
            finally:
                wx.TheClipboard.Close()
    return {'controls': 'native wx', 'keyboard': 'native UIActionSimulator F6/F8',
            'clipboard': 'one explicit native read', 'voice_provider': 'synthetic PCM fixture',
            'playback': 'synthetic sink; no device opened', 'file_pickers': 'controlled answers; NOT_RUN',
            'unsaved_close_confirmation': 'controlled answer; NOT_RUN',
            'sapi': 'NOT_RUN in this synthetic control probe', 'acoustics': 'NOT_RUN', 'nvda': 'NOT_RUN'}


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
            voice_reader = verify_voice_reader_controls(application, parent, library, workspace, check, origins, source)
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                              'platform': platform.system(), 'origins': origins,
                              'source_override': str(source) if source is not None else None,
                              'voice_reader': voice_reader, 'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
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
