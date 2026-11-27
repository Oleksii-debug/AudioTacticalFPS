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
        geometry = []

        def bounds(window, *, client=False):
            point = window.ClientToScreen((0, 0)) if client else window.GetScreenPosition()
            size = window.GetClientSize() if client else window.GetSize()
            return [point.x, point.y, size.width, size.height]

        def assert_visible(control, parent):
            left, top, width, height = bounds(control)
            x, y, available_width, available_height = bounds(parent, client=True)
            assert control.IsShown() and width > 0 and height > 0
            assert (x <= left and y <= top and
                    left + width <= x + available_width and
                    top + height <= y + available_height), (
                        control.GetName(), bounds(control), bounds(parent, client=True))

        def measure(phase):
            dialog = state['dialog']
            body = getattr(dialog, 'body', None)
            minimum = dialog.GetSizer().GetMinSize()
            report = dict(phase=phase, dialog=bounds(dialog),
                          client=bounds(dialog, client=True),
                          minimum=[minimum.width, minimum.height])
            if body is not None:
                virtual, position = body.GetVirtualSize(), body.GetViewStart()
                report.update(body=bounds(body, client=True),
                              virtual=[virtual.width, virtual.height],
                              scroll=[position.x, position.y])
            geometry.append(report)
            print('Import Center geometry: ' + json.dumps(report), flush=True)

        def assert_footer():
            dialog = state['dialog']
            close, = [child for child in dialog.GetChildren()
                      if isinstance(child, wx.Button) and child.GetId() == wx.ID_CANCEL]
            for control in (dialog.status, dialog.cancel, close):
                assert control.GetParent() is dialog
                assert_visible(control, dialog)

        def fail():
            errors.append(traceback.format_exc())
            print('Import Center native phase: ' + state.get('phase', 'unknown'), flush=True)
            print(errors[-1], flush=True)
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
            assert_footer()
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

        def tab_to_lower_action():
            try:
                if errors:
                    return
                if time.monotonic() - started > 90:
                    raise AssertionError('Import Center native acceptance exceeded its deadline.')
                dialog = state['dialog']
                if wx.Window.FindFocus() is state['lower_action']:
                    state['phase'] = 'lower action reached by native Tab after resize'
                    measure(state['phase'])
                    assert state['tabs'] > 0 and dialog.body.GetViewStart().y > 0
                    assert_visible(state['lower_action'], dialog.body)
                    assert_footer()
                    state['keyboard_scrolling'] = True
                    dialog.plan((sources,))
                    assert dialog.busy and dialog.cancel.IsEnabled()
                    wx.CallLater(50, poll, planned)
                    return
                assert state['tabs'] < 80, 'Native Tab did not reach the lower import action.'
                state['tabs'] += 1
                assert state['simulator'].Char(wx.WXK_TAB)
                wx.CallLater(30, tab_to_lower_action)
            except BaseException:
                fail()

        def resized():
            try:
                if errors:
                    return
                dialog = state['dialog']
                state['phase'] = 'resized to existing minimum'
                measure(state['phase'])
                assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                assert dialog.body.GetSizer().GetMinSize().width <= dialog.body.GetClientSize().width
                assert dialog.body.GetVirtualSize().height > dialog.body.GetClientSize().height
                assert_footer()
                state['simulator'], state['tabs'] = wx.UIActionSimulator(), 0
                dialog.collection.SetFocus()
                wx.CallAfter(tab_to_lower_action)
            except BaseException:
                fail()

        def begin():
            try:
                dialog, = [window for window in wx.GetTopLevelWindows()
                           if window.GetTitle() == 'Import Center']
                state['dialog'] = dialog
                state['phase'] = 'initial layout'
                measure(state['phase'])
                assert dialog.batches.GetName() == 'Saved import batches'
                assert dialog.rows.GetName() == 'Files and import status'
                assert dialog.details.GetName() == 'Selected file details'
                assert dialog.status.GetName() == 'Import Center status'
                assert not dialog.cancel.IsEnabled()
                assert dialog.GetSizer().GetMinSize().width <= dialog.GetClientSize().width
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                assert dialog.body.GetName() == 'Import plan settings and saved batches'
                assert_footer()
                lower, = [child for child in dialog.body.GetChildren()
                          if isinstance(child, wx.Button) and child.GetLabel() == 'Save re&port...']
                state['lower_action'] = lower
                dialog.SetSize(dialog.GetMinSize())
                wx.CallAfter(resized)
            except BaseException:
                fail()

        def reopened():
            try:
                dialog, = [window for window in wx.GetTopLevelWindows()
                           if window.GetTitle() == 'Import Center' and window.IsShown()]
                state['dialog'] = dialog
                state['phase'] = 'saved batch focused after reopening'
                measure(state['phase'])
                assert dialog.batch['counts']['complete'] == 1
                assert wx.Window.FindFocus() is dialog.batches
                assert_visible(dialog.batches, dialog.body)
                assert_footer()
                state['reopened'] = True
                dialog.close()
            except BaseException:
                fail()

        # ScrolledPanel queues its size setup during construction. Let that
        # real event settle before checking geometry or injecting native keys.
        wx.CallAfter(lambda: wx.CallAfter(begin))
        frame.import_center()
        if not errors:
            wx.CallAfter(lambda: wx.CallAfter(reopened))
            frame.import_center()
        frame._closing = True
        frame.executor.shutdown(wait=True, cancel_futures=True)
        frame.Destroy()
        application.ProcessPendingEvents()
        if errors:
            raise AssertionError('\n'.join(errors))
        assert state.get('completed'), 'Import Center did not complete the native workflow.'
        assert state.get('keyboard_scrolling') and state.get('reopened')
        print(json.dumps({'status': 'PASS', 'native_controls': 'PASS',
                          'planned_import_search_reopen': 'PASS',
                          'native_tab_scroll_after_resize': 'PASS',
                          'saved_batch_focus_visible_on_reopen': 'PASS',
                          'fixed_status_cancel_close': 'PASS',
                          'geometry': geometry, 'nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
