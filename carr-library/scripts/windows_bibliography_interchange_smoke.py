"""Check installed canonical BibTeX import and safe export on native Windows.

Only file-picker paths are controlled. The academic window, import review,
export options, overwrite question, keyboard input and filesystem operations
are real. This does not exercise citation-style rendering or claim NVDA,
manual keyboard use, native pickers or accessible overwrite-button focus.
Run with the installed interpreter: python -I -B scripts/windows_bibliography_interchange_smoke.py.
"""
from collections import deque
import faulthandler
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import socket
import struct
import sys
import sysconfig
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback


BIBTEX = r'''
@string{press = "Synthetic " # "Press"}
@book{CanonicalNativeKey,
  title = {Canonical bibliography — Український текст},
  author = {M{\"u}ller, Jane and {Research and Development, Inc.}},
  year = {2026}, publisher = press,
  note = {NativePrivateNote},
  customfield = {Preserve this unsupported source metadata},
  file = {C:\never-open-this-synthetic-attachment.pdf}
}
@book{UnselectedNativeKey, title = {UnselectedNativeMarker}, year = {2025}}
'''


def main():
    if os.name != 'nt':
        print(json.dumps({
            'result': 'NOT_RUN', 'reason': 'A native Windows session is required.',
            'native_keyboard': 'NOT_RUN', 'native_file_pickers': 'NOT_RUN',
            'native_overwrite_accessible_focus': 'NOT_RUN', 'nvda': 'NOT_RUN',
        }), flush=True)
        return 77

    faulthandler.enable(file=sys.stderr, all_threads=True)
    print(json.dumps({'native_process_started': os.getpid(),
                      'faulthandler_enabled': faulthandler.is_enabled()}), flush=True)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.research import academic_library, bibtex_import, export_file
    from carr_library.ui import academic_library_dialog as ui

    checks, failures, network_attempts = [], [], []
    failure_exit = [None]
    events = deque(maxlen=20)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetFocus.argtypes = []
    user32.GetFocus.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def offline(*args, **kwargs):
        network_attempts.append('unexpected network operation')
        raise OSError('Network access is disabled during bibliography interchange acceptance.')

    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    socket.create_connection = offline
    socket.getaddrinfo = offline

    def overwrite_prompt():
        handle = user32.GetForegroundWindow()
        process = wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process))
        if process.value != os.getpid() or not user32.IsWindowVisible(handle):
            return None
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(handle, title, len(title))
        return handle if title.value == 'Replace academic bibliography' else None

    def focused(control):
        current = wx.Window.FindFocus()
        while current is not None:
            if current is control:
                return True
            current = current.GetParent()
        return False

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc(), 'native event-loop exception')
            return False

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()

    def diagnostics(reason):
        current = wx.Window.FindFocus()
        top = wx.GetTopLevelParent(current) if current is not None else None
        print(json.dumps({
            'native_input_diagnostic': reason,
            'main_loop_exists': gui.GetMainLoop() is not None,
            'foreground_handle': int(user32.GetForegroundWindow() or 0),
            'thread_focus_handle': int(user32.GetFocus() or 0),
            'overwrite_prompt_handle': int(overwrite_prompt() or 0),
            'wx_focus': current.GetName() if current is not None else None,
            'wx_focus_class': type(current).__name__ if current is not None else None,
            'top_handle': int(top.GetHandle()) if top is not None else None,
            'recent_key_events': list(events),
        }, sort_keys=True), flush=True)

    def abort(error, reason):
        failures.append(error)
        print(json.dumps({'result': 'FAIL', 'error': error, 'native_step': reason}), flush=True)
        if failure_exit[0] is None:
            # A native modal loop can outlive ExitMainLoop. Failure never
            # supplies a confirmation answer or fabricates a passing check.
            failure_exit[0] = threading.Timer(3, lambda: os._exit(1))
            failure_exit[0].daemon = True
            failure_exit[0].start()
        try:
            diagnostics(reason)
        finally:
            gui.ExitMainLoop()

    def observe(control):
        for event_type, label in ((wx.EVT_KEY_DOWN, 'down'), (wx.EVT_KEY_UP, 'up'),
                                  (wx.EVT_CHAR, 'char')):
            def received(event, kind=label):
                events.append({'event': kind, 'key': event.GetKeyCode(),
                               'control': event.GetEventObject().GetName()})
                event.Skip()
            control.Bind(event_type, received)

    def until(predicate, explanation, timeout=12):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() >= deadline:
                diagnostics(explanation)
                raise TimeoutError(explanation)
            yield 20

    def key_marker(phase, code, shift):
        print(json.dumps({'native_key_phase': phase, 'key': code, 'shift': shift}), flush=True)

    def key(code, *, shift=False):
        diagnostics('before native key ' + str(code))
        if shift and not simulator.KeyDown(wx.WXK_SHIFT):
            raise AssertionError('Native Shift key-down was rejected.')
        try:
            if not simulator.KeyDown(code):
                raise AssertionError('Native key-down was rejected.')
            key_marker('after-KeyDown', code, shift)
            try:
                yield 40
            finally:
                key_marker('before-KeyUp', code, shift)
                if not simulator.KeyUp(code):
                    raise AssertionError('Native key-up was rejected.')
                key_marker('after-KeyUp', code, shift)
        finally:
            if shift:
                simulator.KeyUp(wx.WXK_SHIFT)
        yield 80
        key_marker('continuation', code, shift)

    def focus(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        yield from until(lambda: focused(control) and user32.GetForegroundWindow() == top.GetHandle(),
                         'The native control did not acquire foreground keyboard focus.')

    def press(button):
        check('native action button is keyboard reachable', button.AcceptsFocusFromKeyboard())
        yield from focus(button)
        yield from key(wx.WXK_SPACE)

    def descendants(window):
        children = list(window.GetChildren())
        for child in children:
            children.extend(child.GetChildren())
        return children

    def modal(parent, title):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.GetParent() is parent
                     and window.IsShown() and window.IsModal() and window.GetTitle() == title), None)

    class ObservedController(ui.AcademicController):
        """Record entry into the actual controller; keep its real implementation."""
        def __init__(self, *args):
            super().__init__(*args)
            self.export_attempts = []

        def export_to(self, destination, **options):
            self.export_attempts.append({'path': str(destination), 'format': options.get('format', 'text')})
            return super().export_to(destination, **options)

    modules = (ui, academic_library, bibtex_import, export_file)
    installed = Path(sysconfig.get_path('purelib')).resolve()
    check('isolated installed CPython 3.11 x64 is running',
          sys.flags.isolated == 1 and sys.version_info[:2] == (3, 11)
          and struct.calcsize('P') == 8 and sys.prefix != sys.base_prefix)
    check('all interchange modules come from the installed package',
          all(Path(module.__file__).resolve().is_relative_to(installed) for module in modules))
    check('wx uses native Windows controls and the pinned import dependencies',
          wx.Platform == '__WXMSW__'
          and all(version(name) == expected for name, expected in (
              ('wxPython', '4.2.4'), ('bibtexparser', '2.1.0'), ('pylatexenc', '2.11'))))

    frame = academic = None
    original_picker = wx.FileDialog
    original_message_dialog = wx.MessageDialog
    with TemporaryDirectory(prefix='research-interchange-native-') as temporary:
        root = Path(temporary)
        try:
            frame = wx.Frame(None, title='Synthetic bibliography interchange acceptance', size=(980, 860))
            gui.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            completed, next_tick = [], [None]

            def scenarios():
                nonlocal academic
                library = LibraryApp(root / 'private library')
                workspace = library.create_workspace('Synthetic native interchange')
                other_workspace = library.create_workspace('Separate native interchange')
                controller = ObservedController(library, workspace)
                ui.AcademicController(library, other_workspace).create({'title': 'ForeignOnlyMarker'})
                archived = controller.create({'title': 'ArchivedOnlyMarker'})
                controller.archive(archived)
                source = library.add_text(workspace, 'Retained synthetic source',
                    'Exact railway source wording. Український текст.', authors=('Source Author',))
                original = library.assets.resolve(source['asset']).read_bytes()
                topic = library.create_topic(workspace, 'Retained evidence')
                hit, = library.search(workspace, 'railway')
                evidence = library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original source note')

                bibliography = root / 'native bibliography.bib'
                bibliography.write_text(BIBTEX, encoding='utf-8')
                bibliography_bytes = bibliography.read_bytes()
                selected_export = [None]
                pickers = []

                class ChosenFile:
                    def __init__(self, parent, message, **options):
                        if message == 'Choose a RIS, BibTeX or CSL JSON bibliography':
                            check('native import requests the actual BibTeX-capable local picker',
                                  parent is academic and options['style'] & wx.FD_OPEN
                                  and options['style'] & wx.FD_FILE_MUST_EXIST
                                  and '*.bib' in options['wildcard'])
                            self.path = bibliography
                        elif message == 'Save academic bibliography':
                            check('native export uses only a save picker before explicit confirmation',
                                  parent is academic and options['style'] & wx.FD_SAVE
                                  and not options['style'] & wx.FD_OVERWRITE_PROMPT
                                  and selected_export[0] is not None)
                            self.path = selected_export[0]
                        else:
                            raise AssertionError('Unexpected file picker: ' + message)
                        self.destroyed = False
                        pickers.append(self)

                    def ShowModal(self):
                        return wx.ID_OK

                    def GetPath(self):
                        return str(self.path)

                    def Destroy(self):
                        self.destroyed = True

                wx.FileDialog = ChosenFile
                academic = ui.create_academic_dialog(frame, controller)
                for control in (academic.references, academic.actions['import'], academic.actions['export']):
                    observe(control)
                academic.Show()
                academic.Raise()
                check('the real academic library starts without active imported references', not controller.list())
                yield from press(academic.actions['import'])
                yield from until(lambda: modal(academic, 'Review bibliographic import') is not None,
                                 'The real BibTeX import review did not open.')
                review = modal(academic, 'Review bibliographic import')
                observe(review.entries)
                observe(review.import_button)
                check('native BibTeX review has two unchecked entries and has not imported anything',
                      isinstance(review.entries, wx.CheckListBox) and review.entries.GetCount() == 2
                      and not any(review.entries.IsChecked(i) for i in range(2)) and not controller.list())
                check('actual BibTeX preview resolves macros and Unicode while retaining source provenance',
                      review.preview.entries[0].authors == ('Müller, Jane', 'Research and Development, Inc.')
                      and review.preview.entries[0].fields['publisher'] == 'Synthetic Press'
                      and 'CanonicalNativeKey' in review.details.GetValue()
                      and 'customfield' in review.details.GetValue()
                      and bool(review.details.GetWindowStyleFlag() & wx.TE_READONLY))
                yield from focus(review.entries)
                yield from key(wx.WXK_SPACE)
                yield from until(lambda: review.entries.IsChecked(0), 'Space did not select the BibTeX entry.')
                check('real Space selects only the intended import entry', not review.entries.IsChecked(1))
                yield from press(review.import_button)
                yield from until(lambda: modal(academic, 'Review bibliographic import') is None
                                 and academic.status.GetValue().startswith('Imported 1 references;'),
                                 'The native Import selected button did not complete the real import.')
                reference, = controller.list()
                check('the real import assigns application identity and preserves the BibTeX key separately',
                      reference.id != 'CanonicalNativeKey' and 'CanonicalNativeKey' in reference.notes
                      and reference.title == 'Canonical bibliography — Український текст'
                      and not controller.list(query='UnselectedNativeMarker')
                      and academic.selected().id == reference.id)
                check('native import leaves the selected bibliography file unchanged',
                      bibliography.read_bytes() == bibliography_bytes)

                def choose_export(path, format_name):
                    selected_export[0] = path
                    attempts = len(controller.export_attempts)
                    yield from press(academic.actions['export'])
                    yield from until(lambda: modal(academic, 'Export bibliography') is not None,
                                     'The actual academic export options did not open.')
                    options = modal(academic, 'Export bibliography')
                    children = descendants(options)
                    choice = next((child for child in children if isinstance(child, wx.Choice)
                                   and child.GetName() == 'Export format'), None)
                    scope = next((child for child in children if isinstance(child, wx.Choice)
                                  and child.GetName().startswith('Export scope;')), None)
                    notes = next((child for child in children if isinstance(child, wx.CheckBox)), None)
                    check('real export options expose formats with all-active scope and private notes excluded',
                          choice is not None and choice.GetStrings() == [label for _, label, _ in ui.FORMATS]
                          and scope is not None and scope.GetSelection() == 0
                          and notes is not None and not notes.GetValue())
                    observe(choice)
                    yield from focus(choice)
                    yield from key(wx.WXK_HOME)
                    index = next(i for i, item in enumerate(ui.FORMATS) if item[0] == format_name)
                    for _ in range(index):
                        yield from key(wx.WXK_DOWN)
                    yield from until(lambda: choice.GetSelection() == index,
                                     'Native keys did not select the requested export format.')
                    check('actual native keys select the requested academic export format',
                          choice.GetStringSelection() == ui.FORMATS[index][1])
                    yield from press(wx.Window.FindWindowById(wx.ID_OK, options))
                    yield from until(lambda: overwrite_prompt() is not None,
                                     'The actual academic overwrite question did not open.')
                    handle = overwrite_prompt()
                    native_class = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(handle, native_class, len(native_class))
                    check('the overwrite question is the owned foreground native modal dialog',
                          user32.GetWindow(handle, 4) == academic.GetHandle()
                          and native_class.value == '#32770'
                          and not user32.IsWindowEnabled(academic.GetHandle()))
                    return attempts, handle

                def answer_yes(handle, attempts):
                    if overwrite_prompt() != handle:
                        raise AssertionError('The native overwrite question changed before the keyboard answer.')
                    yield from key(wx.WXK_TAB, shift=True)
                    yield from key(wx.WXK_RETURN)
                    yield from until(lambda: overwrite_prompt() is None
                                     and len(controller.export_attempts) == attempts + 1,
                                     'Shift+Tab and Enter did not approve exactly one real export.')
                    check('real Shift+Tab and Enter close the question and enter the actual export once',
                          len(controller.export_attempts) == attempts + 1 and overwrite_prompt() is None)

                declined = root / 'native declined bibliography.bib'
                declined.write_bytes(b'Keep existing bibliography after default No')
                before_status = academic.status.GetValue()
                attempts, _ = yield from choose_export(declined, 'bibtex')
                yield from key(wx.WXK_RETURN)
                yield from until(lambda: overwrite_prompt() is None
                                 and user32.GetForegroundWindow() == academic.GetHandle(),
                                 'Enter did not dismiss the default-No academic overwrite question.')
                check('default Enter declines without export attempts, status errors or file changes',
                      len(controller.export_attempts) == attempts
                      and declined.read_bytes() == b'Keep existing bibliography after default No'
                      and academic.status.GetValue() == before_status)

                approved = root / 'native approved bibliography.bib'
                approved.write_bytes(b'Explicitly approved unchanged bibliography')
                attempts, handle = yield from choose_export(approved, 'bibtex')
                yield from answer_yes(handle, attempts)
                content = approved.read_text(encoding='utf-8')
                roundtrip = controller.preview(approved.read_bytes(), 'bibtex')
                check('real Yes replaces the approved file with an importable scoped BibTeX bibliography',
                      len(roundtrip.entries) == 1 and roundtrip.entries[0].title == reference.title
                      and '@book{reference_' in content and 'Український текст' in content
                      and all(marker not in content for marker in (
                          'NativePrivateNote', 'CanonicalNativeKey', 'ForeignOnlyMarker',
                          'ArchivedOnlyMarker', 'UnselectedNativeMarker'))
                      and academic.status.GetValue().startswith(f'Bibliography exported to {approved}.')
                      and focused(academic.status))

                changed = root / 'native changed bibliography.json'
                changed.write_bytes(b'Original bibliography shown for replacement')
                attempts, handle = yield from choose_export(changed, 'csl-json')
                competing = b'Newer work saved while the actual academic overwrite question is open'
                changed.write_bytes(competing)
                check('the competing save happens while the actual overwrite modal is still open',
                      overwrite_prompt() == handle and changed.read_bytes() == competing)
                yield from answer_yes(handle, attempts)
                check('real Yes cannot replace a destination changed since the question was opened',
                      changed.read_bytes() == competing
                      and academic.status.GetValue().startswith('Bibliography not exported:')
                      and 'destination changed' in academic.status.GetValue() and focused(academic.status))
                check('native export keeps references, original sources and exact saved evidence unchanged',
                      controller.get(reference.id) == reference
                      and library.assets.resolve(source['asset']).read_bytes() == original
                      and library.evidence(workspace, topic)[0].evidence_id == evidence
                      and library.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote)
                check('all controlled file choices are disposed and native confirmation stays real',
                      len(pickers) == 4 and all(picker.destroyed for picker in pickers)
                      and wx.MessageDialog is original_message_dialog
                      and not list(root.glob('.research-export-*')))
                yield from focus(academic.references)
                yield from key(wx.WXK_ESCAPE)
                yield from until(lambda: not academic.IsShown(), 'Escape did not close the actual academic window.')
                check('real Escape closes the academic window', True)
                check('all interchange operations stayed offline', not network_attempts)
                print(json.dumps({
                    'result': 'PASS', 'checks': len(checks), 'python': platform.python_version(),
                    'platform': platform.platform(), 'wx': wx.version(),
                    'installed_modules': {module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
                                          for module in modules},
                    'native_keyboard': 'PASS', 'native_bibtex_import': 'PASS',
                    'native_export_options': 'PASS', 'native_overwrite_confirmation': 'PASS',
                    'file_choices': 'controlled temporary paths only',
                    'native_overwrite_accessible_focus': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN',
                    'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN', 'installer': 'NOT_RUN',
                }, sort_keys=True), flush=True)

            scenario = scenarios()

            def advance():
                try:
                    delay = next(scenario)
                except StopIteration:
                    completed.append(True)
                    gui.ExitMainLoop()
                except BaseException:
                    abort(traceback.format_exc(), 'native interchange scenario failed')
                else:
                    next_tick[0] = wx.CallLater(delay, advance)

            watchdog = wx.CallLater(120000, lambda: abort(
                'Native bibliography interchange exceeded 120 seconds.', 'native acceptance watchdog'))
            next_tick[0] = wx.CallLater(150, advance)
            try:
                gui.MainLoop()
            finally:
                watchdog.Stop()
                if next_tick[0] is not None:
                    next_tick[0].Stop()
                scenario.close()
            if failures:
                raise RuntimeError('\n'.join(failures))
            if not completed:
                raise RuntimeError('The native loop stopped before all interchange scenarios completed.')
        finally:
            wx.FileDialog = original_picker
            if academic is not None:
                academic.Destroy()
            if frame is not None:
                frame.Destroy()
            gui.Yield()
            if failure_exit[0] is not None:
                failure_exit[0].cancel()
    return 0


if __name__ == '__main__':
    try:
        exit_code = main()
    except Exception:
        print(json.dumps({'result': 'FAIL', 'error': traceback.format_exc(),
                          'native_overwrite_accessible_focus': 'NOT_RUN',
                          'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
        raise SystemExit(1)
    except BaseException:
        print(json.dumps({'result': 'FAIL', 'native_process_exit': 'unexpected BaseException',
                          'error': traceback.format_exc()}), flush=True)
        raise
    else:
        print(json.dumps({'native_process_exit': 'main returned', 'exit_code': exit_code}), flush=True)
        raise SystemExit(exit_code)
