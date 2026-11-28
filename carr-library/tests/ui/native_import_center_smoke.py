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

        def visible_in(parent, child):
            origin = parent.ClientToScreen((0, 0))
            client = parent.GetClientSize()
            rect = child.GetScreenRect()
            return (origin.x <= rect.x and origin.y <= rect.y
                    and rect.x + rect.width <= origin.x + client.width
                    and rect.y + rect.height <= origin.y + client.height)

        def layout_snapshot(phase):
            dialog = state['dialog']
            print('IMPORT_CENTER_VIEWPORT: ' + json.dumps({
                'phase': phase,
                'window': list(dialog.GetSize()),
                'client': list(dialog.GetClientSize()),
                'outer_minimum': list(dialog.GetSizer().GetMinSize()),
                'viewport': list(dialog.body.GetClientSize()),
                'content_minimum': list(dialog.body.GetSizer().GetMinSize()),
                'virtual': list(dialog.body.GetVirtualSize()),
            }), flush=True)

        def await_focus(control, callback):
            try:
                if time.monotonic() - started > 90:
                    raise AssertionError('Import Center native acceptance exceeded its deadline.')
                if wx.Window.FindFocus() is control:
                    callback()
                else:
                    wx.CallLater(50, await_focus, control, callback)
            except BaseException:
                fail()

        def start_plan():
            dialog = state['dialog']
            dialog.plan((sources,))
            assert dialog.busy and dialog.cancel.IsEnabled()
            wx.CallLater(50, poll, planned)

        def top_visible():
            dialog = state['dialog']
            assert visible_in(dialog.body, dialog.collection)
            assert dialog.body.GetViewStart()[1] < state['bottom_scroll']
            dialog.SetSize(state['original_size'])
            dialog.Layout()
            wx.CallLater(50, poll, start_plan)

        def footer_focused():
            dialog = state['dialog']
            assert visible_in(dialog, dialog.status)
            dialog.collection.SetFocus()
            wx.CallLater(50, await_focus, dialog.collection, top_visible)

        def bottom_visible():
            dialog = state['dialog']
            assert visible_in(dialog.body, state['last_action'])
            state['bottom_scroll'] = dialog.body.GetViewStart()[1]
            assert state['bottom_scroll'] > 0
            assert visible_in(dialog, dialog.cancel)
            assert visible_in(dialog, state['close'])
            wx.UIActionSimulator().Char(wx.WXK_TAB)
            wx.CallLater(50, await_focus, dialog.status, footer_focused)

        def minimum_layout():
            dialog = state['dialog']
            layout_snapshot('minimum')
            assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
            assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
            assert isinstance(dialog.body, wx.ScrolledWindow)
            assert dialog.body.GetParent() is dialog
            assert dialog.body.GetName() == 'Import options and queued files'
            assert dialog.body.GetVirtualSize().height >= dialog.body.GetSizer().GetMinSize().height
            assert dialog.body.GetVirtualSize().height > dialog.body.GetClientSize().height
            assert all(rate > 0 for rate in dialog.body.GetScrollPixelsPerUnit())
            close = wx.Window.FindWindowById(wx.ID_CANCEL, dialog)
            assert close is not None and close.GetParent() is dialog
            state['close'] = close
            for control in (dialog.status, dialog.cancel, close):
                assert control.GetParent() is dialog and visible_in(dialog, control)
            action, = [child for child in dialog.body.GetChildren()
                       if isinstance(child, wx.Button) and child.GetLabel() == 'Save re&port...']
            assert action.IsEnabled() and action.AcceptsFocusFromKeyboard()
            state['last_action'] = action
            action.SetFocus()
            wx.CallLater(50, await_focus, action, bottom_visible)

        def begin():
            try:
                dialog, = [window for window in wx.GetTopLevelWindows()
                           if window.GetTitle() == 'Import Center']
                state['dialog'] = dialog
                layout_snapshot('default')
                assert dialog.batches.GetName() == 'Saved import batches'
                assert dialog.rows.GetName() == 'Files and import status'
                assert dialog.details.GetName() == 'Selected file details'
                assert dialog.status.GetName() == 'Import Center status'
                assert not dialog.cancel.IsEnabled()
                assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                state['original_size'] = tuple(dialog.GetSize())
                dialog.SetSize(dialog.GetMinSize())
                dialog.Layout()
                # begin was queued before ScrolledPanel's normal deferred setup.
                # Return to the same modal loop before checking its viewport.
                wx.CallLater(50, poll, minimum_layout)
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
