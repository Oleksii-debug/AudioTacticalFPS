"""Real native saved-research search, modal cancellation and evidence navigation.

Run with the installed package: python -I tests/ui/native_notebook_search_smoke.py --native
Only a synthetic discard-confirmation answer is supplied by the probe. Native
controls, events, worker threads, SQLite, source verification and clipboard are
real. All research and source files belong to this probe's temporary directory.
Missing native prerequisites return NOT_RUN/77. NVDA is not tested.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import traceback


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', help='Run real wx controls on the current native display.')
    parser.parse_args()
    if sys.platform not in ('win32', 'darwin') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'No native display.', 'nvda': 'NOT_RUN'}))
        return 77
    try:
        import wx
        from carr_library.application import LibraryApp
        from carr_library.ui import notebook_search
        from carr_library.ui.desktop_app import DesktopFrame
    except (ImportError, OSError) as exc:
        print(json.dumps({'result': 'NOT_RUN', 'reason': str(exc), 'nvda': 'NOT_RUN'}))
        return 77

    failures, checks = [], []
    active_dialog = None
    frame = None

    def abort():
        if active_dialog is not None and not active_dialog._closed:
            active_dialog.end(wx.ID_CANCEL)
        gui.ExitMainLoop()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            abort()
            return False

    try:
        gui = ProbeApp(False)
    except (Exception, SystemExit) as exc:
        print(json.dumps({'result': 'NOT_RUN', 'reason': str(exc), 'nvda': 'NOT_RUN'}))
        return 77

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def guarded(callback):
        if failures:
            return
        try:
            callback()
        except BaseException:
            failures.append(traceback.format_exc())
            abort()

    def later(callback, delay=25):
        wx.CallLater(delay, lambda: guarded(callback))

    def click(button):
        assert button.IsShownOnScreen() and button.IsEnabled(), button.GetLabel()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event), button.GetLabel()

    def choose(control, index, event_type):
        control.SetSelection(index)
        event = wx.CommandEvent(event_type.typeId, control.GetId())
        event.SetEventObject(control)
        event.SetInt(index)
        assert control.GetEventHandler().ProcessEvent(event)

    with TemporaryDirectory(prefix='research-notebook-native-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'private library')
        workspace = library.create_workspace('Native notebook research')
        first = library.create_topic(workspace, 'First topic')
        second = library.create_topic(workspace, 'Second topic')
        archived = library.create_topic(workspace, 'Archived topic')
        imported = library.add_text(workspace, 'Notebook source', 'Notebook railway source.\nExact original text.',
                                    authors=('Synthetic author',))
        source, = library.search(workspace, 'Notebook')
        identity = library.save_hit(workspace, first, source.ref, source.revision, 'First independent research note.')
        saved, = library.evidence(workspace, first)
        with library.evidence_transaction(workspace) as service:
            service.save(second, identity, saved.anchors, 'Second independent research note.')
            service.save(archived, identity, saved.anchors, 'Archived independent research note.')
        with library.topic_transaction(workspace) as tree:
            tree.archive(archived)
        library.set_evidence_tags(workspace, first, identity, ('History',))
        library.set_evidence_tags(workspace, second, identity, ('History',))
        frame = DesktopFrame(library)
        frame._open_workspace(workspace)
        frame.SetSize((1100, 950))
        frame.Show()
        frame.Raise()
        draft = 'Unsaved draft — retain this exact note.'
        previous_book = None
        original_confirm = frame._confirm
        refusals = []

        def get_dialog():
            nonlocal active_dialog
            dialogs = [child for child in frame.GetChildren()
                       if isinstance(child, notebook_search.NotebookSearchDialog)]
            assert len(dialogs) == 1
            active_dialog = dialogs[0]
            return active_dialog

        def search(dialog):
            dialog.query.SetValue('Notebook')
            click(dialog.search)
            check('native modal has its own available cancellation action', dialog.cancel.IsEnabled())
            check('native modal Close remains available during worker search', dialog.close.IsEnabled())
            check('parent is modal-disabled without taking over its global job', not frame.IsEnabled() and not frame.working)

        def select_topic(dialog, topic):
            index = next(i for i, row in enumerate(dialog.hits) if row.evidence.topic_id == topic)
            choose(dialog.items, index, wx.EVT_LISTBOX)

        def first_dialog_ready():
            dialog = get_dialog()
            check('native search field has an explicit accessible name', dialog.query.GetName() == 'Search words')
            search(dialog)
            later(first_search_done)

        def first_search_done():
            dialog = active_dialog
            if dialog._busy:
                later(first_search_done)
                return
            check('default search returns active topics only', len(dialog.hits) == 2 and not any(row.archived for row in dialog.hits))
            check('same evidence ID remains distinct across topics',
                  {notebook_search.evidence_key(row) for row in dialog.hits} == {(first, identity), (second, identity)})
            check('completed search focuses its real readable status', wx.Window.FindFocus() is dialog.status)
            select_topic(dialog, second)
            check('selected quote is original source text', dialog.quote.GetValue() == saved.anchor.quote)
            check('research note is separate from exact quotation', dialog.note.GetValue() == 'Second independent research note.'
                  and dialog.note.GetValue() not in dialog.quote.GetValue())
            click(dialog.copy)
            later(copy_done)

        def copy_done():
            dialog = active_dialog
            if dialog._busy:
                later(copy_done)
                return
            check('verified copy reports completion with native status focus',
                  'copied' in dialog.status.GetValue() and wx.Window.FindFocus() is dialog.status)
            copied = wx.TextDataObject()
            assert wx.TheClipboard.Open()
            try:
                assert wx.TheClipboard.GetData(copied)
            finally:
                wx.TheClipboard.Close()
            check('native clipboard separates source and excludes research note',
                  saved.anchor.quote in copied.GetText() and 'Source citation:' in copied.GetText()
                  and 'Second independent research note.' not in copied.GetText())
            click(dialog.search)
            click(dialog.cancel)
            later(cancel_done)

        def cancel_done():
            dialog = active_dialog
            if dialog._busy:
                later(cancel_done)
                return
            check('native cancellation clears results without closing modal',
                  'cancelled' in dialog.status.GetValue() and not dialog.hits and dialog.IsModal())
            search(dialog)
            later(open_result)

        def open_result():
            dialog = active_dialog
            if dialog._busy:
                later(open_result)
                return
            select_topic(dialog, second)
            click(dialog.open)

        def begin_first():
            later(first_dialog_ready)
            notebook_search.search_saved_research(frame)
            if not failures:
                later(opened_result, 100)

        def opened_result():
            nonlocal previous_book
            if (frame.working or frame.evidence is None or frame.evidence.displayed is None
                    or frame.book.GetCurrentPage() is not frame.evidence
                    or wx.Window.FindFocus() is not frame.evidence.note):
                later(opened_result)
                return
            check('Open targets the exact topic and composite evidence identity',
                  frame.active == (workspace, second) and frame.evidence.displayed is not None
                  and (frame.evidence.displayed.topic_id, frame.evidence.displayed.evidence_id) == (second, identity))
            check('Open reveals the actual evidence notebook page', frame.book.GetCurrentPage() is frame.evidence)
            check('Open returns native keyboard focus to the note', wx.Window.FindFocus() is frame.evidence.note)
            frame.evidence.note.SetValue(draft)
            previous_book = frame.book
            frame.book.ChangeSelection(0)
            # The operator decision is synthetic; the subsequent notebook,
            # note-retention and focus behavior is exercised on actual controls.
            def refuse():
                refusals.append(True)
                return False
            frame._confirm = refuse
            later(second_dialog_ready)
            try:
                notebook_search.search_saved_research(frame)
            finally:
                frame._confirm = original_confirm
            if not failures:
                later(refusal_done, 100)

        def second_dialog_ready():
            search(get_dialog())
            later(second_search_done)

        def second_search_done():
            dialog = active_dialog
            if dialog._busy:
                later(second_search_done)
                return
            select_topic(dialog, first)
            click(dialog.open)

        def refusal_done():
            check('synthetic discard refusal preserves the original native notebook', bool(refusals) and frame.book is previous_book)
            check('refused Open retains the complete unsaved draft', frame.evidence.note.GetValue() == draft)
            check('refused Open reveals and focuses the retained draft', frame.book.GetCurrentPage() is frame.evidence
                  and wx.Window.FindFocus() is frame.evidence.note)
            later(third_dialog_ready)
            notebook_search.search_saved_research(frame)
            if not failures:
                check('closing search re-enables the parent without a blocked global job', frame.IsEnabled() and not frame.working)
                check('search failure leaves the external note draft unchanged', frame.evidence.note.GetValue() == draft)
                gui.ExitMainLoop()

        def third_dialog_ready():
            search(get_dialog())
            later(tamper_after_search)

        def tamper_after_search():
            dialog = active_dialog
            if dialog._busy:
                later(tamper_after_search)
                return
            assert dialog.hits, dialog.status.GetValue()
            retained = library.assets.resolve(imported['asset'])
            retained.chmod(0o600)
            retained.write_bytes(b'Synthetic source tamper after searching.')
            click(dialog.copy)
            later(tamper_rejected)

        def tamper_rejected():
            dialog = active_dialog
            if dialog._busy:
                later(tamper_rejected)
                return
            check('fresh verification rejects an actually changed retained source',
                  'unavailable' in dialog.status.GetValue() and not dialog.hits)
            check('verification failure clears native quotation and citation controls',
                  dialog.quote.GetValue() == dialog.citation.GetValue() == '')
            check('verification failure focuses the actual readable status', wx.Window.FindFocus() is dialog.status)
            click(dialog.close)

        def timed_out():
            failures.append('Native notebook search exceeded 70 seconds.')
            abort()

        timeout = wx.CallLater(70000, timed_out)
        later(begin_first, 100)
        try:
            gui.MainLoop()
        finally:
            timeout.Stop()
            frame._confirm = original_confirm
            if active_dialog is not None and not active_dialog._closed:
                active_dialog.shutdown()
                active_dialog.Destroy()
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            gui.ProcessPendingEvents()

    if failures:
        print(json.dumps({'result': 'FAIL', 'errors': failures, 'nvda': 'NOT_RUN'}, ensure_ascii=False))
        return 1
    assert len(checks) >= 25, 'Native probe exited before completing its checks.'
    print(json.dumps({'result': 'PASS', 'checks': len(checks), 'native_platform': platform.system(),
        'wx': wx.version(), 'source_sha256': sha256(Path(notebook_search.__file__).read_bytes()).hexdigest(),
        'package_file': str(Path(notebook_search.__file__).resolve()),
        'confirmation': 'synthetic discard refusal; native controls and focus are real', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
