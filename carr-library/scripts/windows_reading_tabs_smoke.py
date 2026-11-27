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
    import carr_library.speech.marks as marks_module
    import carr_library.ui.reading_tools as tools_module
    import carr_library.speech.focus as focus_module
    import carr_library.ui.reading_focus as focus_ui_module
    import carr_library.speech.narration as narration_module
    import carr_library.ui.reading_narration as policy_ui_module

    checks, errors, identities, evidence = [], [], {}, {}
    held, loop, reentry, aborting, cycles = [None], [None], [False], [False], [0]
    stage = ['starting', monotonic()]
    deadline = monotonic() + 180
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
    for module in (tabs_module, session_module, reading_module, native_tabs_module, reader_module, marks_module, tools_module, focus_module, focus_ui_module, narration_module, policy_ui_module):
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
                evidence['initial_display'] = editor.GetValue()
                check('first native tab contains exact Unicode source with native line endings',
                      notebook.GetPageCount() == 1 and editor.GetValue().replace('\r\n', '\n') == source.replace('\r\n', '\n'))
                editor.SetInsertionPointEnd()
                key(editor, wx.WXK_SPACE, 'first_edited')
            elif stage[0] == 'first_edited':
                check('first native keyboard edit is retained', editor.GetValue() == evidence['initial_display'] + ' ' and editor.CanUndo())
                key(editor, ord('S'), 'first_saved', wx.MOD_CONTROL)
            elif stage[0] == 'first_saved':
                index, saved = drafts()
                if not saved: return
                check('first copy saves original and independent undo', saved[0].document.source_text == source
                      and saved[0].document.text == evidence['initial_display'] + ' ' and saved[0].document.can_undo)
                evidence['first_id'] = index.tabs[0].id
                editor.SetInsertionPoint(5)
                evidence['bookmark_cursor'] = reader_module.native_insertion_offset(editor, evidence['initial_display'] + ' ')
                key(named('Reading bookmarks and paragraph tools', reader), wx.WXK_SPACE, 'tools_open')
            elif stage[0] in ('tools_open', 'bookmark_added', 'split_tools_open', 'paragraph_split'):
                tools_dialog = next((item for item in wx.GetTopLevelWindows()
                    if item and isinstance(item, wx.Dialog) and item.IsShown()
                    and item.GetTitle() == 'Reading bookmarks and paragraph tools'), None)
                if tools_dialog is None: return
                if stage[0] == 'tools_open':
                    check('actual native bookmark name and list controls are available',
                          isinstance(named('Reading bookmark name', tools_dialog), wx.TextCtrl)
                          and isinstance(named('Saved bookmarks in this reading copy', tools_dialog), wx.ListBox))
                    key(named('Add bookmark at current cursor', tools_dialog), wx.WXK_SPACE, 'bookmark_added')
                elif stage[0] == 'bookmark_added':
                    index, saved = drafts()
                    marks = saved[0].document.bookmarks
                    check('native bookmark action persists exact text and cursor', len(marks) == 1
                          and marks[0].offset == evidence['bookmark_cursor']
                          and saved[0].document.bookmark_position(marks[0].id) == evidence['bookmark_cursor'])
                    evidence['bookmark_id'] = marks[0].id
                    named('Saved bookmarks in this reading copy', tools_dialog).SetSelection(0)
                    key(named('Go to bookmark and close', tools_dialog), wx.WXK_SPACE, 'bookmark_returned')
                elif stage[0] == 'split_tools_open':
                    key(named('Split paragraph at current cursor', tools_dialog), wx.WXK_SPACE, 'paragraph_split')
                else:
                    index, saved = drafts()
                    check('native paragraph split is undoable and preserves the original',
                          saved[0].document.source_text == source and saved[0].document.can_undo
                          and saved[0].document.text != evidence['initial_display'] + ' ' and '\n\n' in saved[0].document.text.replace('\r\n', '\n'))
                    key(named('Close reading text tools', tools_dialog), wx.WXK_SPACE, 'paragraph_closed')
            elif stage[0] == 'bookmark_returned':
                check('native bookmark navigation restores cursor and reader focus',
                      reader_module.native_insertion_offset(editor, editor.GetValue()) == evidence['bookmark_cursor']
                      and wx.Window.FindFocus() is editor)
                key(named('Reading bookmarks and paragraph tools', reader), wx.WXK_SPACE, 'split_tools_open')
            elif stage[0] == 'paragraph_closed':
                undo = next(item for item in reader.GetChildren() if isinstance(item, wx.Button)
                            and item.GetLabel() == '&Undo reading edit')
                key(undo, wx.WXK_SPACE, 'paragraph_undone')
            elif stage[0] == 'paragraph_undone':
                check('native paragraph Undo restores the bookmarked text version', editor.GetValue() == evidence['initial_display'] + ' ')
                key(editor, ord('S'), 'bookmark_saved', wx.MOD_CONTROL)
            elif stage[0] == 'bookmark_saved':
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
                      and editor.GetValue() == evidence['initial_display'] + ' ' and wx.Window.FindFocus() is editor)
                key(editor, wx.WXK_TAB, 'second_selected', wx.MOD_CONTROL)
            elif stage[0] == 'second_selected':
                check('Control Tab restores the second text and cursor', notebook.GetSelection() == 1
                      and editor.GetValue() == 'b' and editor.GetInsertionPoint() == 1)
                evidence['policy_round'] = 1
                key(editor, ord('A'), 'policy_selected', wx.MOD_CONTROL)
            elif stage[0] == 'reopening':
                index, saved = drafts()
                check('fresh reader recovers both pages and selected identity', notebook.GetPageCount() == 2
                      and notebook.GetSelection() == 1 and index.active == evidence['second_id'] and editor.GetValue() == 'b')
                check('restart restores saved cursor with no installed voice', editor.GetInsertionPoint() == 1)
                check('restart retains explicit category and Skip without granting audio timing',
                      saved[1].document.narration_skips_enabled and saved[1].audio_seconds == 0
                      and saved[1].document.narration_selection().empty
                      and saved[1].document.narration_policy['regions'][0]['id'] == evidence['region_id']
                      and saved[1].document.source_text == '' and saved[1].document.text == 'b')
                evidence['policy_round'] = 2
                key(named('Reading bookmarks and paragraph tools', reader), wx.WXK_SPACE, 'policy_tools_open')
            elif stage[0] == 'policy_selected':
                check('actual Control A selects the exact second reading copy', editor.GetStringSelection() == 'b')
                key(named('Reading bookmarks and paragraph tools', reader), wx.WXK_SPACE, 'policy_tools_open')
            elif stage[0] in ('policy_tools_open', 'policy_closed'):
                tools_dialog = next((item for item in wx.GetTopLevelWindows()
                    if item and isinstance(item, wx.Dialog) and item.IsShown()
                    and item.GetTitle() == 'Reading bookmarks and paragraph tools'), None)
                if tools_dialog is None: return
                if stage[0] == 'policy_tools_open':
                    key(named('Read or skip reviewed content', tools_dialog), wx.WXK_SPACE, 'policy_open')
                else:
                    key(named('Close reading text tools', tools_dialog), wx.WXK_SPACE, 'policy_tools_closed')
            elif stage[0] in ('policy_open', 'policy_marked', 'policy_skip_chosen', 'policy_skip_applied',
                    'policy_skip_previewed', 'policy_read_chosen', 'policy_read_applied',
                    'policy_read_previewed', 'policy_reskip_chosen', 'policy_reskipped'):
                policy = next((item for item in wx.GetTopLevelWindows()
                    if item and isinstance(item, wx.Dialog) and item.IsShown()
                    and item.GetTitle() == 'Read or skip reviewed reading content'), None)
                if policy is None or policy.working: return
                mode = named('Read or skip selected category', policy)
                if stage[0] == 'policy_open':
                    check('actual category and Read Skip native choices are accessible',
                          isinstance(named('Reading content category', policy), wx.Choice)
                          and isinstance(mode, wx.Choice)
                          and isinstance(named('Exact narration preview', policy), wx.TextCtrl))
                    if evidence['policy_round'] == 1:
                        check('unmarked content is unclassified and Read by default',
                              not policy.rows and mode.GetSelection() == 0
                              and named('Mark selected reading content', policy).IsEnabled())
                        key(named('Mark selected reading content', policy), wx.WXK_SPACE, 'policy_marked')
                    else:
                        check('restored native policy exposes the exact selected region',
                              len(policy.rows) == 1 and policy.rows[0]['id'] == evidence['region_id']
                              and mode.GetSelection() == 1)
                        key(mode, wx.WXK_HOME, 'policy_read_chosen')
                elif stage[0] == 'policy_marked':
                    index, saved = drafts()
                    region = saved[1].document.narration_policy['regions'][0]
                    check('native mark retains explicit selection and unchanged source',
                          region['start'] == 0 and region['end'] == 1
                          and region['selected_sha256'] == sha256(b'b').hexdigest()
                          and saved[1].document.source_text == '' and saved[1].document.text == 'b')
                    evidence['region_id'] = region['id']
                    key(mode, wx.WXK_END, 'policy_skip_chosen')
                elif stage[0] == 'policy_skip_chosen':
                    check('native keyboard selects Skip', mode.GetSelection() == 1)
                    key(named('Apply category Read or Skip', policy), wx.WXK_SPACE, 'policy_skip_applied')
                elif stage[0] == 'policy_skip_applied':
                    check('native Skip is a saved reading policy', policy.policy['skip'] == ['footnote'])
                    key(named('Preview exact narration', policy), wx.WXK_SPACE, 'policy_skip_previewed')
                elif stage[0] == 'policy_skip_previewed':
                    check('fully skipped native preview is explicitly empty with no engine',
                          named('Exact narration preview', policy).GetValue() == ''
                          and 'Empty narration' in named('Reading content policy status', policy).GetValue()
                          and policy.preview_selection.empty)
                    key(mode, wx.WXK_HOME, 'policy_read_chosen')
                elif stage[0] == 'policy_read_chosen':
                    check('native keyboard explicitly re-enables Read', mode.GetSelection() == 0)
                    key(named('Apply category Read or Skip', policy), wx.WXK_SPACE, 'policy_read_applied')
                elif stage[0] == 'policy_read_applied':
                    key(named('Preview exact narration', policy), wx.WXK_SPACE, 'policy_read_previewed')
                elif stage[0] == 'policy_read_previewed':
                    check('Read restores every narration character without text edits',
                          named('Exact narration preview', policy).GetValue() == 'b'
                          and not policy.preview_selection.filtered)
                    if evidence['policy_round'] == 1:
                        key(mode, wx.WXK_END, 'policy_reskip_chosen')
                    else:
                        evidence['policy_recovered'] = True
                        key(named('Exact narration preview', policy), wx.WXK_ESCAPE, 'policy_closed')
                elif stage[0] == 'policy_reskip_chosen':
                    key(named('Apply category Read or Skip', policy), wx.WXK_SPACE, 'policy_reskipped')
                else:
                    check('explicit Skip is retained before native restart', policy.policy['skip'] == ['footnote'])
                    key(named('Exact narration preview', policy), wx.WXK_ESCAPE, 'policy_closed')
            elif stage[0] == 'policy_tools_closed':
                check('closing category tools restores actual reader keyboard focus',
                      wx.Window.FindFocus() is editor and editor.GetValue() == 'b')
                key(editor, ord('S'), 'policy_saved', wx.MOD_CONTROL)
            elif stage[0] == 'policy_saved':
                index, saved = drafts()
                check('typed policy is retained in the current existing tab draft',
                      saved[1].document.to_dict()['schema'] == 'carr.reading-copy.v4'
                      and saved[1].document.narration_policy['regions'][0]['id'] == evidence['region_id']
                      and saved[1].document.source_text == '')
                if evidence['policy_round'] == 1:
                    key(wx.FindWindowById(wx.ID_CANCEL, reader), wx.WXK_SPACE, 'first_closing')
                else:
                    advance('restart_second_saved')
            elif stage[0] == 'restart_second_saved':
                key(editor, wx.WXK_TAB, 'restart_first', wx.MOD_CONTROL)
            elif stage[0] == 'restart_first':
                check('restart retains first edited Unicode text', notebook.GetSelection() == 0 and editor.GetValue() == evidence['initial_display'] + ' ')
                index, saved = drafts()
                check('restart retains the native-created bookmark and exact version',
                      saved[0].document.bookmarks[0].id == evidence['bookmark_id']
                      and saved[0].document.bookmark_position(evidence['bookmark_id']) == evidence['bookmark_cursor'])
                undo = next(item for item in reader.GetChildren() if isinstance(item, wx.Button)
                            and item.GetLabel() == '&Undo reading edit')
                key(undo, wx.WXK_SPACE, 'first_undone')
            elif stage[0] == 'first_undone':
                check('first undo is independent after actual reader restart', editor.GetValue() == evidence['initial_display'])
                key(editor, ord('S'), 'restart_first_saved', wx.MOD_CONTROL)
            elif stage[0] == 'restart_first_saved':
                index, saved = drafts()
                check('first undo preserves second history and original source', saved[0].document.source_text == source
                      and saved[0].document.text == evidence['initial_display'] and saved[1].document.text == 'b' and saved[1].document.can_undo)
                key(named('Reading focus and approximate audio follow', reader), wx.WXK_SPACE, 'focus_open')
            elif stage[0] in ('focus_open', 'focus_running', 'focus_paused', 'focus_previous'):
                focus = next((item for item in wx.GetTopLevelWindows()
                    if item and isinstance(item, wx.Dialog) and item.IsShown()
                    and item.GetTitle() == 'Reading focus and audio follow'), None)
                if focus is None: return
                if stage[0] == 'focus_open':
                    check('native visual reading opens paused without an engine', not focus.pacer.running
                          and not focus.following and focus.mode.GetSelection() == 0)
                    evidence['focus_first_word'] = named('Current visual reading word', focus).GetValue()
                    named('Visual reading words per minute', focus).SetValue(600)
                    key(named('Start visual reading or audio follow', focus), wx.WXK_SPACE, 'focus_running')
                elif stage[0] == 'focus_running':
                    if named('Current visual reading word', focus).GetValue() == evidence['focus_first_word']:
                        return
                    check('actual native timer advances explicit visual reading',
                          focus.timer.IsRunning() and focus.span is not None)
                    key(named('Pause visual reading or audio follow', focus), wx.WXK_SPACE, 'focus_paused')
                elif stage[0] == 'focus_paused':
                    check('native visual pause keeps automatic movement stopped', not focus.pacer.running
                          and not focus.following)
                    key(named('Previous visual word', focus), wx.WXK_SPACE, 'focus_previous')
                else:
                    evidence['focus_cursor'] = focus.span[0]
                    key(named('Use highlighted cursor and close', focus), wx.WXK_SPACE, 'focus_returned')
            elif stage[0] == 'focus_returned':
                check('explicit visual cursor return restores native reader focus',
                      reader_module.native_insertion_offset(editor, editor.GetValue()) == evidence['focus_cursor']
                      and wx.Window.FindFocus() is editor)
                key(editor, ord('S'), 'focus_saved', wx.MOD_CONTROL)
            elif stage[0] == 'focus_saved':
                index, saved = drafts()
                check('visual cursor persists through the original reading recovery',
                      saved[0].cursor == evidence['focus_cursor'] and saved[0].document.source_text == source)
                check('content policy survived native close restart and explicit re-enabling',
                      evidence.get('policy_recovered', False))
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
        'reading_content_policy': 'PASS', 'voice_inventory': 'EXPLICIT_EMPTY_FIXTURE',
        'speech_engine': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
