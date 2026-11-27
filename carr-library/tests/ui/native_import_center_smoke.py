"""Real wx controls and isolated queue execution; this does not run NVDA."""
from pathlib import Path
import argparse
from hashlib import sha256
import json
import os
import sys
from tempfile import TemporaryDirectory
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--bundle', type=Path)
args = parser.parse_args()
if args.bundle is None:
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
else:
    import carr_library
    from carr_library.native_runtime import activate_bundled_runtime
    bundle = args.bundle.resolve(strict=True)
    assert Path(carr_library.__file__).resolve().is_relative_to(bundle / '.venv')
    runtime_handle = activate_bundled_runtime(bundle_root=bundle)
    assert runtime_handle is not None
os.environ.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_OFFLINE='1',
                  HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')

import wx

from carr_library.application import LibraryApp
from carr_library.ui.desktop_app import DesktopFrame
from carr_library.ui import import_center

if args.bundle is not None:
    for module in (sys.modules[DesktopFrame.__module__], import_center):
        installed = Path(module.__file__).resolve()
        assert installed.is_relative_to(bundle / '.venv')
        source = ROOT / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        assert sha256(installed.read_bytes()).digest() == sha256(source.read_bytes()).digest()


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
                # The operation controls remain available on a short display;
                # keyboard focus scrolls the preparation area into view.
                dialog.SetSize((820, 560))
                dialog.Layout()
                wx.YieldIfNeeded()
                assert dialog.GetSizer().GetMinSize().height <= dialog.GetClientSize().height
                assert dialog.cancel.GetRect().GetBottom() <= dialog.GetClientSize().height
                panel = dialog.content_panel
                assert panel.GetVirtualSize().height > panel.GetClientSize().height
                dialog.details.SetFocus()
                wx.YieldIfNeeded()
                assert wx.Window.FindFocus() is dialog.details
                assert panel.GetViewStart()[1] > 0
                dialog.collection.SetFocus()
                wx.YieldIfNeeded()
                assert wx.Window.FindFocus() is dialog.collection
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
                          'installed_package': args.bundle is not None,
                          'short_display_focus_scrolling': 'PASS',
                          'planned_import_search_reopen': 'PASS', 'nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
