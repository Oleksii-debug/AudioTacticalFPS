"""Real wx controls and isolated queue execution; this does not run NVDA."""
from argparse import ArgumentParser
from pathlib import Path
import importlib
import json
import os
import sys
from tempfile import TemporaryDirectory
import time
import traceback

parser = ArgumentParser(description=__doc__)
parser.add_argument('--require-installed', action='store_true')
options = parser.parse_args()
ROOT = Path(__file__).resolve().parents[2]
if not options.require_installed:
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
os.environ.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_OFFLINE='1',
                  HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')

import wx

from carr_library.application import LibraryApp
from carr_library.ui.desktop_app import DesktopFrame
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.source_file import prepare_source_file, launch_source_file


def installed_origins():
    if not options.require_installed:
        return {}
    if sys.platform != 'win32' or not os.environ.get('CARR_PROGRAM_DIR'):
        raise RuntimeError('Installed acceptance requires Windows and the selected bundle.')
    prefix = Path(os.environ['CARR_PROGRAM_DIR']).resolve() / '.venv'
    if not Path(sys.executable).resolve().is_relative_to(prefix):
        raise RuntimeError('Import Center acceptance uses another Python installation.')
    names = ('carr_library.application', 'carr_library.ui.desktop_app',
             'carr_library.ui.import_center', 'carr_library.extract.document_formats',
             'carr_library.extract.document_formats_text',
             'carr_library.extract.document_formats_xml')
    origins = {name: str(Path(importlib.import_module(name).__file__).resolve()) for name in names}
    if not all(Path(path).is_relative_to(prefix) for path in origins.values()):
        raise RuntimeError('Import Center acceptance loaded checkout modules.')
    return origins


def main():
    origins = installed_origins()
    application = wx.App(False)
    errors, state = [], {}
    started = time.monotonic()
    with TemporaryDirectory(prefix='carr-import-center-') as directory:
        root = Path(directory)
        sources = root / 'selected'
        sources.mkdir()
        (sources / 'one.txt').write_text('Synthetic railway manuscript quotation.', encoding='utf-8')
        (sources / 'two.txt').write_text('Synthetic second academic source.', encoding='utf-8')
        (sources / 'z-html.html').write_bytes(b'<html><head><script>Neverindexscript</script></head>'
            b'<body><p>Extendedmarker retained original.</p></body></html>')
        (sources / 'z-sheet.xlsx').write_bytes((ROOT / 'tests/fixtures/document_formats_openpyxl.xlsx').read_bytes())
        library = LibraryApp(root / 'data')
        workspace = library.create_workspace('Import Center native acceptance')
        library.create_collection(workspace, 'Sources')
        frame = DesktopFrame(library)
        frame.Show()

        def fail():
            errors.append(traceback.format_exc())
            frame.cancel_event.set()
            dialog = state.get('dialog')
            if dialog is not None:
                dialog.EndModal(wx.ID_CANCEL)

        def poll(callback):
            try:
                if time.monotonic() - started > 90:
                    raise AssertionError('Import Center native acceptance exceeded its deadline.')
                if state['dialog'].busy:
                    wx.CallLater(50, poll, callback)
                else:
                    callback()
            except BaseException:
                fail()

        def imported():
            dialog = state['dialog']
            assert dialog.batch['counts']['complete'] == 3, dialog.status.GetValue()
            assert dialog.batch['counts']['excluded'] == 1
            assert len(library.browse(workspace)) == 3
            hit, = library.search(workspace, 'manuscript quotation')
            assert library.copy(workspace, hit.ref, hit.revision, 'quote') == 'manuscript quotation'
            extended, = library.search(workspace, 'Extendedmarker')
            assert not extended.verified
            assert library.search(workspace, 'Neverindexscript') == ()
            prepared = prepare_source_file(library, workspace, extended.ref, extended.revision)
            opened = []
            try:
                launch_source_file(library, prepared, lambda path: opened.append(path) or True)
            except ValueError as error:
                assert 'Reading Desk' in str(error)
            else:
                raise AssertionError('Active-capable source was sent to an external viewer.')
            assert opened == []
            sheet, = [book for book in library.browse(workspace) if book.title == 'z-sheet']
            page = ReadingDesk(library, workspace, sheet.edition_id).page(1)
            assert page.raw_text == 'Українська' and not page.verified
            html_index = next(i for i, row in enumerate(dialog.batch['rows']) if row['file'] == 'z-html.html')
            dialog.rows.SetSelection(html_index)
            dialog.select_row()
            assert 'HTML scripts and active or nontext content were omitted.' in dialog.details.GetValue()
            reopened = LibraryApp(library.root)
            saved = reopened.import_batch(workspace, dialog.batch['batch_id'])
            assert saved['counts']['complete'] == 3
            assert saved['rows'][0]['request_id'] == state['request_id']
            dialog.status.SetFocus()
            wx.YieldIfNeeded()
            assert wx.Window.FindFocus() is dialog.status
            state['completed'] = True
            dialog.close()

        def planned():
            dialog = state['dialog']
            assert dialog.batch['counts']['queued'] == 4, dialog.status.GetValue()
            assert library.browse(workspace) == ()
            assert dialog.rows.GetCount() == 4
            dialog.rows.SetSelection(1)
            dialog.toggle_row()
            assert dialog.batch['counts']['excluded'] == 1
            state['request_id'] = dialog.batch['rows'][0]['request_id']
            dialog.run()
            assert dialog.busy
            dialog.close()
            assert not dialog.closed
            assert 'before closing' in dialog.status.GetValue()
            wx.CallLater(50, poll, imported)

        def begin():
            try:
                dialog, = [window for window in wx.GetTopLevelWindows()
                           if window.GetTitle() == 'Import Center']
                state['dialog'] = dialog
                assert dialog.batches.GetName() == 'Saved import batches'
                assert dialog.rows.GetName() == 'Files and import status'
                assert dialog.details.GetName() == 'Selected file details'
                assert dialog.status.GetName() == 'Import Center status'
                assert not dialog.cancel.IsEnabled()
                assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                dialog.plan((sources,))
                assert dialog.busy and dialog.cancel.IsEnabled()
                wx.CallLater(50, poll, planned)
            except BaseException:
                fail()

        wx.CallAfter(begin)
        frame.import_center()
        frame._closing = True
        frame.executor.shutdown(wait=True, cancel_futures=True)
        frame.Destroy()
        application.ProcessPendingEvents()
        if errors:
            raise AssertionError('\n'.join(errors))
        assert state.get('completed'), 'Import Center did not complete the native workflow.'
        assert installed_origins() == origins
        print(json.dumps({'status': 'PASS', 'native_controls': 'PASS',
                          'planned_import_search_reopen': 'PASS',
                          'installed_modules': origins,
                          'html_xlsx_reader_and_viewer_refusal': 'PASS', 'nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
