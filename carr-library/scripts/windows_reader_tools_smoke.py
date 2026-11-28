"""Installed native Find/Replace, explicit clipboard and keymap interchange.

Real wx controls, the current live reader, regex child and saved keymap are used.
Only voice enumeration and file-picker choices are synthetic. Clipboard text is
explicitly installed for this probe and its preceding text/empty state restored.
This does not claim physical keyboard, NVDA, audio or native file-picker QA.
"""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def _acceptance_bindings():
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_reader_tools_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    try:
        return _run()
    finally:
        if handle is not None:
            handle.close()


def _run():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech import ReadingDocument, Voice, VoiceRegistry
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    from carr_library.speech import reading, regex_worker, session
    from carr_library.ui import find_replace_dialog, keyboard_settings, live_reading
    from carr_library.ui import pronunciation_dialog, speech_dialog
    from carr_library import runtime_settings

    source_identity = _acceptance_bindings().verify_installed_sources(
        find_replace_dialog, keyboard_settings, live_reading, pronunciation_dialog,
        speech_dialog, reading, regex_worker, session, runtime_settings)
    checks, errors, observed, picks = [], [], {}, []
    deadline = monotonic() + 120
    held = [None]
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.CountClipboardFormats.argtypes = []
    user32.CountClipboardFormats.restype = ctypes.c_int

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def release_key():
        key = held[0]
        held[0] = None
        if key is not None and not simulator.KeyUp(*key):
            raise AssertionError('Native key release failed.')

    def fail():
        errors.append(traceback.format_exc())
        try:
            release_key()
        except Exception:
            errors.append(traceback.format_exc())
        for window in wx.GetTopLevelWindows():
            if window and isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail()
            return False

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    check('real native Windows controls', wx.Platform == '__WXMSW__')

    def later(callback, delay=25):
        def invoke():
            if errors:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native reader tools acceptance exceeded its bound.')
                callback()
            except Exception:
                fail()
        wx.CallLater(delay, invoke)

    def until(predicate, complete, *, timeout=15):
        end = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= end:
                raise TimeoutError('The current native reader-tools stage did not finish.')
            else:
                later(poll)
        later(poll)

    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)

    def named(window, name):
        return next(child for child in children(window) if child.GetName() == name)

    def top(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if window and isinstance(window, wx.Dialog) and window.GetTitle() == title), None)

    def foreground(control):
        owner = wx.GetTopLevelParent(control)
        handle = user32.GetForegroundWindow()
        process = wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process))
        ready = (process.value == os.getpid() and int(handle or 0) == int(owner.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            owner.Raise()
            user32.SetForegroundWindow(owner.GetHandle())
            control.SetFocus()
        return ready

    def key(control, code, complete, modifiers=wx.MOD_NONE):
        def down():
            check('target owns foreground and native focus: ' + control.GetName(), foreground(control))
            held[0] = (code, modifiers)
            check('native key down', simulator.KeyDown(code, modifiers))
            def up():
                release_key()
                later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def press(button, complete):
        key(button, wx.WXK_SPACE, complete)

    class EnumerationOnly:
        name = 'Synthetic reader-tools enumeration'
        def voices(self):
            return (Voice('fixture:reader-tools', 'Synthetic enumeration only', 'fixture', 'en'),)
        def synthesize(self, *args, **kwargs):
            observed['synthesis'] = True
            raise AssertionError('Opening or editing a clipboard copy must not start speech.')
        def close(self):
            observed['provider_closed'] = True

    class SelectedFixturePath:
        def __init__(self, *args, **kwargs):
            self.alive = True
        def __bool__(self):
            return self.alive
        def ShowModal(self):
            return wx.ID_OK
        def GetPath(self):
            if not self.alive:
                raise AssertionError('Read a deleted file picker.')
            return str(picks.pop(0))
        def Destroy(self):
            if not self.alive:
                raise AssertionError('Destroyed a file picker twice.')
            self.alive = False

    clipboard_saved, clipboard_changed = None, False
    original_registry = runtime_settings.voice_registry
    original_reader, original_picker = speech_dialog.show_speech_reader, wx.FileDialog
    with TemporaryDirectory(prefix='resonance-native-reader-tools-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        manual = ReaderDraftStore(library.root)
        manual.save(ReaderResume(ReadingDocument('An unrelated manual draft must remain unchanged.')))
        manual_bytes = manual.path.read_bytes()
        source = '😀 Retained clipboard snapshot.\r\nDr. First speaks. Dr. Second listens.'
        frame = wx.Frame(None, title='Resonance reader tools acceptance', size=(600, 350))
        frame.library, frame._closing = library, False
        frame.report = lambda message: observed.setdefault('reports', []).append(str(message))
        frame.menu_bar = wx.MenuBar()
        menu = wx.Menu()
        action = menu.Append(wx.ID_ANY, 'Synthetic native command\tF8')
        other = menu.Append(wx.ID_ANY, 'Unrelated native command\tF10')
        frame.menu_bar.Append(menu, '&Help')
        frame.SetMenuBar(frame.menu_bar)
        native_commands = []
        frame.Bind(wx.EVT_MENU, lambda event: native_commands.append(event.GetId()), action)
        speech_dialog.install_speech_actions(frame)
        keyboard = keyboard_settings.KeyboardSettings(frame)
        frame.keyboard_settings = keyboard
        target = next(row for row in keyboard.commands if row.menu_id == action.GetId())
        unrelated = next(row for row in keyboard.commands if row.menu_id == other.GetId())
        keyboard.apply({unrelated.action: 'Ctrl+Shift+0'})
        prior_keys = dict(keyboard.overrides)
        incoming, outgoing = root / 'incoming-keymap.json', root / 'exported-keymap.json'
        incoming.write_text(json.dumps({'schema': 1, 'overrides': {target.action: 'Ctrl+Shift+9'},
                                      'originals': {target.action: target.default}}), encoding='utf-8')
        input_bytes = incoming.read_bytes()
        anchor = wx.TextCtrl(frame, value='Native shortcut focus target')
        anchor.SetName('Native shortcut focus target')
        gui.SetTopWindow(frame)

        def observed_reader(*args, **kwargs):
            check('the dedicated menu uses its own clipboard recovery slot', kwargs.get('recovery_slot') == 'clipboard')
            check('the dedicated action took exactly one text snapshot', kwargs.get('text') == source)
            return original_reader(*args, **kwargs, on_ready=lambda dialog, view:
                                   observed.update(reader=dialog, reader_view=view))

        def loaded():
            view = observed.get('reader_view')
            return bool(view is not None and not view['live'].core.busy
                        and named(observed['reader'], 'Find and replace').IsEnabled())

        def open_find():
            view = observed['reader_view']
            editor = view['editor']
            check('opening a clipboard copy never starts speech', not observed.get('synthesis')
                  and view['document']().source_text == source)
            observed['initial'] = editor.GetValue()
            editor.SetInsertionPoint(0)
            key(editor, ord('F'), lambda: until(lambda: top('Find and replace in reading copy') is not None,
                find_ready), wx.MOD_CONTROL)

        def find_ready():
            dialog = top('Find and replace in reading copy')
            observed['find'] = dialog
            check('native find fields and review status are named', all(control.GetName() for control in
                  (dialog.pattern, dialog.replacement, dialog.before, dialog.after, dialog.status)))
            dialog.pattern.ChangeValue(r'Dr\.')
            dialog.replacement.ChangeValue('Лікар')
            dialog.regex.SetValue(True)
            press(dialog.buttons['next'], lambda: until(lambda: not dialog.busy, found))

        def found():
            dialog, editor = observed['find'], observed['reader_view']['editor']
            start, end = editor.GetSelection()
            check('find selects the exact native range after CRLF and astral text', editor.GetRange(start, end) == 'Dr.')
            check('find changes selection without editing the reading copy', editor.GetValue() == observed['initial'])
            press(dialog.buttons['one'], lambda: until(lambda: not dialog.busy, previewed))

        def previewed():
            dialog = observed['find']
            check('isolated regex preview includes Unicode replacement',
                  dialog.after.GetValue().count('Лікар') == 1 and dialog.after.GetValue().count('Dr.') == 1)
            check('preview leaves the editable copy unchanged',
                  observed['reader_view']['editor'].GetValue() == observed['initial'])
            press(dialog.buttons['apply'], applied)

        def applied():
            view = observed['reader_view']
            check('explicit apply creates one reversible reading edit',
                  view['document']().text == source.replace('Dr.', 'Лікар', 1))
            check('source snapshot remains immutable', view['document']().source_text == source)
            press(observed['find'].buttons['undo'], undone)

        def undone():
            view = observed['reader_view']
            check('native undo restores exact reading text', view['document']().text == source
                  and view['editor'].GetValue() == observed['initial'])
            press(observed['find'].buttons['close'], lambda: until(
                lambda: top('Find and replace in reading copy') is None, close_reader))

        def close_reader():
            close = next(control for control in children(observed['reader'])
                         if isinstance(control, wx.Button) and control.GetLabel() == 'C&lose')
            press(close, lambda: None)

        def keymap_ready():
            dialog = top('Keyboard shortcuts')
            observed['keymap'] = dialog
            check('native keymap commands are named', all(named(dialog, name) for name in
                ('Preview keymap import...', 'Apply reviewed import', 'Export keymap...', 'Undo last change')))
            picks.append(incoming)
            press(named(dialog, 'Preview keymap import...'), keymap_reviewed)

        def keymap_reviewed():
            dialog = observed['keymap']
            check('keymap import is explicit review before save', keyboard.overrides == prior_keys
                  and named(dialog, 'Apply reviewed import').IsEnabled())
            press(named(dialog, 'Apply reviewed import'), keymap_applied)

        def keymap_applied():
            check('native import preserves unrelated saved shortcuts',
                  keyboard.overrides == {**prior_keys, target.action: 'Ctrl+Shift+9'})
            press(named(observed['keymap'], 'Undo last change'), keymap_undone)

        def keymap_undone():
            check('native undo restores the complete preceding keymap', keyboard.overrides == prior_keys)
            picks.append(incoming)
            press(named(observed['keymap'], 'Preview keymap import...'), lambda:
                  press(named(observed['keymap'], 'Apply reviewed import'), keymap_export))

        def keymap_export():
            picks.append(outgoing)
            press(named(observed['keymap'], 'Export keymap...'), keymap_exported)

        def keymap_exported():
            observed['exported'] = outgoing.read_bytes()
            check('native keymap export retains canonical schema and actual saved choices',
                  json.loads(observed['exported'])['overrides'] == keyboard.overrides)
            picks.append(outgoing)
            press(named(observed['keymap'], 'Export keymap...'), keymap_no_clobber)

        def keymap_no_clobber():
            check('second native keymap export cannot replace existing bytes',
                  outgoing.read_bytes() == observed['exported'] and 'not exported' in
                  named(observed['keymap'], 'Shortcut settings status').GetValue())
            press(named(observed['keymap'], 'Close'), lambda: None)

        def shortcut_delivered():
            check('imported shortcut invokes the actual native menu action', native_commands == [action.GetId()])
            check('clipboard read/edit leaves the clipboard snapshot untouched',
                  speech_dialog._read_clipboard_text(wx) == source)
            check('unrelated manual recovery and imported keymap bytes are preserved',
                  manual.path.read_bytes() == manual_bytes and incoming.read_bytes() == input_bytes)
            observed['complete'] = True
            gui.ExitMainLoop()

        def launch():
            frame.Show()
            until(loaded, open_find)
            clipboard = next(row for row in keyboard.commands if row.title.endswith('Read clipboard...'))
            event = wx.CommandEvent(wx.EVT_MENU.typeId, clipboard.menu_id)
            frame.GetEventHandler().ProcessEvent(event)
            if errors:
                return
            recovered = ReaderDraftStore(library.root, slot='clipboard').load()
            check('clipboard reader saved only its own exact recovery snapshot', recovered is not None
                  and recovered.document.text == source and recovered.document.source_text == source)
            check('owned enumeration closed without synthesis', observed.get('provider_closed') is True
                  and not observed.get('synthesis'))
            until(lambda: top('Keyboard shortcuts') is not None, keymap_ready)
            keyboard.show()
            if not errors:
                key(anchor, ord('9'), lambda: until(lambda: bool(native_commands), shortcut_delivered),
                    wx.MOD_CONTROL | wx.MOD_SHIFT)

        try:
            check('native clipboard opens for bounded fixture capture', wx.TheClipboard.Open())
            try:
                if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
                    data = wx.TextDataObject()
                    check('preceding Unicode clipboard snapshot can be restored', wx.TheClipboard.GetData(data))
                    clipboard_saved = data.GetText()
                else:
                    check('a non-text clipboard is never overwritten by acceptance', user32.CountClipboardFormats() == 0)
                check('explicit synthetic clipboard installed', wx.TheClipboard.SetData(wx.TextDataObject(source)))
                clipboard_changed = True
            finally:
                wx.TheClipboard.Close()
            runtime_settings.voice_registry = lambda *args, **kwargs: VoiceRegistry((EnumerationOnly(),))
            speech_dialog.show_speech_reader = observed_reader
            wx.FileDialog = SelectedFixturePath
            later(launch)
            gui.MainLoop()
        finally:
            release_key()
            runtime_settings.voice_registry = original_registry
            speech_dialog.show_speech_reader, wx.FileDialog = original_reader, original_picker
            for window in tuple(wx.GetTopLevelWindows()):
                if window:
                    window.Destroy()
            if clipboard_changed:
                if not wx.TheClipboard.Open():
                    raise RuntimeError('Could not restore the preceding clipboard after native acceptance.')
                try:
                    if clipboard_saved is None:
                        wx.TheClipboard.Clear()
                    elif not wx.TheClipboard.SetData(wx.TextDataObject(clipboard_saved)):
                        raise RuntimeError('Could not restore the preceding clipboard text.')
                finally:
                    wx.TheClipboard.Close()
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('native event loop completed the whole reader-tools workflow', observed.get('complete') is True)
        print(json.dumps({'status': 'PASS', 'checks': checks, 'source_identity': source_identity,
            'native_keyboard_automation': 'PASS', 'native_file_picker': 'NOT_RUN',
            'clipboard_snapshot': 'PASS', 'keymap_sha256': sha256(observed['exported']).hexdigest(),
            'audio_quality': 'NOT_ASSESSED', 'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
