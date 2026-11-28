"""Additive real-wx Search Center smoke for the existing desktop event loop.

Call run_native_federated_smoke(frame, completed, failed) from an existing wx
callback. No second wx.App is created. It uses synthetic local text, real search,
source validation, clipboard and topic saving. It does not assert NVDA behavior.
"""
from hashlib import sha256
from pathlib import Path
import traceback

import wx

from carr_library.ui import federated_search


def _dismiss_modal_descendant(parent):
    """End the deepest child modal first, leaving its caller time to unwind."""
    descendants = []
    for window in wx.GetTopLevelWindows():
        try:
            if window is parent or not isinstance(window, wx.Dialog) or not window.IsModal():
                continue
            current, depth = window.GetParent(), 1
            while current is not None and current is not parent:
                current, depth = current.GetParent(), depth + 1
            if current is parent:
                descendants.append((depth, window))
        except RuntimeError:
            # wx may have deleted a dialog while removing it from the window list.
            continue
    if not descendants:
        return False
    _, deepest = max(descendants, key=lambda pair: pair[0])
    deepest.EndModal(wx.ID_CANCEL)
    return True


def run_native_federated_smoke(frame, completed, failed):
    """completed(report: dict) or failed(traceback: str), after modal dismissal."""
    app = frame.library
    active, book = frame.active, frame.book
    assert active is not None and not frame.working
    workspace = app.create_workspace('Search Center native source library')
    topic = app.create_topic(workspace, 'Native destination topic')
    app.add_text(workspace, 'Native federated source',
                 'Railway native source. Exact context beyond the selected quotation.',
                 authors=('Synthetic native author',), language='en', edition='First')
    host_selections = dict(app._selections)
    native = federated_search.NativeSearchCenter(frame, app.workspaces())
    controller = native.controller
    errors, checks = [], []
    state = {'context_closed': False, 'topic_chosen': False, 'note_entered': False,
             'completed': False, 'hit': None, 'selected_libraries': (), 'page_libraries': []}

    def later(callback, milliseconds=20):
        wx.CallLater(milliseconds, lambda: guarded(callback))

    def guarded(callback):
        if errors:
            return
        try:
            callback()
        except BaseException:
            errors.append(traceback.format_exc())
            controller.cancel()
            wait_failure_close()

    def wait_failure_close():
        # A failed assertion can occur inside NativeDialogs.show/choose/text.
        # Dismiss that nested loop before attempting to end the parent modal.
        # The next event-loop turn lets its finally block destroy the child.
        if _dismiss_modal_descendant(native.window):
            wx.CallLater(20, wait_failure_close)
        elif controller.running or frame.working:
            wx.CallLater(20, wait_failure_close)
        elif native.window.IsModal():
            native._close()

    def click(button):
        assert button is not None and button.IsShownOnScreen() and button.IsEnabled()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event), 'Native button event was not handled.'

    def dialog_with_title(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.IsShown()
                     and window.GetTitle() == title), None)

    def assert_scope():
        assert frame.library is app and frame.active == active and frame.book is book
        assert app._selections == host_selections, 'Search Center replaced the desktop result cache.'

    def assert_busy():
        assert frame.working and controller.running
        assert native.cancel_button.IsEnabled()
        for control in (native.query, native.libraries_control, native.retrieval,
                        native.presentation, native.search_button, native.results,
                        native.close_button, native.quote_button, native.save_button):
            assert not control.IsEnabled(), control.GetName()

    def begin():
        assert native.window.IsModal()
        checked = tuple(row.id for index, row in enumerate(native.libraries)
                        if native.libraries_control.IsChecked(index))
        assert checked == (active[0],), checked
        assert native.libraries_control.GetName() == 'Libraries to search (Space selects)'
        assert native.results.GetName() == 'Search results with library and verification status'
        assert native.status.GetName() == 'Search Center status'
        for index, row in enumerate(native.libraries):
            native.libraries_control.Check(index, row.id in (active[0], workspace))
        state['selected_libraries'] = tuple(row.id for index, row in enumerate(native.libraries)
            if native.libraries_control.IsChecked(index))
        assert len(state['selected_libraries']) == 2
        assert set(state['selected_libraries']) == {active[0], workspace}
        event = wx.CommandEvent(wx.EVT_CHECKLISTBOX.typeId, native.libraries_control.GetId())
        event.SetEventObject(native.libraries_control)
        native.libraries_control.GetEventHandler().ProcessEvent(event)
        native.query.SetValue('railway')
        click(native.search_button)
        assert_busy()
        later(search_ready)

    def search_ready():
        if frame.working:
            later(search_ready)
            return
        assert not controller.running and controller.rows, native.status.GetValue()
        # The current service admits one nonempty library page at a time.
        # Traverse the real native continuation controls without retaining hits.
        page_libraries = {hit.workspace for hit in controller.rows}
        assert len(page_libraries) == 1 and page_libraries <= {active[0], workspace}, page_libraries
        position = len(state['page_libraries'])
        assert position < len(state['selected_libraries'])
        assert controller.paging.page_number == position + 1
        current_library, = page_libraries
        assert current_library == state['selected_libraries'][position]
        state['page_libraries'].append(current_library)
        assert_scope()
        if controller.paging.can_next:
            assert len(state['page_libraries']) < 2, 'Unexpected extra page for the bounded fixture.'
            later(search_ready)
            click(native.next_button)
            assert_busy()
            return
        assert set(state['page_libraries']) == {active[0], workspace}
        assert tuple(state['page_libraries']) == state['selected_libraries']
        if current_library != workspace:
            assert state['page_libraries'][0] == workspace and controller.paging.can_previous
            later(select_current_target)
            click(native.previous_button)
            assert_busy()
            return
        select_current_target()

    def select_current_target():
        if frame.working:
            later(select_current_target)
            return
        assert not controller.running and controller.rows, native.status.GetValue()
        assert {hit.workspace for hit in controller.rows} == {workspace}
        assert controller.paging.page_number == state['selected_libraries'].index(workspace) + 1
        index = next(index for index, hit in enumerate(controller.rows) if hit.workspace == workspace)
        native.results.SetSelection(index)
        event = wx.CommandEvent(wx.EVT_LISTBOX.typeId, native.results.GetId())
        event.SetEventObject(native.results)
        native.results.GetEventHandler().ProcessEvent(event)
        state['hit'] = controller.rows[index]
        assert 'Search Center native source library' in native.results.GetString(index)
        assert workspace in native.details.GetValue()
        assert native.quote_button.IsEnabled() and native.save_button.IsEnabled()
        assert_scope()
        checks.extend(['current-library-only default', 'explicit multi-library native search event',
                       'library and verification labels', 'worker controls disabled and restored'])
        later(context_dialog)
        click(native.context_button)
        assert_busy()

    def context_dialog():
        dialog = dialog_with_title('Verified original source context')
        if dialog is None:
            assert controller.running or frame.working, native.status.GetValue()
            later(context_dialog)
            return
        readers = [child for child in dialog.GetChildren() if isinstance(child, wx.TextCtrl)]
        assert len(readers) == 1
        assert workspace in readers[0].GetValue()
        assert 'Exact context beyond the selected quotation.' in readers[0].GetValue()
        click(dialog.FindWindow(wx.ID_CANCEL))
        state['context_closed'] = True
        later(copy_begin, milliseconds=40)

    def copy_begin():
        assert state['context_closed'] and wx.Window.FindFocus() is native.results
        checks.append('fresh original context and result focus restoration')
        click(native.quote_button)
        assert_busy()
        later(copy_ready)

    def copy_ready():
        if frame.working:
            later(copy_ready)
            return
        assert 'copied from Search Center native source library' in native.status.GetValue()
        assert wx.TheClipboard.Open(), 'Native clipboard was unavailable.'
        try:
            value = wx.TextDataObject()
            assert wx.TheClipboard.GetData(value)
            assert value.GetText() == state['hit'].result.quote
        finally:
            wx.TheClipboard.Close()
        assert wx.Window.FindFocus() is native.results
        checks.append('fresh verified native clipboard copy')
        later(save_dialogs)
        click(native.save_button)
        assert_busy()

    def save_dialogs():
        choose = dialog_with_title('Save quotation to a topic')
        note = dialog_with_title('Quotation note')
        if choose is not None and not state['topic_chosen']:
            assert isinstance(choose, wx.SingleChoiceDialog)
            choose.SetSelection(0)
            state['topic_chosen'] = True
            # Keep the timer alive while the next native modal opens.
            later(save_dialogs)
            click(choose.FindWindow(wx.ID_OK))
            return
        if note is not None and not state['note_entered']:
            fields = [child for child in note.GetChildren() if isinstance(child, wx.TextCtrl)]
            assert len(fields) == 1
            fields[0].SetValue('Native saved quotation note.')
            state['note_entered'] = True
            click(note.FindWindow(wx.ID_OK))
            later(save_ready)
            return
        assert frame.working or controller.running or state['topic_chosen'], native.status.GetValue()
        later(save_dialogs)

    def save_ready():
        if frame.working:
            later(save_ready)
            return
        saved, = app.evidence(workspace, topic)
        assert saved.anchor.identity.workspace == workspace
        assert saved.anchor.quote == state['hit'].result.quote
        assert saved.note == 'Native saved quotation note.'
        assert_scope()
        assert wx.Window.FindFocus() is native.results
        checks.append('native topic chooser saves only in result library')
        # A subsequent attempt cannot close the modal while completion is pending.
        native.query.SetValue('source')
        assert controller.rows == () and not native.quote_button.IsEnabled()
        click(native.search_button)
        assert_busy()
        native.window.Close()
        assert controller.running and native.window.IsModal()
        assert frame.cancel_event.is_set()
        later(cancel_ready)

    def cancel_ready():
        if frame.working:
            later(cancel_ready)
            return
        assert not controller.running and controller.rows == ()
        assert native.results.GetCount() == 0 and not native.quote_button.IsEnabled()
        assert 'cancelled' in native.status.GetValue().casefold()
        assert_scope()
        checks.extend(['option changes invalidate previous results', 'cancelled search exposes no rows',
                       'close veto while worker active', 'desktop scope and selection cache preserved'])
        state['completed'] = True
        click(native.close_button)

    def timed_out():
        if not state['completed'] and not errors:
            errors.append('Research Search Center native smoke exceeded 15 seconds.')
            controller.cancel()
            wait_failure_close()

    timeout = wx.CallLater(15000, timed_out)
    later(begin)
    try:
        native.show()
    finally:
        timeout.Stop()
    if errors:
        failed('\n'.join(errors))
    elif not state['completed']:
        failed('Research Search Center closed before the native workflow completed.')
    else:
        completed({'status': 'PASS', 'checks': checks, 'windows_nvda': 'NOT_RUN',
                   'federated_ui_sha256': sha256(Path(federated_search.__file__).read_bytes()).hexdigest()})
