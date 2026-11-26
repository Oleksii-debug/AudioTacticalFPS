"""Real wx Research-menu/dialog probe, with only the save-file chooser replaced.

Run against an installed package with Python -I on a native Windows runner, or
against local source with PYTHONPATH=src:. and an existing display. The actual
menu bindings, choice dialogs, read-only text dialogs, jobs, scoped services,
matrix renderer, checked file publisher and unsaved-note controls are exercised.
No screenshot, model, network, screen reader or external document viewer is used.
"""
import csv
from hashlib import sha256
import io
import json
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
import traceback

import wx

from carr_library.application import LibraryApp
from carr_library.ui import catalog_insights_actions
from carr_library.ui.desktop_app import DesktopFrame


def main():
    failures, checks = [], []
    report = {}
    frame = None

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            stop(traceback.format_exc())
            return False

    gui = ProbeApp(False)

    def stop(detail):
        failures.append(detail)
        if frame:
            frame._closing = True
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        wx.CallAfter(gui.ExitMainLoop)

    def guarded(callback):
        try:
            callback()
        except BaseException:
            stop(traceback.format_exc())

    def later(callback, milliseconds=30):
        wx.CallLater(milliseconds, lambda: guarded(callback))

    def check(label, condition):
        assert condition, label
        checks.append(label)

    def modal():
        windows = [window for window in wx.GetTopLevelWindows()
                   if isinstance(window, wx.Dialog) and window.IsShown() and window.IsModal()]
        assert len(windows) <= 1, 'More than one native modal dialog is visible.'
        return windows[0] if windows else None

    def descendants(parent):
        for child in parent.GetChildren():
            yield child
            yield from descendants(child)

    def caption(label):
        return label.split('\t', 1)[0].replace('&', '').rstrip('.')

    with TemporaryDirectory(prefix='catalog-insights-native-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'private library')
        workspace = library.create_workspace('Native academic library')
        topic = library.create_topic(workspace, 'Research topic')
        raw = 'Private native railway quotation — Україна.'
        library.add_text(workspace, 'Selected native source', raw)
        library.add_text(workspace, 'Other native source', 'Other original material.',
                         authors=('Known author',), language='en')
        hit, = library.search(workspace, 'railway')
        library.save_hit(workspace, topic, hit.ref, hit.revision, 'PRIVATE SAVED NOTE')
        before = library.evidence(workspace, topic)
        collection = library.create_collection(workspace, 'One selected source')
        with library.catalog_transaction(workspace) as catalog:
            catalog.assign(collection, hit.edition_id)

        frame = DesktopFrame(library)
        frame.choice.SetSelection(frame.scopes.index((workspace, topic)))
        frame.open_selected()
        assert frame.active == (workspace, topic), frame.status.GetValue()
        frame.SetSize((1100, 950))
        frame.Show()
        frame.Raise()
        book, evidence = frame.book, frame.evidence
        draft = 'UNSAVED PRIVATE DRAFT — keep this native edit'
        destination = root / 'literature-review-matrix.csv'
        actions = frame.catalog_insights_actions
        destinations = []
        original_destination = actions.dialogs.destination

        def save_destination(parent, format):
            assert parent is frame and format == 'csv'
            destinations.append((parent, format))
            return destination

        # Only the destination chooser is substituted. Native overwrite-dialog
        # behavior is not claimed; this path is new and belongs to the probe.
        actions.dialogs.destination = save_destination
        research = frame.menu_bar.GetMenu(frame.menu_bar.FindMenu('Research'))

        def verify_ready():
            assert not frame.working and not frame.busy
            assert frame.choice.IsEnabled() and book.IsEnabled()
            assert not frame.cancel_button.IsEnabled()
            assert all(frame.menu_bar.IsEnabledTop(i) for i in range(frame.menu_bar.GetMenuCount()))
            assert frame.book is book and frame.evidence is evidence
            assert evidence.note.GetValue() == draft, 'Native unsaved note was changed.'

        def invoke(label):
            items = [item for item in research.GetMenuItems()
                     if not item.IsSeparator() and caption(item.GetItemLabel()) == label]
            assert len(items) == 1, f'Expected exactly one Research entry: {label}'
            event = wx.CommandEvent(wx.EVT_MENU.typeId, items[0].GetId())
            event.SetEventObject(frame)
            assert frame.GetEventHandler().ProcessEvent(event), 'Research menu event was not handled.'

        def report_action(label, expected_title, expected_text, next_step):
            state = {'scope': False, 'report': False}

            def drive():
                dialog = modal()
                if dialog is None:
                    later(drive)
                    return
                if dialog.GetTitle() == 'Report scope':
                    assert not state['scope'] and isinstance(dialog, wx.SingleChoiceDialog)
                    state['scope'] = True
                    dialog.SetSelection(0)
                    check(expected_title + ': native scope choice', dialog.GetSelection() == 0)
                    dialog.EndModal(wx.ID_OK)
                    later(drive)
                    return
                assert dialog.GetTitle() == expected_title, dialog.GetTitle()
                assert state['scope'] and not state['report']
                state['report'] = True
                controls = [control for control in descendants(dialog) if isinstance(control, wx.TextCtrl)]
                assert len(controls) == 1
                control = controls[0]
                check(expected_title + ': named read-only text control',
                      control.GetName() == expected_title and not control.IsEditable())
                value = control.GetValue()
                check(expected_title + ': actual scoped report content', expected_text in value)
                assert 'not a fresh check' in value
                assert raw not in value and 'PRIVATE SAVED NOTE' not in value and draft not in value
                assert wx.Window.FindFocus() is control, 'Report text did not receive native focus.'
                dialog.EndModal(wx.ID_CANCEL)
                later(after_close, 80)

            def after_close():
                verify_ready()
                check(expected_title + ': draft and focus restored', wx.Window.FindFocus() is evidence.note)
                next_step()

            evidence.note.SetFocus()
            later(drive)
            invoke(label)

        def begin():
            if frame.working or evidence.displayed is None:
                later(begin)
                return
            assert evidence.displayed.note == 'PRIVATE SAVED NOTE', evidence.status.GetValue()
            book.ChangeSelection(next(i for i in range(book.GetPageCount()) if book.GetPage(i) is evidence))
            evidence.note.SetValue(draft)
            evidence.note.SetFocus()
            labels = ('Library overview', 'Source metadata and indexing checklist',
                      'Export literature review matrix')
            counts_before = {label: sum(not item.IsSeparator() and caption(item.GetItemLabel()) == label
                                       for item in research.GetMenuItems()) for label in labels}
            check('all three Research entries installed exactly once', all(n == 1 for n in counts_before.values()))
            check('installation is idempotent',
                  catalog_insights_actions.install_catalog_insights_actions(frame) is actions)
            check('reinstallation preserves exactly three native entries',
                  all(sum(not item.IsSeparator() and caption(item.GetItemLabel()) == label
                          for item in research.GetMenuItems()) == 1 for label in labels))
            report_action('Library overview', 'Library overview', 'Source editions: 2.', checklist)

        def checklist():
            report_action('Source metadata and indexing checklist',
                          'Source metadata and indexing checklist', hit.edition_id, matrix)

        def matrix():
            state = {'scope': False, 'format': False}

            def drive():
                dialog = modal()
                if dialog is not None:
                    assert isinstance(dialog, wx.SingleChoiceDialog), dialog.GetTitle()
                    if dialog.GetTitle() == 'Report scope':
                        assert not state['scope']
                        state['scope'] = True
                        dialog.SetSelection(1)
                        check('matrix: native selected-collection scope', dialog.GetSelection() == 1)
                    else:
                        assert dialog.GetTitle() == 'Literature review matrix format', dialog.GetTitle()
                        assert state['scope'] and not state['format']
                        state['format'] = True
                        dialog.SetSelection(0)
                        check('matrix: native CSV format choice', dialog.GetSelection() == 0)
                    dialog.EndModal(wx.ID_OK)
                    later(drive)
                    return
                if not state['format'] or frame.working or not destinations:
                    later(drive)
                    return
                verify_ready()
                check('matrix: real checked UTF-8 export', destination.is_file())
                text = destination.read_text(encoding='utf-8')
                rows = list(csv.DictReader(io.StringIO(text)))
                check('matrix: exact collection and report row',
                      len(rows) == 2 and rows[0]['record_type'] == 'report'
                      and rows[0]['source_count'] == '1'
                      and json.loads(rows[0]['collection_name_json']) == 'One selected source')
                check('matrix: selected-source metadata only',
                      rows[1]['record_type'] == 'source'
                      and json.loads(rows[1]['title_json']) == 'Selected native source'
                      and 'Other native source' not in text
                      and raw not in text and 'PRIVATE SAVED NOTE' not in text and draft not in text
                      and 'asset_sha256' not in text and str(library.root) not in text)
                check('matrix: completed native status', 'matrix saved' in frame.status.GetValue())
                cancel_scope()

            later(drive)
            invoke('Export literature review matrix')

        def cancel_scope():
            evidence.note.SetFocus()

            def drive():
                dialog = modal()
                if dialog is None:
                    later(drive)
                    return
                assert dialog.GetTitle() == 'Report scope'
                dialog.EndModal(wx.ID_CANCEL)
                later(finish, 80)

            later(drive)
            invoke('Library overview')

        def finish():
            verify_ready()
            check('cancelled scope restores native draft focus', wx.Window.FindFocus() is evidence.note)
            check('cancelled scope gives readable status', 'Report cancelled' in frame.status.GetValue())
            check('saved evidence and note remain unchanged', library.evidence(workspace, topic) == before)
            report.update(status='PASS', platform=platform.system(), wx_version=wx.version(),
                checks=checks, native_controls='menus, choices, read-only reports, unsaved note',
                save_destination='controlled callback only; native save chooser not exercised',
                source_authenticity='stored inventory only; source verification is not asserted',
                windows_nvda='NOT_RUN',
                controller_sha256=sha256(Path(catalog_insights_actions.__file__).read_bytes()).hexdigest())
            gui.ExitMainLoop()

        timeout = wx.CallLater(30000, lambda: stop('Native catalog-insights probe exceeded 30 seconds.'))
        later(begin, 80)
        try:
            gui.MainLoop()
        finally:
            timeout.Stop()
            actions.dialogs.destination = original_destination
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            if frame:
                frame.Destroy()
                gui.ProcessPendingEvents()

    if failures:
        print(json.dumps({'status': 'FAIL', 'errors': failures, 'checks': checks,
                          'windows_nvda': 'NOT_RUN'}, ensure_ascii=False))
        return 1
    if report.get('status') != 'PASS':
        print(json.dumps({'status': 'FAIL', 'error': 'Probe exited before completing its checks.',
                          'windows_nvda': 'NOT_RUN'}))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
