"""Native import/reading controls for actual additional-format source data.

Default execution imports the installed distribution. Use --source only for an
explicit checkout check. Synthetic input selection is supplied to Import Center;
native picker construction is checked, but operating the OS file picker and
manual NVDA/speech acceptance are not claimed. Missing wx/display exits 77.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


os.environ.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_TELEMETRY='1',
                  HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')


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
        sys.path[:0] = [str(source / 'src'), str(source)]
    import carr_library.extract.document_formats as formats_module
    import carr_library.ui.import_center as import_module
    import carr_library.ui.desktop_app as desktop_module
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.ui.reading_desk import ReadingDialog

    try:
        application = wx.App(False)
    except (Exception, SystemExit) as exc:
        return not_run('Native wx application could not open: ' + str(exc))

    checks, errors, state = [], [], {}
    started = monotonic()
    origins = {module.__name__: str(Path(module.__file__).resolve())
               for module in (formats_module, import_module, desktop_module)}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    checkout = Path(__file__).resolve().parents[2]
    if source is None:
        check('native document modules are loaded from the installed distribution',
              all(not Path(origin).is_relative_to(checkout) for origin in origins.values()))

    def pump(predicate, timeout=10):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            application.Yield()
            if predicate():
                return True
            wx.MilliSleep(10)
        return False

    with TemporaryDirectory(prefix='carr-native-document-formats-') as temporary:
        root = Path(temporary)
        selected = root / 'selected'
        selected.mkdir()
        original = ('<!doctype html><html><head><script>Hiddenexecutor</script></head><body>'
                    '<p>Native document first paragraph. Наукова бібліотека — štúdia.</p>'
                    '<p>Native document second paragraph.</p></body></html>').encode('utf-8')
        html = selected / 'Дослідження.html'
        html.write_bytes(original)
        (selected / 'notes.md').write_text('# Native Markdown\n\nOriginal symbolic **notation**.', encoding='utf-8')
        library = LibraryApp(root / 'data')
        workspace = library.create_workspace('Native additional documents')
        frame = desktop_module.DesktopFrame(library)
        frame.Show()

        try:
            picker = wx.FileDialog(frame, 'Native format choices',
                wildcard=formats_module.file_dialog_wildcard(),
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
            try:
                wildcard = picker.GetWildcard()
                check('native file selector includes all additional formats',
                    all('*' + extension in wildcard for extension in
                        ('.html', '.md', '.eml', '.fb2', '.odt', '.ods', '.odp', '.pptx', '.xlsx')))
            finally:
                picker.Destroy()

            help_menu = None
            for index in range(frame.GetMenuBar().GetMenuCount()):
                menu = frame.GetMenuBar().GetMenu(index)
                for item in menu.GetMenuItems():
                    if item.GetItemLabelText() == 'Document formats':
                        help_menu = item
            check('document support help is reachable from the native menu', help_menu is not None)

            def inspect_help():
                dialog = None
                try:
                    dialog, = [window for window in wx.GetTopLevelWindows()
                               if window.GetTitle() == 'Document formats']
                    text, = [child for child in dialog.GetChildren() if isinstance(child, wx.TextCtrl)]
                    check('format help has a named read-only multiline text control',
                        text.GetName() == 'Document formats'
                        and bool(text.GetWindowStyleFlag() & wx.TE_READONLY)
                        and bool(text.GetWindowStyleFlag() & wx.TE_MULTILINE))
                    check('format help describes unverified text and unsupported conversions',
                        'unverified logical segments' in text.GetValue()
                        and 'RTF' in text.GetValue() and 'not handled' in text.GetValue())
                    text.SetFocus()
                    check('format help can receive native keyboard focus', pump(lambda: wx.Window.FindFocus() is text))
                except BaseException:
                    errors.append(traceback.format_exc())
                finally:
                    if dialog is not None:
                        dialog.EndModal(wx.ID_CANCEL)
            wx.CallAfter(inspect_help)
            event = wx.CommandEvent(wx.EVT_MENU.typeId, help_menu.GetId())
            check('native document help menu event is handled', frame.GetEventHandler().ProcessEvent(event))
            if errors:
                raise AssertionError('\n'.join(errors))

            def fail():
                errors.append(traceback.format_exc())
                frame.cancel_event.set()
                dialog = state.get('import')
                if dialog is not None:
                    dialog.EndModal(wx.ID_CANCEL)

            def poll(callback):
                try:
                    if monotonic() - started > 90:
                        raise TimeoutError('Native document import exceeded its acceptance deadline.')
                    if state['import'].busy:
                        wx.CallLater(50, poll, callback)
                    else:
                        callback()
                except BaseException:
                    fail()

            def imported():
                dialog = state['import']
                check('native queue completes both actual document imports',
                      dialog.batch['state'] == 'complete' and dialog.batch['counts']['complete'] == 2)
                check('queue does not approve additional format text',
                      all(row['result']['verified'] is False for row in dialog.batch['rows']))
                html_index = next(i for i, row in enumerate(dialog.batch['rows']) if row['file'].endswith('.html'))
                dialog.rows.SetSelection(html_index)
                event = wx.CommandEvent(wx.EVT_LISTBOX.typeId, dialog.rows.GetId())
                event.SetEventObject(dialog.rows)
                check('native selected-file event is handled', dialog.rows.GetEventHandler().ProcessEvent(event))
                check('selected file details explain unverified logical segments',
                      'unverified logical segments' in dialog.details.GetValue())
                dialog.details.SetFocus()
                check('actual result details control can receive keyboard focus',
                      pump(lambda: wx.Window.FindFocus() is dialog.details))
                state['html_result'] = dialog.batch['rows'][html_index]['result']
                state['imported'] = True
                dialog.close()

            def planned():
                dialog = state['import']
                check('native queue recognizes HTML and Markdown as two documents',
                      dialog.batch['counts']['queued'] == 2 and dialog.rows.GetCount() == 2)
                button = next(child for child in dialog.GetChildren()
                              if isinstance(child, wx.Button) and child.GetLabel() == '&Start queued files')
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                event.SetEventObject(button)
                check('native Start queued files button event launches work',
                      button.GetEventHandler().ProcessEvent(event) and dialog.busy)
                wx.CallLater(50, poll, imported)

            def begin_import():
                try:
                    dialog, = [window for window in wx.GetTopLevelWindows()
                               if window.GetTitle() == 'Import Center']
                    state['import'] = dialog
                    check('native import controls expose their spoken names',
                          dialog.rows.GetName() == 'Files and import status'
                          and dialog.details.GetName() == 'Selected file details'
                          and dialog.status.GetName() == 'Import Center status')
                    dialog.plan((selected,))
                    check('document preflight runs asynchronously with cancellation enabled',
                          dialog.busy and dialog.cancel.IsEnabled())
                    wx.CallLater(50, poll, planned)
                except BaseException:
                    fail()

            wx.CallAfter(begin_import)
            frame.import_center()
            if errors:
                raise AssertionError('\n'.join(errors))
            check('native document import workflow completed', state.get('imported') is True)
            imported = state['html_result']
            book = next(book for book in library.browse(workspace) if book.edition_id == imported['edition'])
            check('native import retains source bytes and digest exactly',
                  book.asset == sha256(original).hexdigest()
                  and library.assets.resolve(book.asset).read_bytes() == original and html.read_bytes() == original)
            check('script text is not searchable after real HTML parsing', library.search(workspace, 'Hiddenexecutor') == ())
            reader = ReadingDialog(frame, ReadingDesk(library, workspace, book.edition_id),
                                   ReadingState(library.root), lambda: None)
            reader.dialog.Show()
            reader.dialog.Raise()
            try:
                reader._run('Reading imported HTML', reader.controller.start, reader._page)
                check('native document reader loads asynchronously', pump(lambda: not reader.busy))
                check('native reader shows actual imported Unicode text',
                      'Native document first paragraph. Наукова бібліотека — štúdia.' in reader.text.GetValue())
                check('native reader exposes named read-only source text',
                      reader.text.GetName() == 'Full source page or text segment'
                      and bool(reader.text.GetWindowStyleFlag() & wx.TE_READONLY))
                check('native reader exposes the unverified logical source location',
                      reader.controller.current.locator.kind == 'logical_segment'
                      and reader.controller.current.verified is False
                      and 'unverified' in reader.location.GetValue().lower())
                check('native imported text receives keyboard focus', pump(lambda: wx.Window.FindFocus() is reader.text))
                next_button = next(button for button in reader.buttons if button.GetName() == 'Next source location')
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, next_button.GetId())
                event.SetEventObject(next_button)
                check('native next-document-segment button event is handled',
                      next_button.GetEventHandler().ProcessEvent(event))
                check('native next-segment reads second source paragraph',
                      pump(lambda: not reader.busy) and 'Native document second paragraph.' in reader.text.GetValue())
                check('reading never converts imported text into approved quotation',
                      reader.controller.current.verified is False)
            finally:
                reader.close()
                reader.dialog.Destroy()
        finally:
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.Yield()

    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                      'platform': platform.system(), 'origins': origins,
                      'source_override': str(source) if source is not None else None,
                      'input_selection': 'synthetic paths supplied to actual Import Center',
                      'native_file_picker_operation': 'NOT_RUN', 'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--source', type=Path, default=None)
    args = parser.parse_args()
    return run_native(args.source.resolve() if args.source is not None else None)


if __name__ == '__main__':
    raise SystemExit(main())
