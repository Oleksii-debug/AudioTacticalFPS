"""Exercise installed BibTeX and citation controls on a real Windows desktop.

Run with python -I scripts/windows_citation_styles_smoke.py after installing
the application wheel. All records and exported documents are synthetic and
temporary. This checks native simulated keys and the Windows clipboard, not
manual keyboard use, native file pickers, NVDA speech or a packaged installer.
"""
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
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
from zipfile import ZipFile


CUSTOM_STYLE = b'''<?xml version="1.0" encoding="utf-8"?>
<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">
  <info><title>Native local citation style</title>
    <id>urn:uuid:cc5bff97-78c8-4a22-b921-87b6d1d9929a</id>
    <updated>2026-11-26T00:00:00Z</updated></info>
  <citation><layout prefix="[Native local: " suffix="]"><text variable="title"/></layout></citation>
  <bibliography><layout><text variable="title" prefix="NATIVE LOCAL: " font-style="italic"/></layout></bibliography>
</style>'''


def main():
    if os.name != 'nt':
        print(json.dumps({
            'result': 'NOT_RUN', 'reason': 'A native Windows session is required.',
            'native_keyboard': 'NOT_RUN', 'native_clipboard': 'NOT_RUN',
            'native_overwrite_accessible_focus': 'NOT_RUN',
            'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
        }), flush=True)
        return 77

    from carr_library.native_runtime import activate_bundled_runtime
    activate_bundled_runtime()
    import wx
    import ctypes
    from ctypes import wintypes
    from carr_library.application import LibraryApp
    from carr_library.citations import bibliography_workbench, csl
    from carr_library.research import bibtex_import
    from carr_library.ui import academic_library_dialog, citation_styles_dialog

    checks, failures, network_attempts = [], [], []
    failure_exit = [None]
    key_events = deque(maxlen=24)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetFocus.argtypes = []
    user32.GetFocus.restype = wintypes.HWND
    user32.GetActiveWindow.argtypes = []
    user32.GetActiveWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetDlgCtrlID.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL

    def native_title(handle):
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(handle, title, len(title))
        return title.value

    def overwrite_prompt():
        # wxMSW MessageDialog uses a real Win32 message/task dialog. Observe
        # only this process's foreground window; do not replace its factory,
        # ShowModal(), return value, controls, or event handler.
        handle = user32.GetForegroundWindow()
        process = wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process))
        if (process.value == os.getpid() and user32.IsWindowVisible(handle)
                and native_title(handle) == 'Replace bibliography file'):
            return handle
        return None

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def offline(*args, **kwargs):
        network_attempts.append('unexpected network operation')
        raise OSError('Network access is disabled during bibliography acceptance.')

    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    socket.create_connection = offline
    socket.getaddrinfo = offline

    def focused(control):
        current = wx.Window.FindFocus()
        while current is not None:
            if current is control:
                return True
            current = current.GetParent()
        return False

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort_native_run(traceback.format_exc(), 'native event-loop exception')
            return False

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    main_thread = threading.get_ident()

    def diagnostics(reason, control=None):
        current = wx.Window.FindFocus()
        target = control or current
        top = wx.GetTopLevelParent(target) if target is not None else None
        foreground = user32.GetForegroundWindow()
        native_class = ctypes.create_unicode_buffer(256)
        if foreground:
            user32.GetClassNameW(foreground, native_class, len(native_class))
        print(json.dumps({
            'native_input_diagnostic': reason,
            'main_loop_exists': gui.GetMainLoop() is not None,
            'wx_focus': current.GetName() if current is not None else None,
            'wx_focus_class': type(current).__name__ if current is not None else None,
            'target': target.GetName() if target is not None else None,
            'target_handle': int(target.GetHandle()) if target is not None else None,
            'top_handle': int(top.GetHandle()) if top is not None else None,
            'top_active': top.IsActive() if top is not None else None,
            'foreground_handle': int(foreground or 0),
            'foreground_class': native_class.value,
            'overwrite_prompt_handle': int(overwrite_prompt() or 0),
            'thread_focus_handle': int(user32.GetFocus() or 0),
            'thread_focus_control_id': user32.GetDlgCtrlID(user32.GetFocus()),
            'thread_active_handle': int(user32.GetActiveWindow() or 0),
            'recent_key_events': list(key_events),
        }, sort_keys=True), flush=True)

    def observe_keys(control):
        for event_type, name in ((wx.EVT_KEY_DOWN, 'down'), (wx.EVT_KEY_UP, 'up'),
                                 (wx.EVT_CHAR, 'char')):
            def observe(event, kind=name):
                key_events.append({
                    'event': kind, 'key': event.GetKeyCode(),
                    'control': event.GetEventObject().GetName(),
                })
                event.Skip()
            control.Bind(event_type, observe)

    def abort_native_run(error, reason):
        failures.append(error)
        print(json.dumps({'result': 'FAIL', 'error': error, 'native_step': reason}), flush=True)
        if failure_exit[0] is None:
            # A native TaskDialogIndirect loop may outlive wx.ExitMainLoop().
            # Fail within a bound even if its window loses foreground or the
            # GUI cannot unwind; never manufacture an answer or a passing check.
            failure_exit[0] = threading.Timer(3, lambda: os._exit(1))
            failure_exit[0].daemon = True
            failure_exit[0].start()
        try:
            diagnostics(reason)
        finally:
            gui.ExitMainLoop()

    def pump_until(predicate, explanation, timeout=12):
        """Yield to MainLoop between checks; never replace native dispatch."""
        deadline = monotonic() + timeout
        while True:
            if failures:
                raise RuntimeError('\n'.join(failures))
            if predicate():
                return
            if monotonic() >= deadline:
                diagnostics(explanation)
                raise TimeoutError(explanation)
            yield 20

    def key(code, modifiers=wx.MOD_NONE):
        if modifiers not in (wx.MOD_NONE, wx.MOD_SHIFT):
            raise ValueError('The probe only needs plain keys and Shift.')
        diagnostics('before native key ' + str(code))
        shift = modifiers == wx.MOD_SHIFT
        if shift and not simulator.KeyDown(wx.WXK_SHIFT):
            raise AssertionError('Native Shift key-down was rejected.')
        try:
            if not simulator.KeyDown(code):
                raise AssertionError('The native keyboard simulator rejected key-down.')
            try:
                # Let the real MSW event loop translate and dispatch key-down
                # before its paired release, just as in the advanced UI probe.
                yield 40
            finally:
                if not simulator.KeyUp(code):
                    raise AssertionError('The native keyboard simulator rejected key-up.')
        finally:
            if shift:
                simulator.KeyUp(wx.WXK_SHIFT)
        yield 80

    def focus(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        # wx focus belongs to our GUI thread; native input additionally needs
        # its top-level window to own the real foreground input queue.
        activated = bool(user32.SetForegroundWindow(top.GetHandle()))
        control.SetFocus()
        yield from pump_until(
            lambda: focused(control) and user32.GetForegroundWindow() == top.GetHandle(),
            'Native keyboard focus or foreground ownership was not acquired.')
        diagnostics('native focus ready; foreground request=' + str(activated), control)

    def press(button):
        check('native action button is keyboard reachable', button.AcceptsFocusFromKeyboard())
        yield from focus(button)
        yield from key(wx.WXK_SPACE)

    def clipboard_text(value=None):
        if not wx.TheClipboard.Open():
            raise AssertionError('The Windows clipboard was unavailable.')
        try:
            if value is not None:
                if not wx.TheClipboard.SetData(wx.TextDataObject(value)):
                    raise AssertionError('The Windows clipboard rejected text.')
                return value
            data = wx.TextDataObject()
            if not wx.TheClipboard.GetData(data):
                raise AssertionError('The Windows clipboard did not contain text.')
            return data.GetText().replace('\r\n', '\n')
        finally:
            wx.TheClipboard.Close()

    def rejected(operation, exception, reason):
        try:
            operation()
        except exception:
            check(reason, True)
        else:
            raise AssertionError(reason)

    class ObservedExecutor(ThreadPoolExecutor):
        """Run genuine work; record thread identity and controls at submission."""
        def __init__(self):
            super().__init__(max_workers=1, thread_name_prefix='native-bibliography')
            self.jobs = []
            self.observe = lambda: {}

        def submit(self, operation, *args, **kwargs):
            job = {'submitted_on': threading.get_ident(), **self.observe()}
            self.jobs.append(job)

            def run():
                job['executed_on'] = threading.get_ident()
                return operation(*args, **kwargs)

            job['future'] = super().submit(run)
            return job['future']

    installed = Path(sysconfig.get_path('purelib')).resolve()
    modules = (academic_library_dialog, citation_styles_dialog, bibliography_workbench,
               bibtex_import, csl)
    check('isolated installed CPython 3.11 x64 is running',
          sys.flags.isolated == 1 and sys.version_info[:2] == (3, 11)
          and struct.calcsize('P') == 8 and sys.prefix != sys.base_prefix)
    check('all feature modules come from this environment site-packages',
          all(Path(module.__file__).resolve().is_relative_to(installed) for module in modules))
    check('wx uses native Windows controls', wx.Platform == '__WXMSW__')
    check('the pinned bibliography and native dependencies are installed',
          all(version(name) == expected for name, expected in (
              ('wxPython', '4.2.4'), ('bibtexparser', '2.1.0'),
              ('pylatexenc', '2.11'), ('quickjs-ng', '0.18.0.2'))))

    frame = review = dialog = None
    executor = ObservedExecutor()
    release = threading.Event()
    with TemporaryDirectory(prefix='research-bibliography-native-') as temporary:
        root = Path(temporary)
        try:
            frame = wx.Frame(None, title='Synthetic bibliography acceptance', size=(900, 850))
            gui.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            completed = []
            next_tick = [None]

            def scenarios():
                nonlocal review, dialog
                library = LibraryApp(root / 'private library')
                workspace = library.create_workspace('Synthetic bibliography library')
                other_workspace = library.create_workspace('Separate synthetic library')
                controller = academic_library_dialog.AcademicController(library, workspace)
                other = academic_library_dialog.AcademicController(library, other_workspace)
                other.create({'title': 'ForeignOnlyMarker'})
                archived = controller.create({'title': 'ArchivedOnlyMarker'})
                controller.archive(archived)
                source = library.add_text(workspace, 'Retained synthetic source',
                    'Exact railway source wording. Український текст.', authors=('Source Author',))
                retained = library.assets.resolve(source['asset']).read_bytes()
                topic = library.create_topic(workspace, 'Original evidence')
                hit, = library.search(workspace, 'railway')
                evidence = library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original source note')

                incoming = controller.preview(r'''
    @string{press = "Synthetic " # "Press"}
    @book{NativeCiteKey,
      author = {M{\"u}ller, Jane},
      title = {Native bibliography -- Український текст},
      year = {2026}, publisher = press, pages = {11--99},
      note = {NativePrivateNote}, customfield = {Preserve this source metadata}
    }
    @book{UnselectedKey, title = {UnselectedNativeMarker}, year = {2025}}
    '''.encode('utf-8'), 'bibtex')
                check('BibTeX preview resolves safe macros and Unicode without importing',
                      len(incoming.entries) == 2 and not controller.list()
                      and incoming.entries[0].authors == ('Müller, Jane',)
                      and incoming.entries[0].fields['publisher'] == 'Synthetic Press')
                _, _, ImportPreviewDialog, _, _, _ = academic_library_dialog._native_types()
                review = ImportPreviewDialog(frame, controller, incoming)
                observe_keys(review.entries)
                observe_keys(review.import_button)
                review.Show()
                review.Raise()
                check('native BibTeX review starts with no selected imports',
                      isinstance(review.entries, wx.CheckListBox)
                      and not any(review.entries.IsChecked(i) for i in range(2)))
                check('native BibTeX details expose key provenance and unsupported metadata',
                      'NativeCiteKey' in review.details.GetValue()
                      and 'customfield' in review.details.GetValue()
                      and bool(review.details.GetWindowStyleFlag() & wx.TE_READONLY))
                yield from focus(review.entries)
                yield from key(wx.WXK_SPACE)
                yield from pump_until(lambda: review.entries.IsChecked(0), 'Space did not select the BibTeX entry.')
                check('Space selects exactly the intended native import row',
                      not review.entries.IsChecked(1))
                yield from press(review.import_button)
                yield from pump_until(lambda: review.result is not None and not review.IsShown(),
                           'The native import button did not complete.')
                reference = controller.get(review.result.imported_ids[0])
                check('native BibTeX import keeps an application identity and source key provenance',
                      len(controller.list()) == 1 and reference.id != 'NativeCiteKey'
                      and 'NativeCiteKey' in reference.notes
                      and not controller.list(query='UnselectedNativeMarker'))
                review.Destroy()
                review = None
                corporation = controller.create({
                    'title': 'Corporate synthetic report',
                    'authors': ('{Research and Development, Inc.}',),
                    'year': '2025', 'publisher': 'Synthetic Press',
                })

                dialog = citation_styles_dialog.create_citation_styles_dialog(
                    frame, controller, selected_identity=reference.id, executor=executor)
                for control in (*dialog.options, dialog.generate, dialog.copy_bibliography,
                                dialog.copy_citations, dialog.close_button):
                    observe_keys(control)
                executor.observe = lambda: {
                    'busy_controls': not dialog.generate.IsEnabled()
                    and all(not control.IsEnabled() for control in dialog.options),
                    'publication_disabled': all(not control.IsEnabled() for control in (
                        dialog.copy_bibliography, dialog.copy_citations, dialog.export_button)),
                    'close_enabled': dialog.close_button.IsEnabled(),
                }
                dialog.style_choice.SetSelection(next(
                    i for i, style in enumerate(dialog.styles) if style.id == 'harvard'))
                dialog.on_options_changed()
                dialog.Show()
                dialog.Raise()
                yield from focus(dialog.style_choice)
                check('citation controls have explicit accessible names and read-only results',
                      dialog.style_choice.GetName() == 'Citation style'
                      and 'independent of list filters' in dialog.scope_choice.GetName()
                      and dialog.output.GetName() == 'Formatted bibliography and citation examples'
                      and dialog.status.GetName() == 'Citation formatting status'
                      and bool(dialog.output.GetWindowStyleFlag() & wx.TE_READONLY)
                      and bool(dialog.status.GetWindowStyleFlag() & wx.TE_READONLY))
                check('copy and export require a completed preview',
                      not any(button.IsEnabled() for button in (
                          dialog.copy_bibliography, dialog.copy_citations, dialog.export_button)))
                yield from key(wx.WXK_TAB)
                yield from pump_until(lambda: focused(dialog.scope_choice), 'Tab did not move from style to scope.')
                check('real Tab traverses from citation style to reference scope', True)
                yield from key(wx.WXK_TAB, wx.MOD_SHIFT)
                yield from pump_until(lambda: focused(dialog.style_choice), 'Shift+Tab did not return to style.')
                check('real Shift+Tab returns from reference scope to citation style', True)

                def preview(*, success=True):
                    count = len(executor.jobs)
                    yield from press(dialog.generate)
                    yield from pump_until(lambda: len(executor.jobs) > count,
                               'Space did not start an asynchronous formatting job.')
                    job = executor.jobs[-1]
                    check('async formatting disables changing options while keeping Close available',
                          job['busy_controls'] and job['close_enabled'])
                    yield from pump_until(lambda: job['future'].done() and dialog.pending is None,
                               'The native formatting job did not settle.', timeout=45)
                    check('real formatting executes outside the native UI thread',
                          job['submitted_on'] == main_thread and job['executed_on'] != main_thread)
                    if success:
                        check('completed preview is readable, focused and enables publication actions',
                              dialog.preview is not None and focused(dialog.output)
                              and dialog.output.GetValue() == dialog.preview.explanation
                              and all(button.IsEnabled() for button in (
                                  dialog.copy_bibliography, dialog.copy_citations, dialog.export_button)))
                    else:
                        check('async validation failure is focused and leaves no publishable preview',
                              dialog.preview is None and focused(dialog.status)
                              and 'Formatting not completed:' in dialog.status.GetValue()
                              and not dialog.copy_bibliography.IsEnabled()
                              and not dialog.export_button.IsEnabled())
                    return dialog.preview

                selected = yield from preview()
                check('selected-reference preview preserves literal names and excludes other records',
                      selected.identity == reference.id and len(selected.result.entries) == 1
                      and 'Müller, Jane' in selected.bibliography
                      and 'Corporate synthetic report' not in selected.explanation
                      and 'NativePrivateNote' not in selected.explanation
                      and '11-99' not in selected.citations and '11–99' not in selected.citations)
                yield from press(dialog.copy_bibliography)
                yield from pump_until(lambda: dialog.status.GetValue() == 'Bibliography copied.',
                           'Native bibliography copy did not complete.')
                check('native bibliography button copies exactly the current formatted text',
                      clipboard_text() == selected.bibliography and focused(dialog.status))
                yield from press(dialog.copy_citations)
                yield from pump_until(lambda: dialog.status.GetValue() == 'Citation examples copied.',
                           'Native citation copy did not complete.')
                check('native citation button copies exactly the current citation examples',
                      clipboard_text() == selected.citations and focused(dialog.status))

                clipboard_text('Retain clipboard after stale metadata')
                reference = controller.update(reference, {'publisher': 'Revised Synthetic Press'})
                yield from press(dialog.copy_bibliography)
                yield from pump_until(lambda: dialog.status.GetValue().startswith('Text not copied:'),
                           'The stale metadata rejection did not arrive.')
                check('changed metadata prevents native clipboard replacement and focuses the reason',
                      clipboard_text() == 'Retain clipboard after stale metadata'
                      and 'changed' in dialog.status.GetValue() and focused(dialog.status))
                stale_destination = root / 'stale bibliography.txt'
                rejected(lambda: dialog.workbench.export(selected, stale_destination), ValueError,
                         'changed metadata also prevents service export')
                check('rejected stale export leaves no output', not stale_destination.exists())

                yield from focus(dialog.scope_choice)
                yield from key(wx.WXK_HOME)
                yield from pump_until(lambda: dialog.scope_choice.GetSelection() == 0 and dialog.preview is None,
                           'The native scope change did not invalidate the preview.')
                yield from focus(dialog.names_choice)
                yield from key(wx.WXK_END)
                yield from pump_until(lambda: dialog.names_choice.GetSelection() == 1,
                           'The native name convention choice did not change.')
                formatted = yield from preview()
                check('all-active scope excludes archived and foreign references and private notes',
                      formatted.identity is None and len(formatted.result.entries) == 2
                      and all(marker not in formatted.explanation for marker in (
                          'ArchivedOnlyMarker', 'ForeignOnlyMarker', 'NativePrivateNote', 'UnselectedNativeMarker')))
                check('explicit personal names format while protected corporate names remain literal',
                      'Müller, J.' in formatted.bibliography
                      and 'Research and Development, Inc.' in formatted.bibliography
                      and '{Research and Development' not in formatted.bibliography
                      and controller.get(reference.id) == reference
                      and controller.get(corporation.id) == corporation)
                foreign = bibliography_workbench.BibliographyWorkbench(other)
                rejected(lambda: foreign.copy_text(formatted), ValueError,
                         'a preview cannot be copied through another library controller')

                destination = root / 'formatted bibliography.txt'
                text = dialog.workbench.export(formatted, destination, require_absent=True)
                check('installed service exports exact UTF-8 plain text',
                      destination.read_bytes() == text.encode('utf-8')
                      and text == formatted.bibliography)
                before = destination.read_bytes()
                rejected(lambda: dialog.workbench.export(formatted, destination, require_absent=True),
                         (ValueError, FileExistsError), 'create-only export refuses an existing destination')
                check('refused create-only export preserves the existing bytes', destination.read_bytes() == before)
                html = dialog.workbench.export(formatted, root / 'formatted bibliography.html',
                                              format='html', require_absent=True)
                check('installed service exports inert self-contained HTML',
                      'Content-Security-Policy' in html and '<script' not in html
                      and 'href=' not in html and 'Український текст' in html)
                word = dialog.workbench.export(formatted, root / 'formatted bibliography.docx',
                                              format='docx', require_absent=True)
                from docx import Document
                document = Document(BytesIO(word))
                check('installed service exports an editable Word bibliography',
                      document.paragraphs[0].text == 'Bibliography'
                      and any('Український текст' in paragraph.text for paragraph in document.paragraphs))
                with ZipFile(BytesIO(word)) as archive:
                    check('Word bibliography contains no external relationships',
                          all(b'TargetMode="External"' not in archive.read(name)
                              for name in archive.namelist() if name.endswith('.rels')))
                cancelled_destination = root / 'cancelled bibliography.txt'
                rejected(lambda: dialog.workbench.export(formatted, cancelled_destination,
                         cancelled=lambda: True), TimeoutError, 'cancelled export reports cancellation')
                check('cancelled export creates no destination', not cancelled_destination.exists())

                # Exercise the actual export button and both real modal dialogs.
                # Only the file chooser's selected temporary path is controlled;
                # format selection and overwrite answers use native keys.
                selected_export_path = [None]
                export_pickers = []
                original_picker = wx.FileDialog
                native_format_dialog = wx.SingleChoiceDialog
                native_message_dialog = wx.MessageDialog

                class ChosenExportFile:
                    def __init__(self, parent, message, **options):
                        check('native export action requests only its explicit save picker',
                              parent is dialog and message == 'Save formatted bibliography'
                              and options['style'] & wx.FD_SAVE
                              and not options['style'] & wx.FD_OVERWRITE_PROMPT)
                        self.path = selected_export_path[0]
                        self.destroyed = False
                        export_pickers.append(self)

                    def ShowModal(self):
                        return wx.ID_OK

                    def GetPath(self):
                        return str(self.path)

                    def Destroy(self):
                        self.destroyed = True

                def format_dialog():
                    return next((window for window in wx.GetTopLevelWindows()
                                 if isinstance(window, native_format_dialog)
                                 and window.GetParent() is dialog
                                 and window.IsShown() and window.IsModal()
                                 and window.GetTitle() == 'Export bibliography'), None)

                def begin_native_export(path, format_name='text'):
                    selected_export_path[0] = path
                    count = len(executor.jobs)
                    yield from press(dialog.export_button)
                    yield from pump_until(lambda: format_dialog() is not None,
                               'The real export format dialog did not open.')
                    choice = format_dialog()
                    children = list(choice.GetChildren())
                    for child in children:
                        children.extend(child.GetChildren())
                    choices = next((child for child in children if isinstance(child, wx.ListBox)), None)
                    check('export format uses the real native modal list and original choices',
                          choices is not None and choices.GetStrings() == [
                              label for _, label, _ in bibliography_workbench.EXPORT_FORMATS])
                    observe_keys(choices)
                    yield from focus(choices)
                    yield from key(wx.WXK_HOME)
                    index = next(i for i, item in enumerate(bibliography_workbench.EXPORT_FORMATS)
                                 if item[0] == format_name)
                    for _ in range(index):
                        yield from key(wx.WXK_DOWN)
                    yield from pump_until(lambda: choices.GetSelection() == index,
                               'Native keys did not select the intended export format.')
                    check('native format selection matches the requested export',
                          choices.GetStringSelection() == bibliography_workbench.EXPORT_FORMATS[index][1])
                    confirm = wx.Window.FindWindowById(wx.ID_OK, choice)
                    check('the real format dialog exposes its native OK button', isinstance(confirm, wx.Button))
                    yield from press(confirm)
                    return count

                def real_overwrite_prompt():
                    yield from pump_until(lambda: overwrite_prompt() is not None,
                               'The real native overwrite confirmation did not open.')
                    handle = overwrite_prompt()
                    native_class = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(handle, native_class, len(native_class))
                    check('overwrite confirmation is the owned foreground native modal dialog',
                          user32.GetWindow(handle, 4) == dialog.GetHandle()
                          and native_class.value == '#32770'
                          and not user32.IsWindowEnabled(dialog.GetHandle()))
                    # The real CI TaskDialog exposed neither classic button HWNDs
                    # nor UIA Button descendants through our observer. Do not
                    # claim accessible button focus. Prove the actual choices by
                    # native keys and the resulting worker/file behavior below.
                    return handle

                def answer_yes(handle):
                    if overwrite_prompt() != handle:
                        raise AssertionError('The native overwrite question changed before the keyboard answer.')
                    count = len(executor.jobs)
                    yield from key(wx.WXK_TAB, wx.MOD_SHIFT)
                    yield from key(wx.WXK_RETURN)
                    yield from pump_until(lambda: overwrite_prompt() is None
                                          and len(executor.jobs) == count + 1,
                               'Shift+Tab and Enter did not approve and queue the native export.')
                    check('real Shift+Tab and Enter choose Yes and submit exactly one export worker',
                          len(executor.jobs) == count + 1 and overwrite_prompt() is None)

                def export_finished(count):
                    yield from pump_until(lambda: len(executor.jobs) == count + 1,
                               'The native export action did not submit exactly one real worker.')
                    job = executor.jobs[-1]
                    yield from pump_until(lambda: job['future'].done() and dialog.pending is None,
                               'The native export worker did not settle.', timeout=45)
                    check('native export runs in the actual worker with publication controls disabled',
                          job['submitted_on'] == main_thread and job['executed_on'] != main_thread
                          and job['busy_controls'] and job['publication_disabled'] and job['close_enabled'])
                    return job

                wx.FileDialog = ChosenExportFile
                try:
                    declined = root / 'native declined overwrite.txt'
                    declined.write_bytes(b'Existing file retained after default No')
                    count = yield from begin_native_export(declined)
                    yield from real_overwrite_prompt()
                    yield from key(wx.WXK_RETURN)
                    yield from pump_until(lambda: overwrite_prompt() is None
                                          and user32.GetForegroundWindow() == dialog.GetHandle(),
                               'Enter did not dismiss the real default-No confirmation.')
                    check('default No declines without queuing work or changing the selected file',
                          len(executor.jobs) == count and dialog.pending is None
                          and declined.read_bytes() == b'Existing file retained after default No'
                          and dialog.export_button.IsEnabled())

                    changed = root / 'native changed during confirmation.txt'
                    changed.write_bytes(b'Original file shown for replacement')
                    count = yield from begin_native_export(changed)
                    handle = yield from real_overwrite_prompt()
                    competing = b'Newer work saved while the real confirmation is open'
                    changed.write_bytes(competing)
                    check('the competing save occurs while the actual overwrite question remains open',
                          overwrite_prompt() == handle and changed.read_bytes() == competing)
                    yield from answer_yes(handle)
                    job = yield from export_finished(count)
                    check('real Yes after a competing save fails closed and preserves the newer bytes',
                          isinstance(job['future'].exception(), ValueError)
                          and changed.read_bytes() == competing
                          and 'Bibliography not exported:' in dialog.status.GetValue()
                          and 'destination changed' in dialog.status.GetValue() and focused(dialog.status))

                    approved = root / 'native approved overwrite.txt'
                    approved.write_bytes(b'Unchanged file explicitly approved for replacement')
                    count = yield from begin_native_export(approved)
                    handle = yield from real_overwrite_prompt()
                    yield from answer_yes(handle)
                    job = yield from export_finished(count)
                    check('real Yes replaces only the unchanged approved destination',
                          approved.read_bytes() == job['future'].result().encode('utf-8')
                          and approved.read_text(encoding='utf-8') == formatted.bibliography
                          and dialog.status.GetValue() == f'Bibliography saved to {approved}.'
                          and focused(dialog.status))

                    fresh = root / 'native new bibliography.html'
                    check('the native new-export scenario starts with an absent destination', not fresh.exists())
                    count = yield from begin_native_export(fresh, 'html')
                    job = yield from export_finished(count)
                    check('real format selection and native export publish a fresh HTML file through the worker',
                          overwrite_prompt() is None
                          and fresh.read_bytes() == job['future'].result().encode('utf-8')
                          and 'Content-Security-Policy' in job['future'].result()
                          and 'Український текст' in job['future'].result()
                          and dialog.status.GetValue() == f'Bibliography saved to {fresh}.'
                          and focused(dialog.status))
                    check('native export scenarios dispose controlled pickers without replacing real dialogs',
                          len(export_pickers) == 4 and all(item.destroyed for item in export_pickers)
                          and wx.SingleChoiceDialog is native_format_dialog
                          and wx.MessageDialog is native_message_dialog
                          and not list(root.glob('.research-export-*')))
                finally:
                    wx.FileDialog = original_picker

                # Only the file-picker choice is controlled. The native button,
                # bound load handler, filesystem loader and CSL engine remain real.
                custom_path = root / 'native custom style.csl'
                custom_path.write_bytes(CUSTOM_STYLE)
                chosen_files = []
                original_picker = wx.FileDialog

                class ChosenStyleFile:
                    def __init__(self, parent, message, **options):
                        check('native custom-style action requests its explicit local picker',
                              parent is dialog and message == 'Open a custom CSL style'
                              and options['style'] & wx.FD_OPEN
                              and options['style'] & wx.FD_FILE_MUST_EXIST)
                        self.destroyed = False
                        chosen_files.append(self)

                    def ShowModal(self):
                        return wx.ID_OK

                    def GetPath(self):
                        return str(custom_path)

                    def Destroy(self):
                        self.destroyed = True

                wx.FileDialog = ChosenStyleFile
                try:
                    yield from press(dialog.custom_style_button)
                    yield from pump_until(lambda: dialog.custom_style is not None,
                               'The native custom-style action did not load its selected file.')
                    custom = dialog.custom_style
                    check('real local style loading captures exact bytes and updates the native selector',
                          isinstance(custom, csl.CustomStyle) and custom.source == CUSTOM_STYLE
                          and custom.sha256 == sha256(CUSTOM_STYLE).hexdigest()
                          and dialog.style_choice.GetCount() == len(dialog.styles) + 1
                          and dialog.style_choice.GetSelection() == len(dialog.styles)
                          and focused(dialog.style_choice)
                          and dialog.preview is None and not dialog.copy_bibliography.IsEnabled())
                    custom_preview = yield from preview()
                    check('native preview uses the actual custom CSL engine and both active references',
                          custom_preview.style is custom and custom_preview.result.custom_style
                          and custom_preview.result.style_sha256 == custom.sha256
                          and len(custom_preview.result.entries) == 2
                          and all(entry.startswith('NATIVE LOCAL: ') for entry in custom_preview.result.entries)
                          and '[Native local: ' in custom_preview.citations)
                    captured_explanation = dialog.output.GetValue()
                    custom_path.write_bytes(b'<invalid>changed after the user selected the style</invalid>')
                    custom_html = dialog.workbench.export(
                        custom_preview, root / 'native custom bibliography.html',
                        format='html', require_absent=True)
                    check('custom export preserves the captured style after its file changes',
                          'NATIVE LOCAL: ' in custom_html and 'Український текст' in custom_html
                          and 'changed after the user selected' not in custom_html
                          and custom.source == CUSTOM_STYLE and dialog.preview is custom_preview)
                    yield from press(dialog.custom_style_button)
                    yield from pump_until(lambda: dialog.status.GetValue().startswith('Custom style not loaded:'),
                               'Invalid custom-style selection did not report its real loader failure.')
                    check('native custom-style load failure preserves the valid snapshot and preview',
                          dialog.custom_style is custom and dialog.preview is custom_preview
                          and dialog.output.GetValue() == captured_explanation
                          and dialog.copy_bibliography.IsEnabled() and dialog.export_button.IsEnabled()
                          and focused(dialog.status))
                    check('the controlled file choices have both been disposed',
                          len(chosen_files) == 2 and all(item.destroyed for item in chosen_files))
                finally:
                    wx.FileDialog = original_picker
                # Restore Harvard using actual choice keys before the later error
                # and cancellation scenarios, which must remain independent.
                yield from focus(dialog.style_choice)
                harvard_index = next(i for i, style in enumerate(dialog.styles) if style.id == 'harvard')
                for expected in range(len(dialog.styles) - 1, harvard_index - 1, -1):
                    yield from key(wx.WXK_UP)
                    yield from pump_until(lambda index=expected: dialog.style_choice.GetSelection() == index,
                               'Native keyboard style selection did not move toward Harvard.')
                check('native keyboard selection returns from the custom style to Harvard',
                      dialog.style_choice.GetSelection() == harvard_index and dialog.preview is None)

                # A real stored name violates the user's explicit comma convention;
                # the actual worker must return this failure to the native status.
                reference = controller.update(reference, {'authors': ('Too, Many, Name, Parts',)})
                yield from preview(success=False)
                reference = controller.update(reference, {'authors': ('Müller, Jane',)})
                yield from preview()

                # Hold a genuine single-worker executor briefly so cancellation of a
                # queued native preview is deterministic, independent of CSL speed.
                entered = threading.Event()

                def queue_blocker():
                    entered.set()
                    if not release.wait(20):
                        raise TimeoutError('Native cancellation scenario did not release its queue.')

                blocker = executor.submit(queue_blocker)
                yield from pump_until(entered.is_set, 'The cancellation queue did not start.')
                count = len(executor.jobs)
                yield from press(dialog.generate)
                yield from pump_until(lambda: len(executor.jobs) > count and dialog.pending is not None,
                           'The cancellable native preview was not queued.')
                pending = dialog.pending
                yield from key(wx.WXK_ESCAPE)
                yield from pump_until(lambda: not dialog.IsShown(), 'Escape did not close the native citation dialog.')
                release.set()
                yield from pump_until(lambda: blocker.done() and pending.done(),
                           'Closing the native dialog did not settle the queued job.')
                blocker.result()
                check('Escape closes the native dialog and cancels its queued real worker job',
                      dialog.cancelled.is_set() and pending.cancelled())
                check('a cancelled callback cannot publish a preview after the window closes',
                      dialog.preview is None and dialog.output.GetValue() == '')
                check('bibliography work preserves retained originals and exact saved source evidence',
                      library.assets.resolve(source['asset']).read_bytes() == retained
                      and library.evidence(workspace, topic)[0].evidence_id == evidence
                      and library.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote)
                check('all bibliography operations stayed offline', not network_attempts)
                print(json.dumps({
                    'result': 'PASS', 'checks': len(checks), 'python': platform.python_version(),
                    'platform': platform.platform(), 'wx': wx.version(), 'style': 'harvard',
                    'installed_modules': {
                        module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
                        for module in modules
                    },
                    'native_keyboard': 'PASS', 'native_clipboard': 'PASS',
                    'native_export_format': 'PASS', 'native_overwrite_confirmation': 'PASS',
                    'native_overwrite_accessible_focus': 'NOT_RUN',
                    'export_file_choice': 'controlled temporary paths only',
                    'native_custom_style': 'PASS', 'custom_style_file_choice': 'controlled path only',
                    'service_exports': ['text', 'html', 'docx'],
                    'manual_keyboard': 'NOT_RUN', 'native_file_pickers': 'NOT_RUN',
                    'nvda': 'NOT_RUN', 'installer': 'NOT_RUN',
                }, sort_keys=True), flush=True)

            scenario = scenarios()

            def advance():
                try:
                    delay = next(scenario)
                except StopIteration:
                    completed.append(True)
                    gui.ExitMainLoop()
                except BaseException:
                    abort_native_run(traceback.format_exc(), 'native scenario failed')
                else:
                    next_tick[0] = wx.CallLater(delay, advance)

            def expired():
                abort_native_run('Native bibliography acceptance exceeded 150 seconds.',
                                 'native acceptance watchdog')

            watchdog = wx.CallLater(150000, expired)
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
                raise RuntimeError('The native event loop stopped before completing every scenario.')
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=True)
            if dialog is not None:
                dialog.Destroy()
            if review is not None:
                review.Destroy()
            if frame is not None:
                frame.Destroy()
            gui.Yield()
            if failure_exit[0] is not None:
                failure_exit[0].cancel()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({
            'result': 'FAIL', 'error': traceback.format_exc(),
            'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
        }), flush=True)
        raise SystemExit(1)
