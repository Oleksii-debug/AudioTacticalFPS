"""Real wx controls and isolated queue execution; this does not run NVDA."""
from pathlib import Path
import json
import os
import sys
from tempfile import TemporaryDirectory
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
os.environ.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_OFFLINE='1',
                  HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')

import wx

from carr_library.application import LibraryApp
from carr_library.ui.desktop_app import DesktopFrame


def main():
    application = wx.App(False)
    errors, state = [], {}
    started = time.monotonic()
    with TemporaryDirectory(prefix='carr-import-center-') as directory:
        root = Path(directory)
        sources = root / 'selected'
        sources.mkdir()
        (sources / 'one.txt').write_text('Synthetic railway manuscript quotation.', encoding='utf-8')
        (sources / 'two.txt').write_text('Synthetic second academic source.', encoding='utf-8')
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
            assert dialog.batch['counts']['complete'] == 1, dialog.status.GetValue()
            assert dialog.batch['counts']['excluded'] == 1
            assert len(library.browse(workspace)) == 1
            hit, = library.search(workspace, 'manuscript quotation')
            assert library.copy(workspace, hit.ref, hit.revision, 'quote') == 'manuscript quotation'
            reopened = LibraryApp(library.root)
            saved = reopened.import_batch(workspace, dialog.batch['batch_id'])
            assert saved['counts']['complete'] == 1
            assert saved['rows'][0]['request_id'] == state['request_id']
            dialog.status.SetFocus()
            wx.YieldIfNeeded()
            assert wx.Window.FindFocus() is dialog.status
            state['completed'] = True
            dialog.close()

        def planned():
            dialog = state['dialog']
            assert dialog.batch['counts']['queued'] == 2, dialog.status.GetValue()
            assert library.browse(workspace) == ()
            assert dialog.rows.GetCount() == 2
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
                # The added bounded-scan controls must remain reachable in a
                # real smaller window, without hiding the fixed Close footer.
                original_size = dialog.GetSize()
                dialog.SetSize((800, 650))
                dialog.Layout()
                wx.YieldIfNeeded()
                body = dialog.body
                assert body.GetName() == 'Import Center scrollable options and results'
                assert dialog.collection.GetParent() is body and dialog.status.GetParent() is body
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                assert body.GetVirtualSize().height > body.GetClientSize().height
                dialog.status.SetFocus()
                wx.YieldIfNeeded()
                assert wx.Window.FindFocus() is dialog.status
                bottom_scroll = body.GetViewStart()[1]
                assert bottom_scroll > 0
                body_rect = body.GetScreenRect()
                status_rect = dialog.status.GetScreenRect()
                assert status_rect.y >= body_rect.y
                assert status_rect.GetBottom() <= body_rect.GetBottom()
                close = wx.FindWindowByName('Close Import Center', dialog)
                assert close is not None and close.GetParent() is dialog
                assert close.IsShownOnScreen() and close.AcceptsFocusFromKeyboard()
                assert dialog.status.Navigate()
                wx.YieldIfNeeded()
                assert wx.Window.FindFocus() is close
                dialog.collection.SetFocus()
                wx.YieldIfNeeded()
                assert wx.Window.FindFocus() is dialog.collection
                assert body.GetViewStart()[1] < bottom_scroll
                collection_rect = dialog.collection.GetScreenRect()
                assert collection_rect.y >= body.GetScreenRect().y
                assert collection_rect.GetBottom() <= body.GetScreenRect().GetBottom()
                dialog.SetSize(original_size)
                dialog.Layout()
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
        print(json.dumps({'status': 'PASS', 'native_controls': 'PASS',
                          'planned_import_search_reopen': 'PASS', 'nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
