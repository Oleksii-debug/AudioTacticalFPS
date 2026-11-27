"""Actual Windows reader tabs, keyboard edits, separate history and restart.

Uses an explicitly empty voice inventory: native text/recovery acceptance does
not imply speech, acoustic, or manual screen-reader qualification.
"""
from hashlib import sha256
import faulthandler
import importlib.util
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
import traceback

# Isolated installed launches deliberately omit the script directory from
# sys.path. Load only this exact adjacent acceptance helper.
_helper_spec = importlib.util.spec_from_file_location('_carr_reader_acceptance_helpers',
    Path(__file__).with_name('windows_reading_presentation_smoke.py'))
_helpers = importlib.util.module_from_spec(_helper_spec)
_helper_spec.loader.exec_module(_helpers)
_release_key, _cleanup, _unwind = _helpers._release_key, _helpers._cleanup, _helpers._unwind


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
    from carr_library.speech.tabs import ReadingTabsStore
    import carr_library.runtime_settings as settings
    import carr_library.speech.tabs as tabs_module
    import carr_library.speech.session as session_module
    import carr_library.speech.reading as reading_module
    import carr_library.ui.reader_tabs as native_tabs_module
    import carr_library.ui.speech_dialog as reader_module

    checks, errors, identities, evidence = [], [], {}, {}
    held, loop, reentry, aborting, cycles = [None], [None], [False], [False], [0]
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
    def release(): _release_key(simulator, held)
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
        advance(next_stage); held[0] = code, modifiers
        if not simulator.KeyDown(code, modifiers): raise AssertionError('Native reading-tab key press failed.')
        wx.CallLater(40, released)
    def named(name, parent):
        control = wx.FindWindowByName(name, parent)
        if control is None: raise AssertionError('Missing named control: ' + name)
        return control
    def reader_window():
        return next((item for item in wx.GetTopLevelWindows() if item and isinstance(item, wx.Dialog)
                     and item.GetTitle() == 'Voice reader and reading copy' and item.IsShown()), None)
    checkout = Path(__file__).resolve().parents[1]
    installed = (Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR')
                 else Path(sysconfig.get_path('purelib')).resolve())
    for module in (tabs_module, session_module, reading_module, native_tabs_module, reader_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__, actual.is_relative_to(installed)
              and not actual.is_relative_to(checkout) and digest == sha256(expected.read_bytes()).hexdigest())
        identities[module.__name__] = digest
    check('actual native Windows notebook and text controls', wx.Platform == '__WXMSW__')
    with TemporaryDirectory(prefix='resonance-reading-tabs-') as temporary:
        library = LibraryApp(Path(temporary) / 'Бібліотека 😀')
        library.create_workspace('Original reading tabs acceptance source')
        store = ReadingTabsStore(library.root)
        source = '😀 Original first.\r\nДругий рядок.'
        original_registry = settings.voice_registry
        settings.voice_registry = lambda *args, **kwargs: VoiceRegistry()
        frame = wx.Frame(None, title='Native independent reading tabs acceptance', size=(650, 400))
        frame.library, frame._closing = library, False
        frame.report = lambda value: print('READER:', value, flush=True)
        frame.Show(); app.SetTopWindow(frame)
        def drafts():
            index, _ = store.load()
            if index is None: return None, []
            return index, [store.draft(entry).load() for entry in index.tabs]
        def open_reader():
            if errors: return
            if loop[0] is None: loop[0] = app.GetMainLoop()
            try:
                reader_module.show_speech_reader(frame, text=source if cycles[0] == 0 else '')
                release(); cycles[0] += 1
                if errors: return
                if cycles[0] == 1:
                    advance('reopening'); wx.CallAfter(open_reader)
                else:
                    check('both owned reader modal lifetimes completed', evidence.get('complete', False))
                    timer.Stop(); loop[0].ScheduleExit(0)
            except Exception: abort(traceback.format_exc())
        def tick_body():
            if errors or held[0] is not None: return
            if monotonic() > deadline or monotonic() - stage[1] > 25:
                raise TimeoutError('Native reading tabs stage timed out: ' + stage[0])
            reader = reader_window()
            if reader is None: return
            editor = named('Reading copy (editable; Ctrl+S saves)', reader)
            notebook = named('Reading copies; Control+Tab changes reading tab', reader)
            if stage[0] in ('starting', 'reopening') and not named('New reading tab', reader).IsEnabled():
                return
            if stage[0] == 'starting':
                check('first native tab contains exact Unicode source', notebook.GetPageCount() == 1 and editor.GetValue() == source)
                editor.SetInsertionPointEnd()
                key(editor, wx.WXK_SPACE, 'first_edited')
            elif stage[0] == 'first_edited':
                check('first native keyboard edit is retained', editor.GetValue() == source + ' ' and editor.CanUndo())
                key(editor, ord('S'), 'first_saved', wx.MOD_CONTROL)
            elif stage[0] == 'first_saved':
                index, saved = drafts()
                if not saved: return
                check('first copy saves original and independent undo', saved[0].document.source_text == source
                      and saved[0].document.text == source + ' ' and saved[0].document.can_undo)
                evidence['first_id'] = index.tabs[0].id
                key(editor, ord('T'), 'second_created', wx.MOD_CONTROL)
            elif stage[0] == 'second_created':
                check('Control T creates and focuses a separate native page', notebook.GetPageCount() == 2
                      and notebook.GetSelection() == 1 and editor.GetValue() == '' and wx.Window.FindFocus() is editor)
                key(editor, ord('B'), 'second_edited')
            elif stage[0] == 'second_edited':
                check('second copy receives an actual independent keyboard edit', editor.GetValue() == 'b')
                key(editor, ord('S'), 'second_saved', wx.MOD_CONTROL)
            elif stage[0] == 'second_saved':
                index, saved = drafts()
                check('separate durable identities and originals', len(saved) == 2 and index.tabs[0].id == evidence['first_id']
                      and index.tabs[0].id != index.tabs[1].id and saved[0].document.source_text == source
                      and saved[1].document.source_text == '' and saved[1].document.text == 'b')
                evidence['second_id'] = index.tabs[1].id
                key(editor, wx.WXK_TAB, 'first_selected', wx.MOD_CONTROL)
            elif stage[0] == 'first_selected':
                check('Control Tab restores the first native editor and text', notebook.GetSelection() == 0
                      and editor.GetValue() == source + ' ' and wx.Window.FindFocus() is editor)
                key(editor, wx.WXK_TAB, 'second_selected', wx.MOD_CONTROL)
            elif stage[0] == 'second_selected':
                check('Control Tab restores the second text and cursor', notebook.GetSelection() == 1
                      and editor.GetValue() == 'b' and editor.GetInsertionPoint() == 1)
                key(wx.FindWindowById(wx.ID_CANCEL, reader), wx.WXK_SPACE, 'first_closing')
            elif stage[0] == 'reopening':
                index, saved = drafts()
                check('fresh reader recovers both pages and selected identity', notebook.GetPageCount() == 2
                      and notebook.GetSelection() == 1 and index.active == evidence['second_id'] and editor.GetValue() == 'b')
                check('restart restores saved cursor with no installed voice', editor.GetInsertionPoint() == 1)
                key(editor, ord('S'), 'restart_second_saved', wx.MOD_CONTROL)
            elif stage[0] == 'restart_second_saved':
                key(editor, wx.WXK_TAB, 'restart_first', wx.MOD_CONTROL)
            elif stage[0] == 'restart_first':
                check('restart retains first edited Unicode text', notebook.GetSelection() == 0 and editor.GetValue() == source + ' ')
                undo = next(item for item in reader.GetChildren() if isinstance(item, wx.Button)
                            and item.GetLabel() == '&Undo reading edit')
                key(undo, wx.WXK_SPACE, 'first_undone')
            elif stage[0] == 'first_undone':
                check('first undo is independent after actual reader restart', editor.GetValue() == source)
                key(editor, ord('S'), 'restart_first_saved', wx.MOD_CONTROL)
            elif stage[0] == 'restart_first_saved':
                index, saved = drafts()
                check('first undo preserves second history and original source', saved[0].document.source_text == source
                      and saved[0].document.text == source and saved[1].document.text == 'b' and saved[1].document.can_undo)
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
        try: app.MainLoop()
        finally:
            def restore(): settings.voice_registry = original_registry
            def destroy():
                if frame: frame._closing = True; frame.Destroy()
            _cleanup((timer.Stop, release, restore, destroy, app.Yield), errors)
        if errors: raise RuntimeError('\n'.join(errors))
        check('actual native tabs workflow completed twice', cycles[0] == 2 and evidence.get('complete', False))
    print(json.dumps({'result': 'PASS', 'checks': len(checks), 'check_names': checks,
        'source_identity': identities, 'native_reading_tabs': 'PASS', 'keyboard_and_restart': 'PASS',
        'voice_inventory': 'EXPLICIT_EMPTY_FIXTURE', 'speech_engine': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
