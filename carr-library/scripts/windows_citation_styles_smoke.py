"""Exercise installed BibTeX and citation controls on a real Windows desktop.

Run with python -I scripts/windows_citation_styles_smoke.py after installing
the application wheel. All records and exported documents are synthetic and
temporary. This checks native simulated keys and the Windows clipboard, not
manual keyboard use, native file pickers, NVDA speech or a packaged installer.
"""
from concurrent.futures import ThreadPoolExecutor
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
from time import monotonic, sleep
import traceback
from zipfile import ZipFile


def main():
    if os.name != 'nt':
        print(json.dumps({
            'result': 'NOT_RUN', 'reason': 'A native Windows session is required.',
            'native_keyboard': 'NOT_RUN', 'native_clipboard': 'NOT_RUN',
            'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
        }), flush=True)
        return 77

    from carr_library.native_runtime import activate_bundled_runtime
    activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.citations import bibliography_workbench, csl
    from carr_library.research import bibtex_import
    from carr_library.ui import academic_library_dialog, citation_styles_dialog
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND

    checks, failures, network_attempts = [], [], []

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
            failures.append(traceback.format_exc())
            return False

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    main_thread = threading.get_ident()

    def pump_until(predicate, explanation, timeout=12):
        deadline = monotonic() + timeout
        while True:
            gui.Yield()
            if failures:
                raise RuntimeError('\n'.join(failures))
            if predicate():
                return
            if monotonic() >= deadline:
                raise TimeoutError(explanation)
            sleep(0.01)

    def key(code, modifiers=wx.MOD_NONE):
        # Char pairs native key-down/key-up, including modifier release on MSW.
        if not simulator.Char(code, modifiers):
            raise AssertionError('The native keyboard simulator rejected a key.')

    def focus(control):
        # GetFocus (and thus wx.Window.FindFocus) is thread-local on MSW. It
        # can identify a control in a background window while keybd_event,
        # used by UIActionSimulator, sends input to another foreground window.
        # Complete activation first, then give the intended child focus.
        top = wx.GetTopLevelParent(control)
        top.Raise()
        pump_until(lambda: top.IsActive()
                   and user32.GetForegroundWindow() == top.GetHandle(),
                   'The native probe window did not acquire Windows foreground activation.')
        control.SetFocus()
        pump_until(lambda: focused(control) and top.IsActive()
                   and user32.GetForegroundWindow() == top.GetHandle(),
                   'Native keyboard focus was not acquired in the foreground window.')

    def press(button):
        check('native action button is keyboard reachable', button.AcceptsFocusFromKeyboard())
        focus(button)
        key(wx.WXK_SPACE)

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

            frame = wx.Frame(None, title='Synthetic bibliography acceptance', size=(900, 850))
            gui.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
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
            review.Show()
            review.Raise()
            check('native BibTeX review starts with no selected imports',
                  isinstance(review.entries, wx.CheckListBox)
                  and not any(review.entries.IsChecked(i) for i in range(2)))
            check('native BibTeX details expose key provenance and unsupported metadata',
                  'NativeCiteKey' in review.details.GetValue()
                  and 'customfield' in review.details.GetValue()
                  and bool(review.details.GetWindowStyleFlag() & wx.TE_READONLY))
            focus(review.entries)
            check('the focused native import row is the first unchecked entry',
                  review.entries.GetSelection() == 0 and not review.entries.IsChecked(0))
            received_space = []

            def observe_space(event):
                if event.GetKeyCode() == wx.WXK_SPACE:
                    received_space.append((focused(review.entries), review.entries.GetSelection()))
                # Observe native dispatch without replacing the CheckListBox's
                # own key handling or changing any check state in the probe.
                event.Skip()

            review.entries.Bind(wx.EVT_KEY_DOWN, observe_space)
            try:
                key(wx.WXK_SPACE)
                pump_until(lambda: bool(received_space),
                           'Windows did not deliver the simulated Space key to the focused import list.')
            finally:
                review.entries.Unbind(wx.EVT_KEY_DOWN, handler=observe_space)
            check('native Space reaches the selected import row', received_space == [(True, 0)])
            pump_until(lambda: review.entries.IsChecked(0), 'Space did not select the BibTeX entry.')
            check('Space selects exactly the intended native import row',
                  not review.entries.IsChecked(1))
            press(review.import_button)
            pump_until(lambda: review.result is not None and not review.IsShown(),
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
            executor.observe = lambda: {
                'busy_controls': not dialog.generate.IsEnabled()
                and all(not control.IsEnabled() for control in dialog.options),
                'close_enabled': dialog.close_button.IsEnabled(),
            }
            dialog.style_choice.SetSelection(next(
                i for i, style in enumerate(dialog.styles) if style.id == 'harvard'))
            dialog.on_options_changed()
            dialog.Show()
            dialog.Raise()
            focus(dialog.style_choice)
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
            key(wx.WXK_TAB)
            pump_until(lambda: focused(dialog.scope_choice), 'Tab did not move from style to scope.')
            check('real Tab traverses from citation style to reference scope', True)
            key(wx.WXK_TAB, wx.MOD_SHIFT)
            pump_until(lambda: focused(dialog.style_choice), 'Shift+Tab did not return to style.')
            check('real Shift+Tab returns from reference scope to citation style', True)

            def preview(*, success=True):
                count = len(executor.jobs)
                press(dialog.generate)
                pump_until(lambda: len(executor.jobs) > count,
                           'Space did not start an asynchronous formatting job.')
                job = executor.jobs[-1]
                check('async formatting disables changing options while keeping Close available',
                      job['busy_controls'] and job['close_enabled'])
                pump_until(lambda: job['future'].done() and dialog.pending is None,
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

            selected = preview()
            check('selected-reference preview preserves literal names and excludes other records',
                  selected.identity == reference.id and len(selected.result.entries) == 1
                  and 'Müller, Jane' in selected.bibliography
                  and 'Corporate synthetic report' not in selected.explanation
                  and 'NativePrivateNote' not in selected.explanation
                  and '11-99' not in selected.citations and '11–99' not in selected.citations)
            press(dialog.copy_bibliography)
            pump_until(lambda: dialog.status.GetValue() == 'Bibliography copied.',
                       'Native bibliography copy did not complete.')
            check('native bibliography button copies exactly the current formatted text',
                  clipboard_text() == selected.bibliography and focused(dialog.status))
            press(dialog.copy_citations)
            pump_until(lambda: dialog.status.GetValue() == 'Citation examples copied.',
                       'Native citation copy did not complete.')
            check('native citation button copies exactly the current citation examples',
                  clipboard_text() == selected.citations and focused(dialog.status))

            clipboard_text('Retain clipboard after stale metadata')
            reference = controller.update(reference, {'publisher': 'Revised Synthetic Press'})
            press(dialog.copy_bibliography)
            pump_until(lambda: dialog.status.GetValue().startswith('Text not copied:'),
                       'The stale metadata rejection did not arrive.')
            check('changed metadata prevents native clipboard replacement and focuses the reason',
                  clipboard_text() == 'Retain clipboard after stale metadata'
                  and 'changed' in dialog.status.GetValue() and focused(dialog.status))
            stale_destination = root / 'stale bibliography.txt'
            rejected(lambda: dialog.workbench.export(selected, stale_destination), ValueError,
                     'changed metadata also prevents service export')
            check('rejected stale export leaves no output', not stale_destination.exists())

            focus(dialog.scope_choice)
            key(wx.WXK_HOME)
            pump_until(lambda: dialog.scope_choice.GetSelection() == 0 and dialog.preview is None,
                       'The native scope change did not invalidate the preview.')
            focus(dialog.names_choice)
            key(wx.WXK_END)
            pump_until(lambda: dialog.names_choice.GetSelection() == 1,
                       'The native name convention choice did not change.')
            formatted = preview()
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

            # A real stored name violates the user's explicit comma convention;
            # the actual worker must return this failure to the native status.
            reference = controller.update(reference, {'authors': ('Too, Many, Name, Parts',)})
            preview(success=False)
            reference = controller.update(reference, {'authors': ('Müller, Jane',)})
            preview()

            # Hold a genuine single-worker executor briefly so cancellation of a
            # queued native preview is deterministic, independent of CSL speed.
            entered = threading.Event()

            def queue_blocker():
                entered.set()
                if not release.wait(20):
                    raise TimeoutError('Native cancellation scenario did not release its queue.')

            blocker = executor.submit(queue_blocker)
            pump_until(entered.is_set, 'The cancellation queue did not start.')
            count = len(executor.jobs)
            press(dialog.generate)
            pump_until(lambda: len(executor.jobs) > count and dialog.pending is not None,
                       'The cancellable native preview was not queued.')
            pending = dialog.pending
            key(wx.WXK_ESCAPE)
            pump_until(lambda: not dialog.IsShown(), 'Escape did not close the native citation dialog.')
            release.set()
            pump_until(lambda: blocker.done() and pending.done(),
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
                'service_exports': ['text', 'html', 'docx'],
                'manual_keyboard': 'NOT_RUN', 'native_file_pickers': 'NOT_RUN',
                'nvda': 'NOT_RUN', 'installer': 'NOT_RUN',
            }, sort_keys=True), flush=True)
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
