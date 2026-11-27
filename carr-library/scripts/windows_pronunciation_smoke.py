"""Installed Windows pronunciation review probe; synthetic source, no voice/NVDA claim."""
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.',
                          'nvda': 'NOT_RUN', 'voice_quality': 'NOT_RUN'}))
        return 77

    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run_native()
    finally:
        if native_handle is not None:
            native_handle.close()


def _run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech import Voice, VoiceRegistry
    from carr_library.speech.reading import import_dictionary
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.speech.source import read_source_range
    from carr_library.ui.speech_dialog import show_speech_reader
    import carr_library.speech.reading as reading_module
    import carr_library.speech.navigation as navigation_module
    import carr_library.ui.pronunciation_dialog as pronunciation_module
    import carr_library.ui.speech_dialog as reader_module
    import carr_library.speech.spelling as spelling_module
    import carr_library.speech.spelling_worker as spelling_worker_module
    import carr_library.ui.spelling_dialog as spelling_dialog_module
    import carr_library.runtime_settings as runtime_settings

    checks, errors, observed = [], [], {}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    origin = Path(carr_library.__file__).resolve()
    bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
    check('installed package origin', origin.is_relative_to(bundle) if bundle
          else 'site-packages' in origin.parts)
    checkout = Path(__file__).resolve().parents[1]
    source_identity = {}
    for module in (reading_module, navigation_module, pronunciation_module, reader_module,
                   spelling_module, spelling_worker_module, spelling_dialog_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__, not actual.is_relative_to(checkout)
              and digest == sha256(expected.read_bytes()).hexdigest())
        source_identity[module.__name__] = digest

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            fail()
            return False

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    held = [None]
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        key = held[0]
        held[0] = None
        if key is not None:
            check('native key and modifiers released', simulator.KeyUp(*key))

    def fail():
        if not errors:
            errors.append(traceback.format_exc())
        try:
            release()
        except Exception:
            errors.append(traceback.format_exc())
        for window in wx.GetTopLevelWindows():
            try:
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            except RuntimeError:
                pass
        gui.ExitMainLoop()

    def later(callback, delay=30):
        def invoke():
            if errors:
                return
            try:
                callback()
            except Exception:
                errors.append(traceback.format_exc())
                fail()
        wx.CallLater(delay, invoke)

    def until(predicate, complete, *, timeout=30):
        deadline = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= deadline:
                raise TimeoutError('Native pronunciation operation did not finish.')
            else:
                later(poll)
        later(poll)

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

    def key(control, code, complete, modifiers=wx.MOD_NONE):
        name = control.GetName()
        def down():
            check('target owns foreground and native focus: ' + name, foreground(control))
            held[0] = (code, modifiers)
            try:
                check('native key down: ' + name, simulator.KeyDown(code, modifiers))
            except BaseException:
                release()
                raise
            def up():
                release()
                later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def press(control, complete):
        key(control, wx.WXK_SPACE, complete)

    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)

    def named(window, name):
        return next(child for child in children(window) if child.GetName() == name)

    def top_with_title(prefix):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.GetTitle().startswith(prefix)), None)

    class EnumerationOnly:
        name = 'Synthetic pronunciation probe enumeration'
        def voices(self):
            return (Voice('fixture:pronunciation', 'Synthetic enumeration only', 'fixture', 'en'),)
        def synthesize(self, *args, **kwargs):
            raise AssertionError('This probe must not synthesize or qualify a voice.')
        def close(self):
            observed['provider_closed'] = True

    with TemporaryDirectory(prefix='resonance-native-pronunciation-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic pronunciation acceptance')
        original = '😀 Retained source.\r\nDr. First speaks. Dr. Second listens.'
        imported = library.add_text(workspace, 'Synthetic source book', original)
        desk = ReadingDesk(library, workspace, imported['edition'])
        source = read_source_range(desk, 1, 1)
        original_asset = library.assets.resolve(imported['asset'])
        asset_bytes = original_asset.read_bytes()
        output = root / 'reviewed-pronunciations.json'
        unavailable_engine = root / 'deliberately-unavailable-hunspell.dll'
        unavailable_engine.write_bytes(b'Original non-executable fixture; no native Hunspell quality claim.')
        spelling_affix, spelling_dictionary = root / 'original.aff', root / 'original.dic'
        spelling_affix.write_text('SET UTF-8\n', encoding='utf-8')
        spelling_dictionary.write_text('1\nhello\n', encoding='utf-8')
        frame = wx.Frame(None, title='Resonance pronunciation native acceptance', size=(600, 350))
        frame.library = library
        frame.report = lambda message: observed.setdefault('reader_notices', []).append(str(message))
        original_registry = runtime_settings.voice_registry
        original_picker, original_message = wx.FileDialog, wx.MessageBox
        picks = []
        class SelectedFixturePath:
            def __init__(self, *args, **kwargs): pass
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(picks.pop(0))
            def Destroy(self): pass
        runtime_settings.voice_registry = lambda *args, **kwargs: VoiceRegistry((EnumerationOnly(),))
        wx.FileDialog = SelectedFixturePath
        wx.MessageBox = lambda *args, **kwargs: wx.NO

        def loaded():
            reader = top_with_title('Voice reader and reading copy')
            if reader is None:
                return False
            button = named(reader, 'Pronunciation rules')
            return button.IsEnabled()

        def open_review():
            reader = top_with_title('Voice reader and reading copy')
            editor = named(reader, 'Reading copy (editable; Ctrl+S saves)')
            initial = editor.GetValue()
            # Subtract a known ASCII suffix from the native end. This never
            # probes an index in the middle of an emoji's UTF-16 surrogate pair.
            start = editor.GetLastPosition() - len('Dr. Second listens.')
            check('native Unicode/line-ending selection has two matches', initial.count('Dr.') == 2
                  and editor.GetRange(start, start + 3) == 'Dr.')
            editor.SetSelection(start, start + 3)
            observed.update(reader=reader, editor=editor, initial=initial)
            key(editor, ord('N'), lambda: until(
                lambda: top_with_title('Pronunciation rules') is not None, dictionary_ready), wx.MOD_ALT)

        def dictionary_ready():
            dialog = top_with_title('Pronunciation rules')
            observed['dialog'] = dialog
            until(lambda: dialog.loaded and not dialog.busy, add_rule)

        def add_rule():
            dialog = observed['dialog']
            check('native selected-text scope', dialog.scope.GetSelection() == 1)
            check('named preview and review controls', all(control.GetName() for control in
                  (dialog.list, dialog.pattern, dialog.replacement, dialog.before, dialog.after, dialog.status)))
            dialog.pattern.ChangeValue('Dr.')
            dialog.replacement.ChangeValue('Doctor')
            press(dialog.buttons['add'], added)

        def added():
            dialog = observed['dialog']
            check('native rule edit did not apply automatically', len(dialog.rules) == 1
                  and observed['editor'].GetValue() == observed['initial'])
            press(dialog.buttons['preview'], lambda: until(lambda: not dialog.busy, reviewed))

        def reviewed():
            dialog = observed['dialog']
            check('native exact before/after preview', dialog.before.GetValue() == 'Dr.'
                  and dialog.after.GetValue() == 'Doctor' and dialog.buttons['apply'].IsEnabled())
            check('preview did not change the reading copy', observed['editor'].GetValue() == observed['initial'])
            press(dialog.buttons['save'], lambda: until(lambda: not dialog.busy, saved))

        def saved():
            dialog = observed['dialog']
            rules, token = dialog.store.load()
            check('explicit library dictionary save', rules == dialog.rules and token == dialog.saved_token)
            picks.append(output)
            press(dialog.buttons['export'], lambda: until(lambda: not dialog.busy, exported))

        def exported():
            dialog = observed['dialog']
            check('new dictionary export uses the canonical format', import_dictionary(output) == dialog.rules)
            observed['exported'] = output.read_bytes()
            picks.append(output)
            press(dialog.buttons['export'], lambda: until(lambda: not dialog.busy, no_clobber))

        def no_clobber():
            dialog = observed['dialog']
            check('existing export is preserved', output.read_bytes() == observed['exported']
                  and 'exists' in dialog.status.GetValue())
            press(dialog.buttons['remove'], removed)

        def removed():
            dialog = observed['dialog']
            check('remove invalidates preview', not dialog.rules and dialog.preview is None)
            picks.append(output)
            press(dialog.buttons['import'], lambda: until(lambda: not dialog.busy, imported_rules))

        def imported_rules():
            dialog = observed['dialog']
            check('import is review-only', len(dialog.rules) == 1 and dialog.preview is None
                  and observed['editor'].GetValue() == observed['initial'])
            press(dialog.buttons['preview'], lambda: until(lambda: not dialog.busy,
                  lambda: press(dialog.buttons['apply'], applied)))

        def applied():
            dialog = observed['dialog']
            initial = observed['initial']
            start = initial.rindex('Dr.')
            expected = initial[:start] + 'Doctor' + initial[start + 3:]
            check('native apply changes only the selected reading range',
                  observed['editor'].GetValue() == expected)
            check('retained source bytes remain unchanged', original_asset.read_bytes() == asset_bytes)
            press(dialog.buttons['undo'], undone)

        def undone():
            dialog = observed['dialog']
            check('native undo restores the reading copy', observed['editor'].GetValue() == observed['initial'])
            check('undo leaves dictionary rules saved', dialog.store.load()[0] == dialog.rules)
            press(dialog.buttons['close'], close_reader)

        def close_reader():
            until(lambda: top_with_title('Pronunciation rules') is None, open_find)

        def open_find():
            reader, editor = observed['reader'], observed['editor']
            key(editor, ord('F'), lambda: until(
                lambda: top_with_title('Find and replace') is not None, find_ready), wx.MOD_CONTROL)

        def find_ready():
            dialog = top_with_title('Find and replace')
            observed['find_dialog'] = dialog
            check('native find controls have accessible names', all(control.GetName() for control in
                  (dialog.pattern, dialog.replacement, dialog.scope, dialog.list, dialog.before, dialog.after)))
            dialog.pattern.ChangeValue('Dr.')
            dialog.replacement.ChangeValue('Doctor')
            dialog.scope.SetSelection(0)
            press(dialog.buttons['preview'], lambda: until(lambda: not dialog.busy, find_previewed))

        def find_previewed():
            dialog, editor = observed['find_dialog'], observed['editor']
            initial = observed['initial']
            check('actual bounded search previews two exact native matches',
                  dialog.preview is not None and len(dialog.preview.matches) == 2
                  and dialog.before.GetValue() == initial
                  and dialog.after.GetValue() == initial.replace('Dr.', 'Doctor')
                  and editor.GetValue() == initial and editor.GetStringSelection() == 'Dr.')
            check('find keeps focus in the review window', wx.GetTopLevelParent(wx.Window.FindFocus()) is dialog)
            press(dialog.buttons['next'], find_second)

        def find_second():
            dialog, editor = observed['find_dialog'], observed['editor']
            start, end = editor.GetSelection()
            check('native next match selects the second Unicode-offset occurrence',
                  dialog.index == 1 and editor.GetRange(0, start).endswith('speaks. ')
                  and editor.GetRange(start, end) == 'Dr.')
            press(dialog.buttons['one'], find_one_applied)

        def find_one_applied():
            dialog, editor = observed['find_dialog'], observed['editor']
            initial = observed['initial']; start = initial.rindex('Dr.')
            check('native single replacement changes only the reviewed occurrence',
                  editor.GetValue() == initial[:start] + 'Doctor' + initial[start + 3:]
                  and dialog.preview is None and original_asset.read_bytes() == asset_bytes)
            press(dialog.buttons['undo'], find_one_undone)

        def find_one_undone():
            dialog, editor = observed['find_dialog'], observed['editor']
            check('native replacement undo restores exact reading text', editor.GetValue() == observed['initial'])
            press(dialog.buttons['preview'], lambda: until(lambda: not dialog.busy,
                  lambda: press(dialog.buttons['all'], find_all_applied)))

        def find_all_applied():
            dialog, editor = observed['find_dialog'], observed['editor']
            check('native Replace all applies the complete reviewed scope once',
                  editor.GetValue() == observed['initial'].replace('Dr.', 'Doctor')
                  and original_asset.read_bytes() == asset_bytes)
            press(dialog.buttons['undo'], find_all_undone)

        def find_all_undone():
            dialog, editor = observed['find_dialog'], observed['editor']
            recovered = ReaderDraftStore(library.root, slot='pronunciation-native').load()
            check('native find undo is durably recoverable with exact source lineage',
                  editor.GetValue() == observed['initial'] and recovered is not None
                  and recovered.document.text == observed['initial']
                  and recovered.document.source_anchor == source.reading_document().source_anchor
                  and recovered.document.source_text == source.text)
            press(dialog.buttons['close'], lambda: until(
                lambda: top_with_title('Find and replace') is None, open_spelling))

        def open_spelling():
            press(named(observed['reader'], 'Spelling review with Hunspell'), lambda: until(
                lambda: top_with_title('Spelling review — Hunspell') is not None, spelling_ready))

        def spelling_ready():
            dialog = top_with_title('Spelling review — Hunspell')
            observed['spelling_dialog'] = dialog
            check('native optional spelling starts without executable approval', dialog.profile is None
                  and not dialog.buttons['check'].IsEnabled() and not dialog.buttons['apply'].IsEnabled())
            check('native spelling controls have accessible names', all(control.GetName() for control in
                  (dialog.library, dialog.affix, dialog.dictionary, dialog.language, dialog.provenance,
                   dialog.identity, dialog.scope, dialog.issues, dialog.suggestions, dialog.replacement,
                   dialog.preview_text, dialog.status)))
            dialog.library.ChangeValue(str(unavailable_engine))
            dialog.affix.ChangeValue(str(spelling_affix))
            dialog.dictionary.ChangeValue(str(spelling_dictionary))
            dialog.language.ChangeValue('synthetic en')
            dialog.provenance.ChangeValue('Original non-executable fixture and dictionary; no engine qualification.')
            press(dialog.buttons['review'], lambda: until(lambda: not dialog.busy, spelling_reviewed))

        def spelling_reviewed():
            dialog = observed['spelling_dialog']
            check('native spelling reviews exact files without loading a library', dialog.profile is not None
                  and dialog.buttons['check'].IsEnabled() and 'SHA256' in dialog.identity.GetValue()
                  and not dialog.buttons['apply'].IsEnabled())
            press(dialog.buttons['check'], lambda: until(lambda: not dialog.busy, spelling_unavailable))

        def spelling_unavailable():
            dialog = observed['spelling_dialog']
            check('native spelling reports unavailable C API without offering corrections',
                  'C API' in dialog.status.GetValue() and dialog.review is None
                  and not dialog.buttons['apply'].IsEnabled())
            check('native unavailable spelling preserves reading copy and retained source',
                  observed['editor'].GetValue() == observed['initial'] and original_asset.read_bytes() == asset_bytes)
            observed['spelling_unavailable_complete'] = True
            press(dialog.buttons['close'], lambda: until(
                lambda: top_with_title('Spelling review — Hunspell') is None, finish_reader))

        def finish_reader():
            reader = observed['reader']
            editor = observed['editor']
            observed['navigation_initial'] = editor.GetValue()
            named(reader, 'Navigation unit').SetSelection(0)
            editor.SetInsertionPoint(editor.GetLastPosition() - len('listens.'))
            press(named(reader, 'Select current text unit'), selected_word)

        def selected_word():
            reader, editor = observed['reader'], observed['editor']
            check('native current-word selection after emoji and newline',
                  editor.GetStringSelection() == 'listens' and wx.Window.FindFocus() is editor)
            press(named(reader, 'Previous text unit'), previous_word)

        def previous_word():
            reader, editor = observed['reader'], observed['editor']
            check('native previous word without autoplay', editor.GetStringSelection() == 'Second')
            key(editor, wx.WXK_RIGHT, next_word, modifiers=wx.MOD_CONTROL | wx.MOD_ALT)

        def next_word():
            reader, editor = observed['reader'], observed['editor']
            check('native next-word accelerator preserves text', editor.GetStringSelection() == 'listens'
                  and editor.GetValue() == observed['navigation_initial'])
            named(reader, 'Navigation unit').SetSelection(1)
            press(named(reader, 'Select current text unit'), selected_sentence)

        def selected_sentence():
            reader, editor = observed['reader'], observed['editor']
            check('native deterministic sentence boundary', editor.GetStringSelection() == 'Second listens.')
            named(reader, 'Navigation unit').SetSelection(2)
            press(named(reader, 'Select current text unit'), selected_paragraph)

        def selected_paragraph():
            reader, editor = observed['reader'], observed['editor']
            check('native paragraph spans one ordinary line break',
                  editor.GetStringSelection() == observed['navigation_initial'])
            named(reader, 'Navigation unit').SetSelection(0)
            editor.SetInsertionPoint(editor.GetLastPosition() - len('Second listens.'))
            press(named(reader, 'Select current text unit'), navigation_complete)

        def navigation_complete():
            editor = observed['editor']
            check('navigation changes no reading or source bytes',
                  editor.GetStringSelection() == 'Second'
                  and editor.GetValue() == observed['navigation_initial']
                  and original_asset.read_bytes() == asset_bytes)
            observed['expected_cursor'] = observed['navigation_initial'].rindex('Second') + len('Second')
            def cursor_saved():
                saved = ReaderDraftStore(library.root, slot='pronunciation-native').load()
                return saved is not None and saved.cursor == observed['expected_cursor']
            until(cursor_saved, finish_after_cursor_saved, timeout=10)

        def finish_after_cursor_saved():
            reader = observed['reader']
            close = next(control for control in children(reader)
                         if isinstance(control, wx.Button) and control.GetLabel() == 'C&lose')
            press(close, lambda: None)

        def launch():
            frame.Show()
            until(loaded, open_review)
            show_speech_reader(frame, title='Synthetic source reading copy', recovery_slot='pronunciation-native',
                source_loader=lambda cancelled: read_source_range(desk, 1, 1, cancelled=cancelled))
            release()
            if not errors:
                recovered = ReaderDraftStore(library.root, slot='pronunciation-native').load()
                check('reading recovery retains exact source lineage', recovered is not None
                      and recovered.document.source_anchor == source.reading_document().source_anchor
                      and recovered.document.source_text == source.text)
                check('native cursor saved as a Python character offset', recovered.cursor == observed['expected_cursor'])
                check('source locator grants no quotation authority',
                      recovered.document.source_anchor['quotation_authority'] is False)
                check('final source asset digest unchanged', sha256(original_asset.read_bytes()).hexdigest()
                      == sha256(asset_bytes).hexdigest())
                check('owned enumeration provider closed', observed.get('provider_closed') is True)
                observed['workflow_complete'] = True
            frame.Destroy()
            gui.ExitMainLoop()

        try:
            later(launch)
            gui.MainLoop()
        finally:
            release()
            runtime_settings.voice_registry = original_registry
            wx.FileDialog, wx.MessageBox = original_picker, original_message
            for window in tuple(wx.GetTopLevelWindows()):
                try:
                    window.Destroy()
                except RuntimeError:
                    pass
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('normal native MainLoop completed the whole workflow', observed.get('workflow_complete') is True)
        check('normal native spelling unavailable workflow completed', observed.get('spelling_unavailable_complete') is True)
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'package_origin': str(origin),
                          'source_identity': source_identity,
                          'source_sha256': sha256(asset_bytes).hexdigest(),
                          'dictionary_sha256': sha256(observed['exported']).hexdigest(),
                          'native_spelling_controls_and_unavailable_runtime': 'PASS',
                          'actual_hunspell_engine': 'NOT_RUN',
                          'native_keyboard_automation': 'PASS', 'native_file_picker': 'NOT_RUN',
                          'confirmation_dialog': 'NOT_RUN', 'nvda': 'NOT_RUN',
                          'voice_quality': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
