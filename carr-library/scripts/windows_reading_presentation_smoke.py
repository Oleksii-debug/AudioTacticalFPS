"""Installed native reader appearance, exact Unicode selection and recovery.

Font/colour picker selections are explicit fixtures over native wx FontData and
ColourData. The actual reader, appearance dialog, keyboard controls, TextCtrl
attributes, undo history and restart are exercised. No picker, voice or NVDA
human qualification is claimed.
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


def _release_key(simulator, held):
    if held[0] is not None:
        if not simulator.KeyUp(*held[0]):
            raise AssertionError('Native appearance key/modifier release failed.')
        held[0] = None


def _cleanup(actions, errors):
    for action in actions:
        try:
            action()
        except Exception:
            errors.append(traceback.format_exc())


def _unwind(wx, frame, owner_loop, release, schedule, errors):
    """End one owned modal at a time, waiting for the child to be destroyed."""
    if frame is not None:
        frame._closing = True
    try:
        release()
    except Exception:
        errors.append(traceback.format_exc())
    attempts = [0]
    def step():
        attempts[0] += 1
        try:
            candidates = []
            for window in tuple(wx.GetTopLevelWindows()):
                if not window or not isinstance(window, wx.Dialog):
                    continue
                current, depth = window, 0
                while current is not None and current is not frame and depth < 8:
                    current, depth = current.GetParent(), depth + 1
                if current is frame:
                    candidates.append((depth, window))
            if not candidates:
                owner_loop.ScheduleExit(1)
                return
            if attempts[0] > 150:
                raise TimeoutError('Owned appearance/reader modals did not finish their cleanup.')
            _, leaf = max(candidates, key=lambda item: item[0])
            if leaf.IsModal():
                leaf.EndModal(wx.ID_CANCEL)
            # A non-modal but still live child has not completed its finally
            # yet. Never EndModal its inactive parent on the same callback.
            schedule(step)
        except Exception:
            errors.append(traceback.format_exc())
            owner_loop.ScheduleExit(1)
    step()


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.', 'manual_nvda': 'NOT_RUN'}))
        return 77
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(100, repeat=True, file=sys.stderr)
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        return _native()
    finally:
        try:
            if native_handle is not None: native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech import VoiceRegistry
    from carr_library.speech.presentation import PresentationStore, ReadingPresentation
    from carr_library.speech.session import ReaderDraftStore
    import carr_library.runtime_settings as settings
    import carr_library.speech.presentation as profile_module
    import carr_library.speech.reading as reading_module
    import carr_library.speech.session as session_module
    import carr_library.ui.reading_presentation as appearance_module
    import carr_library.ui.speech_dialog as reader_module
    import carr_library.ui.ebook_model as position_module
    import carr_library.durable_state as durable_module
    import carr_library.ui.builtin_keyboard_commands as commands_module
    import carr_library.ui.reading_shortcuts as shortcuts_module

    checks, errors, evidence, identities = [], [], {}, {}
    held, loop, reentry, aborting = [None], [None], [False], [False]
    stage = ['starting', monotonic()]
    deadline = monotonic() + 130
    timer = frame = None
    def check(label, condition):
        if not condition: raise AssertionError(label)
        if label not in checks:
            checks.append(label); print('PASS:', label, flush=True)
    def advance(label):
        stage[:] = [label, monotonic()]; print('STAGE:', label, flush=True)
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc()); return False
    app = ProbeApp(False); app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], wintypes.HWND
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    def release():
        _release_key(simulator, held)
    def abort(detail):
        errors.append(detail); print('FAIL:', detail, flush=True)
        if aborting[0]: return
        aborting[0] = True
        if timer is not None: _cleanup((timer.Stop,), errors)
        _unwind(wx, frame, loop[0] or app.GetMainLoop(), release,
                lambda callback: wx.CallLater(20, callback), errors)
    def released():
        try: release()
        except Exception: abort(traceback.format_exc())
    def key(control, code, next_stage, modifiers=wx.MOD_NONE):
        if held[0] is not None: return
        top, owner = wx.GetTopLevelParent(control), wintypes.DWORD()
        foreground = user32.GetForegroundWindow()
        if foreground: user32.GetWindowThreadProcessId(foreground, ctypes.byref(owner))
        if (owner.value != os.getpid() or int(foreground or 0) != int(top.GetHandle())
                or wx.Window.FindFocus() is not control):
            top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
            return
        advance(next_stage)
        held[0] = code, modifiers
        if not simulator.KeyDown(code, modifiers):
            raise AssertionError('Native appearance key press failed.')
        wx.CallLater(40, released)
    def named(name, parent):
        control = wx.FindWindowByName(name, parent)
        if control is None: raise AssertionError('Missing named control: ' + name)
        return control
    def window(title):
        return next((item for item in wx.GetTopLevelWindows()
                     if item and isinstance(item, wx.Dialog) and item.GetTitle() == title and item.IsShown()), None)
    def rgb(value): return value.Red(), value.Green(), value.Blue()

    checkout = Path(__file__).resolve().parents[1]
    installed = (Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR')
                 else Path(sysconfig.get_path('purelib')).resolve())
    for module in (profile_module, reading_module, session_module, appearance_module, reader_module, position_module,
                   durable_module, commands_module, shortcuts_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__, actual.is_relative_to(installed)
              and not actual.is_relative_to(checkout) and digest == sha256(expected.read_bytes()).hexdigest())
        identities[module.__name__] = digest
    check('actual native Windows controls', wx.Platform == '__WXMSW__')

    with TemporaryDirectory(prefix='resonance-reading-appearance-') as temporary:
        library = LibraryApp(Path(temporary) / 'Бібліотека 😀')
        library.create_workspace('Original appearance acceptance source')
        store, drafts = PresentationStore(library.root), ReaderDraftStore(library.root)
        source = '😀 Original first.\r\nДругий рядок. Second sentence.'
        original_font, original_color, original_registry = wx.FontDialog, wx.ColourDialog, settings.voice_registry
        picks, cycles = [], [0]
        class FontSelection:
            def __init__(self, parent, data):
                picks.append('font'); self.data = data
                selected = wx.Font(wx.TextCtrl.GetClassDefaultAttributes().font)
                selected.SetFractionalPointSize(14); selected.SetNumericWeight(700)
                data.SetChosenFont(selected)
                evidence['font_face'] = selected.GetFaceName()
            def ShowModal(self): return wx.ID_OK
            def GetFontData(self): return self.data
            def Destroy(self): pass
        class ColorSelection:
            def __init__(self, parent, data):
                picks.append('color'); self.data = data
                data.SetColour(wx.Colour(*( (24, 36, 48) if picks.count('color') == 1 else (242, 246, 250) )))
            def ShowModal(self): return wx.ID_OK
            def GetColourData(self): return self.data
            def Destroy(self): pass
        wx.FontDialog, wx.ColourDialog = FontSelection, ColorSelection
        settings.voice_registry = lambda *args, **kwargs: VoiceRegistry()
        frame = wx.Frame(None, title='Native reading appearance acceptance', size=(650, 400))
        frame.library, frame._closing = library, False
        frame.report = lambda value: print('READER:', value, flush=True)
        frame.Show(); app.SetTopWindow(frame)

        def open_reader():
            if errors: return
            if loop[0] is None: loop[0] = app.GetMainLoop()
            try:
                reader_module.show_speech_reader(frame, text=source if cycles[0] == 0 else '')
                release()
                cycles[0] += 1
                if errors: return
                if cycles[0] == 1:
                    advance('reopening'); wx.CallAfter(open_reader)
                elif cycles[0] == 2:
                    check('both real Reader modal lifetimes completed', evidence.get('complete', False))
                    timer.Stop(); loop[0].ScheduleExit(0)
            except Exception:
                abort(traceback.format_exc())

        def tick_body():
            if errors or held[0] is not None: return
            if monotonic() > deadline or monotonic() - stage[1] > 25:
                raise TimeoutError('Native reading appearance stage timed out: ' + stage[0])
            reader = window('Voice reader and reading copy')
            appearance = window('Reading appearance: font, colors and zoom')
            if stage[0] in ('starting', 'reopening'):
                if reader is None: return
                button = named('Reading appearance', reader)
                if not button.IsEnabled(): return
                editor = named('Reading copy (editable; Ctrl+S saves)', reader)
                evidence['editor'] = editor
                if cycles[0] == 0:
                    evidence['base_font'] = editor.GetFont().GetFractionalPointSize()
                    evidence['base_foreground'], evidence['base_background'] = rgb(editor.GetForegroundColour()), rgb(editor.GetBackgroundColour())
                    evidence['initial'] = editor.GetValue()
                    editor.SetInsertionPointEnd()
                    key(editor, wx.WXK_SPACE, 'typed')
                else:
                    check('restart restores exact edited text and selected native font',
                          editor.GetValue() == evidence['edited'] and editor.GetFont().GetFractionalPointSize() == 21
                          and editor.GetFont().GetFaceName() == evidence['font_face'])
                    check('restart retains source and canonical undo history', drafts.load().document.to_dict() == evidence['document'])
                    key(button, wx.WXK_SPACE, 'reset_open')
            elif stage[0] == 'typed':
                editor = evidence['editor']
                check('actual keyboard edit retains native undo', editor.GetValue() == evidence['initial'] + ' ' and editor.CanUndo())
                evidence['edited'] = editor.GetValue()
                first = editor.GetValue().index('Другий')
                editor.SetSelection(position_module.native_position(editor, editor.GetValue(), first),
                                    position_module.native_position(editor, editor.GetValue(), first + len('Другий рядок.')))
                evidence['selection'], evidence['cursor'] = editor.GetSelection(), editor.GetInsertionPoint()
                key(editor, ord('S'), 'saved', wx.MOD_CONTROL)
            elif stage[0] == 'saved':
                if not drafts.path.exists(): return
                saved = drafts.load()
                check('actual saved draft retains immutable original source and an edit',
                      saved.document.source_text == source and saved.document.text == evidence['edited']
                      and saved.document.revision >= 1)
                evidence['document'] = saved.document.to_dict()
                key(named('Reading appearance', reader), wx.WXK_SPACE, 'appearance_open')
            elif stage[0] == 'appearance_open':
                if appearance is not None: key(appearance.buttons['font'], wx.WXK_SPACE, 'font_selected')
            elif stage[0] == 'font_selected': key(appearance.buttons['foreground'], wx.WXK_SPACE, 'foreground_selected')
            elif stage[0] == 'foreground_selected': key(appearance.buttons['background'], wx.WXK_SPACE, 'background_selected')
            elif stage[0] == 'background_selected': key(appearance.zoom, ord('A'), 'zoom_first', wx.MOD_CONTROL)
            elif stage[0] == 'zoom_first': key(appearance.zoom, ord('1'), 'zoom_second')
            elif stage[0] == 'zoom_second': key(appearance.zoom, ord('5'), 'zoom_third')
            elif stage[0] == 'zoom_third': key(appearance.zoom, ord('0'), 'zoom_done')
            elif stage[0] == 'zoom_done':
                check('actual native zoom entry is exact', appearance.zoom.GetValue() == 150)
                key(appearance.buttons['preview'], wx.WXK_SPACE, 'previewed')
            elif stage[0] == 'previewed':
                check('native preview remains separate from reader and durable state',
                      appearance.buttons['apply'].IsEnabled() and appearance.sample.GetFont().GetFractionalPointSize() == 21
                      and evidence['editor'].GetFont().GetFractionalPointSize() == evidence['base_font'] and not store.path.exists())
                key(appearance.buttons['apply'], wx.WXK_SPACE, 'applied')
            elif stage[0] == 'applied':
                editor, saved = evidence['editor'], store.load()[0]
                check('native font colors and zoom match the saved explicit profile',
                      saved.zoom == 150 and saved.font.face == evidence['font_face'] and saved.font.points == 14
                      and editor.GetFont().GetFractionalPointSize() == 21
                      and rgb(editor.GetForegroundColour()) == saved.foreground == (24, 36, 48)
                      and rgb(editor.GetBackgroundColour()) == saved.background == (242, 246, 250))
                check('appearance preserves exact Unicode text native selection caret and undo',
                      editor.GetValue() == evidence['edited'] and editor.GetSelection() == evidence['selection']
                      and editor.GetInsertionPoint() == evidence['cursor'] and editor.CanUndo())
                check('appearance does not edit saved reading source or history', drafts.load().document.to_dict() == evidence['document'])
                check('picker fixture selections were explicit and finite', picks == ['font', 'color', 'color'])
                key(appearance.buttons['close'], wx.WXK_SPACE, 'appearance_closed')
            elif stage[0] == 'appearance_closed':
                if appearance is not None: return
                check('appearance close restores reader keyboard focus', wx.Window.FindFocus() is evidence['editor'])
                key(wx.FindWindowById(wx.ID_CANCEL, reader), wx.WXK_SPACE, 'reader_closing')
            elif stage[0] == 'reset_open':
                if appearance is not None: key(appearance.buttons['reset'], wx.WXK_SPACE, 'reset_previewed')
            elif stage[0] == 'reset_previewed':
                check('OS reset is a preview until explicitly applied',
                      appearance.reviewed == ReadingPresentation() and store.load()[0].zoom == 150
                      and evidence['editor'].GetFont().GetFractionalPointSize() == 21)
                key(appearance.buttons['apply'], wx.WXK_SPACE, 'reset_applied')
            elif stage[0] == 'reset_applied':
                editor = evidence['editor']
                check('explicit reset saves defaults and restores native OS presentation',
                      store.load()[0] == ReadingPresentation()
                      and editor.GetFont().GetFractionalPointSize() == evidence['base_font']
                      and rgb(editor.GetForegroundColour()) == evidence['base_foreground']
                      and rgb(editor.GetBackgroundColour()) == evidence['base_background'])
                key(appearance.buttons['close'], wx.WXK_SPACE, 'reset_closed')
            elif stage[0] == 'reset_closed':
                if appearance is not None: return
                undo = next(item for item in reader.GetChildren()
                            if isinstance(item, wx.Button) and item.GetLabel() == '&Undo reading edit')
                key(undo, wx.WXK_SPACE, 'undone')
            elif stage[0] == 'undone':
                editor = evidence['editor']
                check('canonical undo survives appearance and reader restart', editor.GetValue() == evidence['initial'])
                key(editor, ord('S'), 'undo_saved', wx.MOD_CONTROL)
            elif stage[0] == 'undo_saved':
                check('original source remains exact after undo and new save', drafts.load().document.source_text == source)
                evidence['complete'] = True
                key(wx.FindWindowById(wx.ID_CANCEL, reader), wx.WXK_SPACE, 'finished')

        def tick(event=None):
            if reentry[0]: return
            reentry[0] = True
            try: tick_body()
            except Exception: abort(traceback.format_exc())
            finally: reentry[0] = False

        timer = wx.Timer(frame); frame.Bind(wx.EVT_TIMER, tick, timer); timer.Start(25)
        wx.CallAfter(open_reader)
        try:
            app.MainLoop()
        finally:
            def restore_fixtures():
                wx.FontDialog, wx.ColourDialog, settings.voice_registry = original_font, original_color, original_registry
            def destroy_frame():
                if frame:
                    frame._closing = True; frame.Destroy()
            _cleanup((timer.Stop, release, restore_fixtures, destroy_frame, app.Yield), errors)
        if errors: raise RuntimeError('\n'.join(errors))
        check('actual native reader workflow completed twice', cycles[0] == 2 and evidence.get('complete', False))
    print(json.dumps({'result': 'PASS', 'checks': len(checks), 'check_names': checks, 'source_identity': identities,
                     'native_reader_lifecycle': 'PASS', 'native_text_presentation': 'PASS',
                     'font_colour_picker_selections': 'EXPLICIT_FIXTURES',
                     'native_font_colour_pickers': 'NOT_RUN', 'speech_engine': 'NOT_RUN',
                     'manual_nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    import threading
    def timeout():
        print('FAIL: native reading presentation watchdog expired during work or cleanup.', flush=True)
        os._exit(124)
    watchdog = threading.Timer(180, timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        raise SystemExit(main())
    finally:
        watchdog.cancel()
